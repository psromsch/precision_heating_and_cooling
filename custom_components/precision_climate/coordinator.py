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

It does not re-evaluate on the *valve commands* it issues to TRVs (the
force/block sentinels), to avoid a command/confirm feedback loop. It does,
however, watch TRV setpoints for *manual* changes (any value that isn't one of
our sentinels): a manual change starts "Boost Mode" for that room. The decision
logic itself lives in the unit-tested ``control``, ``scheduler`` and
``failsafes`` modules.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .const import (
    DEFAULT_OVERHEAT_THRESHOLD,
    DOMAIN,
    Mode,
    PAUSE_TARGET,
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
from .runtime_stats import BoilerRuntimeTracker
from .scheduler.engine import next_boundary, resolve_active_set

_LOGGER = logging.getLogger(__name__)

# How often to fold live boiler on-time into the runtime counters (and refresh
# the runtime sensors / handle period rollover) while nothing else triggers.
RUNTIME_TICK = timedelta(minutes=5)


class PrecisionClimateCoordinator:
    """Owns the runtime state and drives the control loop."""

    def __init__(self, hass: HomeAssistant, entry_data: dict, entry_id: str | None = None) -> None:
        self.hass = hass
        self.config: RuntimeConfig = build_runtime(entry_data)
        self.mode = Mode.HEAT
        self._entry_id = entry_id

        # User-facing master controls (mutated by the master switch / pause entities).
        self.master_on: bool = True
        self.paused: bool = False
        # Away mode: while on, each room's target is capped at its configured
        # away target (min of schedule and away). Active/passive is untouched.
        # Driven by the away switch entity (restored across restarts) and, later,
        # by presence automations.
        self._away_on: bool = False
        # Per-room pause: paused rooms get their target dropped to PAUSE_TARGET
        # until resumed. Set is seeded by the per-room pause switches on restore.
        self._room_paused: set[str] = set()
        # Per-room away: each room's target is capped at its away_target while it
        # is in this set. Manual-only (no timer); independent from global away.
        # Persisted via _room_away_store so it survives reloads without triggering one.
        self._room_away: set[str] = set()
        self._room_away_store: Store | None = (
            Store(hass, 1, f"{DOMAIN}_{entry_id}_room_away")
            if entry_id is not None
            else None
        )
        # Per-room boost: a manual TRV change overrides the schedule target AND
        # active/passive flag for a configured number of hours. Maps room_id ->
        # {"target": float, "expires": datetime (UTC)}. Each has an expiry timer.
        self._room_boost: dict[str, dict] = {}
        self._boost_unsub: dict[str, object] = {}
        # Map every TRV entity back to its room, for manual-change detection.
        self._trv_to_room: dict[str, str] = {
            trv: r.room_id for r in self.config.rooms for trv in r.trvs
        }

        # Commanded state (what we last told HA to do).
        self._boiler_on: bool = False
        self._trv_open: dict[str, bool] = {r.room_id: False for r in self.config.rooms}
        self._room_heating: dict[str, bool] = {r.room_id: False for r in self.config.rooms}

        # Away mode source tracking.
        self._away_source: str | None = None   # "manual" | "presence" | None
        self._grace_unsub = None               # async_call_later handle for the grace timer

        # Sunny-day savings (assessed once per morning).
        self._sunny_active: bool = False

        # Boiler runtime accounting (today / week / month). Persisted via a Store
        # so the counters survive restarts; on_since is re-anchored at startup.
        self._runtime = BoilerRuntimeTracker()
        self._runtime_store: Store | None = (
            Store(hass, 1, f"{DOMAIN}_{entry_id}_boiler_runtime")
            if entry_id is not None
            else None
        )
        self._runtime_tick_unsub = None

        # Holiday-away absolute-datetime triggers (start + end), re-armed on setup.
        self._holiday_unsubs: list = []

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
        # Watch TRV setpoints for *manual* changes -> Boost Mode. (Our own valve
        # commands are filtered out inside the handler.)
        trvs = list(self._trv_to_room)
        if trvs:
            self._unsubs.append(
                async_track_state_change_event(self.hass, trvs, self._handle_trv_event)
            )
        self._schedule_next_boundary()
        self._setup_presence_tracking()
        # Seed the commanded state from the REAL entities so the first evaluation
        # produces a genuine delta when reality disagrees with the decision
        # (e.g. the boiler was left on, or config changed and the loop now wants
        # it off). Without this, a fresh coordinator assumes everything is off
        # and would skip the corrective service call.
        self._reconcile_initial_state()
        # Restore per-room away flags.
        if self._room_away_store is not None:
            data = await self._room_away_store.async_load()
            if isinstance(data, list):
                self._room_away = set(data)
        # Restore the boiler runtime counters and re-anchor from the *real* boiler
        # state (downtime must not count as heating, so on_since starts fresh).
        if self._runtime_store is not None:
            self._runtime.restore(await self._runtime_store.async_load())
        self._runtime.set_boiler(self._boiler_on, dt_util.utcnow())
        self._runtime_tick_unsub = async_track_time_interval(
            self.hass, self._handle_runtime_tick, RUNTIME_TICK
        )
        # Arm the holiday-away window (restart-safe; evaluates current state too).
        self._setup_holiday_schedule()
        # Trigger 4: startup safety check — force reality to match the logic.
        await self.async_evaluate()

    def _reconcile_initial_state(self) -> None:
        """Read the actual boiler/TRV state into the commanded-state caches."""
        boiler = self.hass.states.get(self.config.boiler_switch)
        self._boiler_on = boiler is not None and boiler.state == STATE_ON

        # A TRV counts as "open" if its setpoint is nearer the force-flow value
        # than the block-flow value. Use each TRV's own min/max as the bounds
        # (real Zigbee TRVs clamp our sentinels to their limits).
        for cfg in self.config.rooms:
            known = []
            for trv_eid in cfg.trvs:
                t = self._trv_target(trv_eid)
                if t is None:
                    continue
                force = self._trv_force_setpoint(trv_eid)
                block = self._trv_block_setpoint(trv_eid)
                midpoint = (force + block) / 2
                opens_toward_force = force >= block
                known.append((t >= midpoint) if opens_toward_force else (t <= midpoint))
            if not known:
                continue
            # Open only if every known TRV is on the force-flow side.
            self._trv_open[cfg.room_id] = all(known)

    def _setup_presence_tracking(self) -> None:
        """Subscribe to person-entity state changes for presence mode."""
        persons = self.config.presence.persons
        if not persons:
            return
        self._unsubs.append(
            async_track_state_change_event(self.hass, persons, self._handle_person_event)
        )

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._boundary_unsub:
            self._boundary_unsub()
            self._boundary_unsub = None
        for room_id in list(self._boost_unsub):
            self._cancel_boost_timer(room_id)
        self._cancel_grace_timer()
        if self._runtime_tick_unsub is not None:
            self._runtime_tick_unsub()
            self._runtime_tick_unsub = None
        for unsub in self._holiday_unsubs:
            unsub()
        self._holiday_unsubs.clear()
        # Persist final runtime counters on the way out.
        self._runtime.tick(dt_util.utcnow())
        self._save_runtime()

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
    def _handle_person_event(self, event: Event) -> None:
        self.hass.async_create_task(self._async_evaluate_presence())

    @callback
    def _handle_state_event(self, event: Event) -> None:
        self.hass.async_create_task(self.async_evaluate())

    @callback
    def _handle_trv_event(self, event: Event) -> None:
        """Detect a *manual* TRV setpoint change and start Boost Mode.

        We only ever command the force/block sentinels, so any other reported
        setpoint can only have come from a human. Re-touching any TRV in a room
        restarts the boost timer.
        """
        room_id = self._trv_to_room.get(event.data.get("entity_id"))
        if room_id is None:
            return
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        # Ignore restore/availability transitions (e.g. HA restart, Zigbee
        # reconnect). When a TRV comes back from unavailable it re-reports its
        # setpoint, which is NOT a human action and must never trigger Boost.
        old_state = event.data.get("old_state")
        if (
            old_state is None
            or old_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        ):
            return

        new_target = new_state.attributes.get(ATTR_TEMPERATURE)
        try:
            new_target = float(new_target) if new_target is not None else None
        except (ValueError, TypeError):
            new_target = None
        if new_target is None:
            return

        ot = old_state.attributes.get(ATTR_TEMPERATURE)
        try:
            old_target = float(ot) if ot is not None else None
        except (ValueError, TypeError):
            old_target = None

        # Ignore non-setpoint attribute churn (e.g. current_temperature updates).
        if old_target is not None and abs(old_target - new_target) < 0.05:
            return
        # Ignore our own valve commands (the force/block sentinels).
        entity_id = event.data.get("entity_id", "")
        if self._is_trv_sentinel(entity_id, new_target):
            return
        self.hass.async_create_task(self.async_set_room_boost(room_id, new_target))

    def _is_trv_sentinel(self, entity_id: str, value: float) -> bool:
        """True if a setpoint is (near) one of our force/block valve commands.

        Uses the TRV's own min/max bounds so clamped-back values (e.g. a TRV
        that rounds 4 °C up to 5 °C) are still recognised as ours.
        """
        force = self._trv_force_setpoint(entity_id)
        block = self._trv_block_setpoint(entity_id)
        tol = max(1.0, abs(force - block) * 0.05)  # at least 1 °C tolerance
        return abs(value - force) <= tol or abs(value - block) <= tol

    def _trv_setpoint_drifted(self, entity_id: str, want_open: bool) -> bool:
        """True if the TRV's real setpoint isn't the sentinel we intend for it.

        Lets ``_apply`` correct a valve sitting at a stale or manual setpoint even
        when our cached commanded-state already matches the decision. Unavailable
        TRVs are skipped (we can't read or correct them). The setpoints already
        use each valve's own max/min, so once a corrective command lands the real
        value matches the sentinel and this stops firing — no per-cycle spam.
        """
        real = self._trv_target(entity_id)
        if real is None:
            return False
        intended = (
            self._trv_force_setpoint(entity_id)
            if want_open
            else self._trv_block_setpoint(entity_id)
        )
        force = self._trv_force_setpoint(entity_id)
        block = self._trv_block_setpoint(entity_id)
        tol = max(1.0, abs(force - block) * 0.05)
        return abs(real - intended) > tol

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
        # Away mode caps each room's target at its configured away target
        # (min of schedule and away). Active/passive is left as scheduled.
        if self._away_on:
            for r in resolved:
                away = self.config.away_target(r.room_id)
                if away is not None:
                    r.target = min(r.target, away)
        # Per-room away: cap at the room's configured away target (independent of
        # global away). Applied before pause so pause always wins over away.
        for r in resolved:
            if r.room_id in self._room_away:
                away = self.config.away_target(r.room_id)
                if away is not None:
                    r.target = min(r.target, away)
        # Paused rooms have their effective target dropped so they stop calling
        # for heat. The stored schedule is untouched, so resume restores it.
        for r in resolved:
            if r.room_id in self._room_paused:
                r.target = PAUSE_TARGET
        # Boost overrides both the schedule target and the active/passive flag
        # for the boost window. Prune expired boosts first; boost wins over pause.
        self._prune_expired_boosts()
        for r in resolved:
            boost = self._room_boost.get(r.room_id)
            if boost is not None:
                r.target = boost["target"]
                r.is_active = True
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
            self._runtime.set_boiler(decision.boiler_on, dt_util.utcnow())
            self._save_runtime()
        elif not decision.boiler_on:
            # Drift guard: if our cache says OFF but the real switch is ON
            # (e.g. a manual toggle with no demand), issue the corrective call.
            real = self.hass.states.get(self.config.boiler_switch)
            if real is not None and real.state == STATE_ON:
                await self._set_switch(self.config.boiler_switch, False)
        self._boiler_on = decision.boiler_on

        # TRVs — use each valve's own min/max as open/close bounds so we never
        # send a value that the device will silently clamp to something different.
        for cfg in self.config.rooms:
            want_open = decision.trv_open.get(cfg.room_id, self._trv_open.get(cfg.room_id, False))
            state_changed = want_open != self._trv_open.get(cfg.room_id)
            for trv in cfg.trvs:
                # Command the valve when the desired state changed OR when its
                # real setpoint has drifted from the sentinel we intend for it
                # (the TRV analog of the boiler drift guard). The latter corrects
                # a valve left at a stale target — e.g. one carried over from a
                # previous system, or a manual change our cache didn't see — even
                # when our cached commanded-state already matches the decision.
                if not (state_changed or self._trv_setpoint_drifted(trv, want_open)):
                    continue
                if want_open:
                    # Ensure the valve is in heat mode before sending the
                    # setpoint — many Zigbee TRVs ignore set_temperature
                    # while in 'off' or 'auto' hvac_mode.
                    await self._set_trv_hvac_mode(trv, "heat")
                setpoint = (
                    self._trv_force_setpoint(trv)
                    if want_open
                    else self._trv_block_setpoint(trv)
                )
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

    def _trv_force_setpoint(self, entity_id: str) -> float:
        """Setpoint that fully opens this TRV: its max_temp, or the global default."""
        state = self.hass.states.get(entity_id)
        if state is not None:
            v = state.attributes.get("max_temp")
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
        return force_flow_setpoint(self.mode)

    def _trv_block_setpoint(self, entity_id: str) -> float:
        """Setpoint that fully closes this TRV: its min_temp, or the global default."""
        state = self.hass.states.get(entity_id)
        if state is not None:
            v = state.attributes.get("min_temp")
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
        return block_flow_setpoint(self.mode)

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

    async def _set_trv_hvac_mode(self, entity_id: str, hvac_mode: str) -> None:
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {ATTR_ENTITY_ID: entity_id, "hvac_mode": hvac_mode},
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

    # --- Presence mode -------------------------------------------------------

    async def _async_evaluate_presence(self) -> None:
        cfg = self.config.presence
        if not cfg.enabled or not cfg.persons or not cfg.zone:
            return
        # Manual- and holiday-away are never overridden by presence.
        if self._away_source in ("manual", "holiday"):
            return

        anyone_home = self._is_anyone_home()

        if anyone_home:
            # Cancel any pending grace timer and disengage presence-away immediately.
            self._cancel_grace_timer()
            if self._away_on and self._away_source == "presence":
                await self._async_set_away_presence(False)
        else:
            # Nobody home: start grace timer if not already running and not already away.
            if not self._away_on and self._grace_unsub is None:
                self._grace_unsub = async_call_later(
                    self.hass,
                    cfg.grace_minutes * 60.0,
                    self._make_grace_expiry(),
                )

    def _is_anyone_home(self) -> bool:
        zone_eid = self.config.presence.zone
        if not zone_eid:
            return True   # no zone configured → assume home (safe default)
        zone_state = self.hass.states.get(zone_eid)
        if zone_state is None:
            return True
        zone_name = (zone_state.attributes.get("friendly_name") or "").lower()
        for person_eid in self.config.presence.persons:
            state = self.hass.states.get(person_eid)
            if state is None:
                continue
            if state.state.lower() == zone_name or state.state.lower() == "home":
                return True
        return False

    def _make_grace_expiry(self):
        @callback
        def _expire(_now) -> None:
            self._grace_unsub = None
            self.hass.async_create_task(self._async_engage_away_after_grace())
        return _expire

    async def _async_engage_away_after_grace(self) -> None:
        # Re-check in case someone returned while the timer was running.
        if self._is_anyone_home():
            return
        if self._away_source in ("manual", "holiday"):
            return
        await self._async_set_away_presence(True)

    async def _async_set_away_presence(self, on: bool) -> None:
        """Engage/disengage away mode from presence automation."""
        self._away_on = on
        self._away_source = "presence" if on else None
        # Keep the HA away switch in sync.
        away_switch = self._away_switch_entity_id()
        if away_switch:
            await self._set_switch(away_switch, on)
        await self.async_evaluate()
        self._notify_listeners()

    def _away_switch_entity_id(self) -> str | None:
        """Resolve our own away switch entity_id from the entity registry."""
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(self.hass)
        entry_id = next(
            (eid for eid, c in self.hass.data.get("precision_climate", {}).items()
             if c is self),
            None,
        )
        if entry_id is None:
            return None
        return registry.async_get_entity_id("switch", "precision_climate", f"{entry_id}_away")

    def _cancel_grace_timer(self) -> None:
        if self._grace_unsub is not None:
            self._grace_unsub()
            self._grace_unsub = None

    # --- Boiler runtime accounting -------------------------------------------

    @callback
    def _handle_runtime_tick(self, _now) -> None:
        """Periodic tick: fold live on-time in, roll periods over, and run a
        full evaluation so time-based failsafes (prolonged heating, overheat,
        TRV unresponsive) advance even during long quiet periods with no sensor
        state changes."""
        self._runtime.tick(dt_util.utcnow())
        self._save_runtime()
        self.hass.async_create_task(self.async_evaluate())

    def _save_runtime(self) -> None:
        if self._runtime_store is not None:
            # Debounced write; coalesces frequent transitions into one disk write.
            self._runtime_store.async_delay_save(self._runtime.to_dict, 30)

    def boiler_runtime_hours(self, period: str) -> float:
        """Boiler on-time in hours for 'today' | 'week' | 'month'."""
        return self._runtime.hours(period, dt_util.utcnow())

    # --- Holiday away (absolute start/end window) ----------------------------

    def _parse_holiday(self, key: str) -> datetime | None:
        """Parse a stored local ISO datetime; return tz-aware or None."""
        raw = self.config.settings.get(key)
        if not raw:
            return None
        parsed = dt_util.parse_datetime(str(raw))
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return parsed

    def _setup_holiday_schedule(self) -> None:
        """Arm the holiday start/end triggers and apply the current window state.

        Restart-safe: we compare absolute datetimes to ``now`` and re-arm on
        every setup, so a reboot inside the window re-engages immediately and a
        reboot after the window leaves things clear — no countdown to lose.
        """
        for unsub in self._holiday_unsubs:
            unsub()
        self._holiday_unsubs.clear()

        start = self._parse_holiday("away_holiday_start")
        end = self._parse_holiday("away_holiday_end")
        now = dt_util.now()

        in_window = (
            start is not None and now >= start and (end is None or now < end)
        )
        if in_window:
            self.hass.async_create_task(self._async_set_away_holiday(True))
        elif end is not None and now >= end and self._away_source == "holiday":
            # Window already ended (e.g. set entirely in the past): make sure a
            # previously-engaged holiday away is released.
            self.hass.async_create_task(self._async_set_away_holiday(False))

        if start is not None and now < start:
            self._holiday_unsubs.append(
                async_track_point_in_time(self.hass, self._handle_holiday_start, start)
            )
        if end is not None and now < end:
            self._holiday_unsubs.append(
                async_track_point_in_time(self.hass, self._handle_holiday_end, end)
            )

    @callback
    def _handle_holiday_start(self, _now) -> None:
        self.hass.async_create_task(self._async_set_away_holiday(True))

    @callback
    def _handle_holiday_end(self, _now) -> None:
        self.hass.async_create_task(self._async_set_away_holiday(False))

    async def _async_set_away_holiday(self, on: bool) -> None:
        """Engage/disengage away mode from the holiday window."""
        if on:
            # Never override a manual away (the user is explicitly in control).
            if self._away_on and self._away_source == "manual":
                return
            self._away_on = True
            self._away_source = "holiday"
        else:
            # Only release what the holiday engaged; leave manual/presence alone.
            if self._away_source != "holiday":
                return
            self._away_on = False
            self._away_source = None
        away_switch = self._away_switch_entity_id()
        if away_switch:
            await self._set_switch(away_switch, on)
        await self.async_evaluate()
        self._notify_listeners()
        if not on:
            # Hand control back to presence in case nobody is home.
            await self._async_evaluate_presence()

    @property
    def holiday_window(self) -> dict | None:
        """The configured holiday window as ISO strings, or None if unset."""
        start = self.config.settings.get("away_holiday_start")
        end = self.config.settings.get("away_holiday_end")
        if not start and not end:
            return None
        return {"start": start or None, "end": end or None}

    # --- Public control surface (used by entities) ---------------------------

    async def async_set_master(self, on: bool) -> None:
        self.master_on = on
        await self.async_evaluate()

    async def async_set_paused(self, paused: bool) -> None:
        self.paused = paused
        await self.async_evaluate()

    @property
    def away_on(self) -> bool:
        return self._away_on

    async def async_set_away(self, on: bool, source: str = "manual") -> None:
        self._away_on = on
        self._away_source = "manual" if on else None
        # When manually disengaging, re-evaluate presence so it can re-engage
        # if still nobody home (allows presence to take back control).
        self._cancel_grace_timer()
        await self.async_evaluate()
        if not on:
            await self._async_evaluate_presence()

    def room_paused(self, room_id: str) -> bool:
        return room_id in self._room_paused

    async def async_set_room_paused(self, room_id: str, paused: bool) -> None:
        if paused:
            self._room_paused.add(room_id)
        else:
            self._room_paused.discard(room_id)
        await self.async_evaluate()

    def room_away(self, room_id: str) -> bool:
        return room_id in self._room_away

    async def async_set_room_away(self, room_id: str, on: bool) -> None:
        if on:
            self._room_away.add(room_id)
        else:
            self._room_away.discard(room_id)
        if self._room_away_store is not None:
            await self._room_away_store.async_save(list(self._room_away))
        await self.async_evaluate()

    # --- Child lock ----------------------------------------------------------

    async def async_set_room_child_lock(self, room_id: str, on: bool) -> None:
        """Turn the child lock on/off for every TRV in a room.

        Child-lock entities are user-selected per TRV and may be ``switch`` or
        ``lock`` entities; we dispatch the right service for each domain.
        """
        room = self.config.room_by_id(room_id)
        if room is None:
            return
        for entity_id in room.child_lock_entities:
            domain = entity_id.split(".", 1)[0]
            if domain == "lock":
                service = "lock" if on else "unlock"
            else:  # switch (and any toggleable fallback)
                service = "turn_on" if on else "turn_off"
            await self.hass.services.async_call(
                domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=False
            )
        self._notify_listeners()

    # --- Boost ---------------------------------------------------------------

    def room_boost(self, room_id: str) -> dict | None:
        """Return {"target", "expires"} for a boosted room, or None."""
        return self._room_boost.get(room_id)

    async def async_set_room_boost(self, room_id: str, target: float) -> None:
        """Boost a room to ``target`` for the configured duration, (re)starting
        the timer. Re-calling refreshes both the target and the expiry."""
        hours = self.config.boost_duration_hours
        self._room_boost[room_id] = {
            "target": float(target),
            "expires": dt_util.utcnow() + timedelta(hours=hours),
        }
        self._cancel_boost_timer(room_id)
        self._boost_unsub[room_id] = async_call_later(
            self.hass, hours * 3600.0, self._make_boost_expiry(room_id)
        )
        await self.async_evaluate()

    async def async_cancel_room_boost(self, room_id: str) -> None:
        """End a room's boost immediately and return it to its schedule."""
        self._room_boost.pop(room_id, None)
        self._cancel_boost_timer(room_id)
        await self.async_evaluate()

    def _make_boost_expiry(self, room_id: str):
        @callback
        def _expire(_now) -> None:
            self._boost_unsub.pop(room_id, None)
            self.hass.async_create_task(self.async_cancel_room_boost(room_id))

        return _expire

    def _cancel_boost_timer(self, room_id: str) -> None:
        unsub = self._boost_unsub.pop(room_id, None)
        if unsub is not None:
            unsub()

    def _prune_expired_boosts(self) -> None:
        now = dt_util.utcnow()
        for rid in list(self._room_boost):
            if self._room_boost[rid]["expires"] <= now:
                self._room_boost.pop(rid, None)
                self._cancel_boost_timer(rid)

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

    def schedule_payload(self) -> list[dict]:
        """Serialize each room's schedule for the visual editor card."""
        names = {r.room_id: r.name for r in self.config.rooms}
        payload: list[dict] = []
        for sched in self.config.schedules:
            payload.append(
                {
                    "room_id": sched.room_id,
                    "name": names.get(sched.room_id, sched.room_id),
                    "mode": sched.mode.value,
                    "blocks": {
                        key: [
                            {
                                "start_min": b.start_min,
                                "end_min": b.end_min,
                                "target": b.target,
                                "is_active": b.is_active,
                            }
                            for b in blocks
                        ]
                        for key, blocks in sched.blocks.items()
                    },
                }
            )
        return payload
