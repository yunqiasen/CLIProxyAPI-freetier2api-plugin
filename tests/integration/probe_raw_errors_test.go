package tests

import (
	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/vendors/autoclaw"
	"freetier2api-plugin/internal/vendors/cline"
	"freetier2api-plugin/internal/vendors/opencodezen"
	"freetier2api-plugin/internal/vendors/raccoon"
	"strings"
	"testing"
)

func TestProbeRetainsRawBodyAcrossAdapterErrorConversion(t *testing.T) {
	body := `{"message":"busy","detail":"` + strings.Repeat("x", 600) + `"}`
	cases := map[string]*core.Failure{"AutoClaw": autoclaw.Classify(403, 0, body), "Cline": cline.Classify(403, body), "OpenCode": opencodezen.Classify(403, body), "Raccoon": raccoon.Classify(403, body)}
	for name, original := range cases {
		t.Run(name, func(t *testing.T) {
			probe := core.ToProbeError(core.ToPluginError(original)).(*core.PluginError)
			if probe.Message != body || probe.HTTPStatus != 403 {
				t.Fatalf("raw response lost: %+v", probe)
			}
		})
	}
}
