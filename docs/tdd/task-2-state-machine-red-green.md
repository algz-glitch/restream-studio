# Task 2 state machine TDD evidence

## RED

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_state_machine.py
```

Output:

```text
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\MrLai\Documents\Codex\2026-06-30\sw\restream-studio\.worktrees\feature-restream-studio-v1
configfile: pyproject.toml
plugins: anyio-4.15.1, asyncio-1.4.0, cov-7.1.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 0 items / 1 error

=================================== ERRORS ====================================
______________ ERROR collecting tests/unit/test_state_machine.py ______________
ImportError while importing test module 'C:\Users\MrLai\Documents\Codex\2026-06-30\sw\restream-studio\.worktrees\feature-restream-studio-v1\tests\unit\test_state_machine.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
C:\Users\MrLai\AppData\Local\Programs\Python\Python312\Lib\importlib\__init__.py:90: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests\unit\test_state_machine.py:6: in <module>
    from restream_studio.domain.models import (
E   ModuleNotFoundError: No module named 'restream_studio.domain'
=========================== short test summary info ===========================
ERROR tests/unit/test_state_machine.py
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
============================== 1 error in 0.20s ===============================
```

Exit code: `2`. This is the expected failure because the domain package had not been implemented.

## GREEN

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_state_machine.py
```

Output:

```text
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\MrLai\Documents\Codex\2026-06-30\sw\restream-studio\.worktrees\feature-restream-studio-v1
configfile: pyproject.toml
plugins: anyio-4.15.1, asyncio-1.4.0, cov-7.1.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 6 items

tests\unit\test_state_machine.py ......                                  [100%]

============================== 6 passed in 0.03s ==============================
```

Exit code: `0`.

## Final verification

- `npm run verify`: exit code `0`; Ruff passed, mypy passed for 6 source files, and pytest passed 7 tests.
- `npm run build`: exit code `0`; built `restream_studio-0.1.0-py3-none-any.whl`.
- Same-state source transitions are rejected as illegal transitions.
