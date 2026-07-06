"""Tests for the pure control loop.

Each test encodes one of the behaviours agreed in the design discussion. The
loop takes a snapshot of rooms + system state and returns the desired boiler and
TRV states; there is no Home Assistant in the loop here.
"""

from __future__ import annotations

import pathlib
import sys

# Make the repository root importable so we can reach custom_components.*
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.precision_climate.control.loop import evaluate
from custom_components.precision_climate.models.room import RoomState, SystemState


def room(
    room_id="r1",
    temperature=20.0,
    target=20.0,
    is_active=True,
    lower=0.5,
    upper=0.5,
    window_open=False,
):
    return RoomState(
        room_id=room_id,
        temperature=temperature,
        target=target,
        is_active=is_active,
        lower_hysteresis=lower,
        upper_hysteresis=upper,
        window_open=window_open,
    )


# --- Boiler demand & latching ------------------------------------------------

def test_active_room_below_lower_hysteresis_turns_boiler_on():
    # target 20, lower 0.5 -> demand at <= 19.5
    rooms = [room(temperature=19.4)]
    decision = evaluate(rooms, SystemState(boiler_on=False))
    assert decision.boiler_on is True
    assert decision.reason == "demand"


def test_active_room_in_band_holds_previous_boiler_state():
    # 19.8 is between demand (19.5) and satisfied (20.5): latch.
    rooms = [room(temperature=19.8)]
    assert evaluate(rooms, SystemState(boiler_on=True)).boiler_on is True
    assert evaluate(rooms, SystemState(boiler_on=False)).boiler_on is False


def test_boiler_off_only_when_all_active_rooms_satisfied():
    cold = room(room_id="cold", temperature=19.0)
    hot = room(room_id="hot", temperature=21.0)
    # one room still cold -> boiler stays on
    decision = evaluate([cold, hot], SystemState(boiler_on=True))
    assert decision.boiler_on is True

    # both above upper hysteresis (20.5) -> shut down
    both_hot = [room(room_id="a", temperature=20.6), room(room_id="b", temperature=20.7)]
    decision = evaluate(both_hot, SystemState(boiler_on=True))
    assert decision.boiler_on is False
    assert decision.reason == "all_satisfied"


def test_passive_room_never_triggers_boiler():
    passive = room(is_active=False, temperature=10.0)  # freezing passive room
    decision = evaluate([passive], SystemState(boiler_on=False))
    assert decision.boiler_on is False


# --- TRV open/close logic ----------------------------------------------------

def test_active_room_opens_trv_below_target_not_just_below_hysteresis():
    # 19.8 < target 20 but above demand threshold 19.5 -> TRV should open
    rooms = [room(temperature=19.8)]
    decision = evaluate(rooms, SystemState(boiler_on=True))
    assert decision.trv_open["r1"] is True


def test_active_room_above_target_keeps_trv_closed():
    # one cold room calls the boiler, a second active room is above its target
    cold = room(room_id="cold", temperature=19.0)
    warm = room(room_id="warm", temperature=20.2, target=20.0)  # above target, below upper
    decision = evaluate([cold, warm], SystemState(boiler_on=False, trv_open={"warm": False}))
    assert decision.boiler_on is True
    assert decision.trv_open["warm"] is False  # stays closed: above its target


def test_trv_closes_when_above_upper_hysteresis():
    rooms = [room(temperature=20.6)]  # above satisfied threshold 20.5
    decision = evaluate(rooms, SystemState(boiler_on=True, trv_open={"r1": True}))
    assert decision.trv_open["r1"] is False


def test_passive_room_opens_below_target_like_active():
    # Passive rooms now use the same valve rule as active rooms: open as soon as
    # temp < target. Lower hysteresis is irrelevant for passive rooms.
    # 19.8 is below target (20) but above the old demand threshold (19.5) -> now opens.
    passive = room(is_active=False, temperature=19.8)
    decision = evaluate([passive], SystemState(trv_open={"r1": False}))
    assert decision.trv_open["r1"] is True
    assert decision.boiler_on is False  # but a passive room never drives the boiler


def test_passive_room_rides_only_when_boiler_on_but_valve_still_opens():
    # Lower hysteresis irrelevant: 19.8 and 19.4 behave identically (both < target).
    for temp in (19.8, 19.4):
        passive = room(is_active=False, temperature=temp)
        decision = evaluate([passive], SystemState(boiler_on=False, trv_open={"r1": False}))
        assert decision.trv_open["r1"] is True
        assert decision.boiler_on is False


def test_passive_room_closes_at_target_plus_upper():
    # Rides up to target + upper (20.5), then closes — upper hysteresis matters.
    passive_hot = room(is_active=False, temperature=20.5)
    decision = evaluate([passive_hot], SystemState(boiler_on=True, trv_open={"r1": True}))
    assert decision.trv_open["r1"] is False

    # In the band [target, target+upper): hold previous (keep riding).
    passive_band = room(is_active=False, temperature=20.2)
    held_open = evaluate([passive_band], SystemState(boiler_on=True, trv_open={"r1": True}))
    assert held_open.trv_open["r1"] is True
    held_closed = evaluate([passive_band], SystemState(boiler_on=True, trv_open={"r1": False}))
    assert held_closed.trv_open["r1"] is False


