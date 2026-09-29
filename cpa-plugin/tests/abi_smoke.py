"""Load the real C ABI library against a deterministic, offline CPA mock host.

Python is a test dependency only. The delivered library does not use Python.
"""

import base64
import ctypes as C
import json
import pathlib
import sys
import threading
import time


class Buffer(C.Structure):
    _fields_ = [("ptr", C.c_void_p), ("len", C.c_size_t)]


HostCall = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_char_p, C.c_void_p, C.c_size_t, C.POINTER(Buffer))
Free = C.CFUNCTYPE(None, C.c_void_p, C.c_size_t)
Call = C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_void_p, C.c_size_t, C.POINTER(Buffer))
Shutdown = C.CFUNCTYPE(None)


class HostAPI(C.Structure):
    _fields_ = [
        ("abi_version", C.c_uint32),
        ("host_ctx", C.c_void_p),
        ("call", HostCall),
        ("free_buffer", Free),
    ]


class PluginAPI(C.Structure):
    _fields_ = [
        ("abi_version", C.c_uint32),
        ("call", Call),
        ("free_buffer", Free),
        ("shutdown", Shutdown),
    ]


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def b64(value):
    return base64.b64encode(value).decode()


def main(path):
    allocated = {}
    lock = threading.Lock()
    ended = threading.Event()
    chunks = []
    errors = []
    stream = bytearray()
    status = 200
    claims = {"exp": int(time.time()) + 3600, "https://api.openai.com/auth": {"chatgpt_account_id": "mock-account"}}
    jwt = "header." + base64.urlsafe_b64encode(encode(claims)).decode().rstrip("=") + ".signature"
    auth = {"type": "codex", "access_token": jwt, "account_id": "mock-account"}
    auth_entry = {"auth_index": "native-account", "provider": "codex", "status": "active"}
    auth_available = True
    seen_tokens = []

    @Free
    def host_free(ptr, length):
        with lock:
            allocated.pop(ptr, None)

    @HostCall
    def host_call(ctx, method, ptr, length, out):
        nonlocal stream
        try:
            req = json.loads(C.string_at(ptr, length))
            method = method.decode()
            result = {}
            if method == "host.auth.list":
                result = {"files": [auth_entry] if auth_available else []}
            elif method == "host.auth.get_runtime":
                assert req["auth_index"] == "native-account"
                result = {"auth": auth_entry}
            elif method == "host.auth.get":
                assert req["auth_index"] == "native-account"
                result = {"json": auth}
            elif method == "host.http.operation_open":
                result = {"operation_id": "operation-1"}
            elif method == "host.http.do_stream":
                assert req["url"] == "https://bps.openai.com/basispoints/api/responses"
                body = json.loads(base64.b64decode(req["body"]))
                assert body["model"] == "gpt-5.6-sol"
                assert body["reasoning_effort"] == "xhigh"
                assert req["headers"]["Authorization"] == ["Bearer " + auth["access_token"]]
                seen_tokens.append(auth["access_token"])
                response = {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_mock",
                        "object": "response",
                        "status": "completed",
                        "output": [{
                            "id": "msg_mock",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "offline ABI smoke OK"}],
                        }],
                        "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
                    },
                }
                stream = bytearray(b"event: response.completed\ndata: " + encode(response) + b"\n\n")
                result = {"status_code": status, "headers": {"Content-Type": ["text/event-stream"]}, "stream_id": "http-1"}
            elif method == "host.http.stream_read":
                data = bytes(stream[:23])
                del stream[:23]
                result = {"payload": b64(data), "done": not data}
            elif method == "host.stream.emit":
                chunks.append(base64.b64decode(req["payload"]))
            elif method == "host.stream.close":
                if req.get("error"):
                    errors.append(req["error"])
                ended.set()
            elif method not in ("host.http.cancel", "host.http.stream_close"):
                raise AssertionError(f"unexpected callback: {method}")
            response = encode({"ok": True, "result": result})
            rc = 0
        except Exception as exc:
            errors.append(str(exc))
            response = encode({"ok": False, "error": {"code": "mock_error", "message": str(exc), "http_status": 500}})
            rc = 1
        buf = C.create_string_buffer(response)
        address = C.addressof(buf)
        with lock:
            allocated[address] = buf
        out.contents.ptr = address
        out.contents.len = len(response)
        return rc

    library = C.CDLL(str(pathlib.Path(path).resolve()))
    library.cliproxy_plugin_init.argtypes = [C.POINTER(HostAPI), C.POINTER(PluginAPI)]
    library.cliproxy_plugin_init.restype = C.c_int
    host = HostAPI(1, None, host_call, host_free)
    api = PluginAPI()
    assert library.cliproxy_plugin_init(C.byref(host), C.byref(api)) == 0
    assert api.abi_version == 1

    def invoke(method, value):
        request = encode(value)
        buf = C.create_string_buffer(request)
        result = Buffer()
        rc = api.call(method.encode(), C.addressof(buf), len(request), C.byref(result))
        try:
            envelope = json.loads(C.string_at(result.ptr, result.len))
        finally:
            api.free_buffer(result.ptr, result.len)
        return rc, envelope

    rc, registered = invoke("plugin.register", {"schema_version": 6})
    assert rc == 0 and registered["result"]["capabilities"]["executor"]
    assert registered["result"]["metadata"]["Name"] == "bps-excel"
    assert registered["result"]["metadata"]["GitHubRepository"]
    assert registered["result"]["capabilities"]["model_router"]
    assert not registered["result"]["capabilities"].get("auth_provider", False)
    rc, models = invoke("model.static", {})
    assert rc == 0 and len(models["result"]["Models"]) == 7
    assert all(not model["ID"].endswith("-excel") for model in models["result"]["Models"])
    rc, route = invoke("model.route", {"RequestedModel": "any-model", "SourceFormat": "openai-response"})
    assert rc == 0 and route["result"]["Handled"] and route["result"]["TargetKind"] == "self"
    rc, parsed = invoke("auth.parse", {"FileName": "mock.json", "RawJSON": b64(encode(auth))})
    assert rc == 0 and not parsed["result"]["Handled"]
    rc, guard = invoke("request.intercept_after", {"SourceFormat": "openai-response", "ToFormat": "codex", "Metadata": {"selected_auth_id": "native-account"}})
    assert rc == 0 and guard["result"]["Terminate"]
    request = {
        "Model": "gpt-6-sol",
        "Format": "openai-response",
        "Payload": b64(encode({"model": "gpt-6-sol", "input": "hello", "reasoning": {"effort": "max"}})),
        "host_callback_id": "mock-callback",
    }
    rc, response = invoke("executor.execute", request)
    assert rc == 0, response
    decoded = json.loads(base64.b64decode(response["result"]["Payload"]))
    assert decoded["output"][0]["content"][0]["text"] == "offline ABI smoke OK"
    assert decoded["model"] == "gpt-6-sol"
    # Simulate the native CPA refresh process persisting a new token.
    claims["exp"] += 3600
    auth["access_token"] = "header." + base64.urlsafe_b64encode(encode(claims)).decode().rstrip("=") + ".signature"
    request["stream_id"] = "client-1"
    rc, response = invoke("executor.execute_stream", request)
    assert rc == 0, response
    assert ended.wait(10), "async stream did not close"
    assert b"response.completed" in b"".join(chunks)
    assert not errors, errors
    assert seen_tokens[0] != seen_tokens[1], "refreshed native credential was not reread"
    auth_available = False
    rc, response = invoke("executor.execute", request)
    assert rc != 0 and response["error"]["http_status"] == 503
    auth_available = True
    # Reject upstream auth without turning it into a generic HTTP 500.
    status = 401
    rc, response = invoke("executor.execute", request)
    assert rc != 0 and response["error"]["http_status"] == 401
    rc, response = invoke("plugin.quiesce", {})
    assert rc == 0, response
    api.shutdown()
    # Go shared libraries can remain mapped after unload. Re-init must not retain
    # the previous instance's quiesced state.
    status = 200
    assert library.cliproxy_plugin_init(C.byref(host), C.byref(api)) == 0
    rc, response = invoke("plugin.register", {"schema_version": 6})
    assert rc == 0, response
    rc, response = invoke("executor.execute", request)
    assert rc == 0, response
    api.shutdown()
    assert not allocated, "host response buffers leaked"
    print("PASS: native ABI/global-route/plain-models/shared-Codex/refresh-reread/fallback-guard/nonstream/async-stream/HTTP-status/reinit/free")


if __name__ == "__main__":
    main(sys.argv[1])
