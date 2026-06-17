"""Tests for the pure boiler runtime accounting helper."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.precision_climate.runtime_stats import BoilerRuntimeTracker

UTC = timezone.utc


def _dt(y=2026, mo=6, d=17, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_accumulates_on_time_within_a_day():
    t = BoilerRuntimeTracker()
    t.set_boiler(True, _dt(h=8))
    t.set_boiler(False, _dt(h=10))  # 2 hours on
    assert t.hours("today", _dt(h=10)) == 2.0
    assert t.hours("week", _dt(h=10)) == 2.0
    assert t.hours("month", _dt(h=10)) == 2.0


def test_live_on_time_counts_before_turn_off():
    t = BoilerRuntimeTracker()
    t.set_boiler(True, _dt(h=8))
    # Still on; querying at 09:00 should report the live hour.
    assert t.hours("today", _dt(h=9)) == 1.0


def test_today_resets_on_day_rollover():
    t = BoilerRuntimeTracker()
    t.set_boiler(True, _dt(d=17, h=8))
    t.set_boiler(False, _dt(d=17, h=11))  # 3h on day 17
    assert t.hours("today", _dt(d=17, h=11)) == 3.0
    # Next day: today resets, but month keeps accumulating.
    t.set_boiler(True, _dt(d=18, h=8))
    t.set_boiler(False, _dt(d=18, h=9))  # 1h on day 18
    assert t.hours("today", _dt(d=18, h=9)) == 1.0
    assert t.hours("month", _dt(d=18, h=9)) == 4.0


def test_week_and_month_keys_independent():
    t = BoilerRuntimeTracker()
    # Jun 17 2026 is a Wednesday (ISO week 25).
    t.set_boiler(True, _dt(d=17, h=8))
    t.set_boiler(False, _dt(d=17, h=10))
    # New month entirely → all three reset.
    t.set_boiler(True, _dt(mo=7, d=1, h=8))
    t.set_boiler(False, _dt(mo=7, d=1, h=9))
    assert t.hours("today", _dt(mo=7, d=1, h=9)) == 1.0
    assert t.hours("week", _dt(mo=7, d=1, h=9)) == 1.0
    assert t.hours("month", _dt(mo=7, d=1, h=9)) == 1.0


def test_persistence_round_trip_keeps_counters():
    t = BoilerRuntimeTracker()
    t.set_boiler(True, _dt(h=8))
    t.set_boiler(False, _dt(h=10))
    data = t.to_dict()

    # Simulate a restart: new tracker, restore counters, on_since NOT restored.
    t2 = BoilerRuntimeTracker()
    t2.restore(data)
    # No downtime is counted; the previous 2h is preserved.
    assert t2.hours("today", _dt(h=12)) == 2.0
    # Re-anchor from real boiler state (off) → still 2h.
    t2.set_boiler(False, _dt(h=12))
    assert t2.hours("today", _dt(h=12)) == 2.0


def test_restart_does_not_count_downtime():
    t = BoilerRuntimeTracker()
    t.set_boiler(True, _dt(h=8))
    data = t.to_dict()  # boiler was on when we saved
    # Restart 3h later; restore must not credit those 3h of downtime.
    t2 = BoilerRuntimeTracker()
    t2.restore(data)
    t2.set_boiler(True, _dt(h=11))  # re-anchor (boiler genuinely still on)
    t2.set_boiler(False, _dt(h=12))  # 1h of real on-time after restart
    assert t2.hours("today", _dt(h=12)) == 1.0
