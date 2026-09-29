# Verification

Version: 0.2.0. Date: 2026-09-29.

## Results

- 27 Go test functions, including table-driven cases, passed on Windows amd64
  (Go 1.23.4) and Linux amd64 (Go 1.22.2).
- `go test -race -count=1 ./...` passed on both platforms.
- `go vet ./...` passed on Windows amd64 with CGO enabled.
- Native C ABI smoke tests passed on the final Windows DLL and Linux shared
  object: global routing, original model IDs, shared Codex credential reads,
  simulated refreshed-token rereads, fallback guards, non-streaming response,
  async SSE, HTTP status propagation, quiesce, reinitialization and buffer release.
- Actual CPA plugin-host integration passed on Linux, compiled with Go 1.26.0
  against commit `d33f63f8e3d98428440ebca5a5b6a981a61ff71e`. It loaded and registered
  the native library, claimed Responses/Chat Completions/Claude/Gemini routes,
  kept the native Codex executor and refresh ownership, read existing auth files,
  observed file updates/disabled state, and blocked native-provider fallback.
- The actual-host test exposed missing `GitHubRepository` metadata in 0.1.0.
  Version 0.2.0 fixes it and includes a regression check.
- Linux runtime imports: libc / ELF loader; maximum imported GLIBC symbol version
  2.34. Windows runtime imports: KERNEL32.dll and msvcrt.dll.

All tests were offline with synthetic credentials. ABI tests used simulated HTTP
responses. The actual-host integration used expired tokens to stop before
upstream HTTP while exercising CPA's real loader, routing and credential APIs.
No live OAuth refresh was performed; refreshed-file consumption was simulated.

Not verified: the user's installed CPA version, live account permissions,
live BPS responses, proxy connectivity on the server, other architectures,
complete cross-client streaming semantics, or full parity with the Python code.
No server configuration, running service, real credential, or Codex configuration
was modified.

## Final Binary SHA-256

```text
8e2cd3175bf03ac948253dca3bf25a692ae84706b6501ba25493a0a5cf064fa7  dist/linux-amd64/bps-excel.so
510a984d55343b6ff1b6ba8d33579ad862e90438c4d8d331ee59df42bd0af4fc  dist/windows-amd64/bps-excel.dll
```
