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
