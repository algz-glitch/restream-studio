# Task 3 URL normalizer TDD evidence

## RED

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_url_normalizer.py -q
```

Initial collection output:

```text
E   ModuleNotFoundError: No module named 'restream_studio.source'
ERROR tests/unit/test_url_normalizer.py
1 error in 0.19s
```

After exposing the requested public API as an unimplemented stub, the same command produced
31 behavior failures with `DouyinUrlValidationError: URL normalizer is not implemented`. This
confirmed that the tests exercised the new API rather than existing behavior.

A second RED cycle added destination-query variants. It produced:

```text
FAILED ...[target-url=evil.example] - Failed: DID NOT RAISE DouyinUrlValidationError
FAILED ...[from=evil.example] - Failed: DID NOT RAISE DouyinUrlValidationError
2 failed, 32 passed in 0.16s
```

A final focused RED cycle verified that short codes remain alphanumeric-only; it failed once for
`https://v.douyin.com/abc_def` before host-specific segment validation was added. An external-IP
query case also failed once before destination-value validation covered IP addresses.

## GREEN

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_url_normalizer.py -q
```

Output:

```text
....................................                                     [100%]
36 passed in 0.05s
```

The implementation uses only `urllib.parse.urlsplit`, `unquote`, and local parsing. It performs
no network access or short-link resolution.

## Final verification

- `npm run verify`: exit code `0`; Ruff passed, mypy passed for 9 source files, and pytest passed
  43 tests.
- `npm run build`: exit code `0`; built `restream_studio-0.1.0-py3-none-any.whl`.
- `git diff --check`: exit code `0`.
