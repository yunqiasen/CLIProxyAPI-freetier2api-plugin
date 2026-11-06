package qoder

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"freetier2api-plugin/cpasdk/pluginabi"
	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge"
)

func TestExecutePreservesProbeContext(t *testing.T) {
	for _, canceled := range []bool{false, true} {
		t.Run(fmt.Sprintf("canceled=%v", canceled), func(t *testing.T) {
			const authIndex = "AUTH_FIXTURE"
			ctx, cancel := context.WithCancel(core.WithModelProbe(hostbridge.WithAuthIndex(context.Background(), authIndex)))
			defer cancel()
			if canceled {
				cancel()
			}
			v := newInstance(core.Env{}, RegionGlobal).(*adapter)
			native := &Credential{Token: "dt-fixture", Region: RegionGlobal}
			cred := &core.Credential{VendorID: VendorIDGlobal, Region: string(RegionGlobal), Native: native, AuthIndex: authIndex}
			bridge := &Bridge{sess: &SessionContext{}, client: NewBearerClient(&SessionContext{Info: "fixture", CosyKey: "fixture"}), region: RegionGlobal, templateBase: map[string]interface{}{}, chatStreamURL: "https://fixture.invalid/chat"}
			v.bridges.Store(v.bridgeCacheKey(native), bridgeEntry{bridge: bridge, createdAt: time.Now()})
			var mu sync.Mutex
			calls, reads := 0, 0
			var indexes []string
			hostbridge.Configure(func(_ string, method string, payload any) (json.RawMessage, error) {
				mu.Lock()
				defer mu.Unlock()
				switch method {
				case pluginabi.MethodHostHTTPDoStream:
					calls++
					reads = 0
					data, _ := json.Marshal(payload)
					var req struct {
						AuthIndex string `json:"auth_index"`
					}
					_ = json.Unmarshal(data, &req)
					indexes = append(indexes, req.AuthIndex)
					status := 503
					if calls > 1 {
						status = 400
					}
					return json.Marshal(map[string]any{"status_code": status, "stream_id": "fixture-stream"})
				case pluginabi.MethodHostHTTPStreamRead:
					reads++
					if reads == 1 {
						body := `{"message":"service unavailable","request_id":"REQ_FIXTURE"}`
						if calls > 1 {
							body = `{"message":"invalid_request_error"}`
						}
						return json.Marshal(map[string]any{"payload": []byte(body)})
					}
					return json.Marshal(map[string]any{"done": true})
				case pluginabi.MethodHostHTTPStreamClose:
					return json.RawMessage(`{}`), nil
				default:
					return nil, fmt.Errorf("unexpected method %s", method)
				}
			})
			t.Cleanup(func() { hostbridge.Configure(nil) })
			_, err := v.Execute(ctx, cred, &core.ExecuteRequest{Model: "efficient", Payload: []byte(`{"model":"efficient","messages":[{"role":"user","content":"ok"}]}`)})
			mu.Lock()
			defer mu.Unlock()
			if canceled {
				if !errors.Is(err, context.Canceled) || calls != 0 {
					t.Fatalf("canceled request sent: calls=%d err=%v", calls, err)
				}
				return
			}
			if calls != 1 || len(indexes) != 1 || indexes[0] != authIndex {
				t.Fatalf("lost probe/egress context: calls=%d indexes=%q err=%v", calls, indexes, err)
			}
			probe := core.ToProbeError(err).(*core.PluginError)
			if probe.HTTPStatus != 503 || probe.Message != `{"message":"service unavailable","request_id":"REQ_FIXTURE"}` {
				t.Fatalf("lost raw response: %+v", probe)
			}
		})
	}
}
