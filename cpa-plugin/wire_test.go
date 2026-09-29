package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"strings"
	"testing"
	"time"
)

func sourceWithTools() object {
	return object{"model": "gpt-6-sol-excel", "input": "hello", "reasoning": object{"effort": "max"},
		"tools": []any{object{"type": "function", "name": "echo", "parameters": object{"type": "object", "properties": object{"text": object{"type": "string"}}, "required": []string{"text"}, "additionalProperties": false}},
			object{"type": "custom", "name": "patch", "description": "Apply a patch"}}}
}
func nativeCall(id, name string, args any) object {
	return object{"type": "function_call", "id": "fc_" + id, "call_id": id, "name": "run_officejs", "status": "completed",
		"arguments": string(mustJSON(object{"code": string(mustJSON(object{"tool": name, "args": args}))}))}
}
func event(kind string, fields object) string {
	fields = clone(fields)
	fields["type"] = kind
	return "event: " + kind + "\ndata: " + string(mustJSON(fields)) + "\n\n"
}
func completed(output ...any) string {
	return event("response.completed", object{"response": object{"id": "resp_test", "status": "completed", "output": output, "usage": object{"input_tokens": 10, "output_tokens": 2}}})
}
func credential(exp int64) []byte {
	claims := object{"exp": exp, "https://api.openai.com/auth": object{"chatgpt_account_id": "account-a", "chatgpt_account_user_id": "user-a"}}
	token := "header." + base64.RawURLEncoding.EncodeToString(mustJSON(claims)) + ".signature"
	return mustJSON(object{"type": provider, "tokens": object{"access_token": token, "account_id": "stale-account"}})
}
func TestPrepare(t *testing.T) {
	p, err := prepare(mustJSON(sourceWithTools()), "", "a")
	if err != nil {
		t.Fatal(err)
	}
	if p.Body["model"] != "gpt-5.6-sol" || p.Body["reasoning_effort"] != "xhigh" {
		t.Fatal(p.Body)
	}
	if _, ok := p.Body["tools"]; ok {
		t.Fatal("client tools must be conveyed only in the developer prologue")
	}
	if len(p.Tools) != 2 {
		t.Fatal(p.Tools)
	}
	p2, err := prepare(mustJSON(sourceWithTools()), "", "b")
	if err != nil {
		t.Fatal(err)
	}
	if obj(p.Body["metadata"])["task_id"] == obj(p2.Body["metadata"])["task_id"] {
		t.Fatal("account scope must affect task identity")
	}
}
func TestLiteTools(t *testing.T) {
	source := sourceWithTools()
	source["input"] = []any{object{"type": "additional_tools", "role": "developer", "tools": source["tools"]}, message("user", "hello")}
	delete(source, "tools")
	p, err := prepare(mustJSON(source), "", "scope")
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Tools) != 2 {
		t.Fatal(p.Tools)
	}
	source["input"].([]any)[0].(object)["role"] = "user"
	if _, err = prepare(mustJSON(source), "", "scope"); err == nil {
		t.Fatal("accepted untrusted tool declarations")
	}
}
func TestPrepareRejectsUnsupportedInput(t *testing.T) {
	for _, mutate := range []func(object){
		func(s object) { s["previous_response_id"] = "resp_old" },
		func(s object) { s["tools"] = []any{object{"type": "web_search"}} },
		func(s object) {
			s["tools"] = []any{object{"type": "function", "name": "remote", "parameters": object{"$ref": "https://evil.invalid/schema"}}}
		},
		func(s object) {
			s["input"] = []any{object{"type": "function_call_output", "call_id": "missing", "output": "done"}}
		},
		func(s object) { s["input"] = []any{object{"type": "item_reference", "id": "item"}} },
		func(s object) { s["tool_choice"] = object{"type": "function", "name": "missing"} },
	} {
		s := sourceWithTools()
		mutate(s)
		if _, err := prepare(mustJSON(s), "", "scope"); err == nil {
			t.Fatal("unsupported request accepted", s)
		}
	}
}
func TestHistoryReconstruction(t *testing.T) {
	s := sourceWithTools()
	s["input"] = []any{message("user", "hello"), object{"type": "custom_tool_call", "name": "patch", "call_id": "c1", "input": "patch text"}, object{"type": "custom_tool_call_output", "call_id": "c1", "output": "ok"}}
	p, err := prepare(mustJSON(s), "", "scope")
	if err != nil {
		t.Fatal(err)
	}
	input := arr(p.Body["input"])
	call := obj(input[len(input)-2])
	output := obj(input[len(input)-1])
	if call["name"] != "run_officejs" || output["type"] != "function_call_output" {
		t.Fatal(input)
	}
	if obj(p.Body["metadata"])["agent_iteration"] != "2" {
		t.Fatal(p.Body["metadata"])
	}
}
func TestTranslateCalls(t *testing.T) {
	p, err := prepare(mustJSON(sourceWithTools()), "", "scope")
	if err != nil {
		t.Fatal(err)
	}
	valid := nativeCall("c1", "echo", object{"text": "hi"})
	out, err := translateCall(valid, p.Tools)
	if err != nil || out["name"] != "echo" || out["arguments"] != `{"text":"hi"}` {
		t.Fatal(out, err)
	}
	for _, call := range []object{nativeCall("c1", "missing", object{}), nativeCall("c1", "echo", object{"text": 1}), nativeCall("c1", "echo", object{"text": "hi", "extra": true})} {
		if _, err := translateCall(call, p.Tools); err == nil {
			t.Fatal("accepted invalid tool", call)
		}
	}
	custom := nativeCall("c2", "patch", nil)
	custom["arguments"] = string(mustJSON(object{"code": string(mustJSON(object{"tool": "patch", "input": "patch text"}))}))
	out, err = translateCall(custom, p.Tools)
	if err != nil || out["type"] != "custom_tool_call" || out["input"] != "patch text" {
		t.Fatal(out, err)
	}
}
func TestStreamDefersCallsUntilCompleted(t *testing.T) {
	p, _ := prepare(mustJSON(sourceWithTools()), "", "scope")
	call := nativeCall("c1", "echo", object{"text": "hi"})
	text := message("assistant", "hello")
	stream := event("response.output_item.added", object{"output_index": 0, "item": call}) +
		event("response.function_call_arguments.delta", object{"output_index": 0, "delta": "secret native code"}) +
		event("response.output_item.added", object{"output_index": 1, "item": text}) +
		event("response.output_text.delta", object{"output_index": 1, "delta": "hello"}) + completed(call, text)
	var chunks [][]byte
	final, err := rewriteSSE(strings.NewReader(stream), 1<<20, p, func(b []byte) error { chunks = append(chunks, append([]byte{}, b...)); return nil })
	if err != nil {
		t.Fatal(err)
	}
	joined := string(bytes.Join(chunks, nil))
	if strings.Contains(joined, "run_officejs") || strings.Contains(joined, "secret native") {
		t.Fatal(joined)
	}
	if obj(arr(final["output"])[0])["type"] != "message" || obj(arr(final["output"])[1])["name"] != "echo" {
		t.Fatal(final)
	}
	if !bytes.Contains(chunks[1], []byte(`"output_index":0`)) {
		t.Fatal(string(chunks[1]))
	}
}
func TestStreamRejectsFailureBeforeReleasingCalls(t *testing.T) {
	p, _ := prepare(mustJSON(sourceWithTools()), "", "scope")
	call := nativeCall("c1", "echo", object{"text": "hi"})
	prefix := event("response.output_item.done", object{"output_index": 0, "item": call})
	cases := []string{
		prefix,
		prefix + event("response.failed", object{"response": object{"output": []any{call}}}),
		prefix + event("response.incomplete", object{"response": object{"output": []any{call}}}),
		prefix + completed(),
		prefix + completed(call, call),
		completed(call, nativeCall("bad", "not_allowed", object{})),
	}
	for _, stream := range cases {
		var emitted bytes.Buffer
		_, err := rewriteSSE(strings.NewReader(stream), 1<<20, p, func(b []byte) error { emitted.Write(b); return nil })
		if err == nil {
			t.Fatal("accepted invalid stream", stream)
		}
		if strings.Contains(emitted.String(), `"name":"echo"`) {
			t.Fatal("released a tool before validation")
		}
	}
	p.Parallel = false
	if _, err := rewriteSSE(strings.NewReader(completed(call, nativeCall("c2", "echo", object{"text": "two"}))), 1<<20, p, nil); err == nil {
		t.Fatal("accepted parallel calls")
	}
}
func TestSSELimitsAndMultiline(t *testing.T) {
	if err := readSSE(strings.NewReader("data: "+strings.Repeat("x", 1000)+"\n\n"), 128, func(string, object) (bool, error) { return false, nil }); err == nil {
		t.Fatal("line limit bypass")
	}
	if err := readSSE(strings.NewReader(strings.Repeat(":comment\n", 100)), 128, func(string, object) (bool, error) { return false, nil }); err == nil {
		t.Fatal("event limit bypass")
	}
	raw := "event: response.completed\r\ndata: {\"type\":\"response.completed\",\r\ndata: \"response\": {\"output\":[]}}\r\n\r\n"
	if err := readSSE(strings.NewReader(raw), 1<<20, func(k string, m object) (bool, error) { return k == "response.completed", nil }); err != nil {
		t.Fatal(err)
	}
}

