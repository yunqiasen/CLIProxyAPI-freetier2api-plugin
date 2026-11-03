package admin

import (
	"context"
	"encoding/json"
	"testing"

	"freetier2api-plugin/cpasdk/pluginapi"
	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge"
)

type egressQuotaVendor struct{ fakeVendor }

func (v *egressQuotaVendor) Quota(ctx context.Context, _ *core.Credential) (*pluginapi.QuotaFetchResponse, error) {
	response, err := hostbridge.Client(ctx, 0).Get("https://fixture.invalid/balance")
	if err != nil {
		return nil, err
	}
	_ = response.Body.Close()
	return &pluginapi.QuotaFetchResponse{}, nil
}
func TestQuotaUsesSelectedAuthIndex(t *testing.T) {
	var got string
	hostbridge.Configure(func(_, method string, payload any) (json.RawMessage, error) {
		raw, _ := json.Marshal(payload)
		var request map[string]any
		_ = json.Unmarshal(raw, &request)
		got, _ = request["auth_index"].(string)
		return json.RawMessage(`{"StatusCode":200}`), nil
	})
	t.Cleanup(func() { hostbridge.Configure(nil) })
	service := newService(t)
	_, err := service.queryQuota(context.Background(), quotaJob{entry: core.HostAuth{AuthIndex: "quota-selected"}, vendor: &egressQuotaVendor{}, credential: &core.Credential{}})
	if err != nil {
		t.Fatal(err)
	}
	if got != "quota-selected" {
		t.Fatalf("quota auth_index=%q", got)
	}
}
