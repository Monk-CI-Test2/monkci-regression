// Central regression contract: run via scripts/source_contracts.py.
package state

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestRegressionDeletionProofSurvivesMissingVMRecord(t *testing.T) {
	client, cleanup := setupTestRedis(t)
	defer cleanup()
	store := NewVMStore(client)
	ctx := context.Background()
	require.NoError(t, client.Client().SAdd(ctx, "vm:retirement_requests", "vm").Err())
	require.NoError(t, store.ConfirmDeletion(ctx, "vm"))
	require.Equal(t, "1", client.Client().Get(ctx, "vm:deletion_confirmed:vm").Val())
	require.False(t, client.Client().SIsMember(ctx, "vm:retirement_requests", "vm").Val())
}

func TestRegressionReconstructedVMRetainsRetirementIntent(t *testing.T) {
	client, cleanup := setupTestRedis(t)
	defer cleanup()
	store := NewVMStore(client)
	ctx := context.Background()
	require.NoError(t, client.Client().SAdd(ctx, "vm:retirement_requests", "vm").Err())
	require.NoError(t, store.Set(ctx, warmVM("vm", "p", "")))
	require.NoError(t, store.ApplyRetirementRequest(ctx, "vm"))
	vm, err := store.Get(ctx, "vm")
	require.NoError(t, err)
	require.Equal(t, VMStateCompleted, vm.State)
	require.True(t, client.Client().SIsMember(ctx, "pool:p:completed", "vm").Val())
	require.False(t, client.Client().SIsMember(ctx, "pool:p:warm", "vm").Val())
	require.Equal(t, true, readRecord(t, client, "vm")["retirementRequested"])
	require.Equal(t, time.Duration(-1), client.Client().TTL(ctx, "vm:vm").Val())
}

func TestRegressionMissedRestartRepairPreservesBusyClaim(t *testing.T) {
	client, cleanup := setupTestRedis(t)
	defer cleanup()
	store := NewVMStore(client)
	ctx := context.Background()
	oldBoot := time.Now().UTC().Add(-time.Hour)
	require.NoError(t, store.Set(ctx, &VM{VMID: "vm", PoolID: "p", State: VMStateBusy, WarmSince: oldBoot}))
	writeForeignFields(t, client, "vm", map[string]interface{}{"jobId": "job", "busySince": "2026-10-03T00:00:00Z", "migletState": "ready", "lastHeartbeatSourceMs": oldBoot.UnixMilli()})
	vm, err := store.Get(ctx, "vm")
	require.NoError(t, err)
	applied, err := store.ResetWarmBootIfStale(ctx, vm, time.Now().UTC())
	require.NoError(t, err)
	require.True(t, applied)
	record := readRecord(t, client, "vm")
	require.Equal(t, "busy", record["state"])
	require.Equal(t, "job", record["jobId"])
	require.Equal(t, "2026-10-03T00:00:00Z", record["busySince"])
	require.Equal(t, "unknown", record["migletState"])
	require.NotContains(t, record, "lastHeartbeatSourceMs")
}

func TestRegressionMissedRestartRepairPreservesCurrentBootEvidence(t *testing.T) {
	for _, migletState := range []string{"ready", "idle", "job_running"} {
		t.Run(migletState, func(t *testing.T) {
			client, cleanup := setupTestRedis(t)
			defer cleanup()
			store := NewVMStore(client)
			ctx := context.Background()
			newBoot := time.Now().UTC().Add(-time.Minute)
			require.NoError(t, store.Set(ctx, &VM{VMID: "vm", PoolID: "p", State: VMStateBusy, WarmSince: newBoot.Add(-time.Hour)}))
			observed, err := store.Get(ctx, "vm")
			require.NoError(t, err)
			// Approval or a heartbeat lands after the reconciler read its snapshot.
			source := newBoot.Add(30 * time.Second).UnixMilli()
			writeForeignFields(t, client, "vm", map[string]interface{}{
				"jobId": "job", "migletState": migletState, "lastHeartbeatSourceMs": newBoot.Add(-time.Second).UnixMilli(), "lastStateSourceMs": source, "isConnected": true,
			})
			applied, err := store.ResetWarmBootIfStale(ctx, observed, newBoot)
			require.NoError(t, err)
			require.True(t, applied)
			record := readRecord(t, client, "vm")
			require.Equal(t, migletState, record["migletState"])
			require.Equal(t, float64(source), record["lastStateSourceMs"])
			require.Equal(t, float64(newBoot.UnixMilli()), record["warmSinceMs"])
			require.Equal(t, "job", record["jobId"])
		})
	}
}
