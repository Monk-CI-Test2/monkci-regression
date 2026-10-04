// Central regression contract: run via scripts/source_contracts.py.
package handler

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/rs/zerolog"
	"github.com/stretchr/testify/require"
	"google.golang.org/protobuf/proto"

	"github.com/monkci/miglet/internal/runner"
	"github.com/monkci/miglet/internal/state"
	"github.com/monkci/miglet/internal/twirp"
	migletv1 "github.com/monkci/miglet/proto/miglet/v1"
)

func TestRegressionRunnerApprovalRetriesAndStartsOnlyOnce(t *testing.T) {
	h, dir, calls, cleanups, _ := regressionApprovalHandler(t, 2, false)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	require.NoError(t, h.startRegisteredRunner(ctx))
	require.NoError(t, h.startRegisteredRunner(ctx))
	require.NoError(t, h.WaitForRunners(ctx))
	content, err := os.ReadFile(filepath.Join(dir, "runs"))
	require.NoError(t, err)
	require.Equal(t, "started\n", string(content))
	require.Equal(t, int32(3), calls.Load())
	require.Equal(t, int32(0), cleanups.Load())
}

func TestRegressionRunnerApprovalExhaustionCleansOnlyVMAndNeverStarts(t *testing.T) {
	for _, cleanupFails := range []bool{false, true} {
		name := "cleanup_succeeds"
		if cleanupFails {
			name = "heartbeat_cleanup_fallback"
		}
		t.Run(name, func(t *testing.T) {
			h, dir, calls, cleanups, shutdown := regressionApprovalHandler(t, 100, cleanupFails)
			require.Error(t, h.startRegisteredRunner(context.Background()))
			require.Equal(t, int32(3), calls.Load())
			require.Equal(t, int32(1), cleanups.Load())
			require.True(t, h.registrationAbandoned)
			require.False(t, h.runnerStarted)
			require.Error(t, h.startRegisteredRunner(context.Background()), "late duplicate commands cannot restart an abandoned registration")
			require.Equal(t, int32(3), calls.Load())
			_, err := os.Stat(filepath.Join(dir, "runs"))
			require.True(t, os.IsNotExist(err))
			select {
			case <-shutdown:
				t.Fatal("must wait for heartbeat confirmation of cleanup")
			default:
			}
			require.Equal(t, state.StateIdle, h.stateManager.GetState())
		})
	}
}

func regressionApprovalHandler(t *testing.T, failures int32, cleanupFails bool) (*CommandHandler, string, *atomic.Int32, *atomic.Int32, <-chan struct{}) {
	t.Helper()
	calls, cleanups := &atomic.Int32{}, &atomic.Int32{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var reply proto.Message
		if strings.HasSuffix(r.URL.Path, "/CompleteJob") {
			cleanups.Add(1)
			body, err := io.ReadAll(r.Body)
			if err != nil {
				t.Error(err)
				return
			}
			req := &migletv1.CompleteJobRequest{}
			if err := proto.Unmarshal(body, req); err != nil {
				t.Error(err)
				return
			}
			if req.JobId != "" {
				t.Error("cleanup must not complete a customer job")
			}
			if cleanupFails {
				http.Error(w, "unavailable", http.StatusServiceUnavailable)
				return
			}
			reply = &migletv1.CompleteJobResponse{Ok: true}
		} else {
			if calls.Add(1) <= failures {
				http.Error(w, "temporarily unavailable", http.StatusServiceUnavailable)
				return
			}
			reply = &migletv1.ReportEventResponse{Ok: true}
		}
		body, err := proto.Marshal(reply)
		if err != nil {
			t.Error(err)
			return
		}
		w.Header().Set("Content-Type", "application/protobuf")
		_, _ = w.Write(body)
	}))
	t.Cleanup(server.Close)
	log := zerolog.Nop()
	client := twirp.NewClient(twirp.ClientConfig{ControllerURL: server.URL, VMID: "vm", PoolID: "pool", Timeout: time.Second}, log)
	dir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(dir, "run.sh"), []byte("#!/bin/sh\necho started >> runs\n"), 0755))
	sm := state.NewManager(log)
	require.NoError(t, sm.Transition(state.StateReady))
	require.NoError(t, sm.TransitionWithRunner("runner"))
	shutdown := make(chan struct{})
	h := NewCommandHandler("vm", "pool", client, runner.NewManager(dir, dir, log), sm, func() { close(shutdown) }, log, nil, nil)
	return h, dir, calls, cleanups, shutdown
}

func TestRegressionConcurrentDuplicateApprovalsStartOneListener(t *testing.T) {
	h, dir, calls, cleanups, _ := regressionApprovalHandler(t, 0, false)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	done := make(chan error, 12)
	cmd := &migletv1.Command{Type: migletv1.CommandType_COMMAND_TYPE_REGISTER_RUNNER, Payload: &migletv1.Command_RegisterRunner{RegisterRunner: &migletv1.RegisterRunnerCommand{RunnerName: "runner"}}}
	for i := 0; i < 12; i++ {
		go func() { done <- h.Handle(ctx, cmd) }()
	}
	for i := 0; i < 12; i++ {
		require.NoError(t, <-done)
	}
	require.NoError(t, h.WaitForRunners(ctx))
	data, err := os.ReadFile(filepath.Join(dir, "runs"))
	require.NoError(t, err)
	require.Equal(t, "started\n", string(data))
	require.EqualValues(t, 1, calls.Load())
	require.Zero(t, cleanups.Load())
}
