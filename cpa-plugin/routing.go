package main

import (
	"encoding/json"
	"net/http"
)

// The host can decline a route when it has no format translator. Reject that
// path in the interceptor instead of letting inference silently use native CPA.
func interceptRequest(method string, raw []byte) object {
	var req struct {
		SourceFormat, ToFormat string
		Metadata               object
	}
	if json.Unmarshal(raw, &req) != nil {
		return terminate(400, "invalid inference interception request")
	}
	switch req.SourceFormat {
	case "openai-response", "openai", "claude", "gemini":
	default:
		return terminate(501, "this inference protocol is unsupported in Excel-only mode")
	}
	if method == "request.intercept_after" &&
		((req.ToFormat != "openai-response" && req.ToFormat != "codex") || str(req.Metadata["selected_auth_id"]) != "" || str(req.Metadata["selected_auth_index"]) != "") {
		return terminate(503, "Excel-only route unavailable; native-provider fallback is disabled")
	}
	return object{}
}

func terminate(status int, message string) object {
	return object{"Terminate": true, "StatusCode": status,
		"ResponseHeaders": http.Header{"Content-Type": []string{"application/json"}},
		"ResponseBody":    mustJSON(object{"error": object{"type": "invalid_request_error", "code": "excel_only", "message": message}})}
}
