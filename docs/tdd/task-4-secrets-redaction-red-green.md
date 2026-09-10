# Task 4: Secrets and log redaction TDD evidence

## Scope

Only current-user Windows DPAPI secret protection and recursive credential redaction are included.

## RED 1 — missing security package

Command:

```text
.\.venv\Scripts\python.exe -m pytest tests\unit\test_secrets.py tests\unit\test_redaction.py -q
```

Observed result: exit code `1`; collection failed for both test modules with
`ModuleNotFoundError: No module named 'restream_studio.security'`. This was the expected
failure before production files existed.

## GREEN 1 — initial contract

The minimum DPAPI and recursive-redaction implementation was added.

Observed result for the same command: exit code `0`; `9 passed in 0.03s`.

## RED 2 — command flag and standalone authorization coverage

Two tests were then added for inline/separate stream-key command flags and standalone
Bearer/Basic values.

Observed result: exit code `1`; `2 failed, 5 passed in 0.15s`. The failures showed that
an inline stream-key flag and a standalone authorization value were still returned unchanged.
No credential values are reproduced in this evidence record.

## GREEN 2 — completed Task 4 behavior

The string redactor was minimally extended for those two forms.

The original GREEN 2 run completed with `11 passed in 0.03s`. After the two review
regressions below were added and fixed, the current result for both Task 4 test files is
exit code `0`; `13 passed in 0.03s`.

## RED 3 — multi-segment stream key and Cookie header review

Regression tests were added for an RTMP stream key containing multiple path segments and a
Cookie header supplied in a command argument.

For the changes later committed as `1df5dd2`, the required RED run occurred before the
implementation change. Observed result: exit code `1`; `2 failed, 7 passed in 0.15s`. The
failures confirmed that the intermediate stream-key path segments and Cookie header value
were still visible. No credential values are reproduced in this evidence record.

## GREEN 3 — reviewed Task 4 behavior

RTMP redaction now preserves only the scheme, authority, and first application path segment,
then replaces the complete remaining stream-key path with one mask. Cookie header values are
also replaced in full.

For commit `1df5dd2`, the GREEN run used both Task 4 test files. Observed result: exit code
`0`; `13 passed in 0.03s`.

## RED 4 — normalized keys and explicit WinAPI boundary

Tests were added before implementation for recursively normalized sensitive mapping keys,
canonical URL-safe Base64, empty and embedded-NUL strings, non-string rejection, immediate
WinAPI error capture, configured function signatures, and exactly-once `LocalFree` behavior
after successful allocation on both normal decoding and UTF-8 decoding failure.

Observed result for both Task 4 test files: exit code `1`; `4 failed, 20 passed in 0.19s`.
The failures identified the previous limited mapping-key set and the non-controllable legacy
`ctypes.windll` loading path. No credential values are reproduced in this evidence record.

## GREEN 4 — quality-review closure

Sensitive mapping keys are now separator- and case-insensitive across snake_case,
kebab-case, and camelCase forms. Production WinAPI loading uses `ctypes.WinDLL` with
`use_last_error=True`; all three native functions have complete `argtypes` and `restype`
declarations, and native failures capture the error code immediately. DPAPI output is freed
exactly once after every successful allocation, including decode failure.

Observed result for both Task 4 test files: exit code `0`; `24 passed in 0.05s`. The final
verification suite completed with `68 passed in 0.11s`.
