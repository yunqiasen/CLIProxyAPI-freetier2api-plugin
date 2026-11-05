package admin

import "testing"

// TestIsRetryableClaimError 领取状态的限流/5xx/超时必须判为可恢复，
// 而鉴权、账号封禁、参数非法这类确定失败不能——保留旧状态会掩盖真问题。
func TestIsRetryableClaimError(t *testing.T) {
	retryable := []string{
		"upstream soft_rate [429]: 上游限流",
		"upstream 5xx: 上游故障",
		"timeout waiting for upstream",
		"请求上游超时",
		"upstream 502 bad gateway",
	}
	for _, msg := range retryable {
		if !isRetryableClaimError(msg) {
			t.Fatalf("应判可恢复：%q", msg)
		}
	}
	definitive := []string{
		"upstream 401: 凭证失效",
		"upstream 403: 账号被封",
		"参数非法：max_tokens 越界",
	}
	for _, msg := range definitive {
		if isRetryableClaimError(msg) {
			t.Fatalf("不该判可恢复：%q", msg)
		}
	}
}
