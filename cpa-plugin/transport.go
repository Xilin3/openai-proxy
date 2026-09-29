package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

type upstreamStream struct {
	p             *Plugin
	callback      string
	downstream    string
	mu            sync.Mutex
	id, operation string
	closed        bool
	clientOnce    sync.Once
	timer         *time.Timer
	pending       []byte
}

func (p *Plugin) newStream(callback, downstream string, timeout time.Duration) (*upstreamStream, error) {
	s := &upstreamStream{p: p, callback: callback, downstream: downstream}
	p.mu.Lock()
	if p.stopping {
		p.mu.Unlock()
		return nil, fail(503, "plugin is stopping")
	}
	// The pointer key is local bookkeeping, never an account or user identifier.
	p.streams[fmt.Sprintf("%p", s)] = s
	p.mu.Unlock()
	s.mu.Lock()
	s.timer = time.AfterFunc(timeout, s.close)
	s.mu.Unlock()
	var operation struct {
		ID string `json:"operation_id"`
	}
	err := p.host("host.http.operation_open", object{"host_callback_id": callback}, &operation)
	if err != nil {
		s.close()
		return nil, err
	}
	s.mu.Lock()
	s.operation = operation.ID
	closed := s.closed
	s.mu.Unlock()
	if closed {
		s.cancelOperation()
		return nil, fail(504, "upstream request timed out")
	}
	return s, nil
}
func (s *upstreamStream) cancelOperation() {
	s.mu.Lock()
	op := s.operation
	s.mu.Unlock()
	if op != "" {
		_ = s.p.host("host.http.cancel", object{"host_callback_id": s.callback, "operation_id": op}, nil)
	}
}
func (s *upstreamStream) close() {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	id := s.id
	if s.timer != nil {
		s.timer.Stop()
	}
	s.mu.Unlock()
	s.closeClient("BPS operation cancelled or timed out")
	s.cancelOperation()
	if id != "" {
		_ = s.p.host("host.http.stream_close", object{"stream_id": id}, nil)
	}
	s.p.mu.Lock()
	delete(s.p.streams, fmt.Sprintf("%p", s))
	s.p.mu.Unlock()
}
func (s *upstreamStream) closeClient(message string) {
	s.clientOnce.Do(func() {
		if s.downstream != "" {
			_ = s.p.host("host.stream.close", object{"stream_id": s.downstream, "error": message}, nil)
		}
	})
}
func (s *upstreamStream) open(body object, session session) (int, error) {
	s.p.throttle()
	s.mu.Lock()
	closed := s.closed
	op := s.operation
	s.mu.Unlock()
	if closed {
		return 0, fail(504, "upstream operation closed")
	}
	var res struct {
		Status  int         `json:"status_code"`
		Headers http.Header `json:"headers"`
		ID      string      `json:"stream_id"`
	}
	err := s.p.host("host.http.do_stream", object{"host_callback_id": s.callback, "operation_id": op,
		"method": "POST", "url": upstreamURL, "headers": session.headers(), "body": mustJSON(body)}, &res)
	if err != nil {
		return 0, err
	}
	s.mu.Lock()
	s.id = res.ID
	closed = s.closed
	s.mu.Unlock()
	if closed {
		if res.ID != "" {
			_ = s.p.host("host.http.stream_close", object{"stream_id": res.ID}, nil)
		}
		return 0, fail(504, "upstream operation closed")
	}
	if res.Status >= 200 && res.Status < 300 {
		if !strings.Contains(strings.ToLower(res.Headers.Get("Content-Type")), "text/event-stream") {
			return 0, fail(502, "BPS returned a non-SSE response")
		}
		if res.ID == "" {
			return 0, fail(502, "host returned no HTTP stream")
		}
	}
	return res.Status, nil
}
func (s *upstreamStream) Read(dst []byte) (int, error) {
	if len(dst) == 0 {
		return 0, nil
	}
	for len(s.pending) == 0 {
		s.mu.Lock()
		closed, id := s.closed, s.id
		s.mu.Unlock()
		if closed {
			return 0, fail(504, "upstream stream closed or timed out")
		}
		var res struct {
			Payload []byte `json:"payload"`
			Error   string `json:"error"`
			Done    bool   `json:"done"`
		}
		if err := s.p.host("host.http.stream_read", object{"stream_id": id}, &res); err != nil {
			return 0, err
		}
		if res.Error != "" {
			return 0, fail(502, "upstream stream read failed")
		}
		s.pending = res.Payload
		if len(s.pending) == 0 && res.Done {
			return 0, io.EOF
		}
	}
	n := copy(dst, s.pending)
	s.pending = s.pending[n:]
	return n, nil
}
func (p *Plugin) execute(req executorRequest, streaming bool) (result any, err error) {
	cfg, err := p.begin()
	if err != nil {
		return nil, err
	}
	handedOff := false
	defer func() {
		if !handedOff {
			p.finish()
		}
	}()
	if req.CallbackID == "" {
		return nil, fail(500, "CPA host HTTP callback is required")
	}
	if streaming && req.StreamID == "" {
		return nil, fail(400, "CPA stream_id is required")
	}
	if req.Format != "" && req.Format != "openai-response" {
		return nil, fail(400, "executor requires openai-response format")
	}
	if req.Alt != "" {
		return nil, fail(501, "alternate execution endpoints are not supported")
	}
	auth := req.StorageJSON
	if len(auth) == 0 {
		auth = mustJSON(req.AuthMetadata)
	}
	sess, err := sessionFromJSON(auth, true)
	if err != nil {
		return nil, err
	}
	prep, err := prepare(req.Payload, req.Model, req.AuthID+"/"+sess.account)
	if err != nil {
		return nil, err
	}
	downstream := ""
	if streaming {
		downstream = req.StreamID
	}
	s, err := p.newStream(req.CallbackID, downstream, time.Duration(cfg.TimeoutSeconds)*time.Second)
	if err != nil {
		return nil, err
	}
	status, err := s.open(prep.Body, sess)
	if err != nil {
		s.close()
		return nil, err
	}
	// Upload only after an inline-image request is rejected, before any output is exposed.
	if (status == 400 || status == 422) && hasInlineImages(prep.Body) {
		s.mu.Lock()
		id := s.id
		s.id = ""
		s.mu.Unlock()
		if id != "" {
			_ = p.host("host.http.stream_close", object{"stream_id": id}, nil)
		}
		if err = s.uploadImages(prep.Body, sess); err != nil {
			s.close()
			return nil, err
		}
		status, err = s.open(prep.Body, sess)
	}
	if err != nil {
		s.close()
		return nil, err
	}
	if status < 200 || status >= 300 {
		s.close()
		if status < 400 || status > 599 {
			status = 502
		}
		return nil, fail(status, fmt.Sprintf("BPS upstream returned HTTP %d", status))
	}
	if !streaming {
		defer s.close()
		final, err := rewriteSSE(s, cfg.MaxSSEMiB<<20, prep, nil)
		if err != nil {
			return nil, err
		}
		return object{"Payload": mustJSON(final), "Headers": http.Header{"Content-Type": []string{"application/json"}}}, nil
	}
	handedOff = true
	go func() {
		var streamErr error
		defer p.finish()
		defer s.close()
		defer func() {
			if recover() != nil {
				streamErr = fail(500, "internal plugin stream error")
			}
			msg := ""
			if streamErr != nil {
				msg = streamErr.Error()
			}
			s.closeClient(msg)
		}()
		_, streamErr = rewriteSSE(s, cfg.MaxSSEMiB<<20, prep, func(payload []byte) error {
			return p.host("host.stream.emit", object{"stream_id": req.StreamID, "payload": payload}, nil)
		})
	}()
	return object{"headers": safeResponseHeaders()}, nil
}

