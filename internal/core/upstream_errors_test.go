package core

import (
	"encoding/json"
	"fmt"
	"net/http"
	"testing"
)

func TestToPluginErrorPreservesWrappedMessageAndClassification(t *testing.T) {
	for _, kind := range []FailureKind{FailureKindCredential, FailureKindTransient} {
		upstream := &Failure{Kind: kind, Msg: "gateway rejected"}
		wrapped := fmt.Errorf("积分签到成功；福利领取失败: %w", upstream)
		converted, ok := ToPluginError(wrapped).(*PluginError)
		if !ok || converted.Message != wrapped.Error() || converted.HTTPStatus != upstream.HTTPStatus() || converted.Retryable != upstream.Retryable() {
			t.Fatalf("lost context or classification: %+v", converted)
		}
	}
}

func TestErrorMapping(t *testing.T) {
	cases := []struct {
		name       string
		err        *Failure
		wantStatus int
		wantRetry  bool
	}{
		{"credential", &Failure{Kind: FailureKindCredential}, http.StatusUnauthorized, false},
		{"rate", &Failure{Kind: FailureKindRateLimit}, http.StatusTooManyRequests, true},
		{"quota", &Failure{Kind: FailureKindHardCredit}, http.StatusPaymentRequired, true},
		{"bad params", &Failure{Kind: FailureKindBadParams}, http.StatusBadRequest, false},
		{"channel rejected", &Failure{Kind: FailureKindChannelRejected}, http.StatusBadRequest, false},
		{"status passthrough", &Failure{Kind: FailureKindClient, Status: http.StatusConflict}, http.StatusConflict, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.err.HTTPStatus(); got != tc.wantStatus {
				t.Fatalf("HTTPStatus() = %d, want %d", got, tc.wantStatus)
			}
			if got := tc.err.Retryable(); got != tc.wantRetry {
				t.Fatalf("Retryable() = %v, want %v", got, tc.wantRetry)
			}
		})
	}
}

func TestToPluginErrorPreservesAlreadyStructuredError(t *testing.T) {
	original := NewPluginError("upstream_client", `{"code":11158}`, http.StatusBadRequest)
	converted := ToPluginError(fmt.Errorf("executor: %w", original))
	coded, ok := converted.(*PluginError)
	if !ok || coded.HTTPStatus != http.StatusBadRequest {
		t.Fatalf("lost status: %#v", converted)
	}
}

func TestProbeErrorPreservesRawUpstreamDetail(t *testing.T) {
	upstream := &Failure{Kind: FailureKindClient, Status: 400, Msg: `{"code":11158,"msg":"is not allowed for user type"}`}
	converted := ToProbeError(upstream).(*PluginError)
	if converted.Message != upstream.Msg || converted.HTTPStatus != 400 {
		t.Fatalf("lost upstream detail: %#v", converted)
	}
}

func TestProbeErrorEnvelopeStopsAllHostRetries(t *testing.T) {
	for _, original := range []error{&Failure{Kind: FailureKindTransient, Status: 503, Msg: "server"}, fmt.Errorf("dial tcp: EOF")} {
		raw := ErrorEnvelopeFromError(ToProbeError(original))
		var env struct {
			Error struct {
				Stop      bool `json:"request_stop"`
				Retryable bool `json:"retryable"`
			} `json:"error"`
		}
		if err := json.Unmarshal(raw, &env); err != nil {
			t.Fatal(err)
		}
		if !env.Error.Stop || env.Error.Retryable {
			t.Fatalf("probe may replay: %s", raw)
		}
	}
}

func TestProbePreservesRawDetailThroughAdapterConversion(t *testing.T) {
	original := &Failure{Kind: FailureKindCredential, Status: 401, Msg: "classified", RawStatus: 403, RawBody: `{"code":810001,"message":"busy"}`}
	adapterError := ToPluginError(original)
	probe := ToProbeError(ToPluginError(adapterError)).(*PluginError)
	if probe.HTTPStatus != 403 || probe.Message != original.RawBody {
		t.Fatalf("adapter erased diagnostic evidence: %+v", probe)
	}
}
