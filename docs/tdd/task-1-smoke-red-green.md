# Task 1 smoke test RED/GREEN evidence

This is a post-implementation mutation check. It does not change or recreate the initial historical
commit sequence. The check temporarily removes the package implementation from the import path to
prove that the smoke test detects a missing `__version__`, then restores the exact file and proves
the test passes.

## RED: temporarily remove the implementation

Run from the repository root in PowerShell:

```powershell
$source = 'src\restream_studio\__init__.py'
$tempDir = '.tdd-temp'
$temp = Join-Path $tempDir '__init__.py'
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
$before = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash
Move-Item -LiteralPath $source -Destination $temp
.\.venv\Scripts\python.exe -m pytest tests\unit\test_smoke.py -q
$redExit = $LASTEXITCODE
```

Relevant output:

```text
F                                                                        [100%]
E       AttributeError: module 'restream_studio' has no attribute '__version__'
FAILED tests/unit/test_smoke.py::test_package_exposes_version - AttributeErro...
1 failed in 0.12s
RED_EXIT=1
```

The namespace package remains importable while its implementation file is absent, so the expected
failure is the missing `__version__` attribute. Exit code: `1`.

## Restore and verify exact file

```powershell
Move-Item -LiteralPath $temp -Destination $source
Remove-Item -LiteralPath $tempDir
$after = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash
if ($before -ne $after) { throw "Restored file hash mismatch: before=$before after=$after" }
```

Relevant output:

```text
RESTORED_SHA256=5BFE7975546D6E2170FB6A20DF95D22014B63355655FC4EC54F912B894F20DD8
```

The pre-mutation and restored SHA-256 values matched.

## GREEN: run the same test after restoration

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_smoke.py -q
$greenExit = $LASTEXITCODE
```

Relevant output:

```text
.                                                                        [100%]
1 passed in 0.01s
GREEN_EXIT=0
```

Exit code: `0`.
