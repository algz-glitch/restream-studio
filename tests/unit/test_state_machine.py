from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from restream_studio.domain.models import (
    DestinationKind,
    MediaProbe,
    OutputMetrics,
    OutputState,
    OutputStates,
    ResolvedStream,
    SourceState,
)
from restream_studio.domain.state_machine import InvalidTransition, SourceStateMachine


def test_domain_enums_have_stable_string_values() -> None:
    assert [state.value for state in SourceState] == [
        "STOPPED",
        "MONITORING",
        "SOURCE_OFFLINE",
        "STARTING",
        "LIVE",
        "RECONNECTING",
        "STANDBY",
        "ERROR",
    ]
    assert [state.value for state in OutputState] == [
        "DISABLED",
        "STOPPED",
        "CONNECTING",
        "LIVE",
        "RECONNECTING",
        "AUTH_FAILED",
        "ERROR",
    ]
    assert [kind.value for kind in DestinationKind] == ["DOUYIN", "WECHAT", "LOCAL_TEST"]


def test_source_recovers_from_standby() -> None:
    machine = SourceStateMachine(SourceState.STANDBY)

    machine.transition(SourceState.STARTING)
    machine.transition(SourceState.LIVE)

    assert machine.state is SourceState.LIVE


def test_illegal_source_transition_is_rejected() -> None:
    machine = SourceStateMachine(SourceState.STOPPED)

    with pytest.raises(InvalidTransition) as raised:
        machine.transition(SourceState.LIVE)

    assert raised.value.source is SourceState.STOPPED
    assert raised.value.target is SourceState.LIVE
    assert str(raised.value) == "Illegal source transition: STOPPED -> LIVE"
    assert machine.state is SourceState.STOPPED


def test_same_source_state_transition_is_rejected() -> None:
    machine = SourceStateMachine(SourceState.MONITORING)

    with pytest.raises(InvalidTransition, match="MONITORING -> MONITORING"):
        machine.transition(SourceState.MONITORING)


def test_output_states_are_independent() -> None:
    states = OutputStates(
        douyin=OutputState.CONNECTING,
        wechat=OutputState.STOPPED,
        local_test=OutputState.DISABLED,
    )

    updated = states.with_state(DestinationKind.DOUYIN, OutputState.LIVE)

    assert updated.douyin is OutputState.LIVE
    assert updated.wechat is OutputState.STOPPED
    assert updated.local_test is OutputState.DISABLED
    assert states.douyin is OutputState.CONNECTING


def test_domain_records_are_frozen_and_validate_stream_times() -> None:
    acquired_at = datetime(2026, 9, 10, tzinfo=UTC)
    stream = ResolvedStream(
        url="https://example.test/live.m3u8",
        acquired_at=acquired_at,
        expires_at=acquired_at + timedelta(minutes=5),
    )
    probe = MediaProbe(
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        frame_rate=30.0,
    )
    metrics = OutputMetrics(bitrate_kbps=4500, dropped_frames=0, reconnect_count=0)

    with pytest.raises(FrozenInstanceError):
        stream.url = "https://example.test/other.m3u8"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        probe.width = 1280  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        metrics.dropped_frames = 1  # type: ignore[misc]

    with pytest.raises(ValueError, match="timezone-aware"):
        ResolvedStream(
            url="https://example.test/live.m3u8",
            acquired_at=datetime.fromisoformat("2026-09-10T00:00:00"),
            expires_at=None,
        )

    with pytest.raises(ValueError, match="after acquired_at"):
        ResolvedStream(
            url="https://example.test/live.m3u8",
            acquired_at=acquired_at,
            expires_at=acquired_at - timedelta(seconds=1),
        )
