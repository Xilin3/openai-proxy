package main

/*
#include <stdint.h>
#include <stdlib.h>
typedef struct { void* ptr; size_t len; } cliproxy_buffer;
typedef int (*host_call_fn)(void*, const char*, const uint8_t*, size_t, cliproxy_buffer*);
typedef void (*host_free_fn)(void*, size_t);
typedef struct { uint32_t abi_version; void* host_ctx; host_call_fn call; host_free_fn free_buffer; } cliproxy_host_api;
typedef int (*plugin_call_fn)(char*, uint8_t*, size_t, cliproxy_buffer*);
typedef void (*plugin_free_fn)(void*, size_t);
typedef void (*plugin_shutdown_fn)(void);
typedef struct { uint32_t abi_version; plugin_call_fn call; plugin_free_fn free_buffer; plugin_shutdown_fn shutdown; } cliproxy_plugin_api;
extern int cliproxyPluginCall(char*, uint8_t*, size_t, cliproxy_buffer*);
extern void cliproxyPluginFree(void*, size_t);
extern void cliproxyPluginShutdown(void);
static cliproxy_host_api saved_host;
static void save_host(cliproxy_host_api* h) { saved_host = *h; }
static int invoke_host(const char* m, const uint8_t* r, size_t n, cliproxy_buffer* out) {
    return saved_host.call(saved_host.host_ctx, m, r, n, out);
}
static void release_host(cliproxy_buffer out) {
    if (out.ptr) saved_host.free_buffer(out.ptr, out.len);
}
*/
import "C"

import (
	"encoding/json"
	"fmt"
	"unsafe"
)

const maxRPCBytes = 192 << 20

var plugin = newPlugin(nativeHostCall)

func main() {}

//export cliproxy_plugin_init
func cliproxy_plugin_init(host *C.cliproxy_host_api, out *C.cliproxy_plugin_api) C.int {
	if host == nil || out == nil || host.abi_version != 1 || host.call == nil || host.free_buffer == nil {
		return 1
	}
	C.save_host(host)
	plugin = newPlugin(nativeHostCall)
	out.abi_version = 1
	out.call = C.plugin_call_fn(C.cliproxyPluginCall)
	out.free_buffer = C.plugin_free_fn(C.cliproxyPluginFree)
	out.shutdown = C.plugin_shutdown_fn(C.cliproxyPluginShutdown)
	return 0
}

//export cliproxyPluginCall
func cliproxyPluginCall(method *C.char, request *C.uint8_t, n C.size_t, out *C.cliproxy_buffer) (rc C.int) {
	if out == nil {
		return 1
	}
	out.ptr, out.len = nil, 0
	defer func() {
		if recover() != nil {
			writeABI(out, errorJSON(fail(500, "internal plugin error")))
			rc = 1
		}
	}()
	if method == nil || n > maxRPCBytes || (n > 0 && request == nil) {
		writeABI(out, errorJSON(fail(400, "invalid ABI request")))
		return 1
	}
	raw := C.GoBytes(unsafe.Pointer(request), C.int(n))
	result, err := plugin.handle(C.GoString(method), raw)
	if err != nil {
		writeABI(out, errorJSON(err))
		return 1
	}
	data, err := json.Marshal(envelope{OK: true, Result: mustJSON(result)})
	if err != nil {
		writeABI(out, errorJSON(fail(500, "cannot encode plugin response")))
		return 1
	}
	writeABI(out, data)
	return 0
}

//export cliproxyPluginFree
func cliproxyPluginFree(ptr unsafe.Pointer, _ C.size_t) { C.free(ptr) }

//export cliproxyPluginShutdown
func cliproxyPluginShutdown() { plugin.shutdown() }

func writeABI(out *C.cliproxy_buffer, b []byte) {
	out.ptr = C.CBytes(b)
	out.len = C.size_t(len(b))
}

func nativeHostCall(method string, request any, response any) error {
	raw, err := json.Marshal(request)
	if err != nil {
		return err
	}
	m := C.CString(method)
	defer C.free(unsafe.Pointer(m))
	r := C.CBytes(raw)
	defer C.free(r)
	var out C.cliproxy_buffer
	rc := C.invoke_host(m, (*C.uint8_t)(r), C.size_t(len(raw)), &out)
	defer C.release_host(out)
	if out.ptr == nil || out.len > maxRPCBytes {
		return fail(502, "invalid host callback response")
	}
	var env envelope
	if err := json.Unmarshal(C.GoBytes(out.ptr, C.int(out.len)), &env); err != nil {
		return fail(502, "invalid host callback JSON")
	}
	if !env.OK || rc != 0 {
		if env.Error != nil {
			return env.Error
		}
		return fmt.Errorf("host callback %s failed", method)
	}
	if response != nil && len(env.Result) > 0 {
		return json.Unmarshal(env.Result, response)
	}
	return nil
}
