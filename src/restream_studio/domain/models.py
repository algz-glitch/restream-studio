from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime
from enum import StrEnum


class SourceState(StrEnum):
    STOPPED = "STOPPED"
    MONITORING = "MONITORING"
    SOURCE_OFFLINE = "SOURCE_OFFLINE"
    STARTING = "STARTING"
    LIVE = "LIVE"
    RECONNECTING = "RECONNECTING"
    STANDBY = "STANDBY"
    ERROR = "ERROR"


class OutputState(StrEnum):
    DISABLED = "DISABLED"
    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    LIVE = "LIVE"
    RECONNECTING = "RECONNECTING"
    AUTH_FAILED = "AUTH_FAILED"
    ERROR = "ERROR"


class DestinationKind(StrEnum):
    DOUYIN = "DOUYIN"
    WECHAT = "WECHAT"
    LOCAL_TEST = "LOCAL_TEST"


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True, slots=True)
class ResolvedStream:
    url: str = dataclass_field(repr=False)
    acquired_at: datetime
    expires_at: datetime | None
    room_id: str | None = None
    anchor_name: str | None = None
    is_live: bool = True
    selected_quality: str | None = None
    flv_urls: tuple[str, ...] = dataclass_field(default=(), repr=False)
    hls_urls: tuple[str, ...] = dataclass_field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not _is_timezone_aware(self.acquired_at):
            raise ValueError("acquired_at must be timezone-aware")
        if self.expires_at is not None:
            if not _is_timezone_aware(self.expires_at):
                raise ValueError("expires_at must be timezone-aware")
            if self.expires_at <= self.acquired_at:
                raise ValueError("expires_at must be after acquired_at")


@dataclass(frozen=True, slots=True)
class MediaProbe:
    video_codec: str
    audio_codec: str
    width: int
    height: int
    frame_rate: float
    pixel_format: str = ""
    audio_sample_rate: int = 0
    audio_channels: int = 0


@dataclass(frozen=True, slots=True)
class OutputMetrics:
    bitrate_kbps: int
    dropped_frames: int
    reconnect_count: int


@dataclass(frozen=True, slots=True)
class OutputStates:
    douyin: OutputState
    wechat: OutputState
    local_test: OutputState

    def with_state(self, destination: DestinationKind, state: OutputState) -> "OutputStates":
        field_by_destination = {
            DestinationKind.DOUYIN: "douyin",
            DestinationKind.WECHAT: "wechat",
            DestinationKind.LOCAL_TEST: "local_test",
        }
        return replace(self, **{field_by_destination[destination]: state})
