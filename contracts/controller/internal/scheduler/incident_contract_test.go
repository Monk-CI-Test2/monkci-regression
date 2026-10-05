// Central regression contract: run via scripts/source_contracts.py.
package scheduler

import (
	"context"
	"testing"
	"time"

	goredis "github.com/redis/go-redis/v9"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/monkci/mig-controller/internal/redis"
)

func TestRegressionRegistrationRecovery_BootingClaimWaitsForDeletionThenRequeues(t *testing.T) {
	s, store := newAuditorTestScheduler(t)
	s.cfg.Scheduler.AssignmentTimeout = 5 * time.Minute // BOOTING still expires after three
	ctx := context.Background()
	raw := goredis.NewClient(&goredis.Options{Addr: "localhost:6379", DB: 9})
	defer raw.Close()
	jobID, vmID := "job-booting-claim", "vm-booting-claim"
	_, err := store.AtomicEnqueueIfNew(ctx, &redis.Job{ID: jobID, JobID: 101, Label: auditorTestPool})
	require.NoError(t, err)
	claimed, err := store.AtomicDequeueAndClaim(ctx, redis.SchedulePendingAllocation)
	require.NoError(t, err)
	require.NotNil(t, claimed)
	ok, err := store.AtomicUpdateScheduleState(ctx, jobID, redis.SchedulePendingAllocation, redis.ScheduleVMAllocated, vmID)
	require.NoError(t, err)
	require.True(t, ok)
	require.NoError(t, s.vmStore.Update(ctx, &redis.VMStatus{
		VMID: vmID, PoolID: auditorTestPool, State: redis.VMStateBusy,
		JobID: jobID, MigletState: string(redis.MigletStateBooting),
		BusySince: time.Now().Add(-4 * time.Minute),
	}))
	pool := "ubuntu-24.04-4"
	require.NoError(t, raw.SAdd(ctx, "pool:"+pool+":busy", vmID).Err())
	require.NoError(t, store.AddPendingAllocationRequest(ctx, jobID))

	s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	job, err := store.Get(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, redis.ScheduleRetiring, job.ScheduleStatus)
	vm, err := s.vmStore.Get(ctx, vmID)
	require.NoError(t, err)
	assert.Equal(t, redis.VMStateCompleted, vm.State)
	assert.Empty(t, vm.JobID)
	assert.True(t, raw.SIsMember(ctx, "pool:"+pool+":completed", vmID).Val())
	assert.Equal(t, time.Duration(-1), raw.TTL(ctx, "vm:"+vmID).Val(), "retirement record must not expire before GCE deletion")
	assert.False(t, raw.SIsMember(ctx, "pool:"+pool+":busy", vmID).Val())
	assert.False(t, raw.SIsMember(ctx, "pending_allocation_requests:"+auditorTestPool, jobID).Val())

	// A runner event delivered after the timeout cannot return this VM to idle
	// or attach the retired assignment to the job.
	require.NoError(t, s.vmStore.UpdateMigletState(ctx, vmID, redis.MigletStateIdle))
	vm, err = s.vmStore.Get(ctx, vmID)
	require.NoError(t, err)
	assert.Equal(t, string(redis.MigletStateBooting), vm.MigletState)
	assigned, err := store.AtomicMarkAssignedWithReceipt(ctx, jobID, vmID)
	require.NoError(t, err)
	assert.False(t, assigned)

	// A restart of the controller can resume the durable RETIRING index, but
	// demand stays held until custom-mig removes the deleted VM record.
	s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	job, err = store.Get(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, redis.ScheduleRetiring, job.ScheduleStatus)
	require.NoError(t, raw.Del(ctx, "vm:"+vmID).Err())
	s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	job, err = store.Get(ctx, jobID)
	require.NoError(t, err)
	require.Equal(t, redis.ScheduleRetiring, job.ScheduleStatus, "a missing Redis record is not deletion proof")
	require.NoError(t, raw.Set(ctx, "vm:deletion_confirmed:"+vmID, "1", time.Hour).Err())
	s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	job, err = store.Get(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, redis.ScheduleQueued, job.ScheduleStatus)
	assert.Equal(t, 1, job.RetryCount)
}

func TestRegressionRegistrationRecovery_RegisteredRunnerWinsRetirement(t *testing.T) {
	s, store := newAuditorTestScheduler(t)
	s.cfg.Scheduler.AssignmentTimeout = 3 * time.Minute
	ctx := context.Background()
	raw := goredis.NewClient(&goredis.Options{Addr: "localhost:6379", DB: 9})
	defer raw.Close()
	jobID, vmID := "job-registered-claim", "vm-registered-claim"
	_, err := store.AtomicEnqueueIfNew(ctx, &redis.Job{ID: jobID, JobID: 102, Label: auditorTestPool})
	require.NoError(t, err)
	claimed, err := store.AtomicDequeueAndClaim(ctx, redis.SchedulePendingAllocation)
	require.NoError(t, err)
	require.NotNil(t, claimed)
	ok, err := store.AtomicUpdateScheduleState(ctx, jobID, redis.SchedulePendingAllocation, redis.ScheduleVMAllocated, vmID)
	require.NoError(t, err)
	require.True(t, ok)
	require.NoError(t, s.vmStore.Update(ctx, &redis.VMStatus{
		VMID: vmID, PoolID: auditorTestPool, State: redis.VMStateBusy,
		JobID: jobID, MigletState: string(redis.MigletStateIdle),
		BusySince: time.Now().Add(-4 * time.Minute),
	}))
	require.NoError(t, raw.SAdd(ctx, "pool:ubuntu-24.04-4:busy", vmID).Err())
	s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	job, err := store.Get(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, redis.ScheduleAssigned, job.ScheduleStatus)
	vm, err := s.vmStore.Get(ctx, vmID)
	require.NoError(t, err)
	assert.Equal(t, redis.VMStateBusy, vm.State)
}

