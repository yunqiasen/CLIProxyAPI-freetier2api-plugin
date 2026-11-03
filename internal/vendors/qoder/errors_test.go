package qoder

import (
	"freetier2api-plugin/internal/core"
	"testing"
)

func TestProbeErrorKeepsQoderHTTPStatusAndBody(t *testing.T) {
	upstream := NewUpstreamError(403, `{"code":"112","message":"pricing"}`)
	converted := core.ToProbeError(upstream).(*core.PluginError)
	if converted.HTTPStatus != 403 || converted.Message != upstream.Detail {
		t.Fatalf("lost raw qoder response: %#v", converted)
	}
}
