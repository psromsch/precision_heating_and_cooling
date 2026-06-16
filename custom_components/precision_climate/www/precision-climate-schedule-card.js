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

const pad = (n) => String(n).padStart(2, "0");
const minToHHMM = (m) => `${pad(Math.floor(m / 60) % 24 || (m === 1440 ? 24 : 0))}:${pad(m % 60)}`;
const hhmmToMin = (s) => {
  const [h, m] = String(s).split(":").map((x) => parseInt(x, 10));
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  return h * 60 + m;
};

class PrecisionClimateScheduleCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._edit = null; // { room_id, day_key, blocks: [...] }
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 6;
  }

  _statusEntity() {
    if (this._config.entity) return this._config.entity;
    // Auto-detect the Precision Climate status sensor (carries "schedules").
    const states = this._hass.states;
    for (const id of Object.keys(states)) {
      if (id.startsWith("sensor.") && states[id].attributes && states[id].attributes.schedules) {
        return id;
      }
    }
    return null;
  }

  _schedules() {
    const eid = this._statusEntity();
    if (!eid || !this._hass.states[eid]) return [];
    return this._hass.states[eid].attributes.schedules || [];
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
    const blocks = e.blocks.map((b) => ({
      start_min: typeof b.start_min === "string" ? hhmmToMin(b.start_min) : b.start_min,
      end_min: typeof b.end_min === "string" ? hhmmToMin(b.end_min) : b.end_min,
      target: parseFloat(b.target),
      is_active: !!b.is_active,
    }));
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

    this._body.innerHTML = schedules.map((room) => this._renderRoom(room)).join("");
    // Wire "edit" buttons.
    this._body.querySelectorAll("[data-edit]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const [rid, day] = btn.getAttribute("data-edit").split("|");
        const room = schedules.find((r) => r.room_id === rid);
        this._startEdit(room, day);
      });
    });
  }

  _renderRoom(room) {
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
            return `<div class="pcs-seg ${cls}" style="width:${w}%" title="${minToHHMM(
              b.start_min
            )}-${minToHHMM(b.end_min)} ${b.target}°C ${b.is_active ? "active" : "passive"}">
              <span>${b.target}°</span></div>`;
          })
          .join("");
        return `
          <div class="pcs-day">
            <div class="pcs-day-head">
              <span>${DAY_LABELS[key] || key}</span>
              <button class="pcs-btn" data-edit="${room.room_id}|${key}">Edit</button>
            </div>
            <div class="pcs-timeline">${segs}</div>
          </div>`;
      })
      .join("");
    return `<div class="pcs-room"><div class="pcs-room-name">${room.name}</div>${days}
      <div class="pcs-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
    </div>`;
  }

  _renderEditor(schedules) {
    const e = this._edit;
    const room = schedules.find((r) => r.room_id === e.room_id);
    const rows = e.blocks
      .map(
        (b, i) => `
        <tr data-i="${i}">
          <td><input class="pcs-in pcs-start" type="time" value="${minToHHMM(b.start_min)}"></td>
          <td><input class="pcs-in pcs-end" type="time" value="${minToHHMM(b.end_min)}"></td>
          <td><input class="pcs-in pcs-target" type="number" step="0.5" min="5" max="30" value="${b.target}"></td>
          <td style="text-align:center"><input class="pcs-active" type="checkbox" ${b.is_active ? "checked" : ""}></td>
          <td><button class="pcs-btn pcs-del">✕</button></td>
        </tr>`
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
      blocks.push({
        start_min: hhmmToMin(tr.querySelector(".pcs-start").value),
        end_min: hhmmToMin(tr.querySelector(".pcs-end").value),
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
  .pcs-room { margin-bottom: 18px; }
  .pcs-room-name { font-weight: 600; font-size: 1.05em; margin: 6px 0; }
  .pcs-day-head { display: flex; align-items: center; justify-content: space-between; font-size: .85em; opacity: .85; margin-top: 6px; }
  .pcs-timeline { display: flex; height: 26px; border-radius: 6px; overflow: hidden; background: var(--divider-color, #444); }
  .pcs-seg { display: flex; align-items: center; justify-content: center; font-size: .72em; color: #fff; overflow: hidden; white-space: nowrap; border-right: 1px solid rgba(0,0,0,.25); }
  .pcs-seg.active { background: var(--error-color, #d9663b); }
  .pcs-seg.passive { background: var(--primary-color, #3b78d9); opacity: .55; }
  .pcs-axis { display: flex; justify-content: space-between; font-size: .7em; opacity: .6; margin-top: 2px; }
  .pcs-btn { background: var(--secondary-background-color, #333); color: var(--primary-text-color, #fff); border: 1px solid var(--divider-color, #555); border-radius: 6px; padding: 3px 10px; cursor: pointer; font-size: .85em; }
  .pcs-btn:hover { border-color: var(--primary-color, #3b78d9); }
  .pcs-primary { background: var(--primary-color, #3b78d9); border-color: var(--primary-color, #3b78d9); }
  .pcs-table { width: 100%; border-collapse: collapse; margin: 8px 0; }
  .pcs-table th { text-align: left; font-size: .8em; opacity: .7; padding: 2px 4px; }
  .pcs-in { width: 100%; box-sizing: border-box; background: var(--card-background-color, #1c1c1c); color: var(--primary-text-color, #fff); border: 1px solid var(--divider-color, #555); border-radius: 4px; padding: 3px; }
  .pcs-actions { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
  .pcs-hint { font-size: .75em; opacity: .6; margin-top: 6px; }
  .pcs-error { color: var(--error-color, #d9663b); font-size: .85em; margin: 6px 0; }
  .pcs-empty { opacity: .7; padding: 12px 0; }
`;

customElements.define("precision-climate-schedule-card", PrecisionClimateScheduleCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "precision-climate-schedule-card",
  name: "Precision Climate Schedule",
  description: "Visual editor for Precision Climate room schedules.",
});
