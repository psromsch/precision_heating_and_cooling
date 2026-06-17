"""Boiler runtime accounting (pure, Home-Assistant agnostic).

Tracks how long the boiler has been *commanded on* within the current day,
ISO-week and calendar month. The class is deliberately free of any HA imports
so it can be unit-tested with plain ``datetime`` values; the coordinator feeds
it wall-clock timestamps and persists/restores its counters.

Design notes:
  * All accounting is in seconds; callers convert to hours for display.
  * Periods reset when their key changes (date / ISO-week / year-month). A
    heating segment that spans a rollover loses at most the time since the last
    ``tick`` from the *new* period, which is negligible when ticked regularly.
  * ``on_since`` is intentionally **not** persisted: after a restart we must not
    count downtime as heating, so the coordinator re-anchors from the real
    boiler state at startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

PERIODS = ("today", "week", "month")


@dataclass
class _Period:
    key: str = ""
    seconds: float = 0.0


class BoilerRuntimeTracker:
    """Accumulate boiler on-time per day/week/month."""

    def __init__(self) -> None:
        self._periods = {name: _Period() for name in PERIODS}
        self._on_since: datetime | None = None

    # --- period keys ---------------------------------------------------------

    @staticmethod
    def _keys(now: datetime) -> dict[str, str]:
        iso = now.isocalendar()
        return {
            "today": now.strftime("%Y-%m-%d"),
            "week": f"{iso[0]}-W{iso[1]:02d}",
            "month": now.strftime("%Y-%m"),
        }

    def _rollover(self, now: datetime) -> None:
        keys = self._keys(now)
        for name, key in keys.items():
            period = self._periods[name]
            if period.key != key:
                period.key = key
                period.seconds = 0.0

    def _accumulate(self, now: datetime) -> None:
        """Fold the elapsed on-time since the last sample into all counters."""
        if self._on_since is None:
            return
        elapsed = (now - self._on_since).total_seconds()
        if elapsed > 0:
            for period in self._periods.values():
                period.seconds += elapsed
        self._on_since = now

    # --- public surface ------------------------------------------------------

    def set_boiler(self, on: bool, now: datetime) -> None:
        """Record a boiler on/off transition (or re-anchor at startup)."""
        self._accumulate(now)
        self._rollover(now)
        self._on_since = now if on else None

    def tick(self, now: datetime) -> None:
        """Fold in live on-time and apply any period rollover."""
        self._accumulate(now)
        self._rollover(now)

    def seconds(self, period: str, now: datetime) -> float:
        """Seconds the boiler was on in ``period`` ('today'|'week'|'month')."""
        self._accumulate(now)
        self._rollover(now)
        return self._periods[period].seconds

    def hours(self, period: str, now: datetime) -> float:
        return self.seconds(period, now) / 3600.0

    # --- persistence ---------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialise the period counters (on_since is intentionally omitted)."""
        return {
            name: {"key": p.key, "seconds": p.seconds}
            for name, p in self._periods.items()
        }

    def restore(self, data: dict | None) -> None:
        if not data:
            return
        for name in PERIODS:
            raw = data.get(name)
            if not isinstance(raw, dict):
                continue
            self._periods[name] = _Period(
                key=str(raw.get("key", "")),
                seconds=float(raw.get("seconds", 0.0) or 0.0),
            )
