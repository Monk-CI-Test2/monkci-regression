package scheduler

import (
	"context"
	"errors"
	"github.com/monkci/mig-controller/internal/redis"
	"github.com/stretchr/testify/require"
	"testing"
)

func TestRegressionParkedGitHubErrorsArePacedAndCannotInventFailure(t *testing.T) {
	for _, message := range []string{"HTTP 429 too many requests", "HTTP 403 secondary rate limit", "HTTP 503 unavailable", "context deadline exceeded"} {
		t.Run(message, func(t *testing.T) {
			s, store := newAuditorTestScheduler(t)
			ctx := context.Background()
			seedStalledAssignment(t, store, "job", 111)
			job, err := store.Get(ctx, "job")
			require.NoError(t, err)
			job.RecoveryExhausted = true
			require.NoError(t, store.Update(ctx, job))
			ageJobRecord(t, job.ID, parkedPollInterval)
			source := &fakeWorkflowJobSource{err: errors.New(message)}
			s.withWorkflowJobSource(source)
			for i := 0; i < 50; i++ {
				s.recoverStalledAssignmentsForPool(auditorTestPool, 0)
			}
			require.Equal(t, 1, source.calls, "poll errors must still advance the cross-replica poll clock")
			current, err := store.Get(ctx, job.ID)
			require.NoError(t, err)
			require.Equal(t, redis.ScheduleAssigned, current.ScheduleStatus)
			require.True(t, current.RecoveryExhausted)
			require.Equal(t, job.RetryCount, current.RetryCount)
			require.Empty(t, current.Conclusion)
		})
	}
}
