/*
 * Precision Climate — visual schedule card.
 *
 * Renders each room's daily schedule as a 24h timeline and lets you edit the
 * blocks (start/end/target/active) without touching the text format. Saving
 * calls the precision_climate.set_schedule service, which validates full-day
 * coverage server-side and reloads the integration.
 *
 * Usage in a dashboard:
 *   type: custom:precision-climate-schedule-card
 *   entity: sensor.precision_climate_status   # optional; auto-detected if omitted
 *
 * No build step, no external dependencies.
 */

const DAY_LABELS = {
  all: "Every day",
  weekday: "Weekdays",
  weekend: "Weekend",
  mon: "Monday",
  tue: "Tuesday",
  wed: "Wednesday",
  thu: "Thursday",
  fri: "Friday",
  sat: "Saturday",
  sun: "Sunday",
};

const DAY_ORDER = ["all", "weekday", "weekend", "mon", "tue", "wed", "thu", "fri", "sat", "sun"];

// Shown in the card footer so you can confirm which card version is live
// after a HACS update (keep in sync with manifest.json).
const CARD_VERSION = "0.2.3";

const pad = (n) => String(n).padStart(2, "0");
const minToHHMM = (m) => {
  const h = Math.floor(m / 60);
  const mn = m % 60;
  if (m === 1440) return "24:00";
  return `${pad(h % 24)}:${pad(mn)}`;
};
const hhmmToMin = (s) => {
  const [h, m] = String(s).split(":").map((x) => parseInt(x, 10));
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  return h * 60 + m;
};

function nowMinutes() {
  const d = new Date();
  return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60;
}

class PrecisionClimateScheduleCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._edit = null; // { room_id, day_key, blocks: [...] }
    this._error = null;
  }

  connectedCallback() {
    // Update the time-line every 30 seconds.
    this._timerId = setInterval(() => this._updateTimeLine(), 30000);
  }

  disconnectedCallback() {
    clearInterval(this._timerId);
  }

  set hass(hass) {
    this._hass = hass;
    // While the editor is open, live state updates from HA must not trigger a
    // re-render: doing so rebuilds the inputs, dropping focus and discarding
    // any in-progress edits. The editor is rendered explicitly on open.
    if (this._edit) return;
    this._render();
  }

  getCardSize() {
    return 6;
  }

  _statusEntity() {
    if (this._config.entity) return this._config.entity;
    const states = this._hass.states;
    for (const id of Object.keys(states)) {
      if (id.startsWith("sensor.") && states[id].attributes && states[id].attributes.schedules) {
        return id;
      }
    }
    return null;
  }

  _statusState() {
    const eid = this._statusEntity();
    if (!eid || !this._hass.states[eid]) return null;
    return this._hass.states[eid];
  }

  _schedules() {
    const s = this._statusState();
    return s ? (s.attributes.schedules || []) : [];
  }

  _roomsInfo() {
    const s = this._statusState();
    return s ? (s.attributes.rooms || {}) : {};
  }

  _startEdit(room, dayKey) {
    const blocks = (room.blocks[dayKey] || []).map((b) => ({ ...b }));
    this._edit = { room_id: room.room_id, day_key: dayKey, blocks };
    this._render();
  }

  _cancelEdit() {
    this._edit = null;
    this._render();
  }

  async _save() {
    const e = this._edit;
    const toMin = (v) => (typeof v === "string" ? hhmmToMin(v) : v);
    const blocks = e.blocks.map((b, i) => {
      let start = toMin(b.start_min);
      let end = toMin(b.end_min);
      // A blank <input type="time"> yields null. The day's final block ends at
      // 24:00 (1440); a null end on the last row almost always means that. Treat
      // a null/0 end on the last block as the end-of-day boundary so we never
      // send null to the service (which rejects it with "expected int").
      if ((end === null || end === 0) && i === e.blocks.length - 1) end = 1440;
      return {
        start_min: start,
        end_min: end,
        target: parseFloat(b.target),
        is_active: !!b.is_active,
      };
    });
    // Guard: bail out with a friendly message instead of sending bad data.
    const bad = blocks.find((b) => b.start_min === null || b.end_min === null);
    if (bad) {
      this._error = "Every block needs a valid start and end time.";
      this._render();
      return;
    }
    try {
      await this._hass.callService("precision_climate", "set_schedule", {
        room_id: e.room_id,
        day_key: e.day_key,
        blocks,
      });
      this._edit = null;
      this._error = null;
    } catch (err) {
      this._error = (err && err.message) || "Save failed (check full-day coverage).";
    }
    this._render();
  }

  // Move the time-line needle without a full re-render.
  _updateTimeLine() {
    if (!this._body || this._edit) return;
    const pct = (nowMinutes() / 1440) * 100;
    this._body.querySelectorAll(".pcs-needle").forEach((el) => {
      el.style.left = `${pct}%`;
    });
  }

  _render() {
    if (!this._hass) return;
    const schedules = this._schedules();

    if (!this._root) {
      this._root = document.createElement("ha-card");
      this._root.header = "Precision Climate — Schedules";
      this._style = document.createElement("style");
      this._style.textContent = STYLE;
      this._body = document.createElement("div");
      this._body.className = "pcs-body";
      this._root.appendChild(this._style);
      this._root.appendChild(this._body);
      this.innerHTML = "";
      this.appendChild(this._root);
    }

    if (!schedules.length) {
      this._body.innerHTML = `<div class="pcs-empty">No rooms found. Add rooms in the integration's Configure dialog.</div>`;
      return;
    }

    if (this._edit) {
      this._body.innerHTML = this._renderEditor(schedules);
      this._wireEditor();
      return;
    }

    const statusState = this._statusState();
    const boilerOn = statusState ? !!statusState.attributes.boiler_on : false;
    const reason = statusState ? (statusState.state || "") : "";
    const roomsInfo = this._roomsInfo();

    this._body.innerHTML =
      this._renderBoilerStatus(boilerOn, reason) +
      schedules.map((room) => this._renderRoom(room, roomsInfo)).join("") +
      `<div class="pcs-version">card v${CARD_VERSION}</div>`;

    this._body.querySelectorAll("[data-edit]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const [rid, day] = btn.getAttribute("data-edit").split("|");
        const room = schedules.find((r) => r.room_id === rid);
        this._startEdit(room, day);
      });
    });

    // Position needle immediately after render.
    this._updateTimeLine();
  }

  _renderBoilerStatus(boilerOn, reason) {
    const icon = boilerOn ? "🔥" : "⚪";
    const label = boilerOn ? "Boiler ON" : "Boiler OFF";
    const cls = boilerOn ? "pcs-boiler-on" : "pcs-boiler-off";
    const reasonHtml = reason ? ` <span class="pcs-reason">— ${reason}</span>` : "";
    return `<div class="pcs-boiler ${cls}">${icon} <strong>${label}</strong>${reasonHtml}</div>`;
  }

  _renderRoom(room, roomsInfo) {
    // roomsInfo is keyed by room name (as set in StatusSensor).
    const info = roomsInfo[room.name] || {};
    const temp = info.temperature != null ? `${Number(info.temperature).toFixed(1)}°C` : null;
    const heating = !!info.heating;

    const heatIcon = heating ? `<span class="pcs-heat-icon" title="Heating">🔥</span>` : "";
    const tempSpan = temp ? `<span class="pcs-cur-temp">(${temp})</span>` : "";

    const dayKeys = Object.keys(room.blocks).sort(
      (a, b) => DAY_ORDER.indexOf(a) - DAY_ORDER.indexOf(b)
    );
    const days = dayKeys
      .map((key) => {
        const blocks = (room.blocks[key] || []).slice().sort((a, b) => a.start_min - b.start_min);
        const segs = blocks
          .map((b) => {
            const w = ((b.end_min - b.start_min) / 1440) * 100;
            const cls = b.is_active ? "active" : "passive";
            const timeRange = `${minToHHMM(b.start_min)}–${minToHHMM(b.end_min)}`;
            const tooltip = `${b.target}°C (${timeRange}) ${b.is_active ? "active" : "passive"}`;
            return `<div class="pcs-seg ${cls}" style="width:${w.toFixed(4)}%" title="${tooltip}">
              <span class="pcs-seg-label">${b.target}°<br><span class="pcs-seg-time">${timeRange}</span></span>
            </div>`;
          })
          .join("");
        return `
          <div class="pcs-day">
            <div class="pcs-day-head">
              <span>${DAY_LABELS[key] || key}</span>
              <button class="pcs-btn" data-edit="${room.room_id}|${key}">Edit</button>
            </div>
            <div class="pcs-timeline-wrap">
              <div class="pcs-timeline">${segs}</div>
              <div class="pcs-needle"></div>
            </div>
            <div class="pcs-axis">
              <span>00</span><span>04</span><span>08</span><span>12</span>
              <span>16</span><span>20</span><span>24</span>
            </div>
          </div>`;
      })
      .join("");

    return `<div class="pcs-room">
      <div class="pcs-room-name">${room.name}${heatIcon}${tempSpan}</div>
      ${days}
    </div>`;
  }

  _renderEditor(schedules) {
    const e = this._edit;
    const room = schedules.find((r) => r.room_id === e.room_id);
    const rows = e.blocks
      .map(
        (b, i) => {
          // 24:00 (1440 min) is not a valid <input type="time"> value — the
          // browser renders it as "--:--" and hhmmToMin returns null on save.
          // Show it as a read-only label so the user knows it's the day boundary.
          const endCell = b.end_min === 1440
            ? `<span class="pcs-end-fixed pcs-in" data-end1440>24:00</span>`
            : `<input class="pcs-in pcs-end" type="time" value="${minToHHMM(b.end_min)}">`;
          return `
        <tr data-i="${i}">
          <td><input class="pcs-in pcs-start" type="time" value="${minToHHMM(b.start_min)}"></td>
          <td>${endCell}</td>
          <td><input class="pcs-in pcs-target" type="number" step="0.5" min="5" max="30" value="${b.target}"></td>
          <td style="text-align:center"><input class="pcs-active" type="checkbox" ${b.is_active ? "checked" : ""}></td>
          <td><button class="pcs-btn pcs-del">✕</button></td>
        </tr>`;
        }
      )
      .join("");
    return `
      <div class="pcs-editor">
        <div class="pcs-room-name">${room ? room.name : e.room_id} — ${DAY_LABELS[e.day_key] || e.day_key}</div>
        ${this._error ? `<div class="pcs-error">${this._error}</div>` : ""}
        <table class="pcs-table">
          <thead><tr><th>Start</th><th>End</th><th>°C</th><th>Active</th><th></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
        <div class="pcs-actions">
          <button class="pcs-btn pcs-add">+ Add block</button>
          <span style="flex:1"></span>
          <button class="pcs-btn pcs-cancel">Cancel</button>
          <button class="pcs-btn pcs-primary pcs-save">Save</button>
        </div>
        <div class="pcs-hint">Blocks must cover 00:00–24:00 with no gaps or overlaps.</div>
      </div>`;
  }

  _syncEditorFromDom() {
    const rows = this._body.querySelectorAll("tbody tr");
    const blocks = [];
    rows.forEach((tr) => {
      const endFixed = tr.querySelector("[data-end1440]");
      const endInput = tr.querySelector(".pcs-end");
      const end_min = endFixed ? 1440 : hhmmToMin(endInput ? endInput.value : "");
      blocks.push({
        start_min: hhmmToMin(tr.querySelector(".pcs-start").value),
        end_min,
        target: parseFloat(tr.querySelector(".pcs-target").value),
        is_active: tr.querySelector(".pcs-active").checked,
      });
    });
    this._edit.blocks = blocks;
  }

  _wireEditor() {
    this._body.querySelector(".pcs-cancel").addEventListener("click", () => this._cancelEdit());
    this._body.querySelector(".pcs-save").addEventListener("click", () => {
      this._syncEditorFromDom();
      this._save();
    });
    this._body.querySelector(".pcs-add").addEventListener("click", () => {
      this._syncEditorFromDom();
      const last = this._edit.blocks[this._edit.blocks.length - 1];
      const start = last ? last.end_min : 0;
      this._edit.blocks.push({ start_min: start, end_min: 1440, target: 20, is_active: false });
      this._render();
    });
    this._body.querySelectorAll(".pcs-del").forEach((btn) => {
      btn.addEventListener("click", () => {
        this._syncEditorFromDom();
        const i = parseInt(btn.closest("tr").getAttribute("data-i"), 10);
        this._edit.blocks.splice(i, 1);
        this._render();
      });
    });
  }
}

