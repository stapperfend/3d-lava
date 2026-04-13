"""
history.py — In-memory time-series ring buffer for process data.
Thread-safe, no persistence — data lives only for the current session.
"""
import threading
import time as _time
from collections import deque

# 6 hours at 0.5 s polling interval
MAX_SAMPLES = 43_200

_lock   = threading.Lock()
_buffer: deque = deque(maxlen=MAX_SAMPLES)


def append(sample: dict) -> None:
    """Add a sample dict (must contain at least 't' for timestamp)."""
    with _lock:
        _buffer.append(sample)


def get_all() -> list:
    """Return a copy of all stored samples."""
    with _lock:
        return list(_buffer)


def get_last_seconds(seconds: float) -> list:
    """Return only samples within the last *seconds* seconds."""
    cutoff = _time.time() - seconds
    with _lock:
        return [s for s in _buffer if s.get("t", 0) >= cutoff]


def clear() -> None:
    """Reset the buffer (e.g. on new session)."""
    with _lock:
        _buffer.clear()
