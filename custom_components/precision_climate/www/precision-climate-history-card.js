/*
 * Precision Climate — history card.
 *
 * A companion to the schedule card. For each configured room it draws a 24h
 * (configurable) chart with:
 *   - measured room temperature (solid line),
 *   - the effective schedule target (red stepline),
 *   - a translucent band over the periods the room was actively heating.
 *
 * Rooms, their thermometer, their target sensor and their heating sensor are
 * all auto-discovered from the precision_climate status sensor, so no per-room
 * dashboard configuration is needed.
 *
 * History is read from Home Assistant's recorder via the
 * history/history_during_period WebSocket command. No external chart library,
 * no build step, no extra HACS dependency.
 *
 * Usage in a dashboard:
 *   type: custom:precision-climate-history-card
 *   hours: 24            # optional, default 24
 *   entity: sensor.precision_climate_status   # optional; auto-detected
 *
 * No build step, no external dependencies.
 */

const HISTORY_CARD_VERSION = "0.9.5";

// Per-room line colours, assigned round-robin in discovery order.
const ROOM_COLORS = [
  "#ff9800", "#7cd34a", "#22d3ee", "#eab308", "#e879f9", "#60a5fa", "#f87171",
];

const pad = (n) => String(n).padStart(2, "0");

class PrecisionClimateHistoryCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._hours = Number(config && config.hours) || 24;
    this._history = null; // { [entity_id]: [{t: ms, v: number|string}] }
    this._loading = false;
    this._error = null;
    this._lastFetch = 0;
  }

  connectedCallback() {
    // Refresh history periodically while the card is on screen.
    this._timerId = setInterval(() => this._maybeFetch(true), 60000);
  }

  disconnectedCallback() {
    clearInterval(this._timerId);
  }

  set hass(hass) {
    this._hass = hass;
    this._maybeFetch(false);
    this._render();
  }

  getCardSize() {
    return 8;
  }

  _statusState() {
    if (this._config.entity) return this._hass.states[this._config.entity] || null;
    const states = this._hass.states;
    for (const id of Object.keys(states)) {
      const s = states[id];
      if (id.startsWith("sensor.") && s.attributes && s.attributes.schedules) return s;
    }
    return null;
  }

  // Collect every entity_id we need to chart, across all rooms + the boiler.
  _entityIds(status) {
    const ids = new Set();
    const rooms = status ? status.attributes.rooms || {} : {};
    for (const name of Object.keys(rooms)) {
      const r = rooms[name];
      if (r.thermometer_entity_id) ids.add(r.thermometer_entity_id);
      if (r.target_entity_id) ids.add(r.target_entity_id);
      if (r.heating_entity_id) ids.add(r.heating_entity_id);
      if (r.active_entity_id) ids.add(r.active_entity_id);
    }
    const boiler = status && status.attributes.boiler_switch_entity_id;
    if (boiler) ids.add(boiler);
    return [...ids];
  }

  async _maybeFetch(force) {
    if (!this._hass || this._loading) return;
    const status = this._statusState();
    if (!status) return;
    const ids = this._entityIds(status);
    if (!ids.length) return;
    // Throttle: only refetch on demand, on (re)connect, or every ~60s.
    const now = Date.now();
    if (!force && this._history && now - this._lastFetch < 55000) return;

    this._loading = true;
    this._lastFetch = now;
    const start = new Date(now - this._hours * 3600 * 1000).toISOString();
    const end = new Date(now).toISOString();
    try {
      const result = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: start,
        end_time: end,
        entity_ids: ids,
        minimal_response: true,
        no_attributes: true,
        significant_changes_only: false,
      });
      const parsed = {};
      for (const eid of Object.keys(result || {})) {
        // With minimal_response the first row of each entity is a full object,
        // but later rows whose *value* didn't change are returned without a
        // state key (just a timestamp). Carry the last known state forward so a
        // long flat plateau doesn't truncate the line at the last value change.
        let last;
        parsed[eid] = (result[eid] || [])
          .map((p) => {
            // Timestamps: compressed `lu`/`lc` (epoch seconds) or full keys.
            const tsSec = p.lu != null ? p.lu : p.lc;
            const t = tsSec != null ? tsSec * 1000 : Date.parse(p.last_updated || p.last_changed);
            let v = p.s !== undefined ? p.s : p.state;
            if (v === undefined) v = last; // unchanged-value heartbeat row
            else last = v;
            return { t, v };
          })
          .filter((p) => !Number.isNaN(p.t) && p.v !== undefined);
      }
      this._history = parsed;
      this._error = null;
    } catch (err) {
      this._error = (err && err.message) || "Could not load history.";
    } finally {
      this._loading = false;
      this._render();
    }
  }

  _render() {
    if (!this._hass) return;
    const status = this._statusState();

    if (!this._root) {
      this._root = document.createElement("ha-card");
      this._root.header = "Precision Climate — History";
      this._style = document.createElement("style");
      this._style.textContent = STYLE;
      this._body = document.createElement("div");
      this._body.className = "pch-body";
      this._root.appendChild(this._style);
      this._root.appendChild(this._body);
      this.innerHTML = "";
      this.appendChild(this._root);
    }

    if (!status) {
      this._body.innerHTML = `<div class="pch-empty">No Precision Climate status sensor found.</div>`;
      return;
    }

    const rooms = status.attributes.rooms || {};
    const names = Object.keys(rooms);
    if (!names.length) {
      this._body.innerHTML = `<div class="pch-empty">No rooms configured yet.</div>`;
      return;
    }

    const now = Date.now();
    const t0 = now - this._hours * 3600 * 1000;

    let html = "";
    if (this._error) html += `<div class="pch-error">${this._error}</div>`;
    if (!this._history && this._loading) html += `<div class="pch-empty">Loading history…</div>`;

    // Optional boiler strip.
    const boilerId = status.attributes.boiler_switch_entity_id;
    if (boilerId && this._history && this._history[boilerId]) {
      html += this._renderBoilerStrip(this._history[boilerId], t0, now);
    }

    names.forEach((name, i) => {
      html += this._renderRoom(name, rooms[name], ROOM_COLORS[i % ROOM_COLORS.length], t0, now);
    });

    html += `<div class="pch-version">history v${HISTORY_CARD_VERSION}</div>`;
    this._body.innerHTML = html;
  }

  _series(entityId) {
    return (this._history && this._history[entityId]) || [];
  }

  // Build an array of numeric points clamped to [t0, now].
  _numericPoints(entityId, t0, now) {
    return this._series(entityId)
      .map((p) => ({ t: p.t, v: parseFloat(p.v) }))
      .filter((p) => !Number.isNaN(p.v) && p.t >= t0 - 3600000 && p.t <= now);
  }

  _renderRoom(name, info, color, t0, now) {
    const W = 1000;
    const H = 160;
    const padT = 6;
    const padB = 6;
    const innerH = H - padT - padB;

    const tempPts = this._numericPoints(info.thermometer_entity_id, t0, now);
    const targetPts = this._numericPoints(info.target_entity_id, t0, now);
    const heatRanges = this._onRanges(info.heating_entity_id, t0, now);

    const xs = (t) => ((t - t0) / (now - t0)) * W;

    // Away-mode cap for this room (dashed reference line), if configured.
    const away =
      info.away_target != null && !Number.isNaN(Number(info.away_target))
        ? Number(info.away_target)
        : null;

    // Shared y-scale across temp + target so both line up. The away cap is
    // folded into the scale so its dashed line is always visible on the chart.
    const allV = [...tempPts.map((p) => p.v), ...targetPts.map((p) => p.v)];
    if (away != null) allV.push(away);
    if (!allV.length) {
      return `<div class="pch-room"><div class="pch-room-head"><span class="pch-dot" style="background:${color}"></span>${name}</div><div class="pch-nodata">No data in the last ${this._hours}h.</div></div>`;
    }
    let lo = Math.min(...allV);
    let hi = Math.max(...allV);
    if (hi - lo < 2) { const m = (hi + lo) / 2; lo = m - 1; hi = m + 1; }
    lo -= 0.4; hi += 0.4;
    const ys = (v) => padT + (1 - (v - lo) / (hi - lo)) * innerH;

    // Heating bands.
    const bands = heatRanges
      .map((r) => {
        const x1 = xs(Math.max(r.start, t0));
        const x2 = xs(Math.min(r.end, now));
        return `<rect x="${x1.toFixed(1)}" y="${padT}" width="${Math.max(0, x2 - x1).toFixed(1)}" height="${innerH}" fill="${color}" opacity="0.18"/>`;
      })
      .join("");

    // Away-mode cap: a dashed horizontal reference line at the away target.
    const awayLine =
      away != null
        ? `<line x1="0" y1="${ys(away).toFixed(1)}" x2="${W}" y2="${ys(away).toFixed(1)}" stroke="#8ab4f8" stroke-width="1.5" stroke-dasharray="7 5" vector-effect="non-scaling-stroke" opacity="0.9"/>`
        : "";

    // Target stepline (hold each value until the next sample).
    const targetPath = this._steplinePath(targetPts, xs, ys, now);
    // Temperature smooth line; extend to "now" unless the sensor is unavailable.
    const lastTempRaw = this._lastRawState(info.thermometer_entity_id);
    const tempAvailable = lastTempRaw !== null && lastTempRaw !== "unavailable" && lastTempRaw !== "unknown";

    // Active rooms heat as soon as they fall below target; passive rooms only
    // "open and wait". Differentiate the temperature line *per segment*, using
    // the recorded active-state history: solid + full opacity while the room was
    // active, dashed + dimmed while passive. Fall back to the current mode for
    // the whole line when no active history exists yet (fresh install).
    const activeRanges = this._onRanges(info.active_entity_id, t0, now);
    const hasActiveHistory = this._series(info.active_entity_id).length > 0;
    const segments = hasActiveHistory
      ? this._segmentByActive(tempPts, activeRanges)
      : [{ active: info.active === true, pts: tempPts }];
    const tempPaths = segments
      .map((seg, i) => {
        const isLast = i === segments.length - 1;
        const d = this._linePath(seg.pts, xs, ys, isLast && tempAvailable ? now : null);
        if (!d) return "";
        const dash = seg.active ? "" : ` stroke-dasharray="6 4"`;
        const opacity = seg.active ? "1" : "0.7";
        const width = seg.active ? "2.5" : "2";
        return `<path d="${d}" fill="none" stroke="${color}" stroke-width="${width}" vector-effect="non-scaling-stroke" stroke-linejoin="round"${dash} opacity="${opacity}"/>`;
      })
      .join("");

    // Hour gridlines every 6h.
    const grid = this._hourGrid(t0, now, xs, padT, innerH);

    const curTemp = tempPts.length ? tempPts[tempPts.length - 1].v : null;
    const curTarget = targetPts.length ? targetPts[targetPts.length - 1].v : null;

    // Badge reflects the room's *current* mode.
    const isActive = info.active === true;
    const modeBadge = `<span class="pch-mode-badge ${isActive ? "pch-mode-active" : "pch-mode-passive"}">${isActive ? "active" : "passive"}</span>`;

    const stats =
      `<span class="pch-stat" style="color:${color}">${curTemp != null ? curTemp.toFixed(1) + "°" : "—"}</span>` +
      `<span class="pch-stat pch-target-stat">target ${curTarget != null ? curTarget.toFixed(1) + "°" : "—"}</span>` +
      (away != null ? `<span class="pch-stat pch-away-stat">away ${away.toFixed(1)}°</span>` : "");

    // Y-axis: 4 evenly-spaced ticks (hi, 2 intermediate, lo).
    const NUM_TICKS = 4;
    const ticks = Array.from({ length: NUM_TICKS }, (_, i) => hi - (i / (NUM_TICKS - 1)) * (hi - lo));

    // Horizontal gridlines at each tick.
    const hGrid = ticks.map((v) => {
      const y = ys(v).toFixed(1);
      return `<line x1="0" y1="${y}" x2="${W}" y2="${y}" stroke="var(--divider-color,#444)" stroke-width="1" opacity="0.5" vector-effect="non-scaling-stroke"/>`;
    }).join("");

    // Y-axis labels rendered as HTML (outside SVG) so they don't distort with
    // preserveAspectRatio="none" scaling. Shown on both left and right sides.
    const yAxisHtml = (cls) => `
      <div class="${cls}">
        ${ticks.map((v) => `<span>${v.toFixed(1)}</span>`).join("")}
      </div>`;

    return `
      <div class="pch-room">
        <div class="pch-room-head">
          <span><span class="pch-dot" style="background:${color}"></span>${name}${modeBadge}</span>
          <span class="pch-stats">${stats}</span>
        </div>
        <div class="pch-chart-wrap">
          ${yAxisHtml("pch-yaxis")}
          <div class="pch-chart-area">
            <svg class="pch-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
              ${hGrid}
              ${grid}
              ${bands}
              ${awayLine}
              <path d="${targetPath}" fill="none" stroke="var(--error-color,#d9663b)" stroke-width="2" vector-effect="non-scaling-stroke"/>
              ${tempPaths}
            </svg>
            <div class="pch-time-axis">${this._timeLabels(t0, now)}</div>
          </div>
          ${yAxisHtml("pch-yaxis pch-yaxis-right")}
        </div>
      </div>`;
  }

  _renderBoilerStrip(series, t0, now) {
    const W = 1000;
    const H = 22;
    const ranges = this._onRangesFromSeries(series, t0, now, "on");
    const xs = (t) => ((t - t0) / (now - t0)) * W;
    const bands = ranges
      .map((r) => {
        const x1 = xs(Math.max(r.start, t0));
        const x2 = xs(Math.min(r.end, now));
        return `<rect x="${x1.toFixed(1)}" y="0" width="${Math.max(0, x2 - x1).toFixed(1)}" height="${H}" fill="var(--error-color,#d9663b)" opacity="0.55"/>`;
      })
      .join("");
    return `
      <div class="pch-room pch-boiler">
        <div class="pch-room-head"><span>🔥 Boiler</span></div>
        <svg class="pch-svg pch-boiler-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
          <rect x="0" y="0" width="${W}" height="${H}" fill="var(--divider-color,#444)" opacity="0.25"/>
          ${bands}
        </svg>
      </div>`;
  }

  // --- geometry helpers ----------------------------------------------------

  // Return the raw state string of the last point for an entity (may be
  // "unavailable", "unknown", or a numeric string).
  _lastRawState(entityId) {
    const series = this._series(entityId);
    if (!series.length) return null;
    return String(series[series.length - 1].v).toLowerCase();
  }

  _linePath(pts, xs, ys, extendTo) {
    if (!pts.length) return "";
    let d = pts.map((p, i) => `${i === 0 ? "M" : "L"}${xs(p.t).toFixed(1)},${ys(p.v).toFixed(1)}`).join(" ");
    // Extend to the right edge with the last known value (caller passes extendTo
    // only when the sensor is not currently unavailable/unknown).
    if (extendTo != null) {
      const last = pts[pts.length - 1];
      d += ` L${xs(extendTo).toFixed(1)},${ys(last.v).toFixed(1)}`;
    }
    return d;
  }

  _steplinePath(pts, xs, ys, now) {
    if (!pts.length) return "";
    let d = "";
    pts.forEach((p, i) => {
      const x = xs(p.t).toFixed(1);
      const y = ys(p.v).toFixed(1);
      if (i === 0) d += `M${x},${y}`;
      else d += ` L${x},${ys(pts[i - 1].v).toFixed(1)} L${x},${y}`;
    });
    // Hold the last value to the right edge.
    const last = pts[pts.length - 1];
    d += ` L${xs(now).toFixed(1)},${ys(last.v).toFixed(1)}`;
    return d;
  }

  // Build [{start, end}] ranges where a binary entity was "on".
  _onRanges(entityId, t0, now) {
    return this._onRangesFromSeries(this._series(entityId), t0, now, "on");
  }

  _tInRanges(t, ranges) {
    return ranges.some((r) => t >= r.start && t < r.end);
  }

  // Split a point series into consecutive runs sharing the same active state,
  // derived from the active "on" ranges. Adjacent runs share their boundary
  // point so the rendered line stays visually continuous (no gaps).
  _segmentByActive(pts, ranges) {
    if (!pts.length) return [];
    const segs = [];
    let cur = { active: this._tInRanges(pts[0].t, ranges), pts: [pts[0]] };
    for (let i = 1; i < pts.length; i++) {
      const a = this._tInRanges(pts[i].t, ranges);
      cur.pts.push(pts[i]); // extend first so the segment reaches this point
      if (a !== cur.active) {
        segs.push(cur);
        cur = { active: a, pts: [pts[i]] }; // next run starts at the shared point
      }
    }
    segs.push(cur);
    return segs;
  }

  _onRangesFromSeries(series, t0, now, onState) {
    const ranges = [];
    let open = null;
    for (const p of series) {
      const isOn = String(p.v).toLowerCase() === onState;
      if (isOn && open === null) open = p.t;
      else if (!isOn && open !== null) { ranges.push({ start: open, end: p.t }); open = null; }
    }
    if (open !== null) ranges.push({ start: open, end: now });
    return ranges;
  }

  _hourGrid(t0, now, xs, padT, innerH) {
    let g = "";
    const startH = new Date(t0);
    startH.setMinutes(0, 0, 0);
    for (let t = startH.getTime(); t <= now; t += 6 * 3600 * 1000) {
      if (t < t0) continue;
      const x = xs(t).toFixed(1);
      g += `<line x1="${x}" y1="${padT}" x2="${x}" y2="${padT + innerH}" stroke="var(--divider-color,#444)" stroke-width="1" opacity="0.4" vector-effect="non-scaling-stroke"/>`;
    }
    return g;
  }

  _timeLabels(t0, now) {
    const labels = [];
    const startH = new Date(t0);
    startH.setMinutes(0, 0, 0);
    for (let t = startH.getTime(); t <= now; t += 6 * 3600 * 1000) {
      if (t < t0) continue;
      const d = new Date(t);
      const left = ((t - t0) / (now - t0)) * 100;
      labels.push(`<span style="left:${left.toFixed(2)}%">${pad(d.getHours())}:00</span>`);
    }
    return labels.join("");
  }
}

