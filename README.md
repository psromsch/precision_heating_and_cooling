# Precision Climate

**Per-room heating control for a single shared boiler — that does exactly what you tell it, and nothing you didn't.**

Precision Climate turns a house with one central boiler and a set of smart radiator valves (TRVs) into a room-by-room heating system. You give each room a weekly schedule of target temperatures, and the integration decides — every time a thermometer moves, a window opens, or a schedule boundary passes — whether the boiler should run and which radiators should be open.

Its guiding principle is **obedience**. When you say a room should be 20 °C at 7 am, it heats to 20 °C at 7 am. It does not try to learn your habits, predict the weather, or start early because it thinks it knows better. Everything it does follows from rules you can read on this page. If it ever surprises you, that's a bug, not a feature.

---

## Table of contents

- [The 60-second mental model](#the-60-second-mental-model)
- [The pieces](#the-pieces)
- [Active vs. Passive rooms — the one idea to understand](#active-vs-passive-rooms--the-one-idea-to-understand)
- [Hysteresis, explained plainly](#hysteresis-explained-plainly)
- [How a decision is actually made](#how-a-decision-is-actually-made)
- [Away mode (three flavours)](#away-mode-three-flavours)
- [Per-room occupancy sensors](#per-room-occupancy-sensors)
- [The everyday controls](#the-everyday-controls-pause-boost-windows)
- [Safety failsafes](#safety-failsafes)
- [The dashboard cards](#the-dashboard-cards)
- [Setup](#setup)
- [A worked example](#a-worked-example)
- [FAQ](#faq)

---

## The 60-second mental model

Two things get decided, over and over:

1. **Should the boiler be ON?** Only "active" rooms get a vote. If any active room is cold enough, the boiler runs. When all active rooms are warm enough, it stops.
2. **Should each radiator be OPEN?** A room's valve opens when the room is below its target and closes when it's a little above. Heat only actually flows when the boiler is running — an open valve with the boiler off does nothing.

That's the whole engine. Everything else — schedules, away mode, presence, boost — just changes each room's **target** and whether it's **active** or **passive** for the current moment.

---

## The pieces

You'll wire up a few kinds of entities:

| Piece | What it is | Example |
|---|---|---|
| **Boiler switch** | The single switch that turns your central boiler / pump on and off. | `switch.boiler` |
| **TRV(s)** | Smart thermostatic radiator valves — one or more per room. Precision Climate commands them fully **open** or fully **closed**. | `climate.living_trv` |
| **Thermometer** | A temperature sensor per room — the "precision" reading the logic trusts (not the TRV's own sensor). | `sensor.living_temperature` |
| **Window sensor** *(optional)* | Opens the circuit: if an *active* room's window opens, the boiler is held off. | `binary_sensor.living_window` |
| **Occupancy sensor** *(optional)* | An mmWave/presence sensor that makes a room active or passive based on whether anyone's there. | `binary_sensor.living_presence` |

Rooms are the unit of everything. Each room has a thermometer, one or more TRVs, a weekly schedule, and its own hysteresis settings.

---

## Active vs. Passive rooms — the one idea to understand

Every room, at every moment, is either **active** or **passive**. This is the single most important concept, and it means exactly one thing:

> **Active rooms can turn the boiler ON. Passive rooms cannot.**

- An **active** room that gets cold **demands heat** — it fires up the boiler for the whole house.
- A **passive** room never fires the boiler. But it's not left out in the cold: whenever the boiler is *already* running (because some active room asked for it), a passive room below its target **opens its valve and heats too**. We call this "riding" — it grabs the free heat while the water is already hot, then stops when the boiler goes off.

Think of it like a car trip: active rooms decide *when the engine starts*. Passive rooms don't get to start the engine, but once it's running, they hop in.

You set active vs. passive **per time block** in each room's schedule. A common setup: living room active in the evening (it drives the heat), bedrooms passive (they warm up along with it, but don't burn gas on their own overnight).

---

## Hysteresis, explained plainly

Thermostats that switch exactly at the target temperature "chatter" — flicking on and off every few seconds as the temperature wobbles around the setpoint. **Hysteresis** is the small buffer that stops that. Each room has two numbers:

- **Lower hysteresis** — how far *below* target an **active** room must drop before it demands the boiler. With target 20 °C and lower hysteresis 0.5, the boiler is asked for at **19.5 °C**.
- **Upper hysteresis** — how far *above* target a room heats before its valve closes. With upper 0.5, the valve shuts at **20.5 °C**.

So a room "breathes" between roughly `target − lower` and `target + upper`, instead of buzzing at the exact target.

Two things worth knowing:
- **Lower hysteresis only matters for active rooms** — it's the "how cold before I call the boiler" number, and passive rooms never call the boiler. For a passive room, only the target and the upper hysteresis matter.
- You can set one side to 0 (but not both).

---

## How a decision is actually made

Every time something changes (a thermometer reading, a window, a schedule boundary, or a periodic safety tick), the integration runs one pass:

**Boiler (only active rooms vote):**
- Turn **ON** the moment *any* active room falls to `target − lower hysteresis`.
- Turn **OFF** when *every* active room has reached `target + upper hysteresis`.
- Otherwise, **hold** whatever it was doing.

**Each radiator valve:**
- **Open** when the room is below its target.
- **Close** when the room reaches `target + upper hysteresis`.
- In between, hold.
- (Remember: heat only flows while the boiler runs, so a passive room's open valve just waits for the boiler.)

**A few overrides sit on top**, highest priority first:
1. **Master switch OFF** or a room **paused** → boiler off / that room stops.
2. An **active room's window is open** → boiler held off (no point heating the street).
3. **Sunny-day savings** (optional) → active-room targets are trimmed when a sunny day is forecast.

That's it. No hidden estimation, no learning period.

---

## Away mode (three flavours)

"Away" lowers targets to a configured **away temperature** so you don't heat an empty house. There are three independent ways it can switch on:

1. **Manual** — an Away switch (or the button on the card). You're in control; it stays until you turn it off.
2. **Presence / zone based** — tell it which people to watch and which zone counts as "home" (this can be a whole city, not just your house). When everyone has been outside that zone for a grace period, away engages; when someone returns, it lifts. It's *edge-triggered* — it reacts to you actually leaving, and if you turn it off manually while still away, it won't nag you back until your next real departure. You get a notification when it flips.
3. **Holiday window** — set a start and end date/time; away runs for exactly that window, and survives restarts.

**One simple rule to remember: away means passive.** Any room that is individually "away" (either you toggled it, or its occupancy sensor decided nobody's there) stops driving the boiler — it's capped to the away target *and* made passive.

**The single exception:** when the *whole system* is away, rooms keep their scheduled active/passive flags. Otherwise nothing could ever fire the boiler and the house would drift below the away temperature. So global away lowers the targets but still lets an active room hold the house at, say, 15 °C.

You can also set a **per-room away** from the card — handy for a guest room you're not using.

### Soft away (alarm-triggered)

A gentler cousin of away: point it at an alarm panel (e.g. **Alarmo**) and, while that panel is armed (by default `armed_away` / `armed_vacation`), **every room's target drops by a fixed amount** you choose. The house still heats — just cooler. It only lowers the target; it doesn't change active/passive. Any real away (per-room, presence, or whole-system) **overrules** soft away, and it never drops a room below its away target — so "soft" is always gentler than "full." Configure it in the card's settings, under Away Mode.

---

## Per-room occupancy sensors

Give any room an optional occupancy sensor (an mmWave radar is ideal) and let *presence* decide whether it's active or passive, overriding the schedule:

- **When occupied** → the room becomes **active** or **passive** (your choice).
- **When vacant** → the room becomes **passive** or **away** (your choice).
- **Dwell times** — "occupied for X minutes" before it counts (so a quick walk-through doesn't fire the boiler) and "vacant for Y minutes" before it stands down (so a moment of stillness doesn't drop the room). If the sensor goes unavailable, the room simply holds its last known state.

Example: set a bedroom to **occupied → passive, vacant → away**. It rides the boiler while you're in it, and drops to the away temperature when you leave — hands-free.

Rooms with an occupancy sensor show a neutral timeline on the card (targets only) with a 👤 badge, because their active/passive state is now decided live by presence rather than by the schedule.

---

## The everyday controls (pause, boost, windows)

- **Pause a room** — one tap stops a room heating until you resume it. The schedule is untouched.
- **Boost** — just grab a radiator and turn its dial up by hand. Precision Climate notices, makes that room active at the temperature you dialled for a few hours (configurable), then quietly returns it to its schedule. It won't fight your hand while you turn it, and a restart won't cancel a boost in progress.
- **Windows** — open a window in an active room and the boiler is held off until you close it.
- **Child locks** *(optional)* — map a lock entity per TRV and toggle them from the card.

---

## Safety failsafes

Precision Climate watches reality, not just its own intentions, and will alert you (via any `notify.*` services you configure) and self-correct:

- **Unauthorized boiler** — the boiler is on when nothing should be running → forced off.
- **Prolonged heating** — the boiler has run for many hours straight.
- **Overheating** — a room has climbed well past a safe temperature.
- **Stuck / unresponsive / offline valve** — a TRV isn't reaching its setpoint, isn't warming its room, or has dropped off the network while heating.

If an active room's thermometer goes offline, that room is safely excluded (its valve closes) rather than trusted blindly; if *all* active thermometers are offline, the boiler stops.

---

## The dashboard cards

Two custom Lovelace cards ship with the integration (auto-loaded — no manual resource setup):

- **Schedule card** (`custom:precision-climate-schedule-card`) — a visual weekly timeline per room. Edit blocks (start/end/target/active) by hand, reorder rooms, and drive every control (pause, boost cancel, per-room away, master switch, and the global settings panel for away targets, presence, holiday window, boost duration, sunny-day, etc.).
- **History card** (`custom:precision-climate-history-card`) — per-room charts of measured temperature vs. the effective target, with the heating periods shaded, so you can see exactly what happened and why.

Add either with `type: custom:precision-climate-schedule-card` — the card finds the integration automatically.

---

## Setup

1. Install via HACS (or copy `custom_components/precision_climate` into your config).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Precision Climate.**
4. Pick your **boiler switch** and, optionally, notification services.
5. Open **Configure** to add rooms — for each room choose its TRV(s), thermometer, optional windows/occupancy sensor, and hysteresis. New rooms start with a simple all-day 18 °C schedule.
6. Add the **Schedule card** to a dashboard and set your real schedules there.

**Requirements:** Home Assistant 2024.1.0+. Works fully locally — no cloud, no account.

---

## A worked example

Living room (active in the evening, target 20 °C, hysteresis 0.5/0.5) and a bedroom (passive, target 18 °C):

- 8:00 pm — the living room has cooled to **19.5 °C** → it demands heat → **boiler ON**. Its valve is open; it heats toward 20 °C.
- At the same moment the bedroom is at **17.6 °C** (below its 18 °C target) → since the boiler is now running, the bedroom **rides**: its valve opens and it heats too.
- 8:40 pm — the living room reaches **20.5 °C** → its valve closes; it's satisfied. The bedroom, still below 18 °C, keeps heating **as long as the boiler stays on** for other active rooms.
- When every active room is satisfied → **boiler OFF**. The bedroom stops wherever it got to and simply holds that temperature. No gas was burned on the bedroom's behalf — it only ever took a ride.

Now put an mmWave sensor in the bedroom set to **occupied → active, vacant → passive**: walk in, and after your dwell time it starts driving the boiler itself; leave, and it goes back to just riding.

---

## FAQ

**Does it learn my habits or predict the weather?**
No — on purpose. It's reactive and obedient. (A short, honest list of the clever-but-unpredictable features we deliberately *didn't* build lives in `POTENTIAL_IMPROVEMENTS.md`.)

**Why isn't my passive room heating even though it's below target?**
Because the boiler isn't running. Passive rooms only heat while an active room has the boiler on. If you want it to heat on its own, make it active in that time block.

**Why didn't the boiler come on when a room dropped just below target?**
Active rooms call the boiler at `target − lower hysteresis`, not exactly at target. That small buffer prevents rapid on/off cycling.

**I turned a radiator up by hand — will the system override me?**
No. That starts a Boost: your room heats to the temperature you set for a few hours, then returns to its schedule.

**Can two rooms share a thermometer?**
Yes. Only TRVs must be exclusive to one room (two rooms can't command the same valve). Thermometers and window sensors can be shared freely.

**It says a room is "away" — what does that do?**
Caps it to the away temperature and makes it passive. The exception is whole-system away, which keeps rooms' scheduled active/passive so the boiler can still hold the house at the away temperature.

---

*Precision Climate is a local-only Home Assistant custom integration. It controls real heating hardware — set it up thoughtfully, keep your failsafe notifications on, and it will quietly do exactly what you asked.*
