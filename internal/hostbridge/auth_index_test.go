package hostbridge

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
)

func TestHTTPClientCarriesSelectedAuthIndex(t *testing.T) {
	for _, stream := range []bool{false, true} {
		t.Run(map[bool]string{false: "do", true: "stream"}[stream], func(t *testing.T) {
			var got string
			Configure(func(_, method string, payload any) (json.RawMessage, error) {
				if method == "host.http.do" || method == "host.http.do_stream" {
					raw, _ := json.Marshal(payload)
					var body map[string]any
					_ = json.Unmarshal(raw, &body)
					got, _ = body["auth_index"].(string)
					if stream {
						return json.RawMessage(`{"status_code":200,"stream_id":"test"}`), nil
					}
					return json.RawMessage(`{"StatusCode":200,"Body":"b2s="}`), nil
				}
				return json.RawMessage(`{}`), nil
			})
			t.Cleanup(func() { Configure(nil) })
			ctx := WithAuthIndex(context.Background(), "selected-account")
			client := Client(ctx, 0)
			if stream {
				client = StreamClient(ctx, 0)
			}
			req, _ := http.NewRequestWithContext(ctx, http.MethodGet, "https://fixture.invalid/catalog", nil)
			response, err := client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			_ = response.Body.Close()
			if got != "selected-account" {
				t.Fatalf("auth_index=%q, want selected-account", got)
			}
		})
	}
}