def test_passive_rides_alongside_active_when_boiler_fires():
    # An active room hitting its demand threshold fires the boiler, and a passive
    # room below its target opens and heats at the same moment.
    cold_active = room(room_id="a", is_active=True, temperature=19.4)  # <= 19.5 -> demand
    passive = room(room_id="p", is_active=False, temperature=19.8)     # < 20 -> rides
    decision = evaluate([cold_active, passive], SystemState(boiler_on=False))
    assert decision.boiler_on is True
    assert decision.trv_open["a"] is True
    assert decision.trv_open["p"] is True


# --- Overrides ---------------------------------------------------------------

def test_master_off_forces_everything_off():
    rooms = [room(temperature=10.0)]  # freezing
    decision = evaluate(rooms, SystemState(master_on=False, boiler_on=True, trv_open={"r1": True}))
    assert decision.boiler_on is False
    assert decision.trv_open["r1"] is False
    assert decision.reason == "master_off"


def test_paused_forces_everything_off():
    rooms = [room(temperature=10.0)]
    decision = evaluate(rooms, SystemState(paused=True, boiler_on=True))
    assert decision.boiler_on is False
    assert decision.reason == "paused"


def test_active_window_open_keeps_boiler_off():
    cold_open = room(temperature=18.0, window_open=True)
    decision = evaluate([cold_open], SystemState(boiler_on=False))
    assert decision.boiler_on is False
    assert decision.reason == "active_window_open"


def test_passive_window_open_does_not_block_boiler():
    cold_active = room(room_id="active", temperature=18.0)
    passive_open = room(room_id="passive", is_active=False, temperature=18.0, window_open=True)
    decision = evaluate([cold_active, passive_open], SystemState(boiler_on=False))
    assert decision.boiler_on is True  # passive window is irrelevant to the boiler


# --- Sunny day ---------------------------------------------------------------

def test_sunny_day_reduces_active_target():
    # room at 18.5, real target 20 -> would demand. With sunny target 17 it is
    # already satisfied (17.5 upper) and should not call the boiler.
    rooms = [room(temperature=18.5, target=20.0)]
    system = SystemState(boiler_on=False, sunny_day_active=True, sunny_day_target=17.0)
    decision = evaluate(rooms, system)
    assert decision.boiler_on is False
    assert decision.trv_open["r1"] is False


def test_sunny_day_still_heats_below_reduced_minimum():
    rooms = [room(temperature=16.0, target=20.0)]  # below the 17 minimum
    system = SystemState(boiler_on=False, sunny_day_active=True, sunny_day_target=17.0)
    decision = evaluate(rooms, system)
    assert decision.boiler_on is True


# --- Unavailable thermometer -------------------------------------------------

def test_unavailable_thermometer_passive_holds_trv():
    # Passive room with no thermometer: hold previous state, no boiler effect.
    rooms = [room(temperature=None, is_active=False)]
    decision = evaluate(rooms, SystemState(boiler_on=False, trv_open={"r1": True}))
    assert decision.boiler_on is False
    assert decision.trv_open["r1"] is True  # held


def test_active_room_thermometer_offline_closes_trv():
    # Active room with no thermometer: TRV must close (fail-safe).
    rooms = [room(temperature=None, is_active=True)]
    decision = evaluate(rooms, SystemState(boiler_on=False, trv_open={"r1": True}))
    assert decision.trv_open["r1"] is False
    assert decision.reason == "no_active_temp"


def test_all_active_rooms_offline_turns_boiler_off():
    # Every active room has lost its thermometer: boiler must stop.
    r1 = room(room_id="r1", temperature=None, is_active=True)
    r2 = room(room_id="r2", temperature=None, is_active=True)
    decision = evaluate([r1, r2], SystemState(boiler_on=True))
    assert decision.boiler_on is False
    assert decision.reason == "no_active_temp"


def test_partial_active_offline_uses_remaining_sensors():
    # One active room has no thermometer; another is cold -> boiler should fire.
    offline = room(room_id="offline", temperature=None, is_active=True)
    cold = room(room_id="cold", temperature=18.0, target=20.0, is_active=True)
    decision = evaluate([offline, cold], SystemState(boiler_on=False))
    assert decision.boiler_on is True
    assert decision.reason == "demand"
    assert decision.trv_open["offline"] is False  # offline active room TRV closed


def test_passive_room_thermometer_offline_holds_trv():
    # Passive room with no thermometer: hold previous TRV state.
    passive = room(room_id="p1", temperature=None, is_active=False)
    # Previous state: open -> stays open
    decision = evaluate([passive], SystemState(boiler_on=False, trv_open={"p1": True}))
    assert decision.trv_open["p1"] is True
    # Previous state: closed -> stays closed
    decision = evaluate([passive], SystemState(boiler_on=False, trv_open={"p1": False}))
    assert decision.trv_open["p1"] is False
