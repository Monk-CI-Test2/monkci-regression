// Central regression contract: run via scripts/source_contracts.py.
package redis

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestRegressionRunnerStartPermissionAndRetirementAreMutuallyExclusive(t *testing.T) {
	for _, registrationFirst := range []bool{true, false} {
		name := "retirement_wins"
		if registrationFirst {
			name = "registration_wins"
		}
		t.Run(name, func(t *testing.T) {
			jobs := newReceiptTestStore(t)
			ctx := context.Background()
			job := enqueueAllocatedJob(t, jobs, "job", 123, "vm")
			vms := &VMStatusStore{client: jobs.client}
			require.NoError(t, vms.Update(ctx, &VMStatus{VMID: "vm", PoolID: receiptTestPool, State: VMStateBusy, JobID: job.ID, MigletState: string(MigletStateReady), BusySince: time.Now().Add(-4 * time.Minute)}))
			require.NoError(t, jobs.client.SAdd(ctx, "pool:ubuntu-24.04-4:busy", "vm").Err())
			if registrationFirst {
				accepted, err := jobs.AcceptRunnerRegistration(ctx, "vm", job.ID, time.Now().UnixMilli())
				require.NoError(t, err)
				require.Equal(t, job.ID, accepted)
				// Reconciliation or a reordered event must not reopen the
				// registration deadline after permission has been granted.
				require.NoError(t, vms.UpdateMigletState(ctx, "vm", MigletStateUnknown))
				result, err := jobs.BeginVMRetirement(ctx, job)
				require.NoError(t, err)
				require.Equal(t, "skipped", result)
				current, err := jobs.Get(ctx, job.ID)
				require.NoError(t, err)
				require.Equal(t, ScheduleAssigned, current.ScheduleStatus)
				require.True(t, current.AssignmentReceiptPending)
				for _, status := range []ScheduleStatus{ScheduleVMAllocated, ScheduleRegistering, ScheduleRetiring} {
					require.False(t, jobs.client.SIsMember(ctx, "jobs:by_schedule:"+receiptTestPool+":"+string(status), job.ID).Val())
				}
				require.True(t, jobs.client.SIsMember(ctx, "jobs:by_schedule:"+receiptTestPool+":ASSIGNED", job.ID).Val())
				require.False(t, jobs.client.SIsMember(ctx, "vm:retirement_requests", "vm").Val())
			} else {
				result, err := jobs.BeginVMRetirement(ctx, job)
				require.NoError(t, err)
				require.Equal(t, "retiring", result)
				accepted, err := jobs.AcceptRunnerRegistration(ctx, "vm", job.ID, time.Now().UnixMilli())
				require.NoError(t, err)
				require.Empty(t, accepted)
				// Losing the VM record cannot authorize a replacement.
				require.NoError(t, jobs.client.Del(ctx, "vm:vm").Err())
				result, err = jobs.FinishVMRetirement(ctx, job)
				require.NoError(t, err)
				require.Equal(t, "waiting", result)
				require.NoError(t, jobs.client.Set(ctx, "vm:deletion_confirmed:vm", "1", time.Hour).Err())
				result, err = jobs.FinishVMRetirement(ctx, job)
				require.NoError(t, err)
				require.Equal(t, "requeued", result)
			}
		})
	}
}

func TestRegressionRunnerApprovalRejectsOldVMAfterReceiptRecovery(t *testing.T) {
	jobs := newReceiptTestStore(t)
	vms := &VMStatusStore{client: jobs.client}
	ctx := context.Background()
	job := enqueueAllocatedJob(t, jobs, "job", 123, "old")
	require.NoError(t, vms.Update(ctx, &VMStatus{VMID: "old", PoolID: receiptTestPool,
		State: VMStateBusy, JobID: job.ID, MigletState: string(MigletStateReady)}))
	source := time.Now().UnixMilli()
	accepted, err := jobs.AcceptRunnerRegistration(ctx, "old", "", source)
	require.NoError(t, err)
	require.Equal(t, job.ID, accepted)
	first, err := jobs.Get(ctx, job.ID)
	require.NoError(t, err)
	accepted, err = jobs.AcceptRunnerRegistration(ctx, "old", "", source+1)
	require.NoError(t, err)
	require.Equal(t, job.ID, accepted)
	repeated, err := jobs.Get(ctx, job.ID)
	require.NoError(t, err)
	require.Equal(t, first.AssignedAt, repeated.AssignedAt, "a lost approval reply must not reset the receipt clock")

	outcome, err := jobs.RecoverStalledAssignment(ctx, job.ID, *first.AssignedAt)
	require.NoError(t, err)
	require.Equal(t, RecoveryOutcomeRestored, outcome)
	ok, err := jobs.AtomicUpdateScheduleState(ctx, job.ID, SchedulePendingAllocation, ScheduleVMAllocated, "replacement")
	require.NoError(t, err)
	require.True(t, ok)
	accepted, err = jobs.AcceptRunnerRegistration(ctx, "old", "", source+2)
	require.NoError(t, err)
	require.Empty(t, accepted, "an old VM claim must not authorize a listener after replacement")
	current, err := jobs.Get(ctx, job.ID)
	require.NoError(t, err)
	require.Equal(t, "replacement", current.AssignedVMID)
	require.Equal(t, ScheduleVMAllocated, current.ScheduleStatus)
	require.False(t, jobs.client.Exists(ctx, "jobs:by_vm:old").Val() > 0)
}