func TestRegressionRegistrationRecovery_GitHubCompletionDuringRetirementIsTerminal(t *testing.T) {
	s, store := newAuditorTestScheduler(t)
	s.cfg.Scheduler.AssignmentTimeout = 3 * time.Minute
	ctx := context.Background()
	raw := goredis.NewClient(&goredis.Options{Addr: "localhost:6379", DB: 9})
	defer raw.Close()
	jobID, vmID := "job-completed-during-retirement", "vm-completed-during-retirement"
	_, err := store.AtomicEnqueueIfNew(ctx, &redis.Job{ID: jobID, JobID: 103, Label: auditorTestPool})
	require.NoError(t, err)
	claimed, err := store.AtomicDequeueAndClaim(ctx, redis.SchedulePendingAllocation)
	require.NoError(t, err)
	require.NotNil(t, claimed)
	ok, err := store.AtomicUpdateScheduleState(ctx, jobID, redis.SchedulePendingAllocation, redis.ScheduleVMAllocated, vmID)
	require.NoError(t, err)
	require.True(t, ok)
	require.NoError(t, s.vmStore.Update(ctx, &redis.VMStatus{
		VMID: vmID, PoolID: auditorTestPool, State: redis.VMStateBusy,
		JobID: jobID, MigletState: string(redis.MigletStateBooting),
		BusySince: time.Now().Add(-4 * time.Minute),
	}))
	require.NoError(t, raw.SAdd(ctx, "pool:ubuntu-24.04-4:busy", vmID).Err())
	s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	_, err = store.AtomicCompleteFromGitHub(ctx, &redis.Job{JobID: 103, Label: auditorTestPool, Conclusion: redis.ConclusionSuccess})
	require.NoError(t, err)
	require.NoError(t, raw.Del(ctx, "vm:"+vmID).Err())
	s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	job, err := store.Get(ctx, jobID)
	require.NoError(t, err)
	assert.Equal(t, redis.JobStatusCompleted, job.Status)
	assert.Equal(t, redis.ConclusionSuccess, job.Conclusion)
	assert.False(t, raw.SIsMember(ctx, "jobs:by_schedule:"+auditorTestPool+":RETIRING", jobID).Val())
}

func TestRegressionStaleReadyClaimWaitsForPostClaimHeartbeatAndExpiresAtThreeMinutes(t *testing.T) {
	s, store := newAuditorTestScheduler(t)
	s.cfg.Scheduler.AssignmentTimeout = 5 * time.Minute
	ctx := context.Background()
	job := seedPendingJob(t, store, "job-stale-ready", 104)
	ok, err := store.AtomicUpdateScheduleState(ctx, job.ID, redis.SchedulePendingAllocation, redis.ScheduleVMAllocated, "vm")
	require.NoError(t, err)
	require.True(t, ok)
	busy := time.Now().UTC().Add(-time.Minute)
	vm := &redis.VMStatus{VMID: "vm", PoolID: auditorTestPool, State: redis.VMStateBusy, JobID: job.ID, MigletState: string(redis.MigletStateReady), BusySince: busy, LastHeartbeatSourceMs: busy.Add(-time.Second).UnixMilli()}
	require.NoError(t, s.vmStore.Update(ctx, vm))
	job, err = store.Get(ctx, job.ID)
	require.NoError(t, err)
	// Token/publisher dependencies are deliberately nil: no request may reach them.
	s.processSingleRegistration(auditorTestPool, job, zerolog.Nop())
	job, err = store.Get(ctx, job.ID)
	require.NoError(t, err)
	require.Equal(t, redis.ScheduleVMAllocated, job.ScheduleStatus)
	// A heartbeat sharing the claim's millisecond also predates proof of life
	// after the claim. It must not reach the token service or publisher.
	vm.LastHeartbeatSourceMs = busy.UnixMilli()
	require.NoError(t, s.vmStore.Update(ctx, vm))
	s.processSingleRegistration(auditorTestPool, job, zerolog.Nop())
	job, err = store.Get(ctx, job.ID)
	require.NoError(t, err)
	require.Equal(t, redis.ScheduleVMAllocated, job.ScheduleStatus)
	vm.BusySince = time.Now().UTC().Add(-4 * time.Minute)
	require.NoError(t, s.vmStore.Update(ctx, vm))
	raw := goredis.NewClient(&goredis.Options{Addr: "localhost:6379", DB: 9})
	defer raw.Close()
	require.NoError(t, raw.SAdd(ctx, "pool:ubuntu-24.04-4:busy", "vm").Err())
	s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	job, err = store.Get(ctx, job.ID)
	require.NoError(t, err)
	require.Equal(t, redis.ScheduleRetiring, job.ScheduleStatus, "READY must not extend the recovery deadline to five minutes")
}
