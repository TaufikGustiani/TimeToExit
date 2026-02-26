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