func TestRegressionRunnerApprovalRejectsCompletedJob(t *testing.T) {
	jobs := newReceiptTestStore(t)
	vms := &VMStatusStore{client: jobs.client}
	ctx := context.Background()
	job := enqueueAllocatedJob(t, jobs, "job", 123, "vm")
	require.NoError(t, vms.Update(ctx, &VMStatus{VMID: "vm", PoolID: receiptTestPool,
		State: VMStateBusy, JobID: job.ID, MigletState: string(MigletStateReady)}))
	ok, err := jobs.CompleteFromGitHubByID(ctx, job.ID, ConclusionSuccess)
	require.NoError(t, err)
	require.True(t, ok)
	accepted, err := jobs.AcceptRunnerRegistration(ctx, "vm", "", time.Now().UnixMilli())
	require.NoError(t, err)
	require.Empty(t, accepted)
	vm, err := vms.Get(ctx, "vm")
	require.NoError(t, err)
	require.Equal(t, string(MigletStateReady), vm.MigletState, "rejection must not partially approve the VM")
}

func TestRegressionHeartbeatCleanupCannotOvertakeNewerState(t *testing.T) {
	for _, newer := range []MigletState{MigletStateJobRunning, MigletStateIdle} {
		t.Run(string(newer), func(t *testing.T) {
			jobs := newReceiptTestStore(t)
			vms := &VMStatusStore{client: jobs.client}
			ctx := context.Background()
			enqueueAllocatedJob(t, jobs, "job", 123, "vm")
			busy := time.Now().UTC().Add(-10 * time.Minute)
			source := time.Now().UnixMilli()
			require.NoError(t, vms.Update(ctx, &VMStatus{VMID: "vm", PoolID: receiptTestPool, State: VMStateBusy, JobID: "job", BusySince: busy, MigletState: string(MigletStateReady)}))
			require.NoError(t, jobs.client.SAdd(ctx, "pool:ubuntu-24.04-4:busy", "vm").Err())
			accepted, err := vms.ApplyHeartbeatAt(ctx, "vm", MigletStateReady, "", source)
			require.NoError(t, err)
			require.True(t, accepted)
			if newer == MigletStateIdle {
				// Registration may share the same millisecond with the old heartbeat.
				id, err := jobs.AcceptRunnerRegistration(ctx, "vm", "job", source)
				require.NoError(t, err)
				require.Equal(t, "job", id)
			} else {
				accepted, err = vms.ApplyHeartbeatAt(ctx, "vm", newer, "", source+1)
				require.NoError(t, err)
				require.True(t, accepted)
			}
			moved, err := vms.RetireFromHeartbeatIfCurrent(ctx, receiptTestPool, "vm", "job", source, busy, MigletStateReady)
			require.NoError(t, err)
			require.False(t, moved)
			accepted, err = vms.ApplyHeartbeatAt(ctx, "vm", MigletStateReady, "", source)
			require.NoError(t, err)
			require.False(t, accepted)
			vm, err := vms.Get(ctx, "vm")
			require.NoError(t, err)
			require.Equal(t, string(newer), vm.MigletState)
		})
	}
}

func TestRegressionLateVMEventsCannotChangeReplacementOrGitHubConclusion(t *testing.T) {
	jobs := newReceiptTestStore(t)
	ctx := context.Background()
	job := enqueueAllocatedJob(t, jobs, "job", 123, "vm-new")
	applied, err := jobs.MarkRunningFromVM(ctx, job.ID, "vm-old")
	require.NoError(t, err)
	require.False(t, applied)
	applied, err = jobs.CompleteFromVM(ctx, job.ID, "vm-old", ConclusionFailure, "late failure")
	require.NoError(t, err)
	require.False(t, applied)
	got, err := jobs.Get(ctx, job.ID)
	require.NoError(t, err)
	require.Equal(t, ScheduleVMAllocated, got.ScheduleStatus)
	applied, err = jobs.CompleteFromGitHubByID(ctx, job.ID, ConclusionSuccess)
	require.NoError(t, err)
	require.True(t, applied)
	applied, err = jobs.CompleteFromVM(ctx, job.ID, "vm-new", ConclusionFailure, "late failure")
	require.NoError(t, err)
	require.False(t, applied)
	got, err = jobs.Get(ctx, job.ID)
	require.NoError(t, err)
	require.Equal(t, ConclusionSuccess, got.Conclusion)
}

