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

Observed result for both Task 4 test files: exit code `0`; `11 passed in 0.03s`.

## RED 3 — multi-segment stream key and Cookie header review

Regression tests were added for an RTMP stream key containing multiple path segments and a
Cookie header supplied in a command argument.

Observed result: exit code `1`; `2 failed, 7 passed in 0.15s`. The failures confirmed that
the intermediate stream-key path segments and Cookie header value were still visible. No
credential values are reproduced in this evidence record.

## GREEN 3 — reviewed Task 4 behavior

RTMP redaction now preserves only the scheme, authority, and first application path segment,
then replaces the complete remaining stream-key path with one mask. Cookie header values are
also replaced in full.

Observed result for both Task 4 test files: exit code `0`; `13 passed in 0.03s`.
