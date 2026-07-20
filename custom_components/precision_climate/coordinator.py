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

import asyncio
import logging
import time
from datetime import datetime, timedelta

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_OFF,
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
    ABSENT_ACTION_PASSIVE,
    DEFAULT_OVERHEAT_THRESHOLD,
    DOMAIN,
    Mode,
    PAUSE_TARGET,
    PRESENT_ACTION_ACTIVE,
    PROLONGED_HEATING_SECONDS,
    TRV_MISMATCH_SECONDS,
    TRV_UNAVAILABLE_SECONDS,
    TRV_UNRESPONSIVE_MIN_RISE,
    TRV_UNRESPONSIVE_SECONDS,
    block_flow_setpoint,
    force_flow_setpoint,
)
from .control.loop import evaluate
from .control.mode import (
    PRESENCE_ABSENT,
    PRESENCE_PRESENT,
    resolve_room_mode,
)
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

# The TRV drift guard only corrects setpoints that have been stable this long.
# A value that moved more recently is a human dialing the valve (boost) — the
# guard must never snap the dial back mid-turn. Stale valves (the case the
# guard exists for) sit at their value for far longer than this.
TRV_DRIFT_GRACE_SECONDS = 180.0


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
        # Persisted so a restart mid-boost doesn't snap the user's dialed valve
        # back to the block sentinel (the hands-off contract survives reboots).
        self._room_boost: dict[str, dict] = {}
        self._boost_unsub: dict[str, object] = {}
        self._boost_store: Store | None = (
            Store(hass, 1, f"{DOMAIN}_{entry_id}_room_boost")
            if entry_id is not None
            else None
        )
        # Map every TRV entity back to its room, for manual-change detection.
        self._trv_to_room: dict[str, str] = {
            trv: r.room_id for r in self.config.rooms for trv in r.trvs
        }
        # Per-room presence (occupancy) sensors. Confirmed state per room
        # ("present"/"absent"/None-until-confirmed); a pending dwell timer per
        # room debounces both edges. Maps the sensor entity back to its room.
        self._presence_entity_to_room: dict[str, str] = {
            r.presence_entity: r.room_id
            for r in self.config.rooms
            if r.presence_entity
        }
        self._room_presence: dict[str, str | None] = {}
        self._presence_dwell_unsub: dict[str, object] = {}
        # monotonic timestamp of each TRV's last observed setpoint change, so
        # the drift guard leaves recently-touched valves alone (live dialing).
        self._trv_setpoint_changed_mono: dict[str, float] = {}

        # Serializes control cycles: triggers fire evaluate via async_create_task
        # and _apply awaits service calls mid-cycle, so overlapping evaluations
        # could otherwise interleave and desync cached commanded state.
        self._evaluate_lock = asyncio.Lock()

        # Commanded state (what we last told HA to do).
        self._boiler_on: bool = False
        self._trv_open: dict[str, bool] = {r.room_id: False for r in self.config.rooms}
        self._room_heating: dict[str, bool] = {r.room_id: False for r in self.config.rooms}

        # Away mode source tracking.
        self._away_source: str | None = None   # "manual" | "presence" | "holiday" | None
        self._grace_unsub = None               # async_call_later handle for the grace timer
        # Last observed presence state (someone in the zone?). None until the first
        # evaluation. Used to make presence edge-triggered: away engages only on a
        # home→away transition, so a manual away-off while outside is respected.
        self._presence_home: bool | None = None
        # Persisted so a restart can tell apart manual away (sticky) from
        # presence/holiday away (re-derived on boot, never restored from switch state).
        self._away_source_store: Store | None = (
            Store(hass, 1, f"{DOMAIN}_{entry_id}_away_source")
            if entry_id is not None
            else None
        )

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
        # Unauthorized-boiler must persist ~90 s before alerting: our own
        # non-blocking turn-off leaves the state registry reading ON for a
        # moment right after a pause/master-off.
        self._unauthorized = SustainedCondition(90.0)
        # Rooms whose overheat notification has fired (re-armed on recovery).
        self._overheat_alerted: set[str] = set()

    # --- Lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        """Register listeners and run the startup safety evaluation."""
        tracked = [r.thermometer for r in self.config.rooms]
        for r in self.config.rooms:
            tracked.extend(r.windows)
        # Watch the soft-away alarm entity: an arm/disarm re-runs the loop.
        if self.config.soft_away_entity:
            tracked.append(self.config.soft_away_entity)
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
        self._setup_room_presence_tracking()
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
        # Restore active boosts BEFORE the startup evaluation so a mid-boost
        # restart doesn't rewrite the user's dialed valve to the block sentinel.
        await self._restore_boosts()
        # Restore the away source so the away switch's restore path knows whether
        # the last active away was manual (sticky) or presence/holiday (re-derived).
        if self._away_source_store is not None:
            src_data = await self._away_source_store.async_load()
            if isinstance(src_data, dict):
                self._away_source = src_data.get("source")
        # If away was engaged BY PRESENCE when HA stopped, hold it across the
        # restart instead of silently dropping it (edge-triggered presence would
        # otherwise never re-engage until the next real departure, heating an
        # empty house). The presence evaluation at the end of setup disengages
        # it immediately if someone is actually home.
        if self._away_source == "presence":
            p_cfg = self.config.presence
            if p_cfg.enabled and p_cfg.persons and p_cfg.zone:
                self._away_on = True
                self._presence_home = False
            else:
                # Presence mode was disabled while away: don't strand away on.
                self._away_source = None
                self._save_away_source()
        # Restore the boiler runtime counters and re-anchor from the *real* boiler
        # state (downtime must not count as heating, so on_since starts fresh).
        if self._runtime_store is not None:
            self._runtime.restore(await self._runtime_store.async_load())
        self._runtime.set_boiler(self._boiler_on, dt_util.now())
        self._runtime_tick_unsub = async_track_time_interval(
            self.hass, self._handle_runtime_tick, RUNTIME_TICK
        )
        # Arm the holiday-away window (restart-safe; evaluates current state too).
        self._setup_holiday_schedule()
        # Seed presence with reality: sets _presence_home to the current truth
        # (so later edges are computed against a real baseline, not None) and
        # disengages a restored presence-away if someone is already home.
        # Takes no other action — engaging away still requires a real departure.
        await self._async_evaluate_presence()
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

    def _setup_room_presence_tracking(self) -> None:
        """Subscribe to per-room occupancy sensors and seed their state."""
        entities = list(self._presence_entity_to_room)
        if not entities:
            return
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, entities, self._handle_room_presence_event
            )
        )
        # Seed the dwell timers from each sensor's current state so a room that
        # is already occupied at startup confirms after its on-delay (rather
        # than never, if the sensor doesn't change again).
        for entity, room_id in self._presence_entity_to_room.items():
            state = self.hass.states.get(entity)
            self._schedule_presence_confirm(room_id, entity, state)

    # --- Per-room presence (occupancy) --------------------------------------

    @callback
    def _handle_room_presence_event(self, event: Event) -> None:
        entity = event.data.get("entity_id")
        room_id = self._presence_entity_to_room.get(entity)
        if room_id is None:
            return
        self._schedule_presence_confirm(room_id, entity, event.data.get("new_state"))

    def _schedule_presence_confirm(self, room_id: str, entity: str, state) -> None:
        """(Re)arm the dwell timer that confirms a room's presence state.

        Occupied/vacant must hold for the room's on/off dwell minutes before it
        takes effect, debouncing a brief walk-through or a momentary sensor
        drop. An unavailable/unknown sensor holds the last confirmed state.
        """
        # A pending confirmation is always superseded by a newer reading.
        unsub = self._presence_dwell_unsub.pop(room_id, None)
        if unsub is not None:
            unsub()

        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return  # hold last confirmed state

        occupied = state.state == STATE_ON
        target = PRESENCE_PRESENT if occupied else PRESENCE_ABSENT
        if self._room_presence.get(room_id) == target:
            return  # already in this state; nothing to confirm

        cfg = self.config.room_by_id(room_id)
        if cfg is None:
            return
        minutes = cfg.presence_on_minutes if occupied else cfg.presence_off_minutes
        self._presence_dwell_unsub[room_id] = async_call_later(
            self.hass,
            max(0.0, float(minutes)) * 60.0,
            self._make_presence_confirm(room_id, target),
        )

    def _make_presence_confirm(self, room_id: str, target: str):
        @callback
        def _confirm(_now) -> None:
            self._presence_dwell_unsub.pop(room_id, None)
            self._room_presence[room_id] = target
            self.hass.async_create_task(self.async_evaluate())

        return _confirm

    def _cancel_presence_timers(self) -> None:
        for room_id in list(self._presence_dwell_unsub):
            unsub = self._presence_dwell_unsub.pop(room_id, None)
            if unsub is not None:
                unsub()

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
        self._cancel_presence_timers()
        if self._runtime_tick_unsub is not None:
            self._runtime_tick_unsub()
            self._runtime_tick_unsub = None
        for unsub in self._holiday_unsubs:
            unsub()
        self._holiday_unsubs.clear()
        # Persist final runtime counters on the way out.
        self._runtime.tick(dt_util.now())
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
        entity_id = event.data.get("entity_id", "")
        # Remember when this valve's setpoint last moved: the drift guard only
        # corrects setpoints that have been SITTING STILL for a while, so a live
        # human dialing the valve is never fought mid-turn (see
        # _trv_setpoint_drifted). Recorded for our own commands too — harmless,
        # since after a command the real value matches the sentinel anyway.
        self._trv_setpoint_changed_mono[entity_id] = time.monotonic()
        # Ignore our own valve commands (the force/block sentinels).
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
        # A setpoint that moved in the last few minutes is a live human hand
        # (a boost is being dialed, or its event is still in flight) — never
        # correct it. Stale valves — the case this guard exists for — have been
        # sitting at their value far longer than this grace.
        changed = self._trv_setpoint_changed_mono.get(entity_id)
        if changed is not None and time.monotonic() - changed < TRV_DRIFT_GRACE_SECONDS:
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

    def _is_soft_away_active(self) -> bool:
        """True if the configured alarm entity is in one of the armed states."""
        entity = self.config.soft_away_entity
        if not entity:
            return False
        state = self.hass.states.get(entity)
        if state is None:
            return False
        return state.state in self.config.soft_away_states

    @property
    def soft_away_on(self) -> bool:
        return self._is_soft_away_active()

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
        """Run one full control cycle and apply the resulting decision.

        Serialized: every trigger fires this via async_create_task, and _apply
        awaits service calls mid-cycle. Without the lock, two rapid sensor
        events could interleave — the second reading stale cached boiler/TRV
        state at its await points and issuing commands out of order.
        """
        async with self._evaluate_lock:
            await self._async_evaluate_locked()

    async def _async_evaluate_locked(self) -> None:
        now_local = dt_util.now()
        weekday = now_local.weekday()
        minute = now_local.hour * 60 + now_local.minute
        mono = time.monotonic()

        resolved = resolve_active_set(
            self.config.schedules, weekday, minute, self.config.default_room
        )
        resolved_by_id = {r.room_id: r for r in resolved}
        # Prune expired boosts before they feed the resolution below.
        self._prune_expired_boosts()
        soft_away = self._is_soft_away_active()
        soft_away_delta = self.config.soft_away_delta
        # Resolve each room's effective (target, active) through the full
        # override precedence: boost > pause > per-room away > presence >
        # global away > schedule. "Away = passive" (per-room and presence-away),
        # with global away the sole exception (caps only, keeps the active flag
        # so the boiler can still be driven). See control.mode.resolve_room_mode.
        for r in resolved:
            cfg = self.config.room_by_id(r.room_id)
            boost = self._room_boost.get(r.room_id)
            r.target, r.is_active = resolve_room_mode(
                schedule_target=r.target,
                schedule_active=r.is_active,
                away_target=self.config.away_target(r.room_id),
                pause_target=PAUSE_TARGET,
                boost_target=(boost["target"] if boost is not None else None),
                paused=r.room_id in self._room_paused,
                manual_room_away=r.room_id in self._room_away,
                global_away=self._away_on,
                has_presence=bool(cfg and cfg.has_presence),
                presence_state=self._room_presence.get(r.room_id),
                present_action=(cfg.present_action if cfg else PRESENT_ACTION_ACTIVE),
                absent_action=(cfg.absent_action if cfg else ABSENT_ACTION_PASSIVE),
                soft_away_active=soft_away,
                soft_away_delta=soft_away_delta,
            )
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
            self._runtime.set_boiler(decision.boiler_on, dt_util.now())
            self._save_runtime()
        elif not decision.boiler_on:
            # Drift guard: if our cache says OFF but the real switch is ON
            # (e.g. a manual toggle with no demand), issue the corrective call.
            real = self.hass.states.get(self.config.boiler_switch)
            if real is not None and real.state == STATE_ON:
                await self._set_switch(self.config.boiler_switch, False)
        else:
            # Reverse drift guard: cache and decision say ON but the real switch
            # reads a definite OFF (e.g. someone toggled it off by hand while
            # demand exists). Without this the house silently stops heating and
            # the prolonged/unresponsive failsafes count against a cold boiler.
            # Only a definite "off" triggers it — unavailable/unknown states
            # must not cause a per-cycle command storm.
            real = self.hass.states.get(self.config.boiler_switch)
            if real is not None and real.state == STATE_OFF:
                await self._set_switch(self.config.boiler_switch, True)
        self._boiler_on = decision.boiler_on

        # TRVs — use each valve's own min/max as open/close bounds so we never
        # send a value that the device will silently clamp to something different.
        for cfg in self.config.rooms:
            want_open = decision.trv_open.get(cfg.room_id, self._trv_open.get(cfg.room_id, False))
            state_changed = want_open != self._trv_open.get(cfg.room_id)
            # Boosted rooms: hands off the valve. The user just set it manually
            # — rewriting it would snap the dial back to the closed sentinel
            # mid-turn (the drift guard fires while the boost target is still
            # below the room temperature) or yank it to the force sentinel the
            # moment the dial crosses the room temperature. During boost the
            # valve stays exactly where the user put it (their setpoint > room
            # temp opens the valve on its own); we still run the boiler and the
            # caches below. Sentinel discipline resumes when the boost expires.
            if self._room_boost.get(cfg.room_id) is not None:
                self._trv_open[cfg.room_id] = want_open
                self._room_heating[cfg.room_id] = is_heating(self._boiler_on, want_open)
                continue
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
        # Sustained (90 s) because our own turn-off commands are non-blocking:
        # right after a pause/master-off the state registry still reads ON for
        # a moment, which must not raise a false alarm. A genuinely rogue ON
        # survives the window and still gets corrected + notified.
        real_boiler = self.hass.states.get(self.config.boiler_switch)
        real_on = real_boiler is not None and real_boiler.state == STATE_ON
        active_window = any(r.window_open for r in rooms if r.is_active)
        unauthorized = is_unauthorized_boiler(
            real_on, self.master_on, self.paused, active_window
        )
        if self._unauthorized.update(mono, unauthorized):
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

            # Overheating — latched: notify once on the rising edge, re-arm only
            # after the room stops overheating (otherwise every thermometer
            # update while hot would fire another notification).
            if is_overheating(room.temperature, heating, DEFAULT_OVERHEAT_THRESHOLD):
                if cfg.room_id not in self._overheat_alerted:
                    self._overheat_alerted.add(cfg.room_id)
                    self._notify(
                        "overheating",
                        f"{cfg.name} is overheating ({room.temperature}°C).",
                    )
            else:
                self._overheat_alerted.discard(cfg.room_id)

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

            # TRV unresponsive (heating 45 min but the room got colder).
            if self._unresponsive[cfg.room_id].update(mono, heating, room.temperature):
                self._notify(
                    "trv_unresponsive",
                    f"{cfg.name}: heating 45 min but the room lost temperature; check window/TRV.",
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

    def _any_tracker_unavailable(self) -> bool:
        """Return True if any configured person tracker is unavailable or unknown.

        When a tracker can't be read, we have no reliable location data, so
        presence evaluation is frozen — the current away state is held and the
        grace timer is not started. Only a manual override can change the state
        until all trackers come back online.
        """
        for person_eid in self.config.presence.persons:
            state = self.hass.states.get(person_eid)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return True
        return False

    async def _async_evaluate_presence(self) -> None:
        cfg = self.config.presence
        if not cfg.enabled or not cfg.persons or not cfg.zone:
            return
        # Manual- and holiday-away are never overridden by presence.
        if self._away_source in ("manual", "holiday"):
            return
        # If any tracker is unavailable, freeze: cancel any pending grace timer
        # (so away can't engage) and hold the current state until all trackers
        # are readable again. Only manual override can change state in this window.
        if self._any_tracker_unavailable():
            self._cancel_grace_timer()
            return

        anyone_home = self._is_anyone_home()

        # Edge-triggered: presence only acts on an actual transition, never on the
        # steady state. This means turning away off manually while still outside
        # the zone does NOT get re-engaged (no new home→away transition), and it
        # won't fire on startup either. Away re-engages only the next time someone
        # leaves the zone (which requires having returned home first).
        was_home = self._presence_home
        self._presence_home = anyone_home

        if anyone_home:
            # Cancel any pending grace timer and disengage presence-away immediately.
            self._cancel_grace_timer()
            if self._away_on and self._away_source == "presence":
                await self._async_set_away_presence(False)
        elif was_home:
            # Genuine home→away transition: start the grace timer if not already
            # away and none pending. (was_home is None on the first evaluation, so
            # startup-while-away never auto-engages — only a real departure does.)
            if not self._away_on and self._grace_unsub is None:
                self._grace_unsub = async_call_later(
                    self.hass,
                    cfg.grace_minutes * 60.0,
                    self._make_grace_expiry(),
                )

    @staticmethod
    def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Great-circle distance in metres between two GPS points."""
        import math
        R = 6_371_000.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        return 2 * R * math.asin(math.sqrt(a))

    def _is_anyone_home(self) -> bool:
        zone_eid = self.config.presence.zone
        if not zone_eid:
            return True   # no zone configured → assume home (safe default)
        zone_state = self.hass.states.get(zone_eid)
        if zone_state is None:
            return True
        zone_lat = zone_state.attributes.get("latitude")
        zone_lon = zone_state.attributes.get("longitude")
        zone_radius = zone_state.attributes.get("radius", 100)
        zone_name = (zone_state.attributes.get("friendly_name") or "").lower()
        for person_eid in self.config.presence.persons:
            state = self.hass.states.get(person_eid)
            if state is None:
                continue
            lat = state.attributes.get("latitude")
            lon = state.attributes.get("longitude")
            if lat is not None and lon is not None and zone_lat is not None and zone_lon is not None:
                # Geographic check: is this person's GPS point inside the zone's radius?
                # Using Haversine so we own the logic and it can't fail on API changes.
                # We add gps_accuracy to the allowed radius so a person just inside the
                # zone boundary isn't incorrectly reported as outside due to GPS drift.
                accuracy = state.attributes.get("gps_accuracy") or 0
                dist = self._haversine_m(lat, lon, zone_lat, zone_lon)
                if dist <= zone_radius + accuracy:
                    return True
                # Has GPS but is outside the zone — genuinely away; don't fall through.
                continue
            # Non-GPS tracker: fall back to comparing the state string.
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
        # Also re-check tracker availability: the freeze normally cancels this
        # timer, but the expiry callback can race the queued freeze evaluation,
        # and an unreadable tracker must never be interpreted as "not home".
        if self._any_tracker_unavailable():
            return
        if self._is_anyone_home():
            return
        if self._away_source in ("manual", "holiday"):
            return
        await self._async_set_away_presence(True)

    async def _async_set_away_presence(self, on: bool) -> None:
        """Engage/disengage away mode from presence automation."""
        self._away_on = on
        self._away_source = "presence" if on else None
        self._save_away_source()
        if on:
            self._notify(
                "presence_away_on",
                "Away mode ON — nobody is in the presence zone.",
            )
        else:
            self._notify(
                "presence_away_off",
                "Away mode OFF — someone is back in the presence zone.",
            )
        # The away switch entity reflects coordinator state via the listener
        # update below. Do NOT sync it with a switch.turn_on/off service call:
        # that round-trips through AwayModeSwitch.async_turn_on → async_set_away,
        # which would re-label this automatic away as "manual" and strand it.
        await self.async_evaluate()
        self._notify_listeners()

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
        self._runtime.tick(dt_util.now())
        self._save_runtime()
        self.hass.async_create_task(self.async_evaluate())

    def _save_runtime(self) -> None:
        if self._runtime_store is not None:
            # Debounced write; coalesces frequent transitions into one disk write.
            self._runtime_store.async_delay_save(self._runtime.to_dict, 30)

    def boiler_runtime_hours(self, period: str) -> float:
        """Boiler on-time in hours for 'today' | 'week' | 'month'."""
        return self._runtime.hours(period, dt_util.now())

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
        self._save_away_source()
        # Switch entity syncs via the listener update — never via a service
        # call, which would re-label the source as "manual" (see presence path).
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

    @property
    def away_source(self) -> str | None:
        return self._away_source

    def _save_away_source(self) -> None:
        if self._away_source_store is not None:
            self._away_source_store.async_delay_save(
                lambda: {"source": self._away_source}, 5
            )

    async def async_set_away(self, on: bool, source: str = "manual") -> None:
        # If the away switch entity is toggled while an automatic away is
        # already engaged, the toggle must not silently re-label the source as
        # "manual" — that would make presence/holiday away sticky forever
        # (presence refuses to disengage manual away). Turning ON while already
        # on keeps the existing automatic source; turning ON from off is a real
        # manual action.
        if on:
            if not self._away_on:
                self._away_source = source
        else:
            self._away_source = None
        self._away_on = on
        self._save_away_source()
        # When manually disengaging, re-evaluate presence so it can re-engage
        # if still nobody home (allows presence to take back control).
        self._cancel_grace_timer()
        await self.async_evaluate()
        if not on:
            await self._async_evaluate_presence()

    def room_presence_state(self, room_id: str) -> str | None:
        """Confirmed occupancy for a room ('present'/'absent'), or None."""
        return self._room_presence.get(room_id)

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

    def _save_boosts(self) -> None:
        if self._boost_store is not None:
            self._boost_store.async_delay_save(
                lambda: {
                    rid: {
                        "target": b["target"],
                        "expires": b["expires"].isoformat(),
                    }
                    for rid, b in self._room_boost.items()
                },
                1,
            )

    async def _restore_boosts(self) -> None:
        """Reload persisted boosts and re-arm their expiry timers.

        Keeps the hands-off contract across restarts: a valve the user dialed
        must not be snapped back to the block sentinel just because HA rebooted
        mid-boost. Already-expired boosts are dropped.
        """
        if self._boost_store is None:
            return
        data = await self._boost_store.async_load()
        if not isinstance(data, dict):
            return
        now = dt_util.utcnow()
        known = {r.room_id for r in self.config.rooms}
        for rid, raw in data.items():
            if rid not in known or not isinstance(raw, dict):
                continue
            expires = dt_util.parse_datetime(str(raw.get("expires") or ""))
            try:
                target = float(raw.get("target"))
            except (TypeError, ValueError):
                continue
            if expires is None or expires <= now:
                continue
            self._room_boost[rid] = {"target": target, "expires": expires}
            self._boost_unsub[rid] = async_call_later(
                self.hass,
                (expires - now).total_seconds(),
                self._make_boost_expiry(rid),
            )

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
        self._save_boosts()
        await self.async_evaluate()

    async def async_cancel_room_boost(self, room_id: str) -> None:
        """End a room's boost immediately and return it to its schedule."""
        self._room_boost.pop(room_id, None)
        self._cancel_boost_timer(room_id)
        self._save_boosts()
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
        pruned = False
        for rid in list(self._room_boost):
            if self._room_boost[rid]["expires"] <= now:
                self._room_boost.pop(rid, None)
                self._cancel_boost_timer(rid)
                pruned = True
        if pruned:
            self._save_boosts()

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
