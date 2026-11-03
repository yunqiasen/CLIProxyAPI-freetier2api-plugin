package qoder

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"

	"freetier2api-plugin/cpasdk/pluginabi"
	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge"
)

func TestQoderProbeMakesOneAttemptAtTransportAndEnvelopeLayers(t *testing.T) {
	for _, tc := range []struct {
		name      string
		transport bool
		status    int
	}{{"http", false, 503}, {"transport", true, 0}, {"sse envelope", false, 200}} {
		t.Run(tc.name, func(t *testing.T) {
			base, cancel := context.WithCancel(context.Background())
			defer cancel()
			ctx := core.WithModelProbe(base)
			calls, reads := 0, 0
			hostbridge.Configure(func(_, method string, payload any) (json.RawMessage, error) {
				switch method {
				case pluginabi.MethodHostHTTPDoStream:
					calls++
					reads = 0
					if calls > 1 {
						cancel()
					}
					if tc.transport {
						return nil, fmt.Errorf("dial tcp: EOF")
					}
					return json.Marshal(map[string]any{"status_code": tc.status, "stream_id": "probe-stream"})
				case pluginabi.MethodHostHTTPStreamRead:
					reads++
					if reads == 1 {
						body := `{"code":"503","msg":"service unavailable"}`
						if tc.status == 200 {
							body = "data: {\"statusCodeValue\":503,\"body\":\"service unavailable\"}\n\n"
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
			bridge := &Bridge{sess: &SessionContext{}, client: NewBearerClient(&SessionContext{Info: "fixture", CosyKey: "fixture"}), region: RegionGlobal, templateBase: map[string]interface{}{}, chatStreamURL: "https://fixture.test/chat"}
			err := bridge.CallQoderWithOpts(ctx, "agent", []interface{}{map[string]interface{}{"role": "user", "content": "ok"}}, "efficient", nil, CallOpts{}, func(Delta) {})
			if err == nil {
				t.Fatal("expected failure")
			}
			if calls != 1 {
				t.Fatalf("probe made %d upstream attempts", calls)
			}
		})
	}
}
