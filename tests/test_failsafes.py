"""Tests for the pure-logic failsafe conditions."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.precision_climate.failsafes.logic import (
    SustainedCondition,
    UnresponsiveTrv,
    is_heating,
    is_overheating,
    is_unauthorized_boiler,
    trv_setpoint_mismatch,
)

HOUR = 3600.0
MIN = 60.0


# --- Heating boolean ---------------------------------------------------------

def test_is_heating_requires_boiler_and_trv():
    assert is_heating(True, True) is True
    assert is_heating(True, False) is False
    assert is_heating(False, True) is False


# --- Unauthorized boiler -----------------------------------------------------

def test_unauthorized_boiler_when_master_off():
    assert is_unauthorized_boiler(True, master_on=False, paused=False, active_window_open=False)


def test_unauthorized_boiler_when_paused_or_window():
    assert is_unauthorized_boiler(True, True, paused=True, active_window_open=False)
    assert is_unauthorized_boiler(True, True, paused=False, active_window_open=True)


def test_authorized_boiler_is_not_flagged():
    assert not is_unauthorized_boiler(True, True, False, False)
    assert not is_unauthorized_boiler(False, False, True, True)  # boiler already off


# --- Overheating -------------------------------------------------------------

def test_overheating_only_while_heating_and_above_threshold():
    assert is_overheating(24.5, heating=True, threshold=24.0)
    assert not is_overheating(24.5, heating=False, threshold=24.0)
    assert not is_overheating(23.9, heating=True, threshold=24.0)
    assert not is_overheating(None, heating=True, threshold=24.0)


# --- TRV setpoint mismatch ---------------------------------------------------

def test_trv_setpoint_mismatch_flags_low_target():
    # should heat, boiler on, but TRV target reads 18 < schedule 20
    assert trv_setpoint_mismatch(True, True, actual_trv_target=18.0, schedule_target=20.0)


def test_trv_setpoint_no_mismatch_when_open():
    # forced flow target 28 >= schedule 20 -> fine
    assert not trv_setpoint_mismatch(True, True, 28.0, 20.0)


def test_trv_setpoint_mismatch_ignored_when_not_heating_or_boiler_off():
    assert not trv_setpoint_mismatch(False, True, 18.0, 20.0)
    assert not trv_setpoint_mismatch(True, False, 18.0, 20.0)
    assert not trv_setpoint_mismatch(True, True, None, 20.0)


# --- SustainedCondition (prolonged heating, mismatch debounce, unavailable) ---

def test_sustained_condition_fires_once_after_duration():
    cond = SustainedCondition(duration_s=5 * HOUR)
    assert cond.update(0.0, True) is False           # just started
    assert cond.update(4 * HOUR, True) is False       # not yet
    assert cond.update(5 * HOUR, True) is True         # exactly at threshold -> fire
    assert cond.update(6 * HOUR, True) is False        # already alerted, no repeat


def test_sustained_condition_resets_when_condition_clears():
    cond = SustainedCondition(duration_s=10 * MIN)
    cond.update(0.0, True)
    assert cond.update(5 * MIN, False) is False        # cleared -> reset
    assert cond.active_since is None
    # restart timer from scratch
    assert cond.update(6 * MIN, True) is False
    assert cond.update(16 * MIN, True) is True


# --- UnresponsiveTrv ---------------------------------------------------------

def test_unresponsive_trv_fires_when_temp_barely_rises():
    trv = UnresponsiveTrv(window_s=45 * MIN, min_rise=0.5)
    assert trv.update(0.0, heating=True, temperature=18.0) is False
    assert trv.update(44 * MIN, True, 18.2) is False          # before window
    assert trv.update(45 * MIN, True, 18.3) is True            # 0.3 rise < 0.5 -> fire


def test_unresponsive_trv_silent_when_temp_rises_enough():
    trv = UnresponsiveTrv(window_s=45 * MIN, min_rise=0.5)
    trv.update(0.0, True, 18.0)
    assert trv.update(45 * MIN, True, 19.0) is False           # 1.0 rise -> healthy


def test_unresponsive_trv_resets_when_heating_stops():
    trv = UnresponsiveTrv(window_s=45 * MIN, min_rise=0.5)
    trv.update(0.0, True, 18.0)
    assert trv.update(10 * MIN, False, 18.1) is False          # heating stopped -> reset
    # new heating cycle, baseline re-sampled at the higher temp
    trv.update(20 * MIN, True, 19.0)
    assert trv.update(20 * MIN + 45 * MIN, True, 19.1) is True  # only 0.1 rise -> fire
