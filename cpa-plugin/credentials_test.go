package main

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

type authHost struct {
	entries []authEntry
	files   map[string]json.RawMessage
	runtime map[string]authEntry
	read    []string
}

func (h *authHost) call(method string, input any, out any) error {
	index := str(obj(input)["auth_index"])
	switch method {
	case "host.auth.list":
		return assign(out, object{"files": h.entries})
	case "host.auth.get_runtime":
		for _, entry := range h.entries {
			if entry.Index == index {
				if changed, ok := h.runtime[index]; ok {
					entry = changed
				}
				return assign(out, object{"auth": entry})
			}
		}
		return fail(404, "removed")
	case "host.auth.get":
		h.read = append(h.read, index)
		data, ok := h.files[index]
		if !ok {
			return fail(404, "removed")
		}
		return assign(out, object{"json": data})
	default:
		return fail(500, "unexpected auth mutation or inference callback")
	}
}
func activeAuth(index string, priority int) authEntry {
	return authEntry{Index: index, Provider: "codex", Status: "active", Priority: priority}
}
func TestReusesNativeCredentialAndObservesRefresh(t *testing.T) {
	h := &authHost{entries: []authEntry{activeAuth("existing", 0)}, files: map[string]json.RawMessage{"existing": credential(time.Now().Unix() + 3600)}}
	p := newPlugin(h.call)
	first, id, err := p.selectSession("callback")
	if err != nil || id != "existing" {
		t.Fatal(id, err)
	}
	// Simulates CPA persisting a fresh token to the same native auth file.
	h.files["existing"] = credential(time.Now().Unix() + 7200)
	second, id, err := p.selectSession("callback")
	if err != nil || first.token == second.token || id != "existing" {
		t.Fatal("stale credential retained", err)
	}
	if len(h.read) != 2 {
		t.Fatal("credential was not read on every request")
	}
}
func TestCredentialFilteringAndRuntimeRecheck(t *testing.T) {
	good := activeAuth("good", 0)
	disabled := activeAuth("disabled", 100)
	disabled.Disabled = true
	other := activeAuth("other", 100)
	other.Provider = "claude"
	memory := activeAuth("memory", 100)
	memory.RuntimeOnly = true
	cooling := activeAuth("cooling", 100)
	cooling.NextRetryAfter = time.Now().Add(time.Hour)
	unavailable := activeAuth("unavailable", 100)
	unavailable.Unavailable = true
	stale := activeAuth("stale", 100)
	revoked := stale
	revoked.Disabled = true
	h := &authHost{entries: []authEntry{disabled, other, memory, cooling, unavailable, stale, good},
		runtime: map[string]authEntry{"stale": revoked}, files: map[string]json.RawMessage{"good": credential(time.Now().Unix() + 3600)}}
	_, id, err := newPlugin(h.call).selectSession("callback")
	if err != nil || id != "good" || len(h.read) != 1 || h.read[0] != "good" {
		t.Fatal(id, h.read, err)
	}
}
func TestCredentialPriorityAndRotation(t *testing.T) {
	h := &authHost{entries: []authEntry{activeAuth("low", 0), activeAuth("a", 10), activeAuth("b", 10)}, files: map[string]json.RawMessage{}}
	for _, entry := range h.entries {
		h.files[entry.Index] = credential(time.Now().Unix() + 3600)
	}
	p := newPlugin(h.call)
	for _, want := range []string{"a", "b", "a", "b"} {
		_, id, err := p.selectSession("callback")
		if err != nil || id != want {
			t.Fatal(id, want, err)
		}
	}
}
func TestRejectsInvalidSharedCredentials(t *testing.T) {
	for _, kind := range []string{"expired", "api-key", "old-plugin", "disabled-file", "proxy"} {
		t.Run(kind, func(t *testing.T) {
			raw := credential(time.Now().Unix() + 3600)
			if kind == "expired" {
				raw = credential(1)
			}
			var data object
			_ = json.Unmarshal(raw, &data)
			switch kind {
			case "api-key":
				data = object{"type": "codex", "api_key": "not-an-oauth-token"}
			case "old-plugin":
				data["type"] = provider
			case "disabled-file":
				data["disabled"] = true
			case "proxy":
				data["proxy_url"] = "http://proxy.invalid:8080"
			}
			h := &authHost{entries: []authEntry{activeAuth("a", 0)}, files: map[string]json.RawMessage{"a": mustJSON(data)}}
			if _, _, err := newPlugin(h.call).selectSession("callback"); err == nil {
				t.Fatal("invalid shared credential accepted")
			}
		})
	}
}
func TestSupportsCPAFlatCredential(t *testing.T) {
	var nested object
	_ = json.Unmarshal(credential(time.Now().Unix()+3600), &nested)
	flat := obj(nested["tokens"])
	flat["type"] = "codex"
	h := &authHost{entries: []authEntry{activeAuth("a", 0)}, files: map[string]json.RawMessage{"a": mustJSON(flat)}}
	if _, _, err := newPlugin(h.call).selectSession("callback"); err != nil {
		t.Fatal(err)
	}
}
func TestGlobalRoutingAndPlainModelNames(t *testing.T) {
	p := newPlugin(nil)
	if str(obj(registration()["metadata"])["GitHubRepository"]) == "" {
		t.Fatal("CPA requires repository metadata")
	}
	caps := obj(registration()["capabilities"])
	if caps["auth_provider"] == true || caps["executor_model_scope"] != "static" || caps["model_router"] != true {
		t.Fatal(caps)
	}
	for _, name := range []string{"gpt-6-astra", "gpt-5.6-sol", "other-provider-model"} {
		resp, err := p.handle("model.route", mustJSON(object{"RequestedModel": name, "SourceFormat": "openai-response"}))
		if err != nil || resp.(object)["Handled"] != true || resp.(object)["TargetKind"] != "self" {
			t.Fatal(resp, err)
		}
	}
	for _, v := range arr(models()["Models"]) {
		if strings.HasSuffix(str(obj(v)["ID"]), "-excel") {
			t.Fatal("renamed model")
		}
	}
}
func TestNativeFallbackGuard(t *testing.T) {
	allowed := object{"SourceFormat": "openai", "ToFormat": "openai-response"}
	if out := interceptRequest("request.intercept_after", mustJSON(allowed)); out["Terminate"] == true {
		t.Fatal(out)
	}
	for _, input := range []object{
		{"SourceFormat": "openai-response", "ToFormat": "codex", "Metadata": object{"selected_auth_id": "native-account"}},
		{"SourceFormat": "openai-response", "ToFormat": "openai-response", "Metadata": object{"selected_auth_id": "native-account"}},
		{"SourceFormat": "unsupported"},
	} {
		if out := interceptRequest("request.intercept_after", mustJSON(input)); out["Terminate"] != true {
			t.Fatal("native fallthrough permitted", input)
		}
	}
	if out := interceptRequest("request.intercept_after", mustJSON(object{"SourceFormat": "openai", "ToFormat": "codex"})); out["Terminate"] == true {
		t.Fatal("blocked plugin Codex-format translation", out)
	}
}
func TestLegacySuffixIsNotPublishedOrSilentlyMapped(t *testing.T) {
	if _, err := prepare(mustJSON(sourceWithTools()), "gpt-6-sol-excel", "scope"); err == nil {
		t.Fatal("legacy suffix accepted")
	}
}