const STYLE = `
  .pcs-body { padding: 8px 16px 16px; }

  /* Boiler status */
  .pcs-boiler { padding: 8px 12px; border-radius: 8px; margin-bottom: 14px; font-size: 1.05em; }
  .pcs-boiler-on  { background: rgba(217,102,59,.18); border: 1px solid rgba(217,102,59,.5); }
  .pcs-boiler-off { background: var(--secondary-background-color, rgba(255,255,255,.05)); border: 1px solid var(--divider-color, #444); }
  .pcs-reason { opacity: .75; font-size: .88em; }

  /* Room header */
  .pcs-room { margin-bottom: 18px; }
  .pcs-room-name { font-weight: 600; font-size: 1.05em; margin: 6px 0; display: flex; align-items: center; gap: 5px; }
  .pcs-heat-icon { font-size: 1em; line-height: 1; }
  .pcs-cur-temp { font-weight: 400; font-size: .95em; opacity: .8; }

  /* Day row */
  .pcs-day-head { display: flex; align-items: center; justify-content: space-between; font-size: .95em; opacity: .85; margin-top: 6px; }

  /* Timeline */
  .pcs-timeline-wrap { position: relative; margin-top: 2px; }
  .pcs-timeline { display: flex; height: 48px; border-radius: 6px; overflow: hidden; background: var(--divider-color, #444); }
  .pcs-seg {
    display: flex; align-items: center; justify-content: center;
    overflow: hidden; white-space: nowrap;
    border-right: 1px solid rgba(0,0,0,.25);
  }
  .pcs-seg.active  { background: var(--error-color, #d9663b); }
  .pcs-seg.passive { background: var(--primary-color, #3b78d9); opacity: .55; }
  .pcs-seg-label { display: flex; flex-direction: column; align-items: center; font-size: .9em; color: #fff; line-height: 1.25; pointer-events: none; }
  .pcs-seg-time { font-size: .85em; opacity: .85; }

  /* Current-time needle */
  .pcs-needle {
    position: absolute; top: 0; bottom: 0; width: 2px;
    background: rgba(255,255,255,.85);
    box-shadow: 0 0 4px rgba(0,0,0,.6);
    pointer-events: none;
    transform: translateX(-50%);
    border-radius: 1px;
  }

  /* Time axis — 7 ticks for 00 04 08 12 16 20 24 */
  .pcs-axis { display: flex; justify-content: space-between; font-size: .85em; opacity: .6; margin-top: 3px; }

  /* Edit button */
  .pcs-btn { background: var(--secondary-background-color, #333); color: var(--primary-text-color, #fff); border: 1px solid var(--divider-color, #555); border-radius: 6px; padding: 3px 10px; cursor: pointer; font-size: .85em; }
  .pcs-btn:hover { border-color: var(--primary-color, #3b78d9); }
  .pcs-primary { background: var(--primary-color, #3b78d9); border-color: var(--primary-color, #3b78d9); }

  /* Editor */
  .pcs-table { width: 100%; border-collapse: collapse; margin: 8px 0; }
  .pcs-table th { text-align: left; font-size: .8em; opacity: .7; padding: 2px 4px; }
  .pcs-in { width: 100%; box-sizing: border-box; background: var(--card-background-color, #1c1c1c); color: var(--primary-text-color, #fff); border: 1px solid var(--divider-color, #555); border-radius: 4px; padding: 3px; }
  .pcs-end-fixed { display: inline-block; opacity: .6; font-size: .9em; }
  .pcs-actions { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
  .pcs-hint { font-size: .75em; opacity: .6; margin-top: 6px; }
  .pcs-error { color: var(--error-color, #d9663b); font-size: .85em; margin: 6px 0; }
  .pcs-empty { opacity: .7; padding: 12px 0; }
  .pcs-version { text-align: right; font-size: .7em; opacity: .35; margin-top: 8px; }
`;

customElements.define("precision-climate-schedule-card", PrecisionClimateScheduleCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "precision-climate-schedule-card",
  name: "Precision Climate Schedule",
  description: "Visual editor for Precision Climate room schedules.",
});
