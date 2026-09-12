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

## Quality review RED/GREEN

The quality-review RED first failed during collection because `MediaProbe` lacked profile,
level, bitrate, and GOP evidence fields. Focused regressions also specified incremental
stdout/stderr reads, independent per-pipe and aggregate limits, immediate kill/reap,
localhost/private-literal rejection, mocked mixed DNS answers, strict integer parsing,
frame-rate fallback, conservative copy, and complete display-path masking.

GREEN replaces `communicate()` with concurrent 64 KiB reads and cancels the peer reader on
failure. It resolves every hostname through async `getaddrinfo` before spawn and rejects the
entire result if any answer is non-global. FFprobe is launched with
`-protocol_whitelist http,https,tcp,tls,crypto` and the locally documented HTTP option
`-max_redirects 0` (verified from `ffprobe -h full`). This blocks ffprobe HTTP redirects but
does not bind the validated DNS answer to the later TLS connection. A DNS TOCTOU/rebinding
window therefore remains and must be closed at a future connection-isolation boundary; this
module does not claim otherwise.

Normal stream metadata, including a `streams[].gop_size` value, does not prove observed
keyframe spacing. A second, bounded ffprobe invocation therefore requests only keyframe flags
and best-effort timestamps from the first six seconds. It accepts a GOP duration only when at
least two increasing timestamps produce consistent intervals; malformed, insufficient,
timed-out, oversized, or failed frame evidence leaves `gop_seconds=None` and forces
transcoding. Both probe invocations retain independent output limits and timeout kill/wait
reaping.

Copy now requires complete evidence for H.264, AAC, yuv420p, 48 kHz stereo, dimensions,
bitrate, accepted profile/level, a two-second GOP, and a frame rate matching the preset's 30
FPS cadence. Exact 30 FPS and NTSC 30000/1001 are accepted within 0.01 FPS; 15, 24, and 25
FPS force transcoding.

The continuation RED added a direct `gop_size=60` regression and failed because the
interrupted `_parse_probe` returned `None`. GREEN restored the parser, removed all
`streams.gop_size` dependence, and added frame-probe timeout/output-limit kill-and-reap
regressions. The Windows event-loop hang was reproduced on the first standard async selector
and terminated after 30 seconds. A process-local UDP socketpair selector shim (not a repository
or runtime change) then produced `53 passed, 1 deselected`; the deselected local-tool smoke was
reproduced directly with 10-second subprocess timeouts and reported H.264 with a measured
`gop_seconds=2.0`.

The host's Windows loopback `socketpair()` intermittently blocks pytest event-loop creation,
so the standard verification result and the process-local workaround result are recorded
separately rather than presenting the workaround as an unmodified environment pass.
The final standard `npm run verify` completed Ruff and strict mypy, then was terminated after
30 seconds when pytest hung at the same async boundary. The full process-local workaround run
completed with `163 passed, 2 deselected`; the deselections were the configured live-network
test and the separately executed local FFmpeg smoke. The build and diff-check evidence is
recorded in the completion report. Standard `npm run build` reached wheel construction but
the managed host denied writes inside pip/setuptools-created temporary directories; this is
reported as an environment failure rather than a successful build.

## Final timeout-budget review

RED added two regressions. A resolver coroutine that never completes was wrapped by an outer
50 ms test guard and failed with the guard's raw `TimeoutError`, proving DNS resolution was
outside the configured media-probe timeout. A deterministic clock then showed both ffprobe
stages receiving a fresh 10-second timeout (`[10.0, 10.0]`) after DNS had already consumed
two seconds.

GREEN creates one monotonic deadline after argument validation. DNS resolution is bounded by
the current remainder and maps expiration to the URL-free `MediaProbeTimeoutError("media probe
timed out")`. The stream-metadata probe receives the post-DNS remainder. The optional GOP
probe receives only what remains after metadata parsing; if the deadline is exhausted, it is
skipped conservatively and the parsed media result retains `gop_seconds=None`. Focused final
evidence: `28 passed, 1 deselected` in `tests/unit/test_ffprobe.py`, where the deselection is
the separately established local FFmpeg smoke.
