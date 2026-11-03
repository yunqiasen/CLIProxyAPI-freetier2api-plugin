package core

import "context"

const ModelProbeHeader = "X-CPA-FreeTier-Model-Probe"

type modelProbeContextKey struct{}

// WithModelProbe marks an explicit single-model diagnostic request.
func WithModelProbe(ctx context.Context) context.Context {
	return context.WithValue(ctx, modelProbeContextKey{}, true)
}

func IsModelProbe(ctx context.Context) bool {
	value, _ := ctx.Value(modelProbeContextKey{}).(bool)
	return value
}
