package admin

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"freetier2api-plugin/cpasdk/pluginabi"
	"freetier2api-plugin/cpasdk/pluginapi"
	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge"
)

func probeFixture(t *testing.T, execute func(pluginapi.HostModelExecutionRequest) (json.RawMessage, error)) (*Service, stubDeps) {
	t.Helper()
	deps := newStubDeps(t)
	if err := deps.registry.Register(&fakeVendor{Meta: core.Meta{VendorID: "qoderglobal", VendorRegion: "global"}}); err != nil {
		t.Fatal(err)
	}
	deps.manager.CacheStatic("qoderglobal", []pluginapi.ModelInfo{{ID: "official"}})
	deps.manager.SetAlias("qoderglobal", "official", "short")
	hostbridge.Configure(func(cb, method string, payload any) (json.RawMessage, error) {
		raw, _ := json.Marshal(payload)
		switch method {
		case pluginabi.MethodHostAuthList:
			return json.RawMessage(`{"auths":[{"id":"runtime-id","name":"qoderglobal-fixture.json","provider":"freetier","auth_index":"idx"}]}`), nil
		case pluginabi.MethodHostAuthGet:
			return json.Marshal(pluginapi.HostAuthGetResponse{JSON: []byte(`{"vendor":"qoderglobal","proxy_url":"http://custom:8080","access_token":"fixture-secret-12345"}`)})
		case pluginabi.MethodHostModelExecute:
			var req pluginapi.HostModelExecutionRequest
			if err := json.Unmarshal(raw, &req); err != nil {
				return nil, err
			}
			if cb != "callback-fixture" {
				t.Errorf("lost callback scope: %q", cb)
			}
			return execute(req)
		}
		return nil, errors.New("unexpected callback " + method)
	})
	t.Cleanup(func() { hostbridge.Configure(nil) })
	return newServiceWithDeps(t, deps), deps
}

func callProbe(t *testing.T, s *Service, body string) (int, map[string]any) {
	t.Helper()
	request, _ := json.Marshal(pluginapi.ManagementRequest{Method: "POST", Path: "/plugins/freetier2api/models/probe", Headers: http.Header{"X-Host-Callback-Id": []string{"callback-fixture"}}, Body: []byte(body)})
	raw, err := s.Handle(request)
	if err != nil {
		t.Fatal(err)
	}
	var env struct{ Result pluginapi.ManagementResponse }
	if err = json.Unmarshal(raw, &env); err != nil {
		t.Fatal(err)
	}
	var data map[string]any
	if err = json.Unmarshal(env.Result.Body, &data); err != nil {
		t.Fatal(err)
	}
	return env.Result.StatusCode, data
}

func TestModelProbePinsSelectedCredentialAndAliasThroughHost(t *testing.T) {
	calls := 0
	s, _ := probeFixture(t, func(req pluginapi.HostModelExecutionRequest) (json.RawMessage, error) {
		calls++
		if req.Headers.Get(core.ModelProbeHeader) != "1" {
			t.Fatal("probe header missing")
		}
		if req.AuthID != "runtime-id" || req.ForcedProvider != "freetier" || req.Model != "short" || req.ProxyURL != "" || req.Stream {
			t.Fatalf("execution contract: %#v", req)
		}
		return json.Marshal(pluginapi.HostModelExecutionResponse{StatusCode: 200, Body: []byte(`{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}],"usage":{"completion_tokens":1}}`)})
	})
	status, d := callProbe(t, s, `{"auth_index":"idx","vendor_id":"qoderglobal","model":"short"}`)
	if status != 200 || d["ok"] != true || d["text"] != "ok" || d["auth_index"] != "idx" || calls != 1 {
		t.Fatalf("%d %#v calls=%d", status, d, calls)
	}
}

func TestModelProbePreservesBasicUpstreamError(t *testing.T) {
	message := `{"code":11158,"msg":"model [x] is not allowed for user type [personal_free_user]"}`
	s, _ := probeFixture(t, func(pluginapi.HostModelExecutionRequest) (json.RawMessage, error) {
		return nil, &hostbridge.CallError{Code: "host_call_failed", Message: message, HTTPStatus: 400}
	})
	status, d := callProbe(t, s, `{"auth_index":"idx","vendor_id":"qoderglobal","model":"short"}`)
	if status != 200 || d["ok"] != false || d["status"] != float64(400) || d["error"] != message {
		t.Fatalf("%d %#v", status, d)
	}
}

