# Changelog

## 0.2.0

- Route all inference to the Excel executor while enabled, with guards against
  native-provider fallback. Unsupported models/protocols fail explicitly.
- Reuse existing CPA file-backed Codex OAuth credentials through host callbacks.
  Do not claim authentication ownership, copy credentials or cache access tokens.
- Preserve native CPA background refresh; read persisted updates per request.
- Publish original model IDs without `-excel` and preserve client response names.
- Prefer high-priority available accounts and rotate equivalent candidates.
- Remove the dedicated credential template and document migration from 0.1.0.
- Add credential/routing regression tests and an actual CPA host test template.
- Fix missing repository metadata required by the real CPA plugin loader. The
  mock-only 0.1.0 release did not catch this registration failure.

This remains a preview. It does not inherit the native inference scheduler,
trigger immediate OAuth refresh, or support Home/memory-only credentials.

## 0.1.0

- Initial native C ABI implementation with dedicated credentials and suffixed
  model names.
