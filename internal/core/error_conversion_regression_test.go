package core

import (
	"errors"
	"fmt"
	"testing"
)

func TestToPluginErrorIsIdempotent(t *testing.T) {
	original := &PluginError{Code: "upstream_soft_rate", Message: "raw quota detail", HTTPStatus: 429, Retryable: true, RetryAfterSeconds: 13, RequestStop: true}
	for _, input := range []error{original, fmt.Errorf("adapter: %w", original)} {
		got, ok := ToPluginError(input).(*PluginError)
		if !ok || got != original {
			t.Fatalf("structured contract changed: %#v", got)
		}
	}
	raw := errors.New(`host.http.do: host_call_failed: Too Many Requests`)
	first := ToPluginError(raw).(*PluginError)
	second := ToPluginError(first).(*PluginError)
	if first != second {
		t.Fatalf("double conversion: first=%+v second=%+v", first, second)
	}
}

func TestToProbeErrorPreservesBridgeEvidenceThroughConversion(t *testing.T) {
	for _, raw := range []string{
		`host.http.do: host_call_failed: HTTP 429 Too Many Requests; upstream request_id=REQ_FIXTURE; {"code":"daily_limit","message":"quota detail"}`,
		`Post "https://fixture.invalid/chat": host.http.do_stream: host_call_failed: EOF`,
	} {
		original := errors.New(raw)
		converted := ToPluginError(ToPluginError(original))
		probe := ToProbeError(fmt.Errorf("executor: %w", converted)).(*PluginError)
		if probe.Message != raw {
			t.Errorf("lost upstream evidence: got %q want %q", probe.Message, raw)
		}
		wantStatus := 429
		if raw[len(raw)-3:] == "EOF" {
			wantStatus = 502
		}
		if probe.HTTPStatus != wantStatus || !probe.RequestStop || probe.Retryable {
			t.Errorf("wrong probe contract: %+v", probe)
		}
	}
}
