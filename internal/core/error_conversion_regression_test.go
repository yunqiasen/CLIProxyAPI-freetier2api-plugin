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