func TestModelProbeRejectsBadTargetsWithoutExecution(t *testing.T) {
	cases := []struct {
		body     string
		disabled bool
	}{
		{`{"model":"short"}`, false},
		{`{"auth_index":"missing","vendor_id":"qoderglobal","model":"short"}`, false},
		{`{"auth_index":"idx","vendor_id":"other","model":"short"}`, false},
		{`{"auth_index":"idx","vendor_id":"qoderglobal","model":"unknown"}`, false},
		{`{"auth_index":"idx","vendor_id":"qoderglobal","model":"short"}`, true},
	}
	for _, tc := range cases {
		t.Run(tc.body+strings.Repeat("disabled", btoi(tc.disabled)), func(t *testing.T) {
			s, deps := probeFixture(t, func(pluginapi.HostModelExecutionRequest) (json.RawMessage, error) {
				t.Fatal("executed invalid target")
				return nil, nil
			})
			if tc.disabled {
				deps.manager.SetDisabled([]string{"qoderglobal:official"}, true)
			}
			_, d := callProbe(t, s, tc.body)
			if d["ok"] != false {
				t.Fatalf("not a failure: %#v", d)
			}
		})
	}
}
func btoi(v bool) int {
	if v {
		return 1
	}
	return 0
}

func TestModelProbeEmptyTextIsNotSuccess(t *testing.T) {
	s, _ := probeFixture(t, func(pluginapi.HostModelExecutionRequest) (json.RawMessage, error) {
		return json.Marshal(pluginapi.HostModelExecutionResponse{StatusCode: 200, Body: []byte(`{"choices":[{"message":{"content":""},"finish_reason":"length"}]}`)})
	})
	_, d := callProbe(t, s, `{"auth_index":"idx","vendor_id":"qoderglobal","model":"short"}`)
	if d["ok"] != false || d["finish_reason"] != "length" {
		t.Fatalf("false positive: %#v", d)
	}
}

func TestModelProbeKeepsProductionModelTagCollisionResolution(t *testing.T) {
	s, deps := probeFixture(t, func(req pluginapi.HostModelExecutionRequest) (json.RawMessage, error) {
		if req.Model != "official" {
			t.Fatalf("probe changed the production model identity: %q", req.Model)
		}
		return json.Marshal(pluginapi.HostModelExecutionResponse{StatusCode: 200, Body: []byte(`{"choices":[{"message":{"content":"ok"}}]}`)})
	})
	deps.manager.SetAlias("qoderglobal", "official", "")
	deps.manager.CacheStatic("qoderglobal", []pluginapi.ModelInfo{{ID: "official"}, {ID: "qoderglobal-official"}})
	deps.manager.Configure(ModelSettings{ModelNameTag: ModelNameTagPrefix})
	status, d := callProbe(t, s, `{"auth_index":"idx","vendor_id":"qoderglobal","model":"official"}`)
	if status != 200 || d["ok"] != true {
		t.Fatalf("%d %#v", status, d)
	}
}

func TestModelProbeClassifiesNetworkError(t *testing.T) {
	s, _ := probeFixture(t, func(pluginapi.HostModelExecutionRequest) (json.RawMessage, error) {
		return nil, &hostbridge.CallError{Code: "host_call_failed", Message: "dial tcp: TLS handshake timeout", HTTPStatus: 502}
	})
	_, d := callProbe(t, s, `{"auth_index":"idx","vendor_id":"qoderglobal","model":"short"}`)
	if d["error_kind"] != "network" || d["error"] != "dial tcp: TLS handshake timeout" {
		t.Fatalf("%#v", d)
	}
}

func TestModelProbeRedactsUpstreamCredentialEcho(t *testing.T) {
	s, _ := probeFixture(t, func(pluginapi.HostModelExecutionRequest) (json.RawMessage, error) {
		return nil, &hostbridge.CallError{Code: "host_call_failed", Message: "invalid token fixture-secret-12345", HTTPStatus: 401}
	})
	_, d := callProbe(t, s, `{"auth_index":"idx","vendor_id":"qoderglobal","model":"short"}`)
	if d["error"] != "invalid token [redacted]" {
		t.Fatalf("credential was not redacted")
	}
}

func TestModelProbeAcceptsConfiguredExtraModel(t *testing.T) {
	s, deps := probeFixture(t, func(req pluginapi.HostModelExecutionRequest) (json.RawMessage, error) {
		if req.Model != "extra-model" {
			t.Fatalf("model %q", req.Model)
		}
		return json.Marshal(pluginapi.HostModelExecutionResponse{StatusCode: 200, Body: []byte(`{"choices":[{"message":{"content":"ok"}}]}`)})
	})
	deps.manager.Configure(ModelSettings{ExtraModels: []string{"extra-model"}})
	status, d := callProbe(t, s, `{"auth_index":"idx","vendor_id":"qoderglobal","model":"extra-model"}`)
	if status != 200 || d["ok"] != true {
		t.Fatalf("%d %#v", status, d)
	}
}

func TestModelProbePreservesStatusFromWrappedNativeHostError(t *testing.T) {
	s, _ := probeFixture(t, func(pluginapi.HostModelExecutionRequest) (json.RawMessage, error) {
		return nil, fmt.Errorf("host.model.execute: %w", &hostbridge.CallError{Code: "host_call_failed", Message: `{"code":11158,"msg":"is not allowed for user type"}`, HTTPStatus: 400})
	})
	_, d := callProbe(t, s, `{"auth_index":"idx","vendor_id":"qoderglobal","model":"short"}`)
	if d["status"] != float64(400) {
		t.Fatalf("lost wrapped status: %#v", d)
	}
}