func readSSE(reader io.Reader, limit int, handle func(string, object) (bool, error)) error {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64*1024), limit+2)
	var data bytes.Buffer
	event := ""
	size := 0
	dispatch := func() (bool, error) {
		if data.Len() == 0 {
			event = ""
			size = 0
			return false, nil
		}
		raw := bytes.TrimSuffix(data.Bytes(), []byte("\n"))
		if bytes.Equal(raw, []byte("[DONE]")) {
			return false, fail(502, "SSE ended before response.completed")
		}
		var payload object
		if json.Unmarshal(raw, &payload) != nil || payload == nil {
			return false, fail(502, "invalid upstream SSE JSON")
		}
		kind := str(payload["type"])
		if kind == "" {
			kind = event
		}
		if event != "" && event != "message" && str(payload["type"]) != "" && event != kind {
			return false, fail(502, "conflicting SSE event type")
		}
		stop, err := handle(kind, payload)
		data.Reset()
		event = ""
		size = 0
		return stop, err
	}
	for scanner.Scan() {
		line := scanner.Text()
		if len(line) > limit {
			return fail(502, "upstream SSE line too large")
		}
		if line == "" {
			stop, err := dispatch()
			if err != nil {
				return err
			}
			if stop {
				return nil
			}
			continue
		}
		size += len(line) + 1
		if size > limit {
			return fail(502, "upstream SSE event too large")
		}
		if strings.HasPrefix(line, "data:") {
			data.WriteString(strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
			data.WriteByte('\n')
		}
		if strings.HasPrefix(line, "event:") {
			event = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		}
	}
	if err := scanner.Err(); err != nil {
		return fail(502, "upstream SSE read failed or exceeded size limit")
	}
	if data.Len() > 0 {
		stop, err := dispatch()
		if err != nil {
			return err
		}
		if stop {
			return nil
		}
	}
	return fail(502, "upstream ended without a terminal response")
}
func isCall(item object) bool {
	return item["type"] == "function_call" || item["type"] == "custom_tool_call"
}
func rewriteSSE(reader io.Reader, limit int, prep prepared, emit func([]byte) error) (object, error) {
	indices := map[int]int{}
	finished := map[int]bool{}
	pending := map[string]bool{}
	var final object
	seq := 0
	mapped := func(index int) int {
		if i, ok := indices[index]; ok {
			return i
		}
		i := len(indices)
		indices[index] = i
		return i
	}
	send := func(kind string, payload object) error {
		payload["type"] = kind
		payload["sequence_number"] = seq
		seq++
		switch i := payload["output_index"].(type) {
		case float64:
			if i < 0 || i > 100000 || i != float64(int(i)) {
				return fail(502, "invalid upstream output index")
			}
			payload["output_index"] = mapped(int(i))
		case int:
			payload["output_index"] = mapped(i)
		}
		if emit == nil {
			return nil
		}
		raw := append([]byte("event: "+kind+"\ndata: "), mustJSON(payload)...)
		raw = append(raw, []byte("\n\n")...)
		return emit(raw)
	}
	err := readSSE(reader, limit, func(kind string, payload object) (bool, error) {
		if kind == "response.failed" || kind == "response.incomplete" || kind == "error" {
			return false, fail(502, "BPS returned "+kind+"; client tools were not released")
		}
		if kind == "response.completed" {
			res := obj(payload["response"])
			if res == nil {
				return false, fail(502, "completion has no response")
			}
			status := str(res["status"])
			if status != "" && status != "completed" {
				return false, fail(502, "completion has conflicting status")
			}
			output, ok := res["output"].([]any)
			if !ok {
				return false, fail(502, "completion has no output array")
			}
			translated := make([]object, len(output))
			seen := map[string]bool{}
			count := 0
			// Validate the entire terminal output before exposing any executable call.
			for i, v := range output {
				item := obj(v)
				if item == nil {
					return false, fail(502, "invalid output item")
				}
				if isCall(item) {
					id := str(item["call_id"])
					if seen[id] {
						return false, fail(502, "duplicate terminal tool call")
					}
					seen[id] = true
					call, err := translateCall(item, prep.Tools)
					if err != nil {
						return false, err
					}
					translated[i] = call
					count++
				} else {
					translated[i] = item
				}
			}
			for id := range pending {
				if !seen[id] {
					return false, fail(502, "completion omitted a pending tool call")
				}
			}
			if !prep.Parallel && count > 1 {
				return false, fail(502, "upstream returned parallel calls for a sequential request")
			}
			for index := range indices {
				if index < 0 || index >= len(translated) {
					return false, fail(502, "completion omitted an observed output index")
				}
			}
			for i, item := range translated {
				_, known := indices[i]
				if isCall(item) || !known {
					if err := send("response.output_item.added", object{"output_index": i, "item": item}); err != nil {
						return false, err
					}
				}
				if isCall(item) {
					field, kind := "arguments", "response.function_call_arguments.done"
					if item["type"] == "custom_tool_call" {
						field = "input"
						kind = "response.custom_tool_call_input.done"
					}
					if err := send(kind, object{"output_index": i, "item_id": item["id"], "call_id": item["call_id"], "name": item["name"], field: item[field]}); err != nil {
						return false, err
					}
				}
				if isCall(item) || !finished[i] {
					if err := send("response.output_item.done", object{"output_index": i, "item": item}); err != nil {
						return false, err
					}
				}
			}
			reordered := make([]any, len(translated))
			for i, item := range translated {
				j := mapped(i)
				if j >= len(reordered) {
					return false, fail(502, "inconsistent upstream output index")
				}
				reordered[j] = item
			}
			final = clone(res)
			final["output"] = reordered
			final["status"] = "completed"
			payload["response"] = final
			return true, send(kind, payload)
		}
		item := obj(payload["item"])
		if isCall(item) && (kind == "response.output_item.added" || kind == "response.output_item.done") {
			index, ok := payload["output_index"].(float64)
			if !ok || index < 0 || index > 100000 || index != float64(int(index)) {
				return false, fail(502, "upstream tool has no valid output index")
			}
			id := str(item["call_id"])
			if id == "" {
				return false, fail(502, "pending tool call has no call_id")
			}
			pending[id] = true
			return false, nil
		}
		if strings.Contains(kind, "function_call") || strings.Contains(kind, "custom_tool_call") {
			return false, nil
		}
		if kind == "response.output_item.done" {
			if index, ok := payload["output_index"].(float64); ok {
				finished[int(index)] = true
			}
		}
		// Intermediate snapshots must not leak executable native Office calls.
		if res := obj(payload["response"]); res != nil {
			res = clone(res)
			out := []any{}
			for _, v := range arr(res["output"]) {
				if !isCall(obj(v)) {
					out = append(out, v)
				}
			}
			res["output"] = out
			payload["response"] = res
		}
		return false, send(kind, payload)
	})
	return final, err
}