func TestCompletionIndexMismatchDoesNotReleaseTools(t *testing.T) {
	p, _ := prepare(mustJSON(sourceWithTools()), "", "scope")
	stream := event("response.output_text.delta", object{"output_index": 5, "delta": "hello"}) +
		completed(nativeCall("c1", "echo", object{"text": "hi"}))
	var emitted bytes.Buffer
	_, err := rewriteSSE(strings.NewReader(stream), 1<<20, p, func(b []byte) error { emitted.Write(b); return nil })
	if err == nil || strings.Contains(emitted.String(), `"name":"echo"`) {
		t.Fatal("inconsistent completion released a tool", err)
	}
}
func TestAuthScopeAndExpiry(t *testing.T) {
	s, err := sessionFromJSON(credential(time.Now().Unix()+3600), true)
	if err != nil || s.account != "account-a" || s.user != "user-a" {
		t.Fatal(s.account, err)
	}
	if _, err = sessionFromJSON(credential(1), true); err == nil {
		t.Fatal("accepted expired token")
	}
	data := credential(time.Now().Unix() + 3600)
	var unrelated object
	_ = json.Unmarshal(data, &unrelated)
	unrelated["type"] = "codex"
	res, err := parseAuth(mustJSON(object{"Provider": "codex", "RawJSON": mustJSON(unrelated), "FileName": "codex.json"}))
	if err != nil || res.(object)["Handled"] != false {
		t.Fatal("claimed native codex auth")
	}
}
func tinyImage() string {
	var buf bytes.Buffer
	img := image.NewRGBA(image.Rect(0, 0, 2, 2))
	img.Set(0, 0, color.White)
	_ = png.Encode(&buf, img)
	return "data:image/png;base64," + base64.StdEncoding.EncodeToString(buf.Bytes())
}
func TestImages(t *testing.T) {
	s := sourceWithTools()
	s["input"] = []any{object{"role": "user", "content": []any{object{"type": "input_image", "image_url": tinyImage()}}}}
	if _, err := prepare(mustJSON(s), "", "scope"); err != nil {
		t.Fatal(err)
	}
	for _, url := range []string{"data:image/png;base64,AAAA", "https://example.invalid/image.png", strings.Replace(tinyImage(), "image/png", "image/jpeg", 1)} {
		s["input"] = []any{object{"role": "user", "content": []any{object{"type": "input_image", "image_url": url}}}}
		if _, err := prepare(mustJSON(s), "", "scope"); err == nil {
			t.Fatal("accepted bad image")
		}
	}
}
