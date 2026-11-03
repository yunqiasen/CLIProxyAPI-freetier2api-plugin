package hostbridge

import (
	"context"
	"strings"
)

type authIndexContextKey struct{}

// WithAuthIndex pins background HTTP calls to one host credential's egress policy.
// It carries no token or proxy URL; the host validates plugin ownership.
func WithAuthIndex(ctx context.Context, authIndex string) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, authIndexContextKey{}, strings.TrimSpace(authIndex))
}

func AuthIndex(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	value, _ := ctx.Value(authIndexContextKey{}).(string)
	return value
}
