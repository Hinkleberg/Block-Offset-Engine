"""
mirror_health_monitor.py
────────────────────────
Watches lag between Array A (write_seq) and one or more Array B
mirrors (mirror_write_seq). Raises status before the render feed
ever notices a problem. No SQL. No external dependencies.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional


class MirrorStatus(Enum):
    HEALTHY  = "HEALTHY"
    WARNING  = "WARNING"
    DEGRADED = "DEGRADED"
    OFFLINE  = "OFFLINE"


@dataclass
class MirrorSnapshot:
    name:       str
    status:     MirrorStatus
    lag:        int
    primary_seq: int
    mirror_seq:  int
    ts:         float


StatusChangeCb = Callable[[str, MirrorStatus], None]


class _MirrorSource:
    """Adapter to read write_seq from either ResilientStore or RenderStore."""

    def __init__(self, obj) -> None:
        self._obj = obj

    def write_seq(self) -> int:
        return getattr(self._obj, "write_seq", 0)

    def mirror_write_seq(self) -> int:
        return getattr(self._obj, "mirror_write_seq", 0)


class MirrorHealthMonitor:
    """
    Polls primary write_seq and mirror mirror_write_seq at interval_s.

    Status thresholds:
      HEALTHY  — lag < lag_warn_threshold
      WARNING  — lag ≥ warn threshold
      DEGRADED — lag ≥ lag_degraded_threshold
      OFFLINE  — no mirror progress for stale_timeout seconds
    """

    def __init__(
        self,
        primary,
        mirrors: Dict[str, object],
        *,
        lag_warn_threshold:     int   = 100,
        lag_degraded_threshold: int   = 500,
        stale_timeout:          float = 30.0,
        interval_s:             float = 1.0,
        on_status_change: Optional[StatusChangeCb] = None,
    ):
        self._primary   = _MirrorSource(primary)
        self._mirrors   = {name: _MirrorSource(m) for name, m in mirrors.items()}
        self._warn      = lag_warn_threshold
        self._degraded  = lag_degraded_threshold
        self._stale     = stale_timeout
        self._interval  = interval_s
        self._on_change = on_status_change

        self._statuses: Dict[str, MirrorStatus] = {}
        self._last_progress: Dict[str, float]   = {n: time.time() for n in mirrors}
        self._last_mirror_seq: Dict[str, int]   = {}

        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="mirror-health"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=self._interval * 2)

    def _loop(self) -> None:
        while self._running:
            self._poll()
            time.sleep(self._interval)

    def _poll(self) -> None:
        primary_seq = self._primary.write_seq()
        now = time.time()

        for name, src in self._mirrors.items():
            mirror_seq = src.mirror_write_seq()
            lag = max(0, primary_seq - mirror_seq)

            # Progress detection
            prev_seq = self._last_mirror_seq.get(name, -1)
            if mirror_seq > prev_seq:
                self._last_progress[name] = now
            self._last_mirror_seq[name] = mirror_seq

            stale = (now - self._last_progress.get(name, now)) > self._stale

            if stale and primary_seq > 0:
                status = MirrorStatus.OFFLINE
            elif lag >= self._degraded:
                status = MirrorStatus.DEGRADED
            elif lag >= self._warn:
                status = MirrorStatus.WARNING
            else:
                status = MirrorStatus.HEALTHY

            old = self._statuses.get(name)
            self._statuses[name] = status
            if old != status and self._on_change:
                try:
                    self._on_change(name, status)
                except Exception:
                    pass

    def snapshot(self) -> Dict[str, MirrorSnapshot]:
        primary_seq = self._primary.write_seq()
        now = time.time()
        result = {}
        for name, src in self._mirrors.items():
            mirror_seq = src.mirror_write_seq()
            result[name] = MirrorSnapshot(
                name=name,
                status=self._statuses.get(name, MirrorStatus.HEALTHY),
                lag=max(0, primary_seq - mirror_seq),
                primary_seq=primary_seq,
                mirror_seq=mirror_seq,
                ts=now,
            )
        return result

    def report(self) -> str:
        snaps = self.snapshot()
        lines = ["MirrorHealthMonitor:"]
        for name, s in snaps.items():
            lines.append(
                f"  {name}: {s.status.value} "
                f"(lag={s.lag}, primary={s.primary_seq}, mirror={s.mirror_seq})"
            )
        return "\n".join(lines)