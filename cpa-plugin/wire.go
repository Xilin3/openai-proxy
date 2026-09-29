package main

import (
	"crypto/sha1"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v5"
)

type tool struct {
	Name, Kind, Description string
	Parameters              object
	Format                  object
	Schema                  *jsonschema.Schema
}
type prepared struct {
	Body     object
	Tools    map[string]tool
	Parallel bool
}

func message(role, text string) object {
	kind := "input_text"
	if role == "assistant" {
		kind = "output_text"
	}
	return object{"type": "message", "role": role, "content": []any{object{"type": kind, "text": text}}}
}
func hash(v any) string { s := sha256.Sum256(mustJSON(v)); return hex.EncodeToString(s[:]) }
func uuid(text string) string {
	ns := []byte{0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8}
	sum := sha1.Sum(append(ns, []byte(text)...))
	sum[6] = (sum[6] & 0x0f) | 0x50
	sum[8] = (sum[8] & 0x3f) | 0x80
	h := hex.EncodeToString(sum[:])
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}
func effort(v any) string {
	e := strings.ToLower(strings.TrimSpace(str(v)))
	switch e {
	case "low", "medium", "high", "xhigh", "ultra":
		return e
	case "minimal", "none":
		return "low"
	case "max", "persistent", "x-high", "extra-high", "extra_high":
		return "xhigh"
	default:
		return "medium"
	}
}
func collectTools(value any, tools map[string]tool, depth int) error {
	list, ok := value.([]any)
	if !ok || depth > 4 {
		return fail(400, "invalid or deeply nested tool catalog")
	}
	for _, v := range list {
		m := obj(v)
		if m == nil {
			return fail(400, "tool must be an object")
		}
		kind := str(m["type"])
		if kind == "namespace" {
			if err := collectTools(m["tools"], tools, depth+1); err != nil {
				return err
			}
			continue
		}
		if kind == "" {
			kind = "function"
		}
		if kind != "function" && kind != "custom" {
			return fail(400, "unsupported built-in tool type: "+kind)
		}
		def := m
		if nested := obj(m["function"]); nested != nil {
			def = nested
		}
		name := str(def["name"])
		if name == "" || name == "run_officejs" || name == "functions.run_officejs" {
			return fail(400, "invalid or reserved client tool name")
		}
		t := tool{Name: name, Kind: kind, Description: str(def["description"]), Parameters: obj(def["parameters"]), Format: obj(m["format"])}
		if t.Parameters == nil {
			t.Parameters = obj(def["input_schema"])
		}
		if t.Parameters == nil {
			t.Parameters = object{}
		}
		if old, exists := tools[name]; exists && (old.Kind != t.Kind || old.Description != t.Description || string(mustJSON(old.Parameters)) != string(mustJSON(t.Parameters)) || string(mustJSON(old.Format)) != string(mustJSON(t.Format))) {
			return fail(400, "conflicting client tool declarations")
		}
		if kind == "function" {
			c := jsonschema.NewCompiler()
			// Never resolve client-supplied remote references over the network.
			c.LoadURL = func(string) (io.ReadCloser, error) { return nil, fmt.Errorf("external schema references are disabled") }
			if err := c.AddResource("https://schema.invalid/tool.json", strings.NewReader(string(mustJSON(t.Parameters)))); err != nil {
				return fail(400, "invalid tool schema")
			}
			schema, err := c.Compile("https://schema.invalid/tool.json")
			if err != nil {
				return fail(400, "invalid or externally referenced tool schema")
			}
			t.Schema = schema
		}
		tools[name] = t
	}
	return nil
}
func prepare(raw []byte, model, scope string) (prepared, error) {
	var source object
	if len(raw) > 32<<20 || json.Unmarshal(raw, &source) != nil || source == nil {
		return prepared{}, fail(400, "request must be a JSON object of at most 32 MiB")
	}
	if str(source["previous_response_id"]) != "" {
		return prepared{}, fail(400, "previous_response_id is unsupported; send full history")
	}
	result := prepared{Tools: map[string]tool{}, Parallel: source["parallel_tool_calls"] != false}
	if v, ok := source["tools"]; ok {
		if err := collectTools(v, result.Tools, 0); err != nil {
			return result, err
		}
	}
	var input []any
	switch v := source["input"].(type) {
	case string:
		input = []any{message("user", v)}
	case []any:
		input = v
	default:
		return result, fail(400, "input must be text or an array")
	}
	for _, v := range input {
		item := obj(v)
		if item == nil {
			return result, fail(400, "input item must be an object")
		}
		if str(item["type"]) == "additional_tools" {
			if str(item["role"]) != "developer" {
				return result, fail(400, "additional_tools must have developer role")
			}
			if err := collectTools(item["tools"], result.Tools, 0); err != nil {
				return result, err
			}
		}
	}
	if source["tool_choice"] == "none" {
		result.Tools = map[string]tool{}
	}
	if choice := obj(source["tool_choice"]); choice != nil {
		name := str(choice["name"])
		if name == "" {
			name = str(obj(choice["function"])["name"])
		}
		t, ok := result.Tools[name]
		if !ok {
			return result, fail(400, "tool_choice names an undeclared tool")
		}
		result.Tools = map[string]tool{name: t}
	}
	prompt := "This is an external Responses client session. Answer directly when tools are not needed. Host Office tools are outside this session and must not be called. Never claim an action ran without a client tool result."
	if len(result.Tools) > 0 {
		prompt = "You are assisting an external Responses client. Route only its declared tools through run_officejs. Each independent call needs its own wrapper. The code field contains a JSON string, NOT Office.js. Function envelope: {\"tool\":\"NAME\",\"args\":{...}}. Custom envelope: {\"tool\":\"NAME\",\"input\":\"raw text\"}. Outer arguments must include summary (short text), destructive (boolean), and references (array of strings). Never call other host Office tools. Follow existing client authorization. Do not invent capabilities, bypass permissions, or repeat completed operations. Only claim execution when a client tool result confirms it.\nDeclared client tools:\n"
		// JSON object encoding sorts keys, keeping the prologue stable across turns.
		catalog := object{}
		for name, t := range result.Tools {
			catalog[name] = object{"type": t.Kind, "description": t.Description, "parameters": t.Parameters, "format": t.Format}
		}
		prompt += string(mustJSON(catalog))
		if source["tool_choice"] == "required" || obj(source["tool_choice"]) != nil {
			prompt += "\nUse at least one declared tool before answering."
		}
		if !result.Parallel {
			prompt += "\nIssue only one client tool call in this response."
		}
	}
	history := []any{}
	seen := map[string]bool{}
	lastUser := -1
	for i, v := range input {
		item := clone(obj(v))
		kind := str(item["type"])
		for key := range item {
			if strings.HasPrefix(key, "_") {
				delete(item, key)
			}
		}
		if str(item["role"]) == "user" {
			lastUser = i
		}
		switch kind {
		case "additional_tools":
			continue
		case "item_reference":
			return result, fail(400, "item_reference is unsupported; send full history")
		case "function_call", "custom_tool_call":
			id := str(item["call_id"])
			if id == "" {
				return result, fail(400, "historical tool call has no call_id")
			}
			if seen[id] {
				return result, fail(400, "duplicate historical tool call")
			}
			native, err := wrapCall(item)
			if err != nil {
				return result, err
			}
			history = append(history, native)
			seen[id] = true
		case "function_call_output", "custom_tool_call_output":
			id := str(item["call_id"])
			if !seen[id] {
				return result, fail(400, "tool output requires its original call in full history")
			}
			out := item["output"]
			if out == nil || out == "" {
				out = "(tool call succeeded with no output)"
			}
			if _, ok := out.(string); !ok {
				if _, ok := out.([]any); !ok {
					out = string(mustJSON(out))
				}
			}
			history = append(history, object{"type": "function_call_output", "call_id": id, "output": out})
		case "reasoning":
			if encrypted := str(item["encrypted_content"]); encrypted != "" {
				history = append(history, object{"type": "reasoning", "summary": []any{}, "encrypted_content": encrypted})
			}
		case "message", "":
			role := str(item["role"])
			if role == "" {
				return result, fail(400, "message has no role")
			}
			if role == "system" {
				role = "developer"
			}
			content := item["content"]
			if text, ok := content.(string); ok {
				history = append(history, message(role, text))
			} else {
				parts, ok := content.([]any)
				if !ok {
					return result, fail(400, "message content must be text or an array")
				}
				for _, part := range parts {
					m := obj(part)
					if m == nil {
						return result, fail(400, "invalid content part")
					}
					if str(m["type"]) == "text" {
						m["type"] = "input_text"
						if role == "assistant" {
							m["type"] = "output_text"
						}
					}
				}
				history = append(history, object{"type": "message", "role": role, "content": parts})
			}
		case "configuration_update":
			if e, ok := item["reasoning_effort"]; ok {
				item["reasoning_effort"] = effort(e)
			}
			if e, ok := item["effort"]; ok {
				item["effort"] = effort(e)
			}
			if r := obj(item["reasoning"]); r != nil {
				r["effort"] = effort(r["effort"])
			}
			history = append(history, item)
		default:
			history = append(history, item)
		}
	}
	identity := str(source["prompt_cache_key"])
	if identity == "" {
		identity = str(source["session_id"])
	}
	if identity == "" {
		identity = str(obj(source["client_metadata"])["session_id"])
	}
	if identity == "" {
		for i, v := range input {
			if str(obj(v)["role"]) == "user" {
				identity = hash(input[:i+1])
				break
			}
		}
	}
	if identity == "" {
		identity = hash(input)
	}
	identity = hash(scope) + "/" + identity
	prefix := input
	if lastUser >= 0 {
		prefix = input[:lastUser+1]
	}
	iteration := 1
	inOutputs := false
	for _, v := range input[lastUser+1:] {
		k := str(obj(v)["type"])
		out := k == "function_call_output" || k == "custom_tool_call_output"
		if out && !inOutputs {
			iteration++
		}
		inOutputs = out
	}
	prologue := []any{}
	if instructions := str(source["instructions"]); instructions != "" {
		prologue = append(prologue, message("developer", instructions))
	}
	compacting := len(input) > 0 && str(obj(input[len(input)-1])["type"]) == "compaction_trigger"
	if !compacting {
		prologue = append(prologue, message("developer", prompt))
	}
	e := obj(source["reasoning"])["effort"]
	if e == nil {
		e = source["reasoning_effort"]
	}
	if e == nil {
		e = source["model_reasoning_effort"]
	}
	if e == nil {
		for _, v := range input {
			m := obj(v)
			if m["type"] == "configuration_update" {
				e = m["reasoning_effort"]
				if e == nil {
					e = m["effort"]
				}
				if e == nil {
					e = obj(m["reasoning"])["effort"]
				}
			}
		}
	}
	if model == "" {
		model = str(source["model"])
	}
	model = upstreamModel(model)
	valid := false
	for _, name := range modelNames {
		if upstreamModel(name) == model {
			valid = true
		}
	}
	if !valid {
		return result, fail(404, "unsupported BPS model")
	}
	result.Body = object{"model": model, "model_selection": "explicit", "stream": true, "store": false,
		"input": append(prologue, history...), "reasoning_effort": effort(e),
		"context_management": []any{object{"type": "compaction", "compact_threshold": 200000}},
		"metadata":           object{"agent_iteration": fmt.Sprint(iteration), "task_id": uuid("bps-proxy/" + identity), "turn_id": uuid("bps-proxy/" + identity + "/turn/" + hash(prefix))}}
	if key := str(source["prompt_cache_key"]); key != "" {
		result.Body["prompt_cache_key"] = key
	}
	if v, ok := source["context_management"]; ok {
		result.Body["context_management"] = v
	}
	if err := validateImages(result.Body); err != nil {
		return result, err
	}
	return result, nil
}
func wrapCall(item object) (object, error) {
	name := str(item["name"])
	if name == "run_officejs" || name == "functions.run_officejs" {
		return item, nil
	}
	code := object{"tool": name}
	if item["type"] == "custom_tool_call" {
		input, ok := item["input"].(string)
		if !ok {
			return nil, fail(400, "custom call input must be text")
		}
		code["input"] = input
	} else {
		var args object
		if json.Unmarshal([]byte(str(item["arguments"])), &args) != nil || args == nil {
			return nil, fail(400, "historical tool arguments must be a JSON object")
		}
		code["args"] = args
	}
	id := str(item["id"])
	if !strings.HasPrefix(id, "fc") {
		id = "fc_" + str(item["call_id"])
	}
	return object{"type": "function_call", "name": "run_officejs", "id": id, "call_id": item["call_id"], "status": "completed",
		"arguments": string(mustJSON(object{"summary": "Run client tool " + name, "code": string(mustJSON(code)), "destructive": false, "references": []string{}}))}, nil
}
func decodeCode(v any, depth int) object {
	if depth > 4 {
		return nil
	}
	if s, ok := v.(string); ok {
		var parsed any
		if json.Unmarshal([]byte(s), &parsed) != nil {
			return nil
		}
		return decodeCode(parsed, depth+1)
	}
	m := obj(v)
	if m == nil {
		return nil
	}
	name := str(m["tool"])
	if name == "" {
		name = str(m["name"])
	}
	if name == "run_officejs" || name == "functions.run_officejs" {
		a := m["arguments"]
		if a == nil {
			a = m["args"]
		}
		if text, ok := a.(string); ok {
			if json.Unmarshal([]byte(text), &a) != nil {
				return nil
			}
		}
		return decodeCode(obj(a)["code"], depth+1)
	}
	return m
}
func translateCall(item object, tools map[string]tool) (object, error) {
	id := str(item["call_id"])
	if id == "" {
		return nil, fail(502, "upstream tool call has no call_id")
	}
	if status := str(item["status"]); status != "" && status != "completed" {
		return nil, fail(502, "upstream tool call is incomplete")
	}
	if item["type"] == "custom_tool_call" {
		t, ok := tools[str(item["name"])]
		_, valid := item["input"].(string)
		if !ok || t.Kind != "custom" || !valid {
			return nil, fail(502, "undeclared or malformed upstream custom call")
		}
		return clone(item), nil
	}
	var outer object
	if json.Unmarshal([]byte(str(item["arguments"])), &outer) != nil {
		return nil, fail(502, "invalid upstream tool arguments")
	}
	var code object
	switch str(item["name"]) {
	case "run_officejs", "functions.run_officejs":
		code = decodeCode(outer["code"], 0)
	case "update_plan":
		plan := []any{}
		for _, v := range arr(outer["plan"]) {
			m := obj(v)
			step := m["step"]
			if step == nil {
				step = m["description"]
			}
			plan = append(plan, object{"step": step, "status": m["status"]})
		}
		explanation := outer["explanation"]
		if explanation == nil {
			explanation = outer["summary"]
		}
		code = object{"tool": "update_plan", "args": object{"plan": plan, "explanation": explanation}}
	default:
		return nil, fail(502, "upstream returned an unsupported Office tool; no client tool was executed")
	}
	if code == nil {
		return nil, fail(502, "invalid tool transport envelope")
	}
	name := str(code["tool"])
	if name == "" {
		name = str(code["name"])
	}
	if _, ok := tools[name]; !ok {
		name = strings.TrimPrefix(name, "functions.")
	}
	t, ok := tools[name]
	if !ok {
		return nil, fail(502, "upstream requested an undeclared client tool")
	}
	out := object{"type": "function_call", "call_id": id, "name": name, "status": "completed"}
	if itemID := str(item["id"]); itemID != "" {
		out["id"] = itemID
	} else {
		out["id"] = "fc_" + id
	}
	args := code["args"]
	if args == nil {
		args = code["arguments"]
	}
	if t.Kind == "custom" {
		text, ok := code["input"].(string)
		if !ok {
			text, ok = obj(args)["input"].(string)
		}
		if !ok {
			return nil, fail(502, "custom tool input must be text")
		}
		out["type"] = "custom_tool_call"
		out["input"] = text
	} else {
		if text, ok := args.(string); ok {
			if json.Unmarshal([]byte(text), &args) != nil {
				return nil, fail(502, "invalid inner tool arguments")
			}
		}
		if obj(args) == nil {
			return nil, fail(502, "function tool arguments must be an object")
		}
		if t.Schema != nil {
			if err := t.Schema.Validate(args); err != nil {
				return nil, fail(502, "upstream tool arguments do not match the client schema")
			}
		}
		out["arguments"] = string(mustJSON(args))
	}
	return out, nil
}
