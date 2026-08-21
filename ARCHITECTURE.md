# Architecture — decision precedence

This is the single source of truth for **which rule wins** in Precision Climate.
A room's outcome is decided by a three-stage pipeline; each stage consumes the
previous stage's output. Keep this file in sync when you add or reorder a rule.

```
Stage A  control/mode.py  resolve_room_mode()   ->  (target, is_active) per room
Stage B  control/loop.py  evaluate()            ->  boiler_on + per-valve open
Stage C  coordinator.py   _apply + failsafes    ->  commands + notifications
```

## Stage A — per-room mode (`resolve_room_mode`)

Decides each room's effective `(target, is_active)`. Highest rank wins.

| # | Rule | Effect |
|---|------|--------|
| 1 | **Boost** | `(boost_target, active)`. Wins over everything in this stage — including forced_passive. |
| 2 | **Pause** | `(pause_target = 5°, schedule active flag)`. Target so low it never demands; the flag is left unchanged. |
| 3 | **Per-room away** (manual toggle **or** presence-absent→away) | `min(target, away_target)`, **passive**. "Away = passive." |
| 4 | **Presence** | occupied → `present_action`; vacant → `absent_action`. Absent→away folds up into rank 3. |
| 5 | **Global away** | caps target, **keeps** the active flag — the sole exception to "away = passive", so the boiler can still hold the away temp. |
| 6 | **Soft away** (alarm armed) | lowers target by a fixed delta, clamped ≥ away target. Only when no away is active. |
| 7 | **forced_passive** (sticky failsafe action) | forces **passive** (active flag only, target untouched). Applied on the global-away and schedule paths; intentionally exempt on the boost / pause / per-room-away early returns (see the docstring). |
| 8 | **Schedule** | base target + active flag. |

## Stage B — control loop (`evaluate`)

Uses Stage A's `(target, is_active)` plus window / master / sunny to decide the
boiler and each valve.

| # | Rule | Effect |
|---|------|--------|
| 1 | **Master off / paused** | boiler OFF, all valves CLOSED. |
| 2 | **Window open (per-room)** | that valve CLOSED and the room excluded from boiler demand. If *every* active room is windowed → boiler OFF. |
| 3 | **Sunny-day** | active-room target reduced via `min(target, sunny_target)` in `_effective_target` — only ever lowers, so it composes with Stage A reductions. |
| 4 | **All active thermometers offline** | boiler OFF (no feedback is unsafe). |
| 5 | **Boiler hysteresis** | any active `≤ target − lower` → ON; all active `≥ target + upper` → OFF; else hold. |
| 6 | **Valve rule** | window → closed; else open below target, close at `target + upper`, hold in band; active-no-temp → closed; passive-no-temp → hold. |

## Stage C — apply & failsafes (`coordinator`)

- **Apply** — command the boiler and each valve to its force/block sentinel
  (the valve's own max/min). The **boost drift-guard** leaves a hand-dialed
  valve alone for `TRV_DRIFT_GRACE_SECONDS` (180 s) so it isn't fought mid-turn.
- **Failsafes** — `unauthorized_boiler`, `prolonged_heating` (5 h), `overheating`,
  `trv_mismatch`, `trv_unresponsive`, `trv_unavailable`. Each notifies and can
  take a **sticky action** (pause / away / passive / boiler-off), configured per
  warning in the integration Options.

## Glossary — the four "don't heat this room" states

These all mean "this room shouldn't heat" but differ in mechanism. Don't
conflate them.

| State | Mechanism | Target | Active flag | Clears when |
|-------|-----------|--------|-------------|-------------|
| **Manual pause** | Stage A rank 2 | dropped to 5° | unchanged | you resume it |
| **Window pause** | Stage B rank 2 | unchanged | (excluded from demand) | the window closes |
| **forced_passive** | Stage A rank 7 | unchanged | forced False | you clear it (or the failsafe re-fires) |
| **Away** (any) | Stage A ranks 3/5 | capped to away target | passive (rank 3) / kept (rank 5) | you clear away / presence returns |

## Notes for maintainers

- **Two target modifiers, two stages.** Away/soft-away/pause/boost set the target
  in Stage A; sunny-day lowers it again in Stage B (via `min`). If you add a
  third target modifier, decide deliberately which stage and whether it `min`s.
- **"Open" means the sentinel, not the target.** A heating valve is commanded to
  the force sentinel (valve max), far above the room target. `trv_mismatch`
  therefore flags a heating valve whose real setpoint is `<= target` — a valve
  at or below its own target while heating didn't take the open command.
- The pure stages (A and B) are unit-tested in `tests/`. Stage C (orchestration)
  is not yet — that's the current test debt.
