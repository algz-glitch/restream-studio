#!/usr/bin/env python3
"""Fail-closed validator for a real dual-platform acceptance result."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "acceptance" / "platform-result.schema.json"
TARGETS = ("douyin", "wechat_channels")
FORBIDDEN_FIELD_NAMES = {
    "authorization",
    "cookie",
    "key",
    "password",
    "rtmp_url",
    "server",
    "stream_key",
    "token",
    "url",
}
URL_PATTERN = re.compile(r"(?i)\b(?:https?|rtmps?)://")


class AcceptanceError(ValueError):
    """A sanitized validation failure that never includes submitted values."""


class DuplicateKeyError(ValueError):
    """Raised when JSON contains duplicate object keys."""


def _reject(path: str, reason: str) -> NoReturn:
    raise AcceptanceError(f"{path}: {reason}")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError
        result[key] = value
    return result


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=_pairs_without_duplicates)
    except DuplicateKeyError as exc:
        raise AcceptanceError("result: duplicate JSON object key") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError("result: unreadable or invalid JSON") from exc


def _resolve_ref(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        _reject("schema", "external references are disabled")
    node: Any = root_schema
    for segment in reference[2:].split("/"):
        if not isinstance(node, dict) or segment not in node:
            _reject("schema", "unresolvable local reference")
        node = node[segment]
    if not isinstance(node, dict):
        _reject("schema", "reference does not resolve to an object")
    return node


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    _reject("schema", "unsupported type keyword")


def _parse_utc(value: str, path: str) -> datetime:
    if not value.endswith("Z"):
        _reject(path, "must be a UTC date-time ending in Z")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AcceptanceError(f"{path}: invalid UTC date-time") from exc
    return parsed


def _validate_schema(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
) -> None:
    if "$ref" in schema:
        _validate_schema(value, _resolve_ref(root_schema, schema["$ref"]), root_schema, path)
        return

    if "type" in schema and not _matches_type(value, schema["type"]):
        _reject(path, f"must have type {schema['type']}")
    if "const" in schema and value != schema["const"]:
        _reject(path, "must equal the required value")
    if "enum" in schema and value not in schema["enum"]:
        _reject(path, "must be one of the allowed values")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                _reject(f"{path}.{required}", "required field is missing")
        if schema.get("additionalProperties") is False:
            extras = set(value) - set(properties)
            if extras:
                _reject(path, "contains an unexpected field")
        for key, child in value.items():
            if key in properties:
                _validate_schema(child, properties[key], root_schema, f"{path}.{key}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            _reject(path, "contains too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            _reject(path, "contains too many items")
        if "items" in schema:
            for index, child in enumerate(value):
                _validate_schema(child, schema["items"], root_schema, f"{path}[{index}]")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            _reject(path, "is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _reject(path, "is too long")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            _reject(path, "does not match the required format")
        if schema.get("format") == "date-time":
            _parse_utc(value, path)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            _reject(path, "must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            _reject(path, "is below the minimum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            _reject(path, "must be greater than the exclusive minimum")


def _reject_sensitive_material(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in FORBIDDEN_FIELD_NAMES:
                _reject("result", "contains forbidden credential or URL material")
            _reject_sensitive_material(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_material(child)
    elif isinstance(value, str) and URL_PATTERN.search(value):
        _reject("result", "contains forbidden credential or URL material")


def _cross_validate(result: dict[str, Any]) -> None:
    if result["status"] != "PLATFORM_ACCEPTED":
        _reject("result.status", "acceptance gate requires PLATFORM_ACCEPTED")

    minutes = [sample["minute"] for sample in result["minute_samples"]]
    if minutes != list(range(1, 31)):
        _reject("result.minute_samples", "minutes must be unique and ordered 1 through 30")
    minute_times = [
        _parse_utc(sample["captured_at_utc"], f"result.minute_samples[{index}].captured_at_utc")
        for index, sample in enumerate(result["minute_samples"])
    ]
    for index, (previous, current) in enumerate(pairwise(minute_times), start=1):
        interval = (current - previous).total_seconds()
        if interval <= 0 or interval > 75:
            _reject(
                f"result.minute_samples[{index}].captured_at_utc",
                "must follow the prior sample by no more than 75 seconds",
            )

    isolated_targets = [item["stopped_target"] for item in result["stop_isolation"]]
    if sorted(isolated_targets) != sorted(TARGETS):
        _reject("result.stop_isolation", "must cover each target exactly once")
    for index, item in enumerate(result["stop_isolation"]):
        if _parse_utc(item["resumed_at_utc"], "result.stop_isolation") <= _parse_utc(
            item["stopped_at_utc"], "result.stop_isolation"
        ):
            _reject(f"result.stop_isolation[{index}]", "resume must follow stop")

    interruption = result["source_interruption"]
    interruption_start = _parse_utc(
        interruption["started_at_utc"], "result.source_interruption.started_at_utc"
    )
    interruption_end = _parse_utc(
        interruption["restored_at_utc"], "result.source_interruption.restored_at_utc"
    )
    measured_duration = (interruption_end - interruption_start).total_seconds()
    if measured_duration <= 60:
        _reject("result.source_interruption.duration_seconds", "measured outage must exceed 60")
    if abs(measured_duration - interruption["duration_seconds"]) > 1:
        _reject("result.source_interruption.duration_seconds", "does not match outage timestamps")

    probes = interruption["recovery_probes"]
    if interruption["consecutive_probes"] != len(probes):
        _reject(
            "result.source_interruption.consecutive_probes",
            "must equal the recorded successful recovery probes",
        )
    probe_times = [
        _parse_utc(probe["captured_at_utc"], f"result.source_interruption.recovery_probes[{i}]")
        for i, probe in enumerate(probes)
    ]
    for index, (previous, current) in enumerate(pairwise(probe_times), start=1):
        if (current - previous).total_seconds() < 10:
            _reject(
                f"result.source_interruption.recovery_probes[{index}]",
                "successful probes must be at least 10 seconds apart",
            )

    started = _parse_utc(result["started_at_utc"], "result.started_at_utc")
    completed = _parse_utc(result["completed_at_utc"], "result.completed_at_utc")
    terminal_times = probe_times + [
        _parse_utc(result["rollback"]["verified_at_utc"], "result.rollback.verified_at_utc"),
        _parse_utc(
            result["signoff"]["account_holder"]["signed_at_utc"],
            "result.signoff.account_holder.signed_at_utc",
        ),
        _parse_utc(
            result["signoff"]["acceptance_verifier"]["signed_at_utc"],
            "result.signoff.acceptance_verifier.signed_at_utc",
        ),
    ]
    if completed <= started or any(timestamp > completed for timestamp in terminal_times):
        _reject("result.completed_at_utc", "must follow all required acceptance evidence")


def validate(result: Any, schema: Any) -> None:
    if not isinstance(schema, dict):
        _reject("schema", "root must be an object")
    _validate_schema(result, schema, schema, "result")
    if not isinstance(result, dict):
        _reject("result", "root must be an object")
    _reject_sensitive_material(result)
    _cross_validate(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a platform acceptance result without printing submitted data."
    )
    parser.add_argument("result", type=Path, help="Path to the local result.json")
    args = parser.parse_args(argv)

    try:
        schema = _read_json(SCHEMA_PATH)
        result = _read_json(args.result)
        validate(result, schema)
    except AcceptanceError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 1

    print("PLATFORM_ACCEPTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
