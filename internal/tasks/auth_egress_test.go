package tasks

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge"
)

type egressCheckinVendor struct{ fakeCheckinVendor }

func (v *egressCheckinVendor) Checkin(ctx context.Context, _ *core.Credential) (*core.CheckinResult, error) {
	response, err := hostbridge.Client(ctx, 0).Get("https://fixture.invalid/checkin")
	if err != nil {
		return nil, err
	}
	_ = response.Body.Close()
	return &core.CheckinResult{Already: true}, nil
}
func TestCheckinCarriesSelectedCredentialAuthIndex(t *testing.T) {
	var got string
	hostbridge.Configure(func(_, method string, payload any) (json.RawMessage, error) {
		if method == "host.http.do" {
			raw, _ := json.Marshal(payload)
			var body map[string]any
			_ = json.Unmarshal(raw, &body)
			got, _ = body["auth_index"].(string)
			return json.Marshal(map[string]any{"StatusCode": http.StatusOK})
		}
		return json.RawMessage(`{}`), nil
	})
	t.Cleanup(func() { hostbridge.Configure(nil) })
	service := &Service{deps: newFakeDeps(time.Now())}
	credential := &core.Credential{FileID: "fixture"}
	credential.SetAuthIndex("selected")
	if _, err := service.Checkin(context.Background(), &egressCheckinVendor{fakeCheckinVendor{id: "fixture"}}, credential, CheckinTriggerManual); err != nil {
		t.Fatal(err)
	}
	if got != "selected" {
		t.Fatalf("checkin auth_index=%q, want selected", got)
	}
}
