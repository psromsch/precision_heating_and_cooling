"""Runtime coordinator: connects the proven pure logic to Home Assistant.

The coordinator is the only place that talks to HA in the control path. It:

  * subscribes to the *four* allowed re-evaluation triggers
        1. thermometer state changes
        2. window-sensor state changes
        3. schedule boundaries (time)
        4. integration startup (HA restart safety check)
  * on each trigger, reads HA state, builds the pure-logic snapshots, runs the
    control loop + failsafe timers, and applies the resulting decision by
    calling switch/climate services.

It deliberately does NOT re-evaluate on TRV state changes, to avoid the
command/confirm feedback loop. The decision logic itself lives in the
unit-tested ``control``, ``scheduler`` and ``failsafes`` modules.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)
import homeassistant.util.dt as dt_util

from .const import (
    DEFAULT_OVERHEAT_THRESHOLD,
    DOMAIN,
    Mode,
    PROLONGED_HEATING_SECONDS,
    TRV_MISMATCH_SECONDS,
    TRV_UNAVAILABLE_SECONDS,
    TRV_UNRESPONSIVE_MIN_RISE,
    TRV_UNRESPONSIVE_SECONDS,
    block_flow_setpoint,
    force_flow_setpoint,
)
from .control.loop import evaluate
from .failsafes.logic import (
    SustainedCondition,
    UnresponsiveTrv,
    is_heating,
    is_overheating,
    is_unauthorized_boiler,
    trv_setpoint_mismatch,
)
from .models.room import RoomState, SystemState
from .models.runtime import RuntimeConfig, build_runtime
from .scheduler.engine import next_boundary, resolve_active_set

_LOGGER = logging.getLogger(__name__)


class PrecisionClimateCoordinator:
    """Owns the runtime state and drives the control loop."""

    def __init__(self, hass: HomeAssistant, entry_data: dict) -> None:
        self.hass = hass
        self.config: RuntimeConfig = build_runtime(entry_data)
        self.mode = Mode.HEAT

        # User-facing master controls (mutated by the master switch / pause entities).
        self.master_on: bool = True
        self.paused: bool = False

        # Commanded state (what we last told HA to do).
        self._boiler_on: bool = False
        self._trv_open: dict[str, bool] = {r.room_id: False for r in self.config.rooms}
        self._room_heating: dict[str, bool] = {r.room_id: False for r in self.config.rooms}

        # Sunny-day savings (assessed once per morning).
        self._sunny_active: bool = False

        # Subscriptions to clean up on unload.
        self._unsubs: list = []
        self._boundary_unsub = None

        # Entity update callbacks (registered by entities, fired after each cycle).
        self._listeners: list = []

        # Latest schedule-resolved snapshot, exposed to sensor entities.
        self.resolved_targets: dict[str, float] = {}
        self.resolved_active: dict[str, bool] = {}

        # Latest diagnostics, exposed to the status sensor.
        self.last_reason: str = "startup"
        self.observed_temps: dict[str, float | None] = {}

        # Failsafe timers.
        self._prolonged = SustainedCondition(PROLONGED_HEATING_SECONDS)
        self._mismatch = {r.room_id: SustainedCondition(TRV_MISMATCH_SECONDS) for r in self.config.rooms}
        self._unresponsive = {
            r.room_id: UnresponsiveTrv(TRV_UNRESPONSIVE_SECONDS, TRV_UNRESPONSIVE_MIN_RISE)
            for r in self.config.rooms
        }
        self._unavailable = {
            trv: SustainedCondition(TRV_UNAVAILABLE_SECONDS)
            for r in self.config.rooms
            for trv in r.trvs
        }

    # --- Lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        """Register listeners and run the startup safety evaluation."""
        tracked = [r.thermometer for r in self.config.rooms]
        for r in self.config.rooms:
            tracked.extend(r.windows)
        if tracked:
            self._unsubs.append(
                async_track_state_change_event(self.hass, tracked, self._handle_state_event)
            )
        self._schedule_next_boundary()
        # Seed the commanded state from the REAL entities so the first evaluation
        # produces a genuine delta when reality disagrees with the decision
        # (e.g. the boiler was left on, or config changed and the loop now wants
        # it off). Without this, a fresh coordinator assumes everything is off
        # and would skip the corrective service call.
        self._reconcile_initial_state()
        # Trigger 4: startup safety check — force reality to match the logic.
        await self.async_evaluate()

    def _reconcile_initial_state(self) -> None:
        """Read the actual boiler/TRV state into the commanded-state caches."""
        boiler = self.hass.states.get(self.config.boiler_switch)
        self._boiler_on = boiler is not None and boiler.state == STATE_ON

        # A TRV counts as "open" if its setpoint is nearer the force-flow value
        # than the block-flow value (midpoint of 4 °C and 28 °C ≈ 16 °C).
        force = force_flow_setpoint(self.mode)
        block = block_flow_setpoint(self.mode)
        midpoint = (force + block) / 2
        for cfg in self.config.rooms:
            targets = [self._trv_target(t) for t in cfg.trvs]
            known = [t for t in targets if t is not None]
            if not known:
                continue
            # Open only if every known TRV is on the force-flow side.
            opens_toward_force = force >= block
            self._trv_open[cfg.room_id] = all(
                (t >= midpoint) if opens_toward_force else (t <= midpoint)
                for t in known
            )

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._boundary_unsub:
            self._boundary_unsub()
            self._boundary_unsub = None

    # --- Triggers ------------------------------------------------------------

    @callback
    def async_add_listener(self, update_callback) -> "callback":
        """Register an entity update callback; returns an unsubscribe function."""
        self._listeners.append(update_callback)

        @callback
        def _remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove

    @callback
    def _notify_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    @callback
    def _handle_state_event(self, event: Event) -> None:
        self.hass.async_create_task(self.async_evaluate())

    @callback
    def _handle_boundary(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_evaluate())
        self._schedule_next_boundary()

    def _schedule_next_boundary(self) -> None:
        if self._boundary_unsub:
            self._boundary_unsub()
            self._boundary_unsub = None
        now = dt_util.now()
        weekday, minute = now.weekday(), now.hour * 60 + now.minute
        day, point = next_boundary(self.config.schedules, weekday, minute)
        days_ahead = (day - weekday) % 7
        if days_ahead == 0 and point <= minute:
            days_ahead = 7
        target = (now + dt_util.dt.timedelta(days=days_ahead)).replace(
            hour=point // 60, minute=point % 60, second=0, microsecond=0
        )
        self._boundary_unsub = async_track_point_in_time(
            self.hass, self._handle_boundary, target
        )

    # --- State reading -------------------------------------------------------

    def _read_temperature(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _is_window_open(self, entity_id: str) -> bool:
        # A window sensor that is unavailable is assumed CLOSED (fail-safe-to-heat),
        # matching the agreed behaviour: "if a window sensor is unavailable, assume closed".
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            return False
        return state.state == STATE_ON

    def _trv_target(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        target = state.attributes.get(ATTR_TEMPERATURE)
        try:
            return float(target) if target is not None else None
        except (ValueError, TypeError):
            return None

    def _trv_unavailable(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)

    # --- Sunny day -----------------------------------------------------------

    def _evaluate_sunny_day(self, minute_of_day: int) -> tuple[bool, float | None]:
        """Return (active, reduced_target). Active only before the end-of-window."""
        cfg = self.config.sunny_day
        if not cfg.enabled or cfg.forecast_entity is None:
            return False, None
        if minute_of_day >= cfg.end_min:
            self._sunny_active = False
            return False, None
        hours = self._read_temperature(cfg.forecast_entity)  # numeric sensor
        if hours is not None and hours >= cfg.min_hours:
            self._sunny_active = True
        return self._sunny_active, cfg.reduced_target

    # --- Main evaluation -----------------------------------------------------

    async def async_evaluate(self) -> None:
        """Run one full control cycle and apply the resulting decision."""
        now_local = dt_util.now()
        weekday = now_local.weekday()
        minute = now_local.hour * 60 + now_local.minute
        mono = time.monotonic()

        resolved = resolve_active_set(
            self.config.schedules, weekday, minute, self.config.default_room
        )
        resolved_by_id = {r.room_id: r for r in resolved}
        self.resolved_targets = {r.room_id: r.target for r in resolved}
        self.resolved_active = {r.room_id: r.is_active for r in resolved}

        sunny_active, sunny_target = self._evaluate_sunny_day(minute)

        rooms: list[RoomState] = []
        for cfg in self.config.rooms:
            res = resolved_by_id.get(cfg.room_id)
            if res is None:
                continue  # uncovered slot; cannot participate this cycle
            window_open = any(self._is_window_open(w) for w in cfg.windows)
            rooms.append(
                RoomState(
                    room_id=cfg.room_id,
                    target=res.target,
                    is_active=res.is_active,
                    lower_hysteresis=cfg.lower_hysteresis,
                    upper_hysteresis=cfg.upper_hysteresis,
                    temperature=self._read_temperature(cfg.thermometer),
                    window_open=window_open,
                )
            )

        system = SystemState(
            master_on=self.master_on,
            paused=self.paused,
            boiler_on=self._boiler_on,
            trv_open=dict(self._trv_open),
            sunny_day_active=sunny_active,
            sunny_day_target=sunny_target,
        )

        decision = evaluate(rooms, system, self.mode)
        self.last_reason = decision.reason
        self.observed_temps = {r.room_id: r.temperature for r in rooms}

        await self._apply(decision, rooms, resolved_by_id)
        self._run_failsafes(mono, rooms, resolved_by_id)
        self._notify_listeners()

    async def _apply(self, decision, rooms, resolved_by_id) -> None:
        """Execute the decision via HA services and update commanded state."""
        # Boiler.
        if decision.boiler_on != self._boiler_on:
            await self._set_switch(self.config.boiler_switch, decision.boiler_on)
        self._boiler_on = decision.boiler_on

        # TRVs.
        for cfg in self.config.rooms:
            want_open = decision.trv_open.get(cfg.room_id, self._trv_open.get(cfg.room_id, False))
            if want_open != self._trv_open.get(cfg.room_id):
                setpoint = (
                    force_flow_setpoint(self.mode)
                    if want_open
                    else block_flow_setpoint(self.mode)
                )
                for trv in cfg.trvs:
                    await self._set_trv_target(trv, setpoint)
            self._trv_open[cfg.room_id] = want_open
            self._room_heating[cfg.room_id] = is_heating(self._boiler_on, want_open)

    # --- Failsafes -----------------------------------------------------------

    def _run_failsafes(self, mono: float, rooms, resolved_by_id) -> None:
        rooms_by_id = {r.room_id: r for r in rooms}

        # Unauthorized boiler: real switch on while nothing authorises it.
        real_boiler = self.hass.states.get(self.config.boiler_switch)
        real_on = real_boiler is not None and real_boiler.state == STATE_ON
        active_window = any(r.window_open for r in rooms if r.is_active)
        if is_unauthorized_boiler(real_on, self.master_on, self.paused, active_window):
            self.hass.async_create_task(self._set_switch(self.config.boiler_switch, False))
            self._notify("unauthorized_boiler", "Boiler was on without authorization; forced off.")

        # Prolonged heating (system-wide boiler runtime).
        if self._prolonged.update(mono, self._boiler_on):
            self._notify("prolonged_heating", "Boiler has been running for over 5 hours.")

        for cfg in self.config.rooms:
            room = rooms_by_id.get(cfg.room_id)
            if room is None:
                continue
            heating = self._room_heating.get(cfg.room_id, False)

            # Overheating.
            if is_overheating(room.temperature, heating, DEFAULT_OVERHEAT_THRESHOLD):
                self._notify(
                    "overheating",
                    f"{cfg.name} is overheating ({room.temperature}°C).",
                )

            # TRV setpoint mismatch (any TRV in the room reading below schedule target).
            should_heat = heating
            for trv in cfg.trvs:
                if trv_setpoint_mismatch(
                    self._boiler_on, should_heat, self._trv_target(trv), room.target
                ):
                    if self._mismatch[cfg.room_id].update(mono, True):
                        self._notify(
                            "trv_mismatch",
                            f"{cfg.name}: TRV {trv} target is below the schedule target while heating.",
                        )
                    break
            else:
                self._mismatch[cfg.room_id].update(mono, False)

            # TRV unresponsive (heating but temperature barely rises).
            if self._unresponsive[cfg.room_id].update(mono, heating, room.temperature):
                self._notify(
                    "trv_unresponsive",
                    f"{cfg.name}: heating 45 min but temperature barely rose; check window/TRV.",
                )

            # TRV unavailable (offline while the room is heating).
            for trv in cfg.trvs:
                cond = self._unavailable[trv].update(mono, heating and self._trv_unavailable(trv))
                if cond:
                    self._notify("trv_unavailable", f"{cfg.name}: TRV {trv} unavailable while heating.")

    # --- HA service helpers --------------------------------------------------

    async def _set_switch(self, entity_id: str, on: bool) -> None:
        await self.hass.services.async_call(
            "switch",
            "turn_on" if on else "turn_off",
            {ATTR_ENTITY_ID: entity_id},
            blocking=False,
        )

    async def _set_trv_target(self, entity_id: str, setpoint: float) -> None:
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {ATTR_ENTITY_ID: entity_id, ATTR_TEMPERATURE: setpoint},
            blocking=False,
        )

    def _notify(self, kind: str, message: str) -> None:
        """Send a notification if this kind is enabled (default: enabled)."""
        if not self.config.notifications.get(kind, True):
            return
        _LOGGER.warning("[%s] %s", kind, message)
        for notify_service in self.config.notify_services:
            domain, _, service = notify_service.partition(".")
            self.hass.async_create_task(
                self.hass.services.async_call(
                    domain or "notify",
                    service or notify_service,
                    {"title": "Precision Climate", "message": message},
                    blocking=False,
                )
            )

    # --- Public control surface (used by entities) ---------------------------

    async def async_set_master(self, on: bool) -> None:
        self.master_on = on
        await self.async_evaluate()

    async def async_set_paused(self, paused: bool) -> None:
        self.paused = paused
        await self.async_evaluate()

    @property
    def room_heating(self) -> dict[str, bool]:
        return dict(self._room_heating)

    @property
    def boiler_on(self) -> bool:
        """The boiler state the integration currently commands."""
        return self._boiler_on

    @property
    def trv_open(self) -> dict[str, bool]:
        return dict(self._trv_open)
