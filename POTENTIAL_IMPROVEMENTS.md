# Potential improvements (parked)

Ideas that are technically sound but intentionally **not implemented**, because
they rely on learning/heuristics rather than doing exactly what the user told
the system to do. Precision Climate's guiding principle is **obedience**: when
you say a room heats at a given time and target, it does — no hidden estimation.

These are kept here so the analysis isn't lost if we revisit them later.

---

## 1. Automatic open-window detection (sensorless "virtual" window)

**Idea:** Detect an open window from a sudden drop in a room's temperature
(rate-of-change over a short window) and pause heating automatically, without a
physical contact sensor. The room config flow would offer: real sensor /
virtual (temperature-drop) / none.

**Why parked:** It introduces an assumption — "a fast temperature drop means an
open window" — that can misfire (a cold draft, a thermometer glitch, someone
briefly opening a door). We prefer the integration to act only on real,
unambiguous signals. Physical window sensors remain fully supported.

**If revisited:** rate-of-change detector with two tunables
(`drop_rate_threshold`, e.g. 0.5 °C / 5 min, and `recovery_timeout`,
e.g. 30 min). Self-contained, no learning period.

---

## 2. Adaptive / early start ("optimum start", à la Tado/Nest/Netatmo)

**Idea:** Instead of starting to heat *at* a schedule boundary, start *earlier*
so the room reaches target exactly at the boundary time. Learn each room's
heat-up rate and shift the boundary.

**Why parked:** This is fundamentally predictive. The user's prior experience
(Netatmo) and our research both show these systems frequently mis-estimate how
much earlier to start, which breaks the "you said this time, it does this time"
contract we want. Better to be obedient and predictable than clever and
occasionally wrong.

### Research summary (2026-06)

- **Commercial approaches are population models, not per-home models.** Tado
  uses deep neural networks trained on ~120 billion hours from 1M+ homes — a
  statistical average, not *your* home — which is exactly why "early start"
  often feels wrong on an individual install. Netatmo uses PID with a
  documented fallback to a fixed 30-min preheat when outdoor data is missing,
  and maintains a support article for "Auto-Adapt is not working properly."
  Nest is the exception: it builds a per-home thermal model after ~2 weeks and
  reports 6–10 % savings in controlled studies.
- **Real-world user reports** (Tado/Netatmo forums, Reddit, HA community):
  common failure modes are over-estimation (starts hours too early on fast
  radiator systems) and under-estimation (never reaches target in time). Enough
  frustration that the HA community built its own (Predheat, Better Thermostat,
  Intelligent Heating Pilot).
- **Minimum viable algorithm** (if we ever build it): learned heating slope.
  `start = boundary − (target − current) / slope_°C_per_hour`, where the slope
  is a rolling average of observed heat-up cycles. Conservative after ~5
  cycles, accurate after ~20.
- **Outdoor-temperature compensation** meaningfully helps *only* as one feature
  among several (research shows 43–61 % error reduction when adding 2–3 features
  to an outdoor-temp-only model; outdoor temp alone ≈ R² 0.90). For Chile's mild
  winters the slope varies little with outdoor temp, so it would be a phase-2
  refinement at best.

**If revisited:** opt-in per room, off by default; show the learned slope and
predicted start time on the card so the behaviour is transparent and auditable,
never a black box.

---

## 3. Sunny-day logic (outdoor-temp heating suppression)

**Idea:** When outdoor temperature exceeds a configurable threshold (e.g. 18 °C),
disable or reduce heating globally, since solar gain will bring rooms to target
naturally. Requires an outdoor temperature entity in the config.

**Why parked:** Assumes solar gain will compensate for the heating gap — not
guaranteed (overcast sunny day, north-facing rooms, insulation differences).
Makes the system predictive instead of reactive. The user can already configure
a lower away-mode temperature or pause rooms manually when it's warm outside.

**If revisited:** global setting `sunny_threshold_c`; heating suppressed when
an outdoor sensor exceeds it; clearly shown on the status card so the user
understands why heating is off.

---

## 4. Per-room presence sensor (entity-based room occupancy) — ✅ IMPLEMENTED (v1.3.0)

**Shipped** as a richer version of the original idea. Each room can take an
optional occupancy sensor (`presence_entity`, e.g. a mmWave radar) with:
- `presence_on_minutes` / `presence_off_minutes` — dwell/clearance to debounce
  both edges (a brief walk-through or a momentary sensor drop won't flip it),
- `present_action` ∈ {active, passive} — what occupancy does,
- `absent_action` ∈ {passive, away} — what vacancy does.

Presence overrides the schedule's active/passive flag (occupied → present
action, vacant → absent action). "Away = passive" is enforced globally (a
per-room-away room never fires the boiler), with whole-system away the sole
exception. Fully reactive, no learning. See `control/mode.resolve_room_mode`.

