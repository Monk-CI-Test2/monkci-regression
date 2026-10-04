package state

import (
	"context"
	"github.com/stretchr/testify/require"
	"testing"
	"time"
)

func TestRegressionRepeatedWarmRestartsInvalidateBothMessageClocks(t *testing.T) {
	for _, reservation := range []string{"", "reserved-capacity"} {
		t.Run("reservation_"+reservation, func(t *testing.T) {
			client, cleanup := setupTestRedis(t)
			defer cleanup()
			store := NewVMStore(client)
			ctx := context.Background()
			require.NoError(t, store.Set(ctx, warmVM("vm", "p", reservation)))
			for cycle := 0; cycle < 3; cycle++ {
				now := time.Now().Add(-time.Minute)
				writeForeignFields(t, client, "vm", map[string]interface{}{"migletState": "ready", "lastHeartbeatSourceMs": now.UnixMilli(), "lastStateSourceMs": now.UnixMilli(), "lastHeartbeat": now.Format(time.RFC3339Nano), "runnerName": "foreign-field"})
				require.NoError(t, store.Move(ctx, "vm", VMStateTerminated))
				require.NoError(t, store.Move(ctx, "vm", VMStatePendingStart))
				require.NoError(t, store.Move(ctx, "vm", VMStateWarm))
				record := readRecord(t, client, "vm")
				require.Equal(t, "unknown", record["migletState"])
				require.NotContains(t, record, "lastHeartbeatSourceMs")
				require.NotContains(t, record, "lastStateSourceMs")
				require.Equal(t, "foreign-field", record["runnerName"])
				require.Greater(t, record["warmSinceMs"].(float64), float64(now.UnixMilli()))
				for _, s := range []VMState{VMStateWarm, VMStateTerminated, VMStatePendingStart} {
					n, err := store.CountByState(ctx, "p", s)
					require.NoError(t, err)
					if s == VMStateWarm {
						require.EqualValues(t, 1, n)
					} else {
						require.Zero(t, n)
					}
				}
			}
		})
	}
}
