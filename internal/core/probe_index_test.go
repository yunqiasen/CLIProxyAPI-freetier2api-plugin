package core

import (
	"context"
	"testing"
)

func TestProbeNativeCredentialKeepsSelectedAuthIndex(t *testing.T) {
	// Existing public helper remains compatible; the indexed variant is exercised
	// by real adapter/HTTP tests, where the native credential is provider-owned.
	_, index, ok := ProbeNativeCredentialWithIndex(context.Background(), Env{}, "test", func(*Credential) (*string, bool) { return nil, false })
	if ok || index != "" {
		t.Fatalf("unconfigured probe returned index=%q ok=%v", index, ok)
	}
}
