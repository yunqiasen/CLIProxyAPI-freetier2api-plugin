package admin

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"freetier2api-plugin/cpasdk/pluginabi"
	"freetier2api-plugin/cpasdk/pluginapi"
	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge"
)

type modelProbeRequest struct {
	AuthIndex string `json:"auth_index"`
	VendorID  string `json:"vendor_id"`
	Model     string `json:"model"`
}

// handleModelProbe uses CPA's production executor, not a second vendor transport.
// The selected runtime AuthID pins the credential; proxy and alias resolution stay
// in the normal request path. No management key is relayed back over HTTP.
func (s *Service) handleModelProbe(req pluginapi.ManagementRequest) pluginapi.ManagementResponse {
	body, err := decodeRequest[modelProbeRequest](req.Body)
	fail := func(status int, message string) pluginapi.ManagementResponse {
		return JSONResponse(status, map[string]any{"ok": false, "status": status, "error": message})
	}
	if err != nil {
		return fail(400, "invalid request body")
	}
	body.AuthIndex = strings.TrimSpace(body.AuthIndex)
	body.VendorID = strings.TrimSpace(body.VendorID)
	body.Model = strings.TrimSpace(body.Model)
	if body.AuthIndex == "" || body.VendorID == "" || body.Model == "" {
		return fail(400, "auth_index, vendor_id and model are required")
	}
	ctx, cancel := hostbridge.ManagementContext(req, 90*time.Second)
	defer cancel()
	callback := hostbridge.RequestCallbackID(req)
	entries, err := hostbridge.ListAuths(ctx, callback, s.registry)
	if err != nil {
		return fail(502, err.Error())
	}
	var selected *core.HostAuth
	for i := range entries {
		if entries[i].AuthIndex == body.AuthIndex {
			selected = &entries[i]
			break
		}
	}
	if selected == nil {
		return fail(404, "credential not found")
	}
	if selected.Disabled {
		return fail(409, "credential is disabled")
	}
	stored, ok := hostbridge.AuthJSON(ctx, callback, selected.AuthIndex)
	if !ok {
		return fail(502, "credential lookup failed")
	}
	credential, ok := s.registry.ParseCredential(stored, selected.Name, nil)
	if !ok || credential.VendorIDValue() != body.VendorID {
		return fail(400, "credential does not belong to selected vendor")
	}
	vendor, ok := s.registry.ByID(body.VendorID)
	if !ok || !core.RealmEnabled(s.deps.Config().EnabledRealms, vendor.Region()) {
		return fail(409, "vendor is disabled")
	}
	manager := s.deps.Model()
	canonical := manager.ResolveOutboundModel(body.VendorID, body.Model)
	if manager.IsDisabled(body.VendorID, canonical) {
		return fail(409, "model is disabled")
	}
	catalog := manager.FilterForVendor(body.VendorID, manager.Registered(body.VendorID))
	// Extra models are independently registered by the production catalog.
	for _, extra := range manager.ExtraModels() {
		if extra.OwnedBy != body.VendorID {
			continue
		}
		view := manager.ApplyAliases(body.VendorID, []pluginapi.ModelInfo{extra})
		if extra.ID == canonical || (len(view) == 1 && view[0].ID == body.Model) {
			canonical = extra.ID
			catalog = []pluginapi.ModelInfo{extra}
			break
		}
	}
	selectedModel := -1
	for i, m := range catalog {
		if m.ID == canonical {
			selectedModel = i
			break
		}
	}
	if selectedModel < 0 {
		return fail(400, "model not found in vendor catalog; refresh models first")
	}
	if manager.IsDisabled(body.VendorID, canonical) {
		return fail(409, "model is disabled")
	}
	// Resolve tags against the full catalog, just like production registration.
	exposed := manager.ApplyAliases(body.VendorID, catalog)
	if selectedModel >= len(exposed) {
		return fail(409, "model not available")
	}
	runtimeID := core.FirstNonEmpty(selected.ID, selected.Name)
	if runtimeID == "" {
		return fail(409, "credential runtime identity missing")
	}
	payload, _ := json.Marshal(map[string]any{"model": exposed[selectedModel].ID, "stream": false, "max_tokens": 512, "messages": []map[string]string{{"role": "user", "content": "Reply with the single word: ok"}}})
	headers := make(http.Header)
	headers.Set(core.ModelProbeHeader, "1")
	started := time.Now()
	raw, err := hostbridge.CallCtx(ctx, pluginabi.MethodHostModelExecute, struct {
		pluginapi.HostModelExecutionRequest
		Path string `json:"path"`
	}{HostModelExecutionRequest: pluginapi.HostModelExecutionRequest{EntryProtocol: core.FormatChatCompletions, ExitProtocol: core.FormatChatCompletions, Model: exposed[selectedModel].ID, Body: payload, Headers: headers, AuthID: runtimeID, ForcedProvider: core.ProviderKey}, Path: "/v1/chat/completions"})
	result := map[string]any{"ok": false, "model": exposed[selectedModel].ID, "upstream_model": canonical, "vendor_id": body.VendorID, "auth_index": selected.AuthIndex, "latency_ms": time.Since(started).Milliseconds()}
	if err != nil {
		message := err.Error()
		status := hostbridge.ErrorStatus(err)
		var hostErr *hostbridge.CallError
		if errors.As(err, &hostErr) {
			message = hostErr.Message
		}
		result["status"] = status
		result["error"] = probeSafeText(message, stored)
		result["error_kind"] = "upstream"
		lower := strings.ToLower(message)
		if status >= 500 && (strings.Contains(lower, "timeout") || strings.Contains(lower, "deadline exceeded") || strings.Contains(lower, "tls") || strings.Contains(lower, "dial tcp") || strings.Contains(lower, ": eof") || strings.Contains(lower, "connection refused")) {
			result["error_kind"] = "network"
		}
		return JSONResponse(200, result)
	}
	var response pluginapi.HostModelExecutionResponse
	if json.Unmarshal(raw, &response) != nil || response.StatusCode == 0 {
		result["status"] = 502
		result["error"] = "invalid host model response"
		return JSONResponse(200, result)
	}
	result["status"] = response.StatusCode
	var completion struct {
		Choices []struct {
			Message struct {
				Content json.RawMessage `json:"content"`
			} `json:"message"`
			FinishReason string `json:"finish_reason"`
		} `json:"choices"`
		Usage json.RawMessage `json:"usage"`
		Error json.RawMessage `json:"error"`
	}
	if json.Unmarshal(response.Body, &completion) != nil || response.StatusCode < 200 || response.StatusCode >= 300 || (len(completion.Error) > 0 && string(completion.Error) != "null") {
		result["error"] = probeSafeText(string(response.Body), stored)
		result["error_kind"] = "upstream"
		return JSONResponse(200, result)
	}
	text := ""
	for _, choice := range completion.Choices {
		var value string
		if json.Unmarshal(choice.Message.Content, &value) == nil {
			text += value
		} else {
			var parts []struct {
				Type string `json:"type"`
				Text string `json:"text"`
			}
			if json.Unmarshal(choice.Message.Content, &parts) == nil {
				for _, p := range parts {
					if p.Type == "text" {
						text += p.Text
					}
				}
			}
		}
		if choice.FinishReason != "" {
			result["finish_reason"] = choice.FinishReason
		}
	}
	result["text"] = probeSafeText(text, stored)
	if len(completion.Usage) > 0 {
		result["usage"] = completion.Usage
	}
	result["ok"] = strings.TrimSpace(text) != ""
	if result["ok"] == false {
		result["error"] = "empty model response"
		result["error_kind"] = "response"
	}
	return JSONResponse(200, result)
}

// Display excerpts never contain credential values, even when an upstream echoes one.
func probeSafeText(text string, stored []byte) string {
	var record any
	if json.Unmarshal(stored, &record) == nil {
		var visit func(any)
		visit = func(value any) {
			switch v := value.(type) {
			case map[string]any:
				for k, item := range v {
					lower := strings.ToLower(k)
					if strings.Contains(lower, "token") || strings.Contains(lower, "secret") || strings.Contains(lower, "password") || strings.Contains(lower, "cookie") || strings.Contains(lower, "api_key") {
						if secret, ok := item.(string); ok && len(secret) > 3 {
							text = strings.ReplaceAll(text, secret, "[redacted]")
						}
					}
					visit(item)
				}
			case []any:
				for _, item := range v {
					visit(item)
				}
			}
		}
		visit(record)
	}
	runes := []rune(text)
	if len(runes) > 4096 {
		return string(runes[:4096]) + "\n…（已截断）"
	}
	return text
}
