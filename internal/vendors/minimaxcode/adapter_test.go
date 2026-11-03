package minimaxcode

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"testing"

	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge"
)

func TestConfigureBaseURLPerInstance(t *testing.T) {
	vendors := Instances(core.Env{})
	cn := vendors[0].(*adapter)
	global := vendors[1].(*adapter)
	cn.Configure(core.Settings{}, map[string]string{
		"minimaxcode_cn_base_url":     "http://minimax-cn.test",
		"minimaxcode_global_base_url": "http://minimax-global.test",
	})
	global.Configure(core.Settings{}, map[string]string{
		"minimaxcode_cn_base_url":     "http://minimax-cn.test",
		"minimaxcode_global_base_url": "http://minimax-global.test",
	})
	if cn.baseURL != "http://minimax-cn.test" {
		t.Fatalf("cn baseURL = %q", cn.baseURL)
	}
	if global.baseURL != "http://minimax-global.test" {
		t.Fatalf("global baseURL = %q", global.baseURL)
	}
}

// CN fixture follows the November 2 lab capture: status=2 is claimable,
// claim_result=1 awards exactly points. Each region must use its own host.
func TestAdapterCheckinClaimsForBothRegions(t *testing.T) {
	for _, tc := range []struct {
		region Region
		host   string
	}{
		{RegionCN, "agent.minimaxi.com"},
		{RegionGlobal, "agent.minimax.io"},
	} {
		t.Run(string(tc.region), func(t *testing.T) {
			stubRegionalCheckin(t, tc.host, http.StatusOK,
				`{"base_resp":{"status_code":0},"data":{"scene":4,"days":[{"day_no":1,"points":400,"status":2,"is_today":true}]}}`,
				http.StatusOK, `{"base_resp":{"status_code":0},"data":{"claim_result":1,"points":400,"day_no":1}}`)
			var provider core.CheckinProvider
			for _, vendor := range Instances(core.Env{}) {
				if vendor.ID() == VendorIDFor(tc.region) {
					provider = vendor.(core.CheckinProvider)
				}
			}
			if provider == nil {
				t.Fatal("region has no checkin provider")
			}
			if !provider.SupportsCheckin() {
				t.Error("region must declare checkin capability")
			}
			result, err := provider.Checkin(context.Background(), &core.Credential{
				Native: &Credential{Region: tc.region, AccessToken: "fixture-token", AuthMode: "oauth"},
			})
			if err != nil {
				t.Fatalf("Checkin: %v", err)
			}
			if result == nil || result.Already || result.NotRecorded || result.Credit != 400 {
				t.Fatalf("result = %+v, want newly claimed 400 points", result)
			}
		})
	}
}

// Responses are injected at the external HTTP boundary; no real credentials or
// network are involved. A request to the wrong regional host is an error.
func stubRegionalCheckin(t *testing.T, host string, statusCode int, statusBody string, claimCode int, claimBody string) {
	t.Helper()
	hostbridge.Configure(func(_ string, _ string, payload any) (json.RawMessage, error) {
		raw, err := json.Marshal(payload)
		if err != nil {
			return nil, err
		}
		var req struct {
			Method string `json:"method"`
			URL    string `json:"url"`
		}
		if err := json.Unmarshal(raw, &req); err != nil {
			return nil, err
		}
		target, err := url.Parse(req.URL)
		if err != nil {
			return nil, err
		}
		if target.Host != host {
			return nil, fmt.Errorf("unexpected regional host %q", target.Host)
		}
		code, body := http.StatusOK, "{}"
		switch target.Path {
		case EpCheckinStatus:
			if req.Method != http.MethodGet {
				return nil, fmt.Errorf("unexpected status method %q", req.Method)
			}
			code, body = statusCode, statusBody
		case EpCheckinClaim:
			if req.Method != http.MethodPost {
				return nil, fmt.Errorf("unexpected claim method %q", req.Method)
			}
			code, body = claimCode, claimBody
		}
		return json.Marshal(map[string]any{
			"StatusCode": code,
			"Headers":    map[string][]string{"Content-Type": {"application/json"}},
			"Body":       []byte(body),
		})
	})
	t.Cleanup(func() { hostbridge.Configure(nil) })
}