---

## 5. Supplementary electric heater per room

**Idea:** Some rooms have a secondary electric heater (e.g. a smart plug) for
when the radiator alone can't keep up — typically at night when the boiler
schedule drops the target or the room is the furthest from the boiler. The
plug should turn on when the room temperature falls a configurable margin below
the room's effective target and the radiator is not actively heating, and turn
off when either the radiator kicks in or the temperature recovers to within a
smaller margin.

**Why parked:** Requires a per-room `supplementary_heater_entity` (switch/plug)
in the room config, plus two tunables (`on_offset` and `off_offset` in °C below
target). The logic is reactive and obedient (no prediction), which fits the
integration's philosophy — but it adds a new actuator type and more config UI
surface. Also needs a "master off" propagation so the plug is cut immediately
when the integration's master switch is turned off.

**If revisited:** add `supplementary_heater` to room config (entity_id + on/off
offsets); integrate control into `_apply` alongside the TRV command; ensure the
plug is turned off whenever master is off or the room is paused/away with a
target low enough that it would never trigger.

---

## 6. Air-conditioner inclusion (summer cooling, and AC-as-heater on solar surplus)

**Idea:** Bring AC units into the same per-room, schedule-driven control the TRVs
already get, in two distinct modes:

1. **Summer cooling.** When a room's temperature rises *above* its effective
   target (plus a hysteresis band), command the room's AC into `cool` mode; turn
   it off once the room drops back within the band. This is the exact mirror of
   the existing bang-bang heating loop — same schedules, same targets, same
   active/passive distinction — just with the inequality flipped.
2. **AC-as-heater on solar surplus.** In shoulder/winter conditions, a heat-pump
   AC running in `heat` mode can be cheaper than the gas boiler *when there is
   enough on-site solar generation to power it*. When a configurable solar
   surplus threshold is met, prefer the AC (heat mode) over opening the TRV /
   firing the boiler for that room; fall back to the boiler when the sun goes in.

**Why parked:** Both add a fundamentally new actuator type — a `climate` entity
rather than a TRV — which means new state handling (HVAC modes, setpoints,
fan/swing attributes), a per-room `ac_entity` config selector, and a season/mode
question the system currently never asks (heat vs. cool). The solar-surplus
variant is also mildly **predictive**: deciding when "enough" solar is available
involves a threshold that's only meaningful with a power/solar sensor and some
debouncing (clouds), which pushes against the strict-obedience, purely-reactive
philosophy. Cooling alone is reactive and would fit cleanly; it's parked mainly
because the integration is heating-first today and the cooling path duplicates a
lot of loop/UI surface for a feature only used part of the year.

**Same integration, not a separate one:** everything around the decision is
already shared — room model, schedule blocks, active/passive, master/pause/away,
presence, the card UI and history charts. Splitting cooling into its own
integration would duplicate all of it and force the two to stay in sync forever.
It's the same control problem with the inequality flipped, so it belongs here.

**Dual targets are mandatory.** A schedule block today stores one `target`, but
one number can't serve both modes: 18 °C means "heat to 18" in winter and
"freeze to 18" in summer. Each room/block needs its own `heat_target` and
`cool_target`, and the system picks which is live from an **explicit mode**
(heat / cool / off) — never by guessing the season — so it stays obedient. Mode
selection is where the real complexity lives (a global manual toggle is the
obedient default; auto-by-outdoor-temp would be predictive and is avoided).

**If revisited:**
- Per-room optional `ac_entity` (a `climate` entity) in the room config.
- Separate `heat_target` and `cool_target` per schedule block, plus a global
  heat/cool/off mode toggle that selects which target is live.
- **Cooling:** reuse the schedule/target machinery; add a `cool_hysteresis` and
  drive the AC with the inverted comparison (on when `temp ≥ target + hyst`, off
  when `temp ≤ target − hyst`). Respect master-off, pause and away exactly like
  the TRV path.
- **AC-as-heater:** a single global `solar_threshold_w` setting plus a
  solar/surplus power sensor; when surplus ≥ threshold, route a room's heat
  demand to its AC (heat mode) instead of the boiler. Keep it reactive — act on
  the *current* measured surplus with a short debounce, never a forecast — so it
  still honours the obedience principle. Surface clearly on the status card which
  rooms are being heated by AC vs. boiler, and why.
