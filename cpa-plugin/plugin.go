package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

const provider = "bps-excel"
const upstreamURL = "https://bps.openai.com/basispoints/api/responses"

type object = map[string]any
type hostCall func(string, any, any) error
type rpcError struct {
	Code       string `json:"code"`
	Message    string `json:"message"`
	HTTPStatus int    `json:"http_status,omitempty"`
}

func (e *rpcError) Error() string { return e.Message }
func fail(status int, message string) error {
	return &rpcError{Code: "bps_error", Message: message, HTTPStatus: status}
}

type envelope struct {
	OK     bool            `json:"ok"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  *rpcError       `json:"error,omitempty"`
}

func errorJSON(err error) []byte {
	var re *rpcError
	if !errors.As(err, &re) {
		re = &rpcError{Code: "bps_error", Message: err.Error(), HTTPStatus: 502}
	}
	return mustJSON(envelope{Error: re})
}
func mustJSON(v any) []byte { b, _ := json.Marshal(v); return b }
func str(v any) string      { s, _ := v.(string); return s }
func obj(v any) object      { m, _ := v.(map[string]any); return m }
func arr(v any) []any       { a, _ := v.([]any); return a }
func clone(m object) object { var out object; _ = json.Unmarshal(mustJSON(m), &out); return out }

type config struct {
	MaxConcurrent  int `yaml:"max_concurrent"`
	MaxSSEMiB      int `yaml:"max_sse_event_mib"`
	TimeoutSeconds int `yaml:"timeout_seconds"`
}
type Plugin struct {
	host      hostCall
	mu        sync.Mutex
	cfg       config
	active    int
	stopping  bool
	streams   map[string]*upstreamStream
	wg        sync.WaitGroup
	lastStart time.Time
}

func newPlugin(host hostCall) *Plugin {
	return &Plugin{host: host, cfg: config{8, 16, 900}, streams: map[string]*upstreamStream{}}
}
func (p *Plugin) handle(method string, raw []byte) (any, error) {
	switch method {
	case "plugin.register", "plugin.reconfigure":
		var req struct {
			ConfigYAML []byte `json:"config_yaml"`
		}
		if len(raw) > 0 {
			if err := json.Unmarshal(raw, &req); err != nil {
				return nil, fail(400, "invalid registration request")
			}
		}
		cfg := config{8, 16, 900}
		if err := yaml.Unmarshal(req.ConfigYAML, &cfg); err != nil {
			return nil, fail(400, "invalid plugin configuration")
		}
		if cfg.MaxConcurrent < 1 || cfg.MaxConcurrent > 128 || cfg.MaxSSEMiB < 1 || cfg.MaxSSEMiB > 64 || cfg.TimeoutSeconds < 1 || cfg.TimeoutSeconds > 3600 {
			return nil, fail(400, "configuration limits: concurrency 1..128, SSE MiB 1..64, timeout 1..3600")
		}
		p.mu.Lock()
		p.cfg = cfg
		p.mu.Unlock()
		return registration(), nil
	case "plugin.quiesce", "plugin.shutdown":
		p.shutdown()
		return object{}, nil
	case "executor.identifier", "auth.identifier":
		return object{"identifier": provider}, nil
	case "model.static", "model.for_auth":
		return models(), nil
	case "auth.parse":
		return parseAuth(raw)
	case "auth.refresh", "auth.login.start", "auth.login.poll":
		return nil, fail(501, "import a current bps-excel credential; interactive login and token refresh are not implemented")
	case "executor.execute", "executor.execute_stream":
		var req executorRequest
		if err := json.Unmarshal(raw, &req); err != nil {
			return nil, fail(400, "invalid execution request")
		}
		return p.execute(req, method == "executor.execute_stream")
	case "executor.count_tokens":
		return nil, fail(501, "exact BPS token counting is not available")
	case "executor.http_request":
		return nil, fail(501, "arbitrary HTTP forwarding is not supported")
	default:
		return nil, fail(400, "unsupported plugin method")
	}
}
func registration() object {
	return object{
		"schema_version": 6,
		"metadata": object{"Name": provider, "Version": "0.1.0", "Author": "openai-proxy contributors",
			"ConfigFields": []any{
				object{"Name": "max_concurrent", "Type": "number", "Description": "Maximum active BPS requests (1-128).", "Default": 8},
				object{"Name": "max_sse_event_mib", "Type": "number", "Description": "Maximum SSE event size in MiB (1-64).", "Default": 16},
				object{"Name": "timeout_seconds", "Type": "number", "Description": "Total upstream request timeout (1-3600 seconds).", "Default": 900},
			}},
		"capabilities": object{"auth_provider": true, "model_provider": true, "executor": true,
			"executor_model_scope": "oauth", "executor_input_formats": []string{"openai-response"},
			"executor_output_formats": []string{"openai-response"}},
	}
}

var modelNames = []string{"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-6-sol", "gpt-6-luna", "gpt-6-terra"}

func models() object {
	list := []any{}
	for _, name := range modelNames {
		list = append(list, object{"ID": name + "-excel", "Name": name, "Object": "model", "OwnedBy": provider,
			"DisplayName": name + " (Excel)", "Type": provider, "SupportedGenerationMethods": []string{"chat"},
			"SupportedInputModalities": []string{"text", "image"}, "SupportedOutputModalities": []string{"text"},
			"Thinking": object{"Levels": []string{"low", "medium", "high", "xhigh", "max", "ultra"}}, "UserDefined": true})
	}
	return object{"Provider": provider, "Models": list}
}

type executorRequest struct {
	AuthID, AuthProvider, Model, Format, SourceFormat, Alt string
	Payload, OriginalRequest, StorageJSON                  []byte
	AuthMetadata                                           object
	AuthAttributes                                         map[string]string
	StreamID                                               string `json:"stream_id"`
	CallbackID                                             string `json:"host_callback_id"`
}

func parseAuth(raw []byte) (any, error) {
	var req struct {
		Provider, Path, FileName string
		RawJSON                  []byte
	}
	if err := json.Unmarshal(raw, &req); err != nil {
		return nil, fail(400, "invalid auth parse request")
	}
	var data object
	if json.Unmarshal(req.RawJSON, &data) != nil {
		return object{"Handled": false}, nil
	}
	if req.Provider != provider && str(data["type"]) != provider {
		return object{"Handled": false}, nil
	}
	if _, err := sessionFromJSON(req.RawJSON, false); err != nil {
		return nil, err
	}
	name := req.FileName
	if name == "" {
		name = filepath.Base(req.Path)
	}
	return object{"Handled": true, "Auth": object{"Provider": provider, "ID": name, "FileName": name,
		"Label": str(data["email"]), "Prefix": str(data["prefix"]), "ProxyURL": str(data["proxy_url"]),
		"Disabled": data["disabled"] == true, "StorageJSON": req.RawJSON,
		"Metadata": data, "Attributes": map[string]string{"provider": provider}}}, nil
}
func (p *Plugin) begin() (config, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.stopping {
		return p.cfg, fail(503, "plugin is shutting down")
	}
	if p.active >= p.cfg.MaxConcurrent {
		return p.cfg, fail(503, "BPS concurrency limit reached")
	}
	p.active++
	p.wg.Add(1)
	return p.cfg, nil
}
func (p *Plugin) finish() { p.mu.Lock(); p.active--; p.mu.Unlock(); p.wg.Done() }
func (p *Plugin) shutdown() {
	p.mu.Lock()
	p.stopping = true
	streams := make([]*upstreamStream, 0, len(p.streams))
	for _, s := range p.streams {
		streams = append(streams, s)
	}
	p.mu.Unlock()
	for _, s := range streams {
		s.close()
	}
	p.wg.Wait()
}
func (p *Plugin) throttle() {
	p.mu.Lock()
	next := p.lastStart.Add(210 * time.Millisecond)
	if time.Now().After(next) {
		next = time.Now()
	}
	p.lastStart = next
	p.mu.Unlock()
	time.Sleep(time.Until(next))
}
func safeResponseHeaders() http.Header {
	return http.Header{"Content-Type": []string{"text/event-stream"}, "Cache-Control": []string{"no-cache"}}
}
func upstreamModel(name string) string {
	name = strings.TrimSuffix(name, "-excel")
	switch name {
	case "gpt-6-sol":
		return "gpt-5.6-sol"
	case "gpt-6-luna":
		return "gpt-5.6-luna"
	case "gpt-6-terra":
		return "gpt-5.6-terra"
	}
	return name
}
