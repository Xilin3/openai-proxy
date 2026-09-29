# BPS Excel CPA Native Plugin

Native Go/C ABI port of the core `kokojacket/openai-proxy` transport.
CPA loads one shared library in-process. No Python interpreter, local HTTP
proxy, child process, or additional listening port is used at runtime.

Chinese installation guide: `README_CN.md`.

## Compatibility

- Targets CLIProxyAPI v8, C ABI 1, JSON schema 6, including
  `host.http.operation_open`, HTTP stream callbacks and async stream emission.
- Interface reference: CPA commit `d33f63f8e3d98428440ebca5a5b6a981a61ff71e`.
- Python source reference: `f1b6764fcd3f050479512f4b3b0ec2e2624d9832`.
- Older CPA releases without these callbacks are not supported.
- Build with Go 1.22+ and a C compiler matching the target platform.
  CGO is required in both the plugin and the CPA host.

## Build

Linux amd64:

```sh
sh build.sh
```

Windows amd64, with MinGW-w64 GCC on PATH:

```powershell
.\build.ps1
```

Artifacts: `dist/linux-amd64/bps-excel.so` and
`dist/windows-amd64/bps-excel.dll`. Only the library is installed into CPA;
the generated C header is for developers. Other architectures require a matching
native C compiler or a correctly configured cross compiler (`CC`).
The supplied Linux amd64 binary requires glibc 2.34+; it is not an Alpine/musl
binary. The supplied Windows binary imports only KERNEL32.dll and msvcrt.dll.

## Install

1. Check that the installed CPA version has the interfaces listed above.
2. Put the platform-matching library in CPA's configured plugin directory.
   Keep the filename `bps-excel.so` or `bps-excel.dll`.
3. Merge `config.example.yaml` into the existing CPA configuration; do not replace
   existing providers, API keys, plugin entries or routing rules.
4. Create a dedicated JSON credential in CPA's configured authentication directory,
   using the structure in `auth.example.json`. Use your own current access token
   and account ID. A flat `access_token` / `account_id` structure is also accepted.
   The top-level `type` MUST be `bps-excel`; ordinary `codex` credentials are not
   claimed or modified by this plugin.
5. Restrict credential-file access, restart CPA, and inspect its plugin log and
   authenticated `/v1/models` endpoint. Do not send tokens in chat or commit them.

Use CPA's normal client API key, not the upstream ChatGPT token:

```sh
curl "$CPA_BASE_URL/v1/responses" \
  -H "Authorization: Bearer $CPA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-6-astra-excel","input":"Hello","stream":true}'
```

No deployment or changes to an existing CPA server are performed by the build.

## Models And Protocol

The plugin registers these separate model IDs to avoid overriding normal providers:

| Client model | BPS upstream |
| --- | --- |
| `gpt-6-astra-excel` | `gpt-6-astra` |
| `gpt-5.6-sol-excel` | `gpt-5.6-sol` |
| `gpt-5.6-luna-excel` | `gpt-5.6-luna` |
| `gpt-5.6-terra-excel` | `gpt-5.6-terra` |
| `gpt-6-sol-excel` | `gpt-5.6-sol` |
| `gpt-6-luna-excel` | `gpt-5.6-luna` |
| `gpt-6-terra-excel` | `gpt-5.6-terra` |

The executor accepts and emits `openai-response`. Conversion from other client
protocols is delegated to CPA and depends on its installed translator support.
The plugin itself does not implement Chat Completions or Anthropic translators.
Availability still depends on the upstream account.

Implemented:

- Streaming and non-streaming Responses, text, encrypted reasoning history,
  model aliases, reasoning-level normalization (`max` becomes `xhigh`).
- Function/custom tools, nested namespaces and developer `additional_tools`
  declarations used by Responses Lite.
- `run_officejs` wrapping, custom-call reconstruction, JSON Schema validation of
  function arguments, sequential-call enforcement, duplicate/missing-call checks.
- Tools are released only after a consistent `response.completed`; raw Office
  argument deltas are never emitted.
- PNG/JPEG/GIF/WebP data images, including tool-result image arrays; size and
  dimension limits; one attachment-upload retry after HTTP 400/422. Duplicate
  images are uploaded once within a request.
- Host-managed HTTP transport, proxy/cancellation integration, total timeout,
  bounded SSE events, global concurrency limit and approximately 5 request
  starts/second. Account-scoped task identity; no on-disk replay cache.
- Upstream HTTP errors retain their status for CPA routing and error handling.

## Deliberate Limits

This is an initial native implementation, not a byte-for-byte port of every Python
feature:

- Supply full conversation history, including original calls before tool outputs,
  and the tool catalog on each request. There is no cross-request call/catalog
  persistence. `previous_response_id` and `item_reference` are rejected.
- No interactive OAuth login or automatic token refresh. Expired tokens return
  401; reimport a current credential. Refresh/login RPCs return 501 rather than
  pretending to succeed. This does not use or modify local Codex login files.
- Malformed/unknown Office calls fail closed with 502. Automatic tool-repair
  retries, lenient JavaScript parsing, shell-tool name repair and automatic
  code-mode tool inference from the Python service are not ported.
- Only declared function/custom tools are supported; built-in search and other
  tool types are rejected instead of silently dropped.
- Custom grammar declarations are forwarded in instructions but grammar
  conformance is not validated by this plugin.
- Remote image URLs are rejected. Existing BPS `file_id` values can pass through,
  but cross-request attachment caching and expired-attachment recovery are not
  implemented.
- No pending-request queue: saturation returns 503 immediately.
- Exact token counting, arbitrary HTTP forwarding and alternate compact endpoints
  return 501. In-band compaction items/context management are passed through.
- Failed/incomplete responses terminate with an error; they are never reported
  as successful completions.

## Verification

```sh
CGO_ENABLED=1 go test -race ./...
CGO_ENABLED=1 go vet ./...
python3 tests/abi_smoke.py dist/linux-amd64/bps-excel.so
```

Windows tests must override any global cross-compilation environment:

```powershell
$env:GOOS = "windows"
$env:GOARCH = "amd64"
$env:CGO_ENABLED = "1"
go test ./...
python tests/abi_smoke.py dist/windows-amd64/bps-excel.dll
```

The smoke test loads the actual library through C ABI, registers models and a
synthetic credential, exercises both execution modes and validates HTTP status
propagation and buffer release. It uses an offline mock CPA host, not a live
OpenAI account. Python is used only to run this test.

Live upstream access and integration into your installed CPA build require
separate verification. A successful build or mock test does not establish account
access, upstream stability, or exact parity with the Python implementation.

Upstream project license: MIT, see `../LICENSE`. Third-party Go dependencies
retain their respective licenses.
