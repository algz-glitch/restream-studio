from collections.abc import Mapping

from restream_studio.domain.models import SourceState


class InvalidTransition(ValueError):
    def __init__(self, source: SourceState, target: SourceState) -> None:
        self.source = source
        self.target = target
        super().__init__(f"Illegal source transition: {source.value} -> {target.value}")


SOURCE_TRANSITIONS: Mapping[SourceState, frozenset[SourceState]] = {
    SourceState.STOPPED: frozenset({SourceState.MONITORING}),
    SourceState.MONITORING: frozenset(
        {SourceState.SOURCE_OFFLINE, SourceState.STARTING, SourceState.STOPPED, SourceState.ERROR}
    ),
    SourceState.SOURCE_OFFLINE: frozenset(
        {SourceState.MONITORING, SourceState.STARTING, SourceState.STOPPED, SourceState.ERROR}
    ),
    SourceState.STARTING: frozenset(
        {SourceState.LIVE, SourceState.RECONNECTING, SourceState.STANDBY, SourceState.ERROR}
    ),
    SourceState.LIVE: frozenset(
        {SourceState.RECONNECTING, SourceState.STANDBY, SourceState.STOPPED, SourceState.ERROR}
    ),
    SourceState.RECONNECTING: frozenset(
        {SourceState.LIVE, SourceState.STANDBY, SourceState.STOPPED, SourceState.ERROR}
    ),
    SourceState.STANDBY: frozenset(
        {SourceState.STARTING, SourceState.STOPPED, SourceState.ERROR}
    ),
    SourceState.ERROR: frozenset({SourceState.MONITORING, SourceState.STOPPED}),
}


class SourceStateMachine:
    def __init__(self, state: SourceState = SourceState.STOPPED) -> None:
        self._state = state

    @property
    def state(self) -> SourceState:
        return self._state

    def transition(self, target: SourceState) -> None:
        if target not in SOURCE_TRANSITIONS[self._state]:
            raise InvalidTransition(self._state, target)
        self._state = target
