package scheduler

import (
	"context"
	"fmt"
	"github.com/monkci/mig-controller/internal/redis"
	goredis "github.com/redis/go-redis/v9"
	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
	"testing"
	"time"
)

// Reproduce an entire pool backlog rather than proving only one job recovers.
func TestRegressionSilentPoolBacklogRecoversEveryJobWithoutConsumingHealthyClaim(t *testing.T) {
	s, store := newAuditorTestScheduler(t)
	ctx := context.Background()
	raw := goredis.NewClient(&goredis.Options{Addr: "localhost:6379", DB: 9})
	defer raw.Close()
	s.cfg.Scheduler.AssignmentTimeout = 5 * time.Minute
	states := []redis.MigletState{redis.MigletStateBooting, redis.MigletStateUnknown, redis.MigletStateReady}
	for i := 0; i < 13; i++ {
		jid, vid := fmt.Sprintf("job-%02d", i), fmt.Sprintf("vm-%02d", i)
		job := seedPendingJob(t, store, jid, int64(1000+i))
		ok, err := store.AtomicUpdateScheduleState(ctx, job.ID, redis.SchedulePendingAllocation, redis.ScheduleVMAllocated, vid)
		require.NoError(t, err)
		require.True(t, ok)
		busy := time.Now().Add(-4 * time.Minute)
		if i == 12 {
			busy = time.Now().Add(-time.Minute)
		}
		require.NoError(t, s.vmStore.Update(ctx, &redis.VMStatus{VMID: vid, PoolID: auditorTestPool, State: redis.VMStateBusy, JobID: jid, MigletState: string(states[i%3]), BusySince: busy}))
		require.NoError(t, raw.SAdd(ctx, "pool:ubuntu-24.04-4:busy", vid).Err())
	}
	for pass := 0; pass < 3; pass++ {
		s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	}
	for i := 0; i < 12; i++ {
		job, err := store.Get(ctx, fmt.Sprintf("job-%02d", i))
		require.NoError(t, err)
		require.Equal(t, redis.ScheduleRetiring, job.ScheduleStatus)
		require.NoError(t, raw.Del(ctx, "vm:"+job.AssignedVMID).Err())
	}
	s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	for i := 0; i < 12; i++ {
		job, err := store.Get(ctx, fmt.Sprintf("job-%02d", i))
		require.NoError(t, err)
		require.Equal(t, redis.ScheduleRetiring, job.ScheduleStatus)
		require.NoError(t, raw.Set(ctx, "vm:deletion_confirmed:"+job.AssignedVMID, "1", time.Hour).Err())
	}
	for pass := 0; pass < 3; pass++ {
		s.recoverStalledRegistrationsForPool(auditorTestPool, zerolog.Nop())
	}
	for i := 0; i < 12; i++ {
		job, err := store.Get(ctx, fmt.Sprintf("job-%02d", i))
		require.NoError(t, err)
		require.Equal(t, redis.ScheduleQueued, job.ScheduleStatus)
		require.Equal(t, 1, job.RetryCount, "replayed recovery must not double charge retries")
		require.Empty(t, job.AssignedVMID)
	}
	healthy, err := store.Get(ctx, "job-12")
	require.NoError(t, err)
	require.Equal(t, redis.ScheduleVMAllocated, healthy.ScheduleStatus)
	require.Equal(t, "vm-12", healthy.AssignedVMID)
	require.False(t, raw.SIsMember(ctx, "vm:retirement_requests", "vm-12").Val())
}
