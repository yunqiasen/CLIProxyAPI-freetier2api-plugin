package tests

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"freetier2api-plugin/cpasdk/pluginabi"
	"freetier2api-plugin/internal/app"
	"freetier2api-plugin/internal/core"
)

func TestPluginLifecycleReconfigureResumesAfterQuiesce(t *testing.T) {
	instance, request := newLifecycleFixture(t, app.Options{})
	callLifecycle(t, instance, pluginabi.MethodPluginRegister, request)
	if !instance.BackgroundRunning() {
		t.Fatal("register must start background work")
	}

	for cycle := 0; cycle < 3; cycle++ {
		callLifecycle(t, instance, pluginabi.MethodPluginQuiesce, nil)
		callLifecycle(t, instance, pluginabi.MethodPluginQuiesce, nil)
		if instance.BackgroundRunning() {
			t.Fatalf("cycle %d: repeated quiesce must leave background work stopped", cycle)
		}

		result := callLifecycle(t, instance, pluginabi.MethodPluginReconfigure, request)
		var registration app.RegistrationInfo
		if err := json.Unmarshal(result, &registration); err != nil {
			t.Fatalf("decode reconfigure response: %v", err)
		}
		if registration.SchemaVersion != pluginabi.SchemaVersion || registration.Metadata.Name == "" {
			t.Fatalf("reconfigure must return the complete registration: %+v", registration)
		}
		if !instance.BackgroundRunning() {
			t.Fatalf("cycle %d: successful rollback reconfigure must resume background work", cycle)
		}
	}
}

func TestPluginLifecycleFailedReconfigureKeepsBackgroundStopped(t *testing.T) {
	for _, failure := range []string{"decode", "apply"} {
		t.Run(failure, func(t *testing.T) {
			instance, request := newLifecycleFixture(t, app.Options{})
			callLifecycle(t, instance, pluginabi.MethodPluginRegister, request)
			callLifecycle(t, instance, pluginabi.MethodPluginQuiesce, nil)

			config := "log_level: invalid\n"
			if failure == "apply" {
				path := filepath.Join(t.TempDir(), "not-a-directory")
				if err := os.WriteFile(path, []byte("fixture"), 0o600); err != nil {
					t.Fatal(err)
				}
				config = fmt.Sprintf("state_dir: %q\n", path)
			}
			if _, err := instance.HandleMethod(pluginabi.MethodPluginReconfigure,
				mustMarshal(app.LifecycleRequest{ConfigYAML: []byte(config)})); err == nil {
				t.Fatal("invalid reconfigure must fail")
			}
			if instance.BackgroundRunning() {
				t.Fatal("failed reconfigure must leave background work stopped")
			}
			callLifecycle(t, instance, pluginabi.MethodPluginReconfigure, request)
			if !instance.BackgroundRunning() {
				t.Fatal("a later successful reconfigure must resume background work")
			}
		})
	}
}

func TestPluginLifecycleReconfigureBeforeRegisterDoesNotStartBackground(t *testing.T) {
	instance, request := newLifecycleFixture(t, app.Options{})
	callLifecycle(t, instance, pluginabi.MethodPluginQuiesce, nil)
	callLifecycle(t, instance, pluginabi.MethodPluginReconfigure, request)
	if instance.BackgroundRunning() {
		t.Fatal("reconfigure must not start background work before registration")
	}
	callLifecycle(t, instance, pluginabi.MethodPluginRegister, request)
	if !instance.BackgroundRunning() {
		t.Fatal("registration must still start background work after an early reconfigure")
	}
}

func TestPluginLifecycleRunningReconfigureDoesNotRestartScheduler(t *testing.T) {
	var evaluations atomic.Int64
	started := make(chan struct{}, 1)
	// Keep every wake in the future so each scheduler generation evaluates once.
	now := time.Now().Add(24 * time.Hour)
	instance, request := newLifecycleFixture(t, app.Options{Now: func() time.Time {
		if evaluations.Add(1) == 1 {
			started <- struct{}{}
		}
		return now
	}})
	callLifecycle(t, instance, pluginabi.MethodPluginRegister, request)
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("scheduler did not start")
	}
	for cycle := 0; cycle < 10; cycle++ {
		callLifecycle(t, instance, pluginabi.MethodPluginReconfigure, request)
		callLifecycle(t, instance, pluginabi.MethodPluginRegister, request)
		if !instance.BackgroundRunning() {
			t.Fatalf("cycle %d: repeated lifecycle configuration must keep background work running", cycle)
		}
	}
	callLifecycle(t, instance, pluginabi.MethodPluginQuiesce, nil)
	// 启动期有一次 pruneCheckinHistory 也会取 now，调度循环本身只该跑一轮。
	if got := evaluations.Load(); got > 2 {
		t.Fatalf("scheduler generations = %d, want <=2 (prune + one loop) across repeated register/reconfigure", got)
	}
}

func TestPluginLifecycleQuiesceAndRollbackPreserveInflightStreams(t *testing.T) {
	instance, request := newLifecycleFixture(t, app.Options{})
	callLifecycle(t, instance, pluginabi.MethodPluginRegister, request)
	ctx, _, finish := instance.BeginPluginStream()
	defer finish()

	callLifecycle(t, instance, pluginabi.MethodPluginQuiesce, nil)
	if err := ctx.Err(); err != nil {
		t.Fatalf("quiesce must leave stream draining to the host: %v", err)
	}
	callLifecycle(t, instance, pluginabi.MethodPluginReconfigure, request)
	if err := ctx.Err(); err != nil {
		t.Fatalf("rollback must preserve the inflight stream: %v", err)
	}

	drained := make(chan struct{})
	go func() {
		<-ctx.Done()
		finish()
		close(drained)
	}()
	instance.WaitPluginStreams(time.Second)
	select {
	case <-drained:
	case <-time.After(3 * time.Second):
		t.Fatal("explicit stream shutdown did not cancel and drain the stream")
	}

	resumed, _, finishResumed := instance.BeginPluginStream()
	defer finishResumed()
	if err := resumed.Err(); err != nil {
		t.Fatalf("a new stream must remain usable after rollback: %v", err)
	}
}