const STYLE = `
  .pch-body { padding: 8px 14px 14px; }
  .pch-room { margin-bottom: 14px; }
  .pch-room-head { display: flex; align-items: center; justify-content: space-between; font-weight: 600; font-size: .95em; margin-bottom: 2px; }
  .pch-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  .pch-mode-badge { font-weight: 600; font-size: .68em; text-transform: uppercase; letter-spacing: .04em; padding: 1px 6px; border-radius: 8px; margin-left: 8px; vertical-align: middle; }
  .pch-mode-active { background: var(--error-color, #d9663b); color: #fff; }
  .pch-mode-passive { background: var(--secondary-background-color, rgba(255,255,255,.1)); color: var(--secondary-text-color, #bbb); border: 1px solid var(--divider-color, #444); }
  .pch-stats { font-weight: 400; }
  .pch-stat { font-weight: 600; margin-left: 8px; }
  .pch-target-stat { color: var(--error-color, #d9663b); opacity: .9; }
  .pch-away-stat { color: #8ab4f8; opacity: .9; }

  /* Chart layout: [y-axis] [svg+time-axis] [y-axis] */
  .pch-chart-wrap { display: flex; align-items: stretch; gap: 4px; }
  .pch-yaxis {
    display: flex; flex-direction: column; justify-content: space-between;
    font-size: .72em; opacity: .6; min-width: 30px;
    text-align: right; padding-bottom: 16px; /* align with SVG, not time axis */
    color: var(--secondary-text-color, #999);
  }
  .pch-yaxis-right { text-align: left; }
  .pch-chart-area { flex: 1; min-width: 0; }

  .pch-svg { width: 100%; height: 140px; display: block; background: var(--secondary-background-color, #1c1c1c); border-radius: 6px; }
  .pch-boiler-svg { height: 22px; }
  .pch-time-axis { position: relative; height: 16px; font-size: .72em; opacity: .55; margin-top: 2px; }
  .pch-time-axis span { position: absolute; transform: translateX(-50%); }
  .pch-nodata, .pch-empty { opacity: .6; font-size: .85em; padding: 8px 0; }
  .pch-error { color: var(--error-color, #d9663b); font-size: .85em; margin-bottom: 8px; }
  .pch-version { text-align: right; font-size: .7em; opacity: .35; margin-top: 8px; }
`;

customElements.define("precision-climate-history-card", PrecisionClimateHistoryCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "precision-climate-history-card",
  name: "Precision Climate History",
  description: "Per-room temperature / target / heating history for Precision Climate.",
});
