package autoclaw

import (
	"context"
	"encoding/json"
	"testing"

	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge"
)

func TestStaticCatalogCarriesProbeCredentialAuthIndex(t *testing.T) {
	var got string
	hostbridge.Configure(func(_, method string, payload any) (json.RawMessage, error) {
		if method != "host.http.do" {
			return json.RawMessage(`{}`), nil
		}
		raw, _ := json.Marshal(payload)
		var request map[string]any
		_ = json.Unmarshal(raw, &request)
		got, _ = request["auth_index"].(string)
		return json.Marshal(map[string]any{"StatusCode": 200, "Body": []byte(modelConfigCNFixture)})
	})
	t.Cleanup(func() { hostbridge.Configure(nil) })
	registry := core.NewRegistry()
	vendor := Instances(core.Env{Registry: registry, Host: cnCatalogHost{}})[0]
	if err := registry.Register(vendor); err != nil {
		t.Fatal(err)
	}
	models, err := vendor.StaticModels(context.Background())
	if err != nil || len(models) == 0 {
		t.Fatalf("models=%v err=%v", models, err)
	}
	if got != "test" {
		t.Fatalf("probe auth_index=%q, want test", got)
	}
}
