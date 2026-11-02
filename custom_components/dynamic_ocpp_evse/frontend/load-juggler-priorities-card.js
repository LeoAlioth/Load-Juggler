/*
 * Load Juggler Priorities card (the owner, 2026-11-01; docs/plans/2026-11-01/dashboard-cards.md): the loads in
 * lanes by mode urgency, sorted by effective rank, each with its draw / allowance and arrows that move its
 * configured priority (dynamic_ocpp_evse.move_priority). Clicking a load's name opens its own entities.
 * Registered by the integration itself (__init__._register_card), so no dashboard resource is needed.
 *
 *   type: custom:load-juggler-priorities-card
 *   title: Priorities     # optional
 *   stacked: true         # one load per row (default); false: pills that wrap
 *   unit: A               # A or W to start with; the header switch changes it
 *   hub: <entry id>       # only with more than one hub; default: the hub of the first load found
 */
const DOMAIN = "dynamic_ocpp_evse";
const TIERS = [
  { n: 1, label: "Always (Standard / Continuous / Normal)", color: "#e5534b" },
  { n: 2, label: "Solar Priority", color: "#e0a43a" },
  { n: 3, label: "Solar Only", color: "#4fa35b" },
  { n: 4, label: "Excess", color: "#4a8fd6" },
];
// Only while the engine has not published a tier yet (an older build, the first cycle).
const MODE_TIER = { Standard: 1, Continuous: 1, Normal: 1, "Freeze Protection": 1,
                    "Solar Priority": 2, "Solar Only": 3, Excess: 4 };
// The popup's entity order, by entity_id suffix; the rest follow.
const POPUP_ORDER = ["_operating_mode", "_dynamic_control", "_status", "_charging_status", "_tank_status",
                     "_device_power", "_element_power", "_away_temperature", "_normal_temperature",
                     "_boost_temperature", "_allocated_current", "_available_current"];
const DEFAULT_VOLTAGE = 230;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : null; };
const watts = (w) => (w >= 1000 ? `${(w / 1000).toFixed(2)} k` : `${Math.round(w)} `);

class LoadJugglerPrioritiesCard extends HTMLElement {
  static getConfigForm() {
    return {
      schema: [
        { name: "title", selector: { text: {} } },
        { name: "stacked", selector: { boolean: {} } },
        { name: "unit", selector: { select: { mode: "dropdown", options: ["A", "W"] } } },
        { name: "hub", selector: { config_entry: { integration: DOMAIN } } },
      ],
    };
  }

  static getStubConfig() {
    return { stacked: true };
  }

  setConfig(config) {
    if (config.unit && !["A", "W"].includes(config.unit)) throw new Error("unit must be A or W");
    this._config = { title: "Priorities", stacked: true, unit: "A", ...config };
    this._unit = this._config.unit;
    this._key = null;
    this._pending = null;   // {entity, until}: an arrow press waiting for the reload
  }

  getCardSize() {
    return 3 + (this._rows?.length || 4);
  }

  set hass(hass) {
    this._hass = hass;
    if (this._popupCard) this._popupCard.hass = hass;
    const loads = this._loads();
    const key = JSON.stringify([loads, this._unit, this._pending]);
    if (key === this._key) return;
    this._key = key;
    this._rows = loads;
    this._render();
  }

  /** Every load of the hub, from its own entities: the Effective Priority sensor carries the configured
   * priority, the tier and the voltage; its siblings on the same device carry mode, draw and allowance. */
  _loads() {
    const h = this._hass;
    const ents = Object.values(h.entities).filter((e) => e.platform === DOMAIN);
    const loads = [];
    for (const e of ents.filter((x) => x.entity_id.endsWith("_effective_priority"))) {
      const prio = h.states[e.entity_id];
      if (!prio) continue;
      const mine = ents.filter((x) => x.device_id === e.device_id).map((x) => x.entity_id);
      const find = (suffix) => mine.find((x) => x.endsWith(suffix));
      const state = (suffix) => h.states[find(suffix)];
      const avail = state("_available_current");
      if (this._config.hub && avail?.attributes.hub_entry_id && avail.attributes.hub_entry_id !== this._config.hub) continue;
      const mode = state("_operating_mode")?.state;
      const tier = num(prio.attributes.urgency_tier) ?? MODE_TIER[mode] ?? 4;
      const device = h.devices[e.device_id];
      loads.push({
        entity: e.entity_id,
        device: e.device_id,
        hub: avail?.attributes.hub_entry_id,
        name: device?.name_by_user || device?.name || prio.attributes.friendly_name,
        conf: num(prio.attributes.configured_priority) ?? 0,
        eff: num(prio.state),
        tier: Math.min(4, Math.max(1, tier)),
        draw: num(state("_allocated_current")?.state) ?? 0,
        allow: num(avail?.state) ?? 0,
        phases: num(avail?.attributes.phases) || 1,
        voltage: num(prio.attributes.voltage) || DEFAULT_VOLTAGE,
        fixedW: num(state("_device_power")?.state) ?? num(state("_element_power")?.state),
      });
    }
    // One hub only: the configured one, else the first load's.
    const hub = this._config.hub || loads[0]?.hub;
    return loads.filter((l) => !hub || !l.hub || l.hub === hub)
                .sort((a, b) => (a.eff ?? 99) - (b.eff ?? 99) || a.conf - b.conf);
  }

