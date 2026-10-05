// Central regression contract: run via scripts/source_contracts.py.
package handlers

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/monkci/mig-controller/internal/redis"
	migletv1 "github.com/monkci/mig-controller/proto/miglet/v1"
)

func TestRegressionRegistrationReplyRejectsObsoleteAssignment(t *testing.T) {
	h, vms, _ := newHeartbeatTestHandlers(t)
	ctx := context.Background()
	jobs, err := redis.NewMultiPoolJobStore(getHeartbeatTestRedisConfig(), []string{completedElsewhereTestPool}, "staging")
	require.NoError(t, err)
	t.Cleanup(func() { _ = jobs.Close() })
	h.multiJobStore = jobs
	store, err := jobs.GetJobStoreForPool(completedElsewhereTestPool)
	require.NoError(t, err)
	_, err = store.AtomicEnqueueIfNew(ctx, &redis.Job{ID: "registration", JobID: 9911, Label: completedElsewhereTestPool})
	require.NoError(t, err)
	job, err := store.AtomicDequeueAndClaim(ctx, redis.SchedulePendingAllocation)
	require.NoError(t, err)
	require.NotNil(t, job)
	ok, err := store.AtomicUpdateScheduleState(ctx, job.ID, redis.SchedulePendingAllocation, redis.ScheduleVMAllocated, "old")
	require.NoError(t, err)
	require.True(t, ok)
	require.NoError(t, vms.Update(ctx, &redis.VMStatus{VMID: "old", PoolID: completedElsewhereTestPool,
		State: redis.VMStateBusy, JobID: job.ID, MigletState: string(redis.MigletStateReady)}))
	event := &migletv1.ReportEventRequest{VmId: "old", PoolId: completedElsewhereTestPool,
		Event: migletv1.EventType_EVENT_TYPE_RUNNER_REGISTERED, TimestampMs: time.Now().UnixMilli()}
	require.True(t, h.HandleEvent(ctx, "old", event))
	job, err = store.Get(ctx, job.ID)
	require.NoError(t, err)
	require.Equal(t, redis.ScheduleAssigned, job.ScheduleStatus)
	outcome, err := store.RecoverStalledAssignment(ctx, job.ID, *job.AssignedAt)
	require.NoError(t, err)
	require.Equal(t, redis.RecoveryOutcomeRestored, outcome)
	ok, err = store.AtomicUpdateScheduleState(ctx, job.ID, redis.SchedulePendingAllocation, redis.ScheduleVMAllocated, "replacement")
	require.NoError(t, err)
	require.True(t, ok)
	event.TimestampMs++
	require.False(t, h.HandleEvent(ctx, "old", event), "the RPC reply must deny the old listener, not just skip the job write")
}
