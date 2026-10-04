"""Device state: evaluation from facts, and the allowed transitions.

    INITIALIZING -> READY | DEGRADED | OFFLINE | UPDATING | ERROR | MAINTENANCE
    READY, DEGRADED, OFFLINE  <-> each other, -> UPDATING | ERROR | MAINTENANCE
    UPDATING     -> READY | DEGRADED | OFFLINE | ERROR
    ERROR        -> INITIALIZING | MAINTENANCE   (recovery always re-initialises)
    MAINTENANCE  -> INITIALIZING

Precedence when evaluating: ERROR (identity or configuration unusable) >
MAINTENANCE > UPDATING > OFFLINE > DEGRADED > READY. OFFLINE never affects
local work: Windows applications, the CLI and the Compatibility Manager keep
working without a Control Plane.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .errors import AgentError
from .models import DeviceState

S = DeviceState
TRANSITIONS: dict[DeviceState, frozenset] = {
    S.INITIALIZING: frozenset({S.READY, S.DEGRADED, S.OFFLINE, S.UPDATING, S.ERROR, S.MAINTENANCE}),
    S.READY: frozenset({S.DEGRADED, S.OFFLINE, S.UPDATING, S.ERROR, S.MAINTENANCE, S.INITIALIZING}),
    S.DEGRADED: frozenset({S.READY, S.OFFLINE, S.UPDATING, S.ERROR, S.MAINTENANCE, S.INITIALIZING}),
    S.OFFLINE: frozenset({S.READY, S.DEGRADED, S.UPDATING, S.ERROR, S.MAINTENANCE, S.INITIALIZING}),
    S.UPDATING: frozenset({S.READY, S.DEGRADED, S.OFFLINE, S.ERROR, S.INITIALIZING}),
    S.ERROR: frozenset({S.INITIALIZING, S.MAINTENANCE}),
    S.MAINTENANCE: frozenset({S.INITIALIZING}),
}


class InvalidTransition(AgentError):
    code = "INVALID_STATE_TRANSITION"


@dataclass
class Facts:
    identity_ok: bool = True
    config_ok: bool = True
    maintenance: bool = False
    updating: bool = False
    runtime_healthy: bool | None = None      # None: not checked yet
    inventory_ok: bool | None = None
    compliance_failed: bool = False
    offline: bool = False                    # enrolled and the Control Plane unreachable
    problems: list[str] = field(default_factory=list)


def evaluate(f: Facts) -> tuple[DeviceState, list[str]]:
    """Target state and the reasons for it."""
    reasons = list(f.problems)
    if not f.identity_ok or not f.config_ok:
        return S.ERROR, reasons or ["identity or configuration unusable"]
    if f.maintenance:
        return S.MAINTENANCE, ["maintenance mode set by a local administrator (remote commands paused)"]
    if f.updating:
        return S.UPDATING, ["an agent or application update is in progress"]
    if f.offline:
        return S.OFFLINE, ["the Control Plane is unreachable; local applications keep working"]
    degraded = []
    if f.runtime_healthy is False:
        degraded.append("the Windows compatibility runtime needs attention (Wine, bubblewrap or AppArmor)")
    if f.inventory_ok is False:
        degraded.append("inventory collection failed")
    if f.compliance_failed:
        degraded.append("a security posture check failed")
    if degraded:
        return S.DEGRADED, reasons + degraded
    return S.READY, reasons


@dataclass
class Transition:
    at: float
    old: DeviceState
    new: DeviceState
    reasons: tuple[str, ...]


class StateMachine:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.state = S.INITIALIZING
        self.reasons: list[str] = ["starting"]
        self.since = clock()
        self.history: deque[Transition] = deque(maxlen=50)

    def transition(self, new: DeviceState, reasons: list[str] | None = None) -> None:
        if new not in TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.state.value} -> {new.value} is not an allowed transition")
        self.history.append(Transition(self.clock(), self.state, new, tuple(reasons or ())))
        self.state, self.reasons, self.since = new, list(reasons or []), self.clock()

    def move_to(self, target: DeviceState, reasons: list[str]) -> bool:
        """Reach target by an allowed path (through INITIALIZING if needed). True if the state changed."""
        if target == self.state:
            self.reasons = list(reasons)
            return False
        if target not in TRANSITIONS[self.state]:
            self.transition(S.INITIALIZING, ["re-initialising"])
            if target == S.INITIALIZING:
                return True
        self.transition(target, reasons)
        return True

    def to_dict(self) -> dict:
        return {"state": self.state.value, "reasons": list(self.reasons),
                "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.since))}
