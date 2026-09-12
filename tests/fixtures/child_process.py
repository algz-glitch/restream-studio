"""Deterministic subprocess used by output-supervisor integration tests."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=(
            "healthy",
            "ignore-stop",
            "spawn-descendant-ignore-stop",
            "exit",
            "auth-fail",
            "malformed-metrics",
        ),
    )
    parser.add_argument("--code", type=int, default=17)
    parser.add_argument("--secret", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--lines", type=int, default=0)
    parser.add_argument("--line-length", type=int, default=0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.mode == "exit":
        for index in range(args.lines):
            print(f"line-{index}:" + ("x" * args.line_length) + args.secret, file=sys.stderr)
        return int(args.code)
    if args.mode == "auth-fail":
        print(f"{args.message} {args.secret}", file=sys.stderr)
        for index in range(args.lines):
            print(f"post-auth-line-{index}", file=sys.stderr)
        return int(args.code)
    if args.mode == "malformed-metrics":
        print("unknown=" + ("x" * 10_000), file=sys.stderr)
        print("fps=nan", file=sys.stderr)
        print("fps=999999999", file=sys.stderr)
        print("bitrate=infkbits/s", file=sys.stderr)
        print("speed=-1x", file=sys.stderr)
        print("out_time=99:99:99", file=sys.stderr)
        print("progress=forever", file=sys.stderr)
        return int(args.code)

    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    if args.mode == "healthy":
        signal.signal(signal.SIGTERM, request_stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, request_stop)
    else:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)

    if args.mode == "spawn-descendant-ignore-stop":
        descendant = subprocess.Popen(
            [sys.executable, __file__, "ignore-stop"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        print(f"descendant_pid={descendant.pid}", file=sys.stderr, flush=True)

    print("fps=29.97", file=sys.stderr, flush=True)
    print("bitrate=4123.5kbits/s", file=sys.stderr, flush=True)
    print("speed=1.02x", file=sys.stderr, flush=True)
    print("out_time=00:01:02.500000", file=sys.stderr, flush=True)
    print("progress=continue", file=sys.stderr, flush=True)
    while not stopping:
        time.sleep(0.02)
    print("progress=end", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