  _render() {
    if (!this._card) {
      this._card = document.createElement("ha-card");
      (this.shadowRoot || this.attachShadow({ mode: "open" })).appendChild(this._card);
      this._card.addEventListener("click", (ev) => this._click(ev));
    }
    const rows = this._rows;
    const stacked = this._config.stacked !== false;
    const confs = rows.map((r) => r.conf);
    const lo = Math.min(...confs), hi = Math.max(...confs);
    const seg = (u) => `<button class="seg ${this._unit === u ? "on" : ""}" data-unit="${u}">${u}</button>`;
    const lanes = TIERS.map((t) => {
      const mine = rows.filter((r) => r.tier === t.n);
      const body = mine.length ? mine.map((r) => this._row(r, t, lo, hi)).join("") : `<span class="none">none</span>`;
      return `<div class="lane" style="--tier:${t.color}">
        <div class="lane-title">Tier ${t.n} · ${esc(t.label)}</div>
        <div class="rows ${stacked ? "stacked" : "pills"}">${body}</div></div>`;
    }).join("");
    this._card.innerHTML = `<style>${STYLE}</style>
      <div class="head"><div class="title">${esc(this._config.title)}</div>
        <span class="unit">${seg("A")}${seg("W")}</span></div>
      <div class="lanes">${rows.length ? lanes : `<div class="none">No Load Juggler loads found.</div>`}</div>`;
  }

  _row(r, t, lo, hi) {
    const allowA = r.fixedW && r.allow > 0 ? Math.min(r.allow, r.fixedW / r.voltage / r.phases) : r.allow;
    const off = allowA <= 0;
    const running = r.draw > 0.05;
    const amps = (a) => a.toFixed(1);
    const drawTxt = this._unit === "A" ? amps(r.draw) : watts(r.draw * r.voltage * r.phases).trim();
    const allowTxt = this._unit === "A" ? amps(allowA) : watts(r.fixedW && !off ? r.fixedW : allowA * r.voltage * r.phases).trim();
    const pct = off ? 0 : Math.min(100, (100 * r.draw) / allowA);
    const busy = this._pending && Date.now() < this._pending.until;
    const arrow = (dir, enabled) => `<button class="arrow" data-move="${dir}" data-entity="${esc(r.entity)}"
        ${enabled && !busy ? "" : "disabled"} title="${dir === "up" ? "Move up (served sooner)" : "Move down (served later)"}">${dir === "up" ? "▲" : "▼"}</button>`;
    return `<div class="row ${off ? "off" : ""}">
      <span class="badge" title="Effective priority ${r.eff ?? "–"}: the order this load is served in right now, after its mode's urgency tier and its configured priority.">${r.eff ?? "–"}</span>
      <div class="main">
        <div class="line">
          <a class="name" data-device="${esc(r.device)}" title="Settings">${running ? "⚡ " : ""}${esc(r.name)}</a>
          <span class="val">${off ? `<span class="dim">not allowed</span>`
            : `<b class="${running ? "lit" : ""}">${drawTxt}</b><span class="dim"> / ${allowTxt} ${this._unit}</span>`}</span>
          <span class="set">${arrow("up", r.conf > lo)}<span class="setn" title="Configured priority P${r.conf}: the place you gave this load among all loads. The arrows move it one place; within its urgency tier it decides who is served first.">P${r.conf}</span>${arrow("down", r.conf < hi)}</span>
        </div>
        <div class="bar"><div style="width:${pct}%"></div></div>
      </div></div>`;
  }

  _click(ev) {
    const unit = ev.target.closest("[data-unit]");
    if (unit) { this._unit = unit.dataset.unit; this.hass = this._hass; return; }
    const move = ev.target.closest("[data-move]");
    if (move && !move.disabled) { this._move(move.dataset.entity, move.dataset.move); return; }
    const name = ev.target.closest("[data-device]");
    if (name) { ev.preventDefault(); this._popup(name.dataset.device); }
  }

  async _move(entity, direction) {
    this._pending = { entity, until: Date.now() + 15000 };
    this.hass = this._hass;
    try {
      await this._hass.callService(DOMAIN, "move_priority", { entity_id: entity, direction });
    } catch (err) {
      this._pending = null;
      alert(`Could not move the priority: ${err.message || err}`);
    }
    // The reloaded loads publish their new numbers within a cycle or two; the arrows come back then.
    setTimeout(() => { this._pending = null; this._key = null; this.hass = this._hass; }, 15000);
  }

