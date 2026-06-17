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

## 4. Per-room presence sensor (entity-based room occupancy)

**Idea:** Allow selecting a presence sensor (binary, e.g. a mmWave radar) per
room in the room config. When the room is unoccupied, apply the room's away
temperature automatically (without affecting other rooms).

**Why parked:** Requires a per-room binary_sensor entity selector in the room
config flow UI, plus logic to distinguish per-room away from global away.
Adds complexity to an already multi-layered away-mode system.

**If revisited:** add `presence_entity` to room config; when its state is `off`,
treat the room as individually away (capped to `away_target`); restore when
state returns to `on`. No learning, fully reactive — fits the obedience
philosophy. Lower priority than the global zone-based presence that's already
implemented.