func TestRegressionParkedPollClaimAllowsOnlyOneReplica(t *testing.T) {
	jobs := newReceiptTestStore(t)
	ctx := context.Background()
	job := enqueueAllocatedJob(t, jobs, "job", 123, "vm")
	parked, err := jobs.ParkJob(ctx, job.ID, "test", time.Time{})
	require.NoError(t, err)
	require.True(t, parked)
	job, err = jobs.Get(ctx, job.ID)
	require.NoError(t, err)
	for _, want := range []bool{true, false} {
		claimed, err := jobs.TouchParkedAssignment(ctx, job.ID, job.UpdatedAt)
		require.NoError(t, err)
		require.Equal(t, want, claimed)
	}
}

func TestRegressionDurableRetirementBlocksReconstructedVM(t *testing.T) {
	jobs := newReceiptTestStore(t)
	vms := &VMStatusStore{client: jobs.client}
	ctx := context.Background()
	now := time.Now().UTC()
	require.NoError(t, jobs.client.SAdd(ctx, "vm:retirement_requests", "vm").Err())
	require.NoError(t, vms.Update(ctx, &VMStatus{VMID: "vm", PoolID: receiptTestPool, State: VMStateWarm, MigletState: string(MigletStateReady), WarmSinceMs: now.Add(-time.Minute).UnixMilli(), LastHeartbeatSourceMs: now.UnixMilli(), LastHeartbeat: now}))
	require.NoError(t, jobs.client.SAdd(ctx, "pool:ubuntu-24.04-4:warm", "vm").Err())
	claimed, err := vms.ClaimReadyVM(ctx, receiptTestPool, "job")
	require.NoError(t, err)
	require.Nil(t, claimed)
	accepted, err := vms.ApplyHeartbeatAt(ctx, "vm", MigletStateReady, "", now.UnixMilli())
	require.NoError(t, err)
	require.False(t, accepted)
	// Even an incorrectly reconstructed busy record cannot grant permission.
	require.NoError(t, vms.Update(ctx, &VMStatus{VMID: "vm", PoolID: receiptTestPool, State: VMStateBusy, JobID: "job", MigletState: string(MigletStateReady)}))
	id, err := jobs.AcceptRunnerRegistration(ctx, "vm", "job", now.UnixMilli())
	require.NoError(t, err)
	require.Empty(t, id)
}

func TestRegressionVMClaimPreservesPrecisionAndClassIndexes(t *testing.T) {
	for _, reservation := range []string{"", "reservation"} {
		t.Run(reservation, func(t *testing.T) {
			jobs := newReceiptTestStore(t)
			vms := &VMStatusStore{client: jobs.client}
			ctx := context.Background()
			class := "onDemand"
			if reservation != "" {
				class = "reserved"
			}
			prefix := "pool:ubuntu-24.04-4:"
			now := time.Now().UTC()
			require.NoError(t, vms.Update(ctx, &VMStatus{VMID: "vm", PoolID: receiptTestPool,
				State: VMStateWarm, MigletState: string(MigletStateReady),
				WarmSinceMs: now.Add(-time.Minute).UnixMilli(), LastHeartbeatSourceMs: now.UnixMilli()}))
			require.NoError(t, jobs.client.Eval(ctx, `local vm = cjson.decode(redis.call('GET', KEYS[1])); vm.reservation = ARGV[1]; redis.call('SET', KEYS[1], cjson.encode(vm)); return 1`, []string{"vm:vm"}, reservation).Err())
			require.NoError(t, jobs.client.SAdd(ctx, prefix+"warm", "vm").Err())
			require.NoError(t, jobs.client.SAdd(ctx, prefix+"warm:"+class, "vm").Err())
			before := time.Now().UTC()
			vm, err := vms.ClaimReadyVM(ctx, receiptTestPool, "job")
			require.NoError(t, err)
			require.NotNil(t, vm)
			require.False(t, vm.BusySince.Before(before), "claim timestamp must not round down to the second")
			check := func(state string) {
				for _, candidate := range []string{"warm", "busy", "completed"} {
					want := candidate == state
					require.Equal(t, want, jobs.client.SIsMember(ctx, prefix+candidate, "vm").Val())
					require.Equal(t, want, jobs.client.SIsMember(ctx, prefix+candidate+":"+class, "vm").Val())
				}
			}
			check("busy")
			released, err := vms.ReleaseUnusedClaim(ctx, receiptTestPool, "vm", "job")
			require.NoError(t, err)
			require.True(t, released)
			check("warm")
			vm, err = vms.ClaimReadyVM(ctx, receiptTestPool, "job")
			require.NoError(t, err)
			require.NotNil(t, vm)
			require.NoError(t, vms.MoveBusyVMToCompleted(ctx, receiptTestPool, "vm"))
			check("completed")
		})
	}
}
