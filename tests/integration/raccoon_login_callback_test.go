package tests

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"freetier2api-plugin/cpasdk/pluginabi"
	"freetier2api-plugin/cpasdk/pluginapi"
	"freetier2api-plugin/internal/app"
	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge"
	"freetier2api-plugin/internal/vendors/raccoon"
	"freetier2api-plugin/tests/testhost"
)

func TestRaccoonCallbackMakesAuthAvailableToHostPoll(t *testing.T) {
	host := testhost.New()
	var exchanges atomic.Int32
	host.SetUpstream(func(_ string, target, _ string) testhost.UpstreamResponse {
		if strings.HasSuffix(target, raccoon.LoginCodePath) {
			exchanges.Add(1)
			return testhost.UpstreamResponse{Status: 200, Body: `{"code":0,"data":{"access_token":"ACCESS_FIXTURE","refresh_token":"REFRESH_FIXTURE","user_id":"USER_FIXTURE"}}`}
		}
		return testhost.UpstreamResponse{Status: 200, Body: `{"code":0,"data":{"id":"USER_FIXTURE","name":"登录夹具"}}`}
	})
	hostbridge.Configure(host.Call)
	t.Cleanup(func() { hostbridge.Configure(nil) })
	instance, request := newLifecycleFixture(t, app.Options{Factories: []core.VendorFactory{{Instances: raccoon.Instances}}})
	callLifecycle(t, instance, pluginabi.MethodPluginRegister, request)
	raw, err := instance.HandleMethod(pluginabi.MethodAuthLoginStart, mustMarshal(map[string]any{"metadata": map[string]any{"vendor": "raccoon"}}))
	if err != nil {
		t.Fatal(err)
	}
	var started pluginapi.AuthLoginStartResponse
	if err = json.Unmarshal(decodeEnvelopeResultForTest(t, raw), &started); err != nil {
		t.Fatal(err)
	}
	authorize, _ := url.Parse(started.URL)
	callback := raccoon.CallbackURL() + "?code=CODE_FIXTURE&state=" + authorize.Query().Get("state")
	client := &http.Client{Transport: &http.Transport{Proxy: nil}, Timeout: 3 * time.Second}
	defer client.CloseIdleConnections()
	resp, err := client.Get(callback)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("callback status=%d body=%s", resp.StatusCode, body)
	}
	if strings.Contains(string(body), "ACCESS_FIXTURE") {
		t.Fatal("callback leaked credential")
	}
	for i := 0; i < 2; i++ {
		raw, err = instance.HandleMethod(pluginabi.MethodAuthLoginPoll, mustMarshal(map[string]any{"state": started.State}))
		if err != nil {
			t.Fatal(err)
		}
		var polled pluginapi.AuthLoginPollResponse
		if err = json.Unmarshal(decodeEnvelopeResultForTest(t, raw), &polled); err != nil {
			t.Fatal(err)
		}
		if polled.Status != pluginapi.AuthLoginStatusSuccess || polled.Auth.FileName == "" || !strings.Contains(string(polled.Auth.StorageJSON), "ACCESS_FIXTURE") {
			t.Fatalf("host cannot save callback auth: status=%s filename=%s", polled.Status, polled.Auth.FileName)
		}
	}
	if exchanges.Load() != 1 {
		t.Fatalf("single-use code exchanged %d times", exchanges.Load())
	}
}

func TestRaccoonCallbackListenerClosesOnPluginQuiesce(t *testing.T) {
	hostbridge.Configure(testhost.New().Call)
	t.Cleanup(func() { hostbridge.Configure(nil) })
	instance, request := newLifecycleFixture(t, app.Options{Factories: []core.VendorFactory{{Instances: raccoon.Instances}}})
	callLifecycle(t, instance, pluginabi.MethodPluginRegister, request)
	start := func() {
		_, err := instance.HandleMethod(pluginabi.MethodAuthLoginStart, mustMarshal(map[string]any{"metadata": map[string]any{"vendor": "raccoon"}}))
		if err != nil {
			t.Fatal(err)
		}
	}
	start()
	callLifecycle(t, instance, pluginabi.MethodPluginQuiesce, nil)
	listener, err := net.Listen("tcp", "127.0.0.1:37913")
	if err != nil {
		t.Fatalf("callback port still held after quiesce: %v", err)
	}
	listener.Close()
	callLifecycle(t, instance, pluginabi.MethodPluginReconfigure, request)
	start()
	conn, err := net.DialTimeout("tcp", "127.0.0.1:37913", time.Second)
	if err != nil {
		t.Fatalf("listener did not restart: %v", err)
	}
	conn.Close()
}

func TestRaccoonCallbackExpiryAndInstanceIsolation(t *testing.T) {
	var old, current raccoon.CallbackCatcher
	defer old.Stop()
	defer current.Stop()
	handler := func(context.Context, string, string) (string, error) { return "<script>fixture</script>", nil }
	if !old.Start("state-fixture", 20*time.Millisecond, handler) {
		t.Fatal("start expired fixture")
	}
	deadline := time.Now().Add(time.Second)
	for {
		listener, err := net.Listen("tcp", "127.0.0.1:37913")
		if err == nil {
			listener.Close()
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("expired listener still held: %v", err)
		}
		time.Sleep(5 * time.Millisecond)
	}
	if !current.Start("state-fixture", time.Minute, handler) {
		t.Fatal("new instance did not acquire port")
	}
	old.Stop() // a retired instance must not close the current listener.
	client := &http.Client{Transport: &http.Transport{Proxy: nil}, Timeout: time.Second}
	defer client.CloseIdleConnections()
	for _, state := range []string{"wrong-state", "state-fixture"} {
		resp, err := client.Get(raccoon.CallbackURL() + "?code=CODE_FIXTURE&state=" + state)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if state == "wrong-state" {
			if resp.StatusCode != 400 {
				t.Fatalf("invalid state accepted: %d", resp.StatusCode)
			}
		} else if resp.StatusCode != 200 || strings.Contains(string(body), "<script>") || !strings.Contains(string(body), "&lt;script&gt;") {
			t.Fatalf("callback result not rendered as text: %d %s", resp.StatusCode, body)
		}
	}
}
