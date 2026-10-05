package redis

import (
	"context"
	"encoding/json"
	"github.com/stretchr/testify/require"
	"testing"
	"time"
)

// These cases use the real allocator Lua, not a second readiness model.
func TestRegressionReadinessEligibility(t *testing.T) {
	for _, tc := range []struct {
		name, state, miglet                          string
		boot, heartbeat                              time.Duration
		missingBoot, missingHeartbeat, retired, want bool
	}{
		{name: "fresh_current_boot", state: "warm", miglet: "ready", boot: -time.Minute, want: true},
		{name: "old_vm_fresh_agent", state: "warm", miglet: "ready", boot: -24 * time.Hour, want: true},
		{name: "booting_with_fresh_heartbeat", state: "warm", miglet: "booting", boot: -time.Minute},
		{name: "unknown_with_fresh_heartbeat", state: "warm", miglet: "unknown", boot: -time.Minute},
		{name: "error_with_fresh_heartbeat", state: "warm", miglet: "error", boot: -time.Minute},
		{name: "idle_not_available", state: "warm", miglet: "idle", boot: -time.Minute},
		{name: "running_not_available", state: "warm", miglet: "job_running", boot: -time.Minute},
		{name: "heartbeat_before_boot", state: "warm", miglet: "ready", boot: -10 * time.Second, heartbeat: -20 * time.Second},
		{name: "expired_heartbeat", state: "warm", miglet: "ready", boot: -time.Hour, heartbeat: -2 * time.Minute},
		{name: "future_heartbeat", state: "warm", miglet: "ready", boot: -time.Minute, heartbeat: 2 * time.Minute},
		{name: "missing_boot", state: "warm", miglet: "ready", missingBoot: true},
		{name: "missing_heartbeat", state: "warm", miglet: "ready", boot: -time.Minute, missingHeartbeat: true},
		{name: "busy_with_stale_warm_index", state: "busy", miglet: "ready", boot: -time.Minute},
		{name: "completed_with_stale_warm_index", state: "completed", miglet: "ready", boot: -time.Minute},
		{name: "durable_retirement", state: "warm", miglet: "ready", boot: -time.Minute, retired: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			jobs := newReceiptTestStore(t)
			vms := &VMStatusStore{client: jobs.client}
			ctx := context.Background()
			now := time.Now()
			boot, hb := now.Add(tc.boot).UnixMilli(), now.Add(tc.heartbeat).UnixMilli()
			if tc.missingBoot {
				boot = 0
			}
			if tc.missingHeartbeat {
				hb = 0
			}
			vm := &VMStatus{VMID: "vm", PoolID: receiptTestPool, State: VMState(tc.state), MigletState: tc.miglet, WarmSinceMs: boot, LastHeartbeatSourceMs: hb}
			raw, err := json.Marshal(vm)
			require.NoError(t, err)
			require.NoError(t, jobs.client.Set(ctx, "vm:vm", raw, time.Hour).Err())
			require.NoError(t, jobs.client.SAdd(ctx, "pool:ubuntu-24.04-4:warm", "vm").Err())
			if tc.retired {
				require.NoError(t, jobs.client.SAdd(ctx, "vm:retirement_requests", "vm").Err())
			}
			got, err := vms.ClaimReadyVM(ctx, receiptTestPool, "job")
			require.NoError(t, err)
			require.Equal(t, tc.want, got != nil)
			if !tc.want {
				require.True(t, jobs.client.SIsMember(ctx, "pool:ubuntu-24.04-4:warm", "vm").Val(), "failed claims must not consume capacity")
				require.False(t, jobs.client.SIsMember(ctx, "pool:ubuntu-24.04-4:busy", "vm").Val())
				require.Equal(t, string(raw), jobs.client.Get(ctx, "vm:vm").Val(), "failed claims must not modify shared state")
			}
		})
	}
}

func TestRegressionOldMessagesCannotOverwriteAnyCurrentState(t *testing.T) {
	for _, current := range []MigletState{MigletStateReady, MigletStateBooting, MigletStateIdle, MigletStateJobRunning, MigletStateError} {
		for _, late := range []MigletState{MigletStateReady, MigletStateBooting, MigletStateIdle, MigletStateJobRunning, MigletStateError} {
			t.Run(string(current)+"_then_old_"+string(late), func(t *testing.T) {
				jobs := newReceiptTestStore(t)
				vms := &VMStatusStore{client: jobs.client}
				ctx := context.Background()
				now := time.Now().UnixMilli()
				require.NoError(t, vms.Update(ctx, &VMStatus{VMID: "vm", PoolID: receiptTestPool, State: VMStateBusy, JobID: "owner", WarmSinceMs: now - 10000}))
				accepted, err := vms.ApplyHeartbeatAt(ctx, "vm", current, "", now)
				require.NoError(t, err)
				require.True(t, accepted)
				before := jobs.client.Get(ctx, "vm:vm").Val()
				accepted, err = vms.ApplyHeartbeatAt(ctx, "vm", late, "foreign-job", now-1)
				require.NoError(t, err)
				require.False(t, accepted)
				accepted, err = vms.ApplyStateEventAt(ctx, "vm", late, "foreign-job", now-1)
				require.NoError(t, err)
				require.False(t, accepted)
				require.Equal(t, before, jobs.client.Get(ctx, "vm:vm").Val())
			})
		}
	}
}

func TestRegressionReadyEventDoesNotInventHeartbeat(t *testing.T) {
	jobs := newReceiptTestStore(t)
	vms := &VMStatusStore{client: jobs.client}
	ctx := context.Background()
	now := time.Now().UnixMilli()
	require.NoError(t, vms.Update(ctx, &VMStatus{VMID: "vm", PoolID: receiptTestPool, State: VMStateWarm, WarmSinceMs: now - 1000}))
	require.NoError(t, jobs.client.SAdd(ctx, "pool:ubuntu-24.04-4:warm", "vm").Err())
	accepted, err := vms.ApplyStateEventAt(ctx, "vm", MigletStateReady, "", now)
	require.NoError(t, err)
	require.True(t, accepted)
	vm, err := vms.ClaimReadyVM(ctx, receiptTestPool, "job")
	require.NoError(t, err)
	require.Nil(t, vm)
	accepted, err = vms.ApplyHeartbeatAt(ctx, "vm", MigletStateReady, "", now+1)
	require.NoError(t, err)
	require.True(t, accepted)
	vm, err = vms.ClaimReadyVM(ctx, receiptTestPool, "job")
	require.NoError(t, err)
	require.NotNil(t, vm)
}
