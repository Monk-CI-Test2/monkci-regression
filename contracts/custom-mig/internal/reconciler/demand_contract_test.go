package reconciler

import (
	"context"
	"github.com/stretchr/testify/require"
	"testing"
	"time"
)

func TestRegressionMultipleSilentBootsDoNotHideHealthyCapacity(t *testing.T) {
	for _, stuck := range []int{1, 3, 12} {
		t.Run(time.Duration(stuck).String(), func(t *testing.T) {
			r, _, _, cleanup := newAllocatorTestReconciler(t, false)
			defer cleanup()
			for i := 0; i < stuck; i++ {
				seedWarmWithMigletState(t, r, time.Duration(i).String(), "booting", time.Now().Add(-10*time.Minute))
			}
			seedWarmWithMigletState(t, r, "healthy", "ready", time.Now().Add(-time.Hour))
			seedWarmWithMigletState(t, r, "starting", "booting", time.Now().Add(-time.Minute))
			capacity, overdue, err := r.warmDemandCapacity(context.Background(), r.cfg.Pools[0].PoolID)
			require.NoError(t, err)
			require.Equal(t, 2, capacity)
			require.Len(t, overdue, stuck)
		})
	}
}
