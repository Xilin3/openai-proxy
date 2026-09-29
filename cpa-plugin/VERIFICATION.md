# Verification

Date: 2026-09-29.

## Results

- 19 Go test functions, including table-driven cases, passed on Windows amd64
  (Go 1.23.4) and Linux amd64 (Go 1.22.2).
- `go test -race -count=1 ./...` passed on both platforms.
- `go vet ./...` passed on Windows amd64 with CGO enabled.
- Native C ABI smoke tests passed on the final Windows DLL and Linux shared
  object: initialization, registration, model listing, credential parsing,
  non-streaming response, async SSE, HTTP 401 propagation, quiesce, reinitialization
  and host-buffer release.
- Linux runtime imports: libc / ELF loader; maximum imported GLIBC symbol version
  2.34. Windows runtime imports: KERNEL32.dll and msvcrt.dll.

All tests were offline with synthetic credentials and simulated host HTTP
responses. The smoke harness loads the actual built dynamic libraries, but is
not a live CPA deployment.

Not verified: the user's installed CPA version, live account permissions,
live BPS responses, proxy connectivity on the server, other architectures,
cross-client protocol conversion, or full behavior parity with the Python code.
No server configuration, running service, real credential, or Codex configuration
was modified.

## Final Binary SHA-256

```text
633ff0760042bb7efa9970574ebd88a2d7325ae2eb18fe3af013559ca73c7bc7  dist/linux-amd64/bps-excel.so
f02401be88113ffef057186639d9dd1d3952611d3cec3f75c6e39511cfd069c8  dist/windows-amd64/bps-excel.dll
```
