package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

type mockHost struct {
	mu         sync.Mutex
	status     int
	data       []byte
	chunks     [][]byte
	methods    []string
	closed     chan struct{}
	closeOnce  sync.Once
	blockRead  bool
	cancelled  chan struct{}
	cancelOnce sync.Once
	uploads    int
	requests   []object
}

func newMockHost(data string) *mockHost {
	return &mockHost{status: 200, data: []byte(data), closed: make(chan struct{}), cancelled: make(chan struct{})}
}
func assign(dst any, value any) error {
	if dst == nil {
		return nil
	}
	return json.Unmarshal(mustJSON(value), dst)
}
func (m *mockHost) call(method string, input any, out any) error {
	req := obj(input)
	m.mu.Lock()
	m.methods = append(m.methods, method)
	m.mu.Unlock()
	switch method {
	case "host.http.operation_open":
		return assign(out, object{"operation_id": "op-1"})
	case "host.http.do_stream":
		m.mu.Lock()
		defer m.mu.Unlock()
		var body object
		_ = json.Unmarshal(req["body"].([]byte), &body)
		m.requests = append(m.requests, body)
		return assign(out, object{"status_code": m.status, "stream_id": "http-1", "headers": http.Header{"Content-Type": []string{"text/event-stream"}}})
	case "host.http.stream_read":
		if m.blockRead {
			<-m.cancelled
			return fail(499, "cancelled")
		}
		m.mu.Lock()
		defer m.mu.Unlock()
		if len(m.data) == 0 {
			return assign(out, object{"done": true})
		}
		n := 17
		if len(m.data) < n {
			n = len(m.data)
		}
		chunk := append([]byte{}, m.data[:n]...)
		m.data = m.data[n:]
		return assign(out, object{"payload": chunk})
	case "host.http.cancel":
		m.cancelOnce.Do(func() { close(m.cancelled) })
		return nil
	case "host.http.stream_close":
		return nil
	case "host.stream.emit":
		m.mu.Lock()
		m.chunks = append(m.chunks, append([]byte{}, req["payload"].([]byte)...))
		m.mu.Unlock()
		return nil
	case "host.stream.close":
		m.closeOnce.Do(func() { close(m.closed) })
		return nil
	case "host.http.do":
		m.mu.Lock()
		defer m.mu.Unlock()
		m.uploads++
		m.status = 200
		return assign(out, object{"StatusCode": 200, "Body": mustJSON(object{"openai_file_id": "file-test"})})
	default:
		return fail(500, "unexpected mock callback")
	}
}
func executionRequest() executorRequest {
	return executorRequest{AuthID: "auth-a", Model: "gpt-6-sol-excel", Format: "openai-response",
		Payload: mustJSON(sourceWithTools()), StorageJSON: credential(time.Now().Unix() + 3600), StreamID: "client-1", CallbackID: "callback-1"}
}
func TestExecutorNonStreaming(t *testing.T) {
	host := newMockHost(completed(message("assistant", "hello")))
	p := newPlugin(host.call)
	out, err := p.execute(executionRequest(), false)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out.(object)["Payload"].([]byte)), "hello") {
		t.Fatal(out)
	}
	p.shutdown()
	if p.active != 0 || len(p.streams) != 0 {
		t.Fatal("resources leaked")
	}
}
func TestExecutorAsyncStreaming(t *testing.T) {
	host := newMockHost(completed(nativeCall("c1", "echo", object{"text": "hello"})))
	p := newPlugin(host.call)
	if _, err := p.execute(executionRequest(), true); err != nil {
		t.Fatal(err)
	}
	select {
	case <-host.closed:
	case <-time.After(5 * time.Second):
		t.Fatal("stream did not close")
	}
	p.shutdown()
	host.mu.Lock()
	defer host.mu.Unlock()
	if len(host.chunks) != 4 {
		t.Fatalf("expected 4 translated events, got %d", len(host.chunks))
	}
	if p.active != 0 || len(p.streams) != 0 {
		t.Fatal("stream leaked resources")
	}
}
func TestExecutorHTTPStatusPreserved(t *testing.T) {
	for _, status := range []int{400, 401, 403, 404, 429, 500, 503} {
		for _, stream := range []bool{false, true} {
			host := newMockHost("")
			host.status = status
			p := newPlugin(host.call)
			_, err := p.execute(executionRequest(), stream)
			var re *rpcError
			if !errors.As(err, &re) || re.HTTPStatus != status {
				t.Fatalf("%d: %v", status, err)
			}
			p.shutdown()
		}
	}
}
func TestShutdownCancelsBlockedStream(t *testing.T) {
	host := newMockHost("")
	host.blockRead = true
	p := newPlugin(host.call)
	if _, err := p.execute(executionRequest(), true); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() { p.shutdown(); close(done) }()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown did not cancel blocked HTTP stream")
	}
	if _, err := p.execute(executionRequest(), true); err == nil {
		t.Fatal("accepted work after shutdown")
	}
}
func TestConcurrencyLimit(t *testing.T) {
	host := newMockHost("")
	host.blockRead = true
	p := newPlugin(host.call)
	p.cfg.MaxConcurrent = 1
	if _, err := p.execute(executionRequest(), true); err != nil {
		t.Fatal(err)
	}
	_, err := p.execute(executionRequest(), true)
	var re *rpcError
	if !errors.As(err, &re) || re.HTTPStatus != 503 {
		t.Fatal(err)
	}
	p.shutdown()
}

func TestTotalTimeoutCancelsHost(t *testing.T) {
	host := newMockHost("")
	host.blockRead = true
	p := newPlugin(host.call)
	p.cfg.TimeoutSeconds = 1
	if _, err := p.execute(executionRequest(), true); err != nil {
		t.Fatal(err)
	}
	select {
	case <-host.cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("deadline did not cancel the host operation")
	}
	p.shutdown()
}
func TestUploadFallback(t *testing.T) {
	host := newMockHost(completed(message("assistant", "image received")))
	host.status = 422
	p := newPlugin(host.call)
	req := executionRequest()
	s := sourceWithTools()
	s["input"] = []any{object{"role": "user", "content": []any{object{"type": "input_image", "image_url": tinyImage()}, object{"type": "input_image", "image_url": tinyImage()}}}}
	req.Payload = mustJSON(s)
	if _, err := p.execute(req, false); err != nil {
		t.Fatal(err)
	}
	p.shutdown()
	if host.uploads != 1 || len(host.requests) != 2 {
		t.Fatal(host.uploads, len(host.requests))
	}
	parts := imageParts(host.requests[1])
	if len(parts) != 2 || parts[0]["file_id"] != "file-test" || parts[1]["file_id"] != "file-test" {
		t.Fatal(parts)
	}
}
func TestConfigurationValidation(t *testing.T) {
	p := newPlugin(nil)
	for _, yaml := range []string{"max_concurrent: 0", "max_sse_event_mib: 65", "timeout_seconds: -1", "max_concurrent: invalid"} {
		_, err := p.handle("plugin.register", mustJSON(object{"config_yaml": []byte(yaml)}))
		if err == nil {
			t.Fatal("invalid config accepted", yaml)
		}
	}
	if _, err := p.handle("plugin.register", mustJSON(object{"config_yaml": []byte("max_concurrent: 2\n")})); err != nil {
		t.Fatal(err)
	}
	if p.cfg.MaxConcurrent != 2 || p.cfg.MaxSSEMiB != 16 {
		t.Fatal(p.cfg)
	}
}
