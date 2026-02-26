"""
TimeToExit — Bear market indicator and exit-assist engine for dashboards.
Domain anchor: 0x7e2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8e0f2a4
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# -----------------------------------------------------------------------------
# CONSTANTS (unique to TimeToExit)
# -----------------------------------------------------------------------------

BPS_DENOM = 10000
MAX_INDICATORS = 16
MAX_SEVERITY = 5
MAX_DRAWDOWN_BPS = 10000
MAX_SNAPSHOTS = 50000
MAX_SIGNALS = 2000
BATCH_SIZE = 100
DOMAIN_SALT = 0x7e2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8e0f2a4

# -----------------------------------------------------------------------------
# EXCEPTIONS
# -----------------------------------------------------------------------------


class TimeToExitError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class TTE_ZeroAddress(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_ZeroAddress", "Address cannot be zero")


class TTE_ZeroAmount(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_ZeroAmount", "Amount cannot be zero")


class TTE_Halted(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_Halted", "System is halted")


class TTE_NotGuardian(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_NotGuardian", "Caller is not guardian")


class TTE_NotReporter(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_NotReporter", "Caller is not reporter")


class TTE_DrawdownOutOfRange(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_DrawdownOutOfRange", "Drawdown out of range")


class TTE_IndicatorOutOfRange(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_IndicatorOutOfRange", "Indicator value out of range")


class TTE_SeverityOutOfRange(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_SeverityOutOfRange", "Severity out of range")


class TTE_ThresholdInvalid(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_ThresholdInvalid", "Threshold invalid")


class TTE_SnapshotNotFound(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_SnapshotNotFound", "Snapshot not found")


class TTE_MaxSnapshotsReached(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_MaxSnapshotsReached", "Max snapshots reached")


class TTE_ArrayLengthMismatch(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_ArrayLengthMismatch", "Array length mismatch")


class TTE_BatchTooLarge(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_BatchTooLarge", "Batch too large")


class TTE_InvalidIndicatorId(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_InvalidIndicatorId", "Invalid indicator id")


class TTE_SignalNotFound(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_SignalNotFound", "Signal not found")


class TTE_AdvisoryNotFound(TimeToExitError):
    def __init__(self):
        super().__init__("TTE_AdvisoryNotFound", "Advisory not found")

# -----------------------------------------------------------------------------
# ENUMS
# -----------------------------------------------------------------------------


class ExitAction(IntEnum):
    HOLD = 0
    REDUCE = 1
    EXIT = 2


# -----------------------------------------------------------------------------
# DATA STRUCTURES
# -----------------------------------------------------------------------------


@dataclass
class DrawdownSnapshot:
    snapshot_id: int
    reporter: str
    drawdown_bps: int
    peak_value: int
    current_value: int
    at_block: int
    at_time: float = field(default_factory=time.time)


@dataclass
class ExitSignal:
    signal_id: int
    indicator_id: int
    value: int
    threshold: int
    label_hash: bytes
    at_block: int
    at_time: float = field(default_factory=time.time)


@dataclass
class ExitAdvisory:
    advisory_id: int
    author: str
    severity: int
    at_block: int
    at_time: float = field(default_factory=time.time)


# -----------------------------------------------------------------------------
# CORE ENGINE
# -----------------------------------------------------------------------------


class TimeToExitEngine:
    def __init__(
        self,
        guardian_address: str = "0x3c5e7a9b1d4f6a8c0e2a4b6c8d0e2f4a6b8c0d2e",
        reporter_address: str = "0x6f2b4d8a0c2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a",
        treasury_address: str = "0x9a1c3e5b7d9f1a3b5c7d9e1f3a5b7c9d1e3f5a7b",
    ):
        if not guardian_address or not reporter_address or not treasury_address:
            raise TTE_ZeroAddress()
        self._guardian = guardian_address
        self._reporter = reporter_address
        self._treasury = treasury_address
        self._halted = False
        self._drawdown_threshold_bps = 1500
        self._snapshot_counter = 0
        self._signal_counter = 0
        self._advisory_counter = 0
        self._treasury_balance = 0
        self._reporter_fee_wei = 0
        self._snapshots: Dict[int, DrawdownSnapshot] = {}
        self._signals: Dict[int, ExitSignal] = {}
        self._advisories: Dict[int, ExitAdvisory] = {}
        self._snapshot_ids: List[int] = []
        self._signal_ids: List[int] = []
        self._advisory_ids: List[int] = []
        self._latest_indicator_value: Dict[int, int] = {i: 0 for i in range(MAX_INDICATORS)}
        self._indicator_threshold: Dict[int, int] = {i: 0 for i in range(MAX_INDICATORS)}
        self._lock = threading.RLock()

    def _require_not_halted(self) -> None:
        if self._halted:
            raise TTE_Halted()

    def _require_guardian(self, caller: str) -> None:
        if caller != self._guardian:
            raise TTE_NotGuardian()

    def _require_reporter(self, caller: str) -> None:
        if caller != self._reporter:
            raise TTE_NotReporter()

    @property
    def guardian(self) -> str:
        return self._guardian

    @property
    def reporter(self) -> str:
        return self._reporter

    @property
    def treasury(self) -> str:
        return self._treasury

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def drawdown_threshold_bps(self) -> int:
        return self._drawdown_threshold_bps

    def set_halted(self, halted: bool, caller: str) -> None:
        self._require_guardian(caller)
        self._halted = halted

    def set_guardian(self, new_guardian: str, caller: str) -> None:
        if caller != self._guardian:
            raise TTE_NotGuardian()
        if not new_guardian:
            raise TTE_ZeroAddress()
        self._guardian = new_guardian

    def set_reporter(self, new_reporter: str, caller: str) -> None:
        if caller != self._guardian:
            raise TTE_NotGuardian()
        if not new_reporter:
            raise TTE_ZeroAddress()
        self._reporter = new_reporter

    def set_drawdown_threshold_bps(self, new_bps: int, caller: str) -> None:
        self._require_guardian(caller)
        if new_bps > MAX_DRAWDOWN_BPS:
            raise TTE_ThresholdInvalid()
        self._drawdown_threshold_bps = new_bps

    def update_indicator(self, indicator_id: int, value: int, caller: str) -> None:
        self._require_reporter(caller)
        self._require_not_halted()
        if indicator_id < 0 or indicator_id >= MAX_INDICATORS:
            raise TTE_InvalidIndicatorId()
        self._latest_indicator_value[indicator_id] = value

    def set_indicator_threshold(self, indicator_id: int, threshold: int, caller: str) -> None:
        self._require_guardian(caller)
        if indicator_id < 0 or indicator_id >= MAX_INDICATORS:
            raise TTE_InvalidIndicatorId()
        self._indicator_threshold[indicator_id] = threshold

    def record_drawdown(
        self,
        drawdown_bps: int,
        peak_value: int,
        current_value: int,
        caller: str,
        at_block: int = 0,
    ) -> int:
        self._require_reporter(caller)
        self._require_not_halted()
        if drawdown_bps > MAX_DRAWDOWN_BPS:
            raise TTE_DrawdownOutOfRange()
        if len(self._snapshot_ids) >= MAX_SNAPSHOTS:
            raise TTE_MaxSnapshotsReached()
        at_block = at_block or int(time.time() // 12)
        with self._lock:
            self._snapshot_counter += 1
            sid = self._snapshot_counter
            self._snapshots[sid] = DrawdownSnapshot(
                snapshot_id=sid,
                reporter=caller,
                drawdown_bps=drawdown_bps,
                peak_value=peak_value,
                current_value=current_value,
                at_block=at_block,
            )
            self._snapshot_ids.append(sid)
            if drawdown_bps >= self._drawdown_threshold_bps and len(self._signal_ids) < MAX_SIGNALS:
                self._signal_counter += 1
                sig_id = self._signal_counter
                self._signals[sig_id] = ExitSignal(
                    signal_id=sig_id,
                    indicator_id=0,
                    value=drawdown_bps,
                    threshold=self._drawdown_threshold_bps,
                    label_hash=hashlib.sha256(b"TimeToExit.drawdown").digest()[:32],
                    at_block=at_block,
                )
                self._signal_ids.append(sig_id)
        return sid

    def raise_exit_signal(
        self,
        indicator_id: int,
        value: int,
        threshold: int,
        label_hash: bytes,
        caller: str,
        at_block: int = 0,
    ) -> int:
        self._require_reporter(caller)
        self._require_not_halted()
        if indicator_id < 0 or indicator_id >= MAX_INDICATORS:
            raise TTE_InvalidIndicatorId()
        if len(self._signal_ids) >= MAX_SIGNALS:
            raise TimeToExitError("TTE_MaxSignalsReached", "Max signals reached")
        at_block = at_block or int(time.time() // 12)
        with self._lock:
            self._signal_counter += 1
            sig_id = self._signal_counter
            self._signals[sig_id] = ExitSignal(
                signal_id=sig_id,
                indicator_id=indicator_id,
                value=value,
                threshold=threshold,
                label_hash=label_hash[:32] if len(label_hash) >= 32 else label_hash.ljust(32, b"\0"),
                at_block=at_block,
            )
            self._signal_ids.append(sig_id)
        return sig_id

    def post_exit_advisory(self, severity: int, caller: str, at_block: int = 0) -> int:
        self._require_reporter(caller)
        self._require_not_halted()
        if severity < 0 or severity > MAX_SEVERITY:
            raise TTE_SeverityOutOfRange()
        at_block = at_block or int(time.time() // 12)
        with self._lock:
            self._advisory_counter += 1
            aid = self._advisory_counter
            self._advisories[aid] = ExitAdvisory(
                advisory_id=aid,
                author=caller,
                severity=severity,
                at_block=at_block,
            )
            self._advisory_ids.append(aid)
        return aid

    def get_snapshot(self, snapshot_id: int) -> DrawdownSnapshot:
        if snapshot_id not in self._snapshots:
            raise TTE_SnapshotNotFound()
        return self._snapshots[snapshot_id]

    def get_signal(self, signal_id: int) -> ExitSignal:
        if signal_id not in self._signals:
            raise TTE_SignalNotFound()
        return self._signals[signal_id]

    def get_advisory(self, advisory_id: int) -> ExitAdvisory:
        if advisory_id not in self._advisories:
            raise TTE_AdvisoryNotFound()
        return self._advisories[advisory_id]

    def snapshot_count(self) -> int:
        return len(self._snapshot_ids)

    def signal_count(self) -> int:
        return len(self._signal_ids)

    def advisory_count(self) -> int:
        return len(self._advisory_ids)

    def is_exit_signal_active(self) -> bool:
        if not self._signal_ids:
            return False
        last_id = self._signal_ids[-1]
        s = self._signals[last_id]
        return s.value >= self._drawdown_threshold_bps

    def recent_signals(self, limit: int) -> List[Tuple[int, int, int]]:
        n = len(self._signal_ids)
        limit = min(limit, n)
        if limit == 0:
            return []
        out = []
        for i in range(limit):
            idx = n - 1 - i
            sid = self._signal_ids[idx]
            s = self._signals[sid]
            out.append((sid, s.value, s.at_block))
        return out

    def recent_drawdowns(self, limit: int) -> List[Tuple[int, int, int]]:
        n = len(self._snapshot_ids)
        limit = min(limit, n)
        if limit == 0:
            return []
        out = []
        for i in range(limit):
            idx = n - 1 - i
            sid = self._snapshot_ids[idx]
            sn = self._snapshots[sid]
            out.append((sid, sn.drawdown_bps, sn.at_block))
        return out

    def average_drawdown_bps(self, last_n: int) -> int:
        n = len(self._snapshot_ids)
        if n == 0 or last_n == 0:
            return 0
        last_n = min(last_n, n)
        total = sum(
            self._snapshots[self._snapshot_ids[n - 1 - i]].drawdown_bps
            for i in range(last_n)
        )
        return total // last_n

    def max_drawdown_bps(self, last_n: int) -> int:
        n = len(self._snapshot_ids)
        if n == 0:
            return 0
        last_n = min(last_n, n)
        return max(
            self._snapshots[self._snapshot_ids[n - 1 - i]].drawdown_bps
            for i in range(last_n)
        )

    def exit_readiness_bps(self) -> int:
        if not self._snapshot_ids or self._drawdown_threshold_bps == 0:
            return 0
        latest_bps = self._snapshots[self._snapshot_ids[-1]].drawdown_bps
        bps = (latest_bps * BPS_DENOM) // self._drawdown_threshold_bps
        return min(bps, BPS_DENOM)

    def should_exit(self) -> bool:
        if not self._snapshot_ids:
            return False
        latest_bps = self._snapshots[self._snapshot_ids[-1]].drawdown_bps
        return latest_bps >= self._drawdown_threshold_bps

    def recommended_action(self) -> Tuple[int, int]:
        if not self._snapshot_ids:
            return (ExitAction.HOLD, 0)
        latest_bps = self._snapshots[self._snapshot_ids[-1]].drawdown_bps
        if latest_bps >= self._drawdown_threshold_bps:
            action = ExitAction.EXIT
            confidence = min(
                BPS_DENOM,
                (latest_bps * BPS_DENOM) // self._drawdown_threshold_bps,
            )
        else:
            action = ExitAction.REDUCE
            confidence = (latest_bps * BPS_DENOM) // self._drawdown_threshold_bps
        return (int(action), confidence)

    def get_drawdown_stats(self) -> Dict[str, Any]:
        total_snapshots = len(self._snapshot_ids)
        total_signals = len(self._signal_ids)
        total_advisories = len(self._advisory_ids)
        latest_bps = 0
        if total_snapshots > 0:
            latest_bps = self._snapshots[self._snapshot_ids[-1]].drawdown_bps
        return {
            "total_snapshots": total_snapshots,
            "total_signals": total_signals,
            "total_advisories": total_advisories,
            "current_threshold_bps": self._drawdown_threshold_bps,
            "latest_drawdown_bps": latest_bps,
        }

    def get_indicator_snapshot(self) -> List[int]:
        return [self._latest_indicator_value[i] for i in range(MAX_INDICATORS)]

    def get_indicator_threshold(self, indicator_id: int) -> int:
        if 0 <= indicator_id < MAX_INDICATORS:
            return self._indicator_threshold[indicator_id]
        return 0

    def get_dashboard_payload(self) -> Dict[str, Any]:
        snap_count = len(self._snapshot_ids)
        sig_count = len(self._signal_ids)
        adv_count = len(self._advisory_ids)
        thresh_bps = self._drawdown_threshold_bps
        latest_bps = 0
        exit_flag = False
        avg_bps_10 = 0
        max_bps_10 = 0
        if snap_count > 0:
            latest_bps = self._snapshots[self._snapshot_ids[-1]].drawdown_bps
            exit_flag = latest_bps >= thresh_bps
            n10 = min(10, snap_count)
            avg_bps_10 = self.average_drawdown_bps(n10)
            max_bps_10 = self.max_drawdown_bps(n10)
        exit_readiness = self.exit_readiness_bps()
        return {
            "snap_count": snap_count,
            "sig_count": sig_count,
            "adv_count": adv_count,
            "latest_bps": latest_bps,
            "thresh_bps": thresh_bps,
            "exit_flag": exit_flag,
            "avg_bps_10": avg_bps_10,
            "max_bps_10": max_bps_10,
            "exit_readiness": exit_readiness,
        }

    def record_drawdown_batch(
        self,
        drawdown_bps_list: List[int],
        peak_values: List[int],
        current_values: List[int],
        caller: str,
        at_block: int = 0,
    ) -> List[int]:
        self._require_reporter(caller)
        self._require_not_halted()
        n = len(drawdown_bps_list)
        if n == 0 or n > BATCH_SIZE or len(peak_values) != n or len(current_values) != n:
            raise TTE_ArrayLengthMismatch()
        if len(self._snapshot_ids) + n > MAX_SNAPSHOTS:
            raise TTE_MaxSnapshotsReached()
        at_block = at_block or int(time.time() // 12)
        snapshot_ids = []
        with self._lock:
            for i in range(n):
                if drawdown_bps_list[i] > MAX_DRAWDOWN_BPS:
                    raise TTE_DrawdownOutOfRange()
                self._snapshot_counter += 1
                sid = self._snapshot_counter
                self._snapshots[sid] = DrawdownSnapshot(
                    snapshot_id=sid,
                    reporter=caller,
                    drawdown_bps=drawdown_bps_list[i],
                    peak_value=peak_values[i],
                    current_value=current_values[i],
                    at_block=at_block,
                )
                self._snapshot_ids.append(sid)
                snapshot_ids.append(sid)
        return snapshot_ids

    def get_snapshot_ids_paginated(self, offset: int, limit: int) -> List[int]:
        total = len(self._snapshot_ids)
        if offset >= total:
            return []
        limit = min(limit, total - offset)
        return [self._snapshot_ids[offset + i] for i in range(limit)]

    def get_signal_ids_paginated(self, offset: int, limit: int) -> List[int]:
        total = len(self._signal_ids)
        if offset >= total:
            return []
        limit = min(limit, total - offset)
        return [self._signal_ids[offset + i] for i in range(limit)]

    def get_advisory_ids_paginated(self, offset: int, limit: int) -> List[int]:
        total = len(self._advisory_ids)
        if offset >= total:
            return []
        limit = min(limit, total - offset)
        return [self._advisory_ids[offset + i] for i in range(limit)]

    def drawdown_trend(self, last_n: int) -> int:
        n = len(self._snapshot_ids)
        if n < 2 or last_n < 2:
            return 0
        last_n = min(last_n, n)
        first_bps = self._snapshots[self._snapshot_ids[n - last_n]].drawdown_bps
        last_bps = self._snapshots[self._snapshot_ids[-1]].drawdown_bps
        return last_bps - first_bps

    def severity_distribution(self) -> List[int]:
        counts = [0] * (MAX_SEVERITY + 1)
        for aid in self._advisory_ids:
            a = self._advisories[aid]
            if 0 <= a.severity <= MAX_SEVERITY:
                counts[a.severity] += 1
        return counts

    def count_indicators_above_threshold(self) -> int:
        count = 0
        for i in range(MAX_INDICATORS):
            if self._indicator_threshold[i] > 0 and self._latest_indicator_value[i] >= self._indicator_threshold[i]:
                count += 1
        return count

    def breached_indicators(self) -> List[int]:
        out = []
        for i in range(MAX_INDICATORS):
            if self._indicator_threshold[i] > 0 and self._latest_indicator_value[i] >= self._indicator_threshold[i]:
                out.append(i)
        return out

    def get_drawdown_series(self, n: int) -> List[int]:
        total = len(self._snapshot_ids)
        if total == 0:
            return []
        n = min(n, total)
        return [
            self._snapshots[self._snapshot_ids[total - 1 - i]].drawdown_bps
            for i in range(n)
        ]

    def get_signal_value_series(self, n: int) -> List[int]:
        total = len(self._signal_ids)
        if total == 0:
            return []
        n = min(n, total)
        return [
            self._signals[self._signal_ids[total - 1 - i]].value
            for i in range(n)
        ]


# -----------------------------------------------------------------------------
# CLI / RUNNER
# -----------------------------------------------------------------------------


def main() -> None:
    guardian = "0x3c5e7a9b1d4f6a8c0e2a4b6c8d0e2f4a6b8c0d2e"
    reporter = "0x6f2b4d8a0c2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a"
    treasury = "0x9a1c3e5b7d9f1a3b5c7d9e1f3a5b7c9d1e3f5a7b"
    engine = TimeToExitEngine(guardian, reporter, treasury)
    engine.set_drawdown_threshold_bps(1500, guardian)
    sid = engine.record_drawdown(1200, 10000, 8800, reporter)
    print("Snapshot id:", sid)
    sig_id = engine.raise_exit_signal(1, 1800, 1500, b"bear_volume", reporter)
    print("Signal id:", sig_id)
    aid = engine.post_exit_advisory(3, reporter)
    print("Advisory id:", aid)
    payload = engine.get_dashboard_payload()
    print("Dashboard:", payload)
    print("Should exit:", engine.should_exit())
    print("Exit readiness bps:", engine.exit_readiness_bps())
    action, confidence = engine.recommended_action()
    print("Recommended action:", action, "confidence:", confidence)


# -----------------------------------------------------------------------------
# CONFIG LOADER
# -----------------------------------------------------------------------------


class TimeToExitConfig:
    DEFAULT_THRESHOLD_BPS = 1500
    DEFAULT_GUARDIAN = "0x3c5e7a9b1d4f6a8c0e2a4b6c8d0e2f4a6b8c0d2e"
    DEFAULT_REPORTER = "0x6f2b4d8a0c2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a"
    DEFAULT_TREASURY = "0x9a1c3e5b7d9f1a3b5c7d9e1f3a5b7c9d1e3f5a7b"
    RATE_LIMIT_PER_MINUTE = 60
    LOG_RETENTION_SNAPSHOTS = 50000


# -----------------------------------------------------------------------------
# EVENT LOG (in-memory)
# -----------------------------------------------------------------------------


class TimeToExitEventLog:
    def __init__(self, max_events: int = 10000):
        self._events: List[Tuple[str, Dict[str, Any], float]] = []
        self._max = max_events
        self._lock = threading.Lock()

    def emit(self, kind: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._events.append((kind, payload, time.time()))
            while len(self._events) > self._max:
                self._events.pop(0)

    def recent(self, n: int) -> List[Tuple[str, Dict[str, Any], float]]:
        with self._lock:
            if n >= len(self._events):
                return list(self._events)
            return list(self._events[-n:])


# -----------------------------------------------------------------------------
# VALIDATION HELPERS
# -----------------------------------------------------------------------------


def validate_address(addr: str) -> bool:
    if not addr or not isinstance(addr, str):
        return False
    addr = addr.strip().lower()
    if not addr.startswith("0x") or len(addr) != 42:
        return False
    return all(c in "0123456789abcdef" for c in addr[2:])


def validate_drawdown_bps(bps: int) -> bool:
    return 0 <= bps <= MAX_DRAWDOWN_BPS


def validate_severity(sev: int) -> bool:
    return 0 <= sev <= MAX_SEVERITY


def validate_indicator_id(iid: int) -> bool:
    return 0 <= iid < MAX_INDICATORS


# -----------------------------------------------------------------------------
# SERIALIZATION HELPERS
# -----------------------------------------------------------------------------


def snapshot_to_dict(s: DrawdownSnapshot) -> Dict[str, Any]:
    return {
        "snapshot_id": s.snapshot_id,
        "reporter": s.reporter,
        "drawdown_bps": s.drawdown_bps,
        "peak_value": s.peak_value,
        "current_value": s.current_value,
        "at_block": s.at_block,
        "at_time": s.at_time,
    }