  async _popup(deviceId) {
    const h = this._hass;
    const device = h.devices[deviceId];
    const base = device?.name_by_user || device?.name || "";
    const rank = (e) => { const i = POPUP_ORDER.findIndex((s) => e.endsWith(s)); return i < 0 ? POPUP_ORDER.length : i; };
    const entities = Object.values(h.entities)
      .filter((e) => e.platform === DOMAIN && e.device_id === deviceId && !e.hidden && !e.entity_id.endsWith("_effective_priority"))
      .map((e) => e.entity_id).sort((a, b) => rank(a) - rank(b))
      .map((e) => {
        const full = h.states[e]?.attributes.friendly_name || e;
        const n = full.toLowerCase().startsWith(base.toLowerCase() + " ") ? full.slice(base.length + 1) : full;
        return { entity: e, name: n.charAt(0).toUpperCase() + n.slice(1) };
      });
    const back = document.createElement("div");
    back.className = "ljp-popup";
    back.innerHTML = `<style>${POPUP_STYLE}</style><ha-card class="box">
      <div class="phead"><span>${esc(base)}</span><button class="close" title="Close">✕</button></div></ha-card>`;
    const helpers = await window.loadCardHelpers();
    const card = helpers.createCardElement({ type: "entities", state_color: true, entities });
    card.hass = h;
    card.classList.add("inner");
    back.querySelector(".box").appendChild(card);
    document.body.appendChild(back);
    this._popupCard = card;
    const close = () => { this._popupCard = null; back.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = (ev) => { if (ev.key === "Escape") close(); };
    back.querySelector(".close").onclick = close;
    back.onclick = (ev) => { if (ev.target === back) close(); };
    document.addEventListener("keydown", onKey);
  }
}

const STYLE = `
  .head { display:flex; justify-content:space-between; align-items:center; padding:12px 16px 0; gap:8px; }
  .title { font-size:var(--ha-card-header-font-size, 20px); }
  .unit { border:1px solid var(--divider-color); border-radius:10px; padding:1px; font-size:12px; }
  .seg { all:unset; cursor:pointer; padding:1px 8px; border-radius:9px; }
  .seg.on { background:var(--primary-color); color:var(--text-primary-color, #fff); }
  .lanes { padding:8px 16px 16px; display:flex; flex-direction:column; gap:8px; }
  .lane { border-left:4px solid var(--tier); padding:4px 0 4px 10px; }
  .lane-title { font-size:12px; color:var(--secondary-text-color); margin-bottom:4px; }
  .rows { display:flex; gap:6px; }
  .rows.stacked { flex-direction:column; }
  .rows.pills { flex-wrap:wrap; }
  .none { opacity:.5; font-size:12px; }
  .row { display:flex; align-items:center; gap:8px; padding:5px 10px 5px 6px; border-radius:16px;
         background:color-mix(in srgb, var(--tier) 14%, transparent); border:1px solid var(--tier); }
  .row.off { background:color-mix(in srgb, var(--tier) 5%, transparent); border-color:color-mix(in srgb, var(--tier) 35%, transparent); opacity:.75; }
  .badge { background:var(--tier); color:#fff; border-radius:50%; width:26px; height:26px; flex:none;
           display:flex; align-items:center; justify-content:center; font-size:13px; font-weight:bold; }
  .main { display:flex; flex-direction:column; gap:3px; flex:1; min-width:0; }
  .line { display:flex; align-items:center; gap:6px; }
  .name { color:var(--primary-text-color); cursor:pointer; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .rows.stacked .name { flex:1; }
  .val { font-size:12px; white-space:nowrap; }
  .lit { color:var(--tier); }
  .dim { opacity:.6; }
  .set { display:inline-flex; align-items:center; gap:3px; font-size:11px; margin-left:6px; }
  .setn { opacity:.7; min-width:34px; text-align:center; }
  .arrow { all:unset; cursor:pointer; width:18px; height:18px; border-radius:50%; border:1px solid var(--tier);
           display:inline-flex; align-items:center; justify-content:center; font-size:10px; line-height:1; }
  .arrow:disabled { cursor:default; opacity:.25; }
  .bar { height:3px; border-radius:2px; background:color-mix(in srgb, var(--tier) 20%, transparent); }
  .bar > div { height:3px; border-radius:2px; background:var(--tier); }
`;

const POPUP_STYLE = `
  .ljp-popup { position:fixed; inset:0; z-index:1000; background:rgba(0,0,0,.5); display:flex; align-items:center; justify-content:center; }
  .ljp-popup .box { width:min(460px, 94vw); max-height:85vh; overflow:auto; }
  .ljp-popup .phead { display:flex; justify-content:space-between; align-items:center; padding:14px 16px 0; font-size:18px; }
  .ljp-popup .close { all:unset; cursor:pointer; font-size:20px; padding:0 4px; }
  .ljp-popup .inner { --ha-card-box-shadow:none; --ha-card-border-width:0; }
`;

customElements.define("load-juggler-priorities-card", LoadJugglerPrioritiesCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "load-juggler-priorities-card",
  name: "Load Juggler Priorities",
  description: "Loads by mode urgency and effective priority, with draw / allowance and arrows to reorder them.",
});
