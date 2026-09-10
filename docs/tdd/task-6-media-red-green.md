# Task 6 media probe and FFmpeg commands — RED/GREEN

## Scope

Task 6 adds only the media boundary: shell-free ffprobe execution, bounded parsing into the
immutable `MediaProbe`, and one shell-free FFmpeg argv per Douyin/WeChat destination. Raw
source/output credentials are retained only in execution argv; display data is redacted.

DNS pinning is intentionally not claimed here. This layer rejects non-HTTP(S) inputs,
userinfo, control characters, and unsafe literal output IPs. Hostname resolution and
connection-point IP enforcement require the later process/network execution boundary.

## RED 1 — modules absent

The initial focused command was:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_ffprobe.py tests/unit/test_ffmpeg_commands.py -q
```

It exited `1` during collection with two `ModuleNotFoundError` errors for
`restream_studio.media`.

## GREEN 1 — probe and command boundary

Minimal implementations added typed probe errors, safe rational parsing, timeout kill/wait,
URL validation, immutable metadata extensions, destination presets, copy eligibility, and
redacted command views. The first executable focused run exposed one incorrect implementation
detail: a 61 FPS transcode generated GOP 120 rather than the preset's 30 FPS / GOP 60. The
implementation was corrected to set `-r 30` and `-g 60`.

## RED/GREEN 2 — complete URL redaction

A regression test using `wsSecret=SOURCE-SECRET` and a one-segment RTMP key first failed
because both values appeared in `display_command` (`1 failed`). Display URL construction was
then narrowed to mask every source query and the output key/query while preserving raw values
only in `argv`.

## Final evidence

The focused suite executes fake subprocesses without network. When local FFmpeg tools exist,
it also generates a 64x64 H.264/AAC fixture and parses real ffprobe JSON; both commands have
10-second process timeouts. Final project verification and build results are recorded in the
Task 6 completion commit/report.