func TestPluginLifecycleConcurrentQuiesceAndReconfigure(t *testing.T) {
	instance, request := newLifecycleFixture(t, app.Options{})
	callLifecycle(t, instance, pluginabi.MethodPluginRegister, request)
	const workers = 8
	errors := make(chan error, workers)
	var workersDone sync.WaitGroup
	for worker := 0; worker < workers; worker++ {
		workersDone.Add(1)
		go func() {
			defer workersDone.Done()
			for cycle := 0; cycle < 10; cycle++ {
				for _, method := range []string{pluginabi.MethodPluginQuiesce, pluginabi.MethodPluginReconfigure} {
					if _, err := instance.HandleMethod(method, request); err != nil {
						errors <- err
						return
					}
				}
			}
		}()
	}
	workersDone.Wait()
	close(errors)
	for err := range errors {
		t.Errorf("concurrent lifecycle call: %v", err)
	}
	callLifecycle(t, instance, pluginabi.MethodPluginReconfigure, request)
	if !instance.BackgroundRunning() {
		t.Fatal("final rollback must leave background work running")
	}
	callLifecycle(t, instance, pluginabi.MethodPluginQuiesce, nil)
	if instance.BackgroundRunning() {
		t.Fatal("final quiesce must leave background work stopped")
	}
}

func TestPluginLifecycleQuiesceWaitsForReconfigure(t *testing.T) {
	vendor := &blockingLifecycleVendor{
		recordingVendor: recordingVendor{region: "cn"},
		entered:         make(chan struct{}),
		release:         make(chan struct{}),
	}
	instance, request := newLifecycleFixture(t, app.Options{Factories: []core.VendorFactory{{
		Instances: func(core.Env) []core.Vendor { return []core.Vendor{vendor} },
	}}})
	callLifecycle(t, instance, pluginabi.MethodPluginRegister, request)

	var releaseOnce sync.Once
	release := func() { releaseOnce.Do(func() { close(vendor.release) }) }
	defer release()
	var config app.LifecycleRequest
	if err := json.Unmarshal(request, &config); err != nil {
		t.Fatal(err)
	}
	config.ConfigYAML = append(config.ConfigYAML, []byte("lifecycle_pause: true\n")...)
	reconfigureDone := make(chan error, 1)
	go func() {
		_, err := instance.HandleMethod(pluginabi.MethodPluginReconfigure, mustMarshal(config))
		reconfigureDone <- err
	}()
	select {
	case <-vendor.entered:
	case <-time.After(3 * time.Second):
		t.Fatal("reconfigure did not reach vendor configuration")
	}

	quiesceStarted := make(chan struct{})
	quiesceDone := make(chan error, 1)
	go func() {
		close(quiesceStarted)
		_, err := instance.HandleMethod(pluginabi.MethodPluginQuiesce, nil)
		quiesceDone <- err
	}()
	<-quiesceStarted
	select {
	case err := <-quiesceDone:
		t.Errorf("quiesce returned before pending reconfigure completed: %v", err)
		quiesceDone = nil
	case <-time.After(100 * time.Millisecond):
	}

	release()
	for method, done := range map[string]<-chan error{
		pluginabi.MethodPluginReconfigure: reconfigureDone,
		pluginabi.MethodPluginQuiesce:     quiesceDone,
	} {
		if done == nil {
			continue
		}
		select {
		case err := <-done:
			if err != nil {
				t.Errorf("%s: %v", method, err)
			}
		case <-time.After(3 * time.Second):
			t.Fatalf("%s did not finish", method)
		}
	}
	if instance.BackgroundRunning() {
		t.Fatal("quiesce must stop background work after pending reconfigure finishes")
	}
}

type blockingLifecycleVendor struct {
	recordingVendor
	entered chan struct{}
	release chan struct{}
}

func (v *blockingLifecycleVendor) Configure(_ core.Settings, raw map[string]string) {
	if raw["lifecycle_pause"] == "true" {
		close(v.entered)
		<-v.release
	}
}

func newLifecycleFixture(t *testing.T, options app.Options) (*app.App, []byte) {
	t.Helper()
	instance, err := app.New(options)
	if err != nil {
		t.Fatalf("new app: %v", err)
	}
	config := fmt.Sprintf("state_dir: %q\nlog_level: error\nlog_to_file: false\nauto_checkin: false\nauto_tasks: false\n", t.TempDir())
	t.Cleanup(func() {
		instance.StopBackgroundWork()
		instance.WaitPluginStreams(0)
	})
	return instance, mustMarshal(app.LifecycleRequest{ConfigYAML: []byte(config)})
}

func callLifecycle(t *testing.T, instance *app.App, method string, request []byte) json.RawMessage {
	t.Helper()
	raw, err := instance.HandleMethod(method, request)
	if err != nil {
		t.Fatalf("%s: %v", method, err)
	}
	return decodeEnvelopeResultForTest(t, raw)
}
