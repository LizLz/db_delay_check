"use strict";
/* Delay Assistant front end: plain JavaScript, no build step.
   It only talks to the JSON API (/api/...). All numbers come from the server; this file draws them. */

const COLORS = { ICE: "#2a78d6", IC: "#eb6834", RE: "#1baf7a", RB: "#eda100", S: "#e87ba4", All: "#121821" };
const SERIES = ["ICE", "IC", "RE", "RB", "S", "All"];
const RED = "#D03B3B", GRAY = "#9AA4AF";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fold = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const pad = (n) => String(n).padStart(2, "0");

const state = { from: null, to: null, window: 7, dt: false, result: null, open: null, tip: null, loading: false };

/* ------------------------------------------------------------------ API */
async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const r = await fetch(url);
  if (!r.ok) {
    let detail = "";
    try {
      const j = await r.json();
      detail = Array.isArray(j.detail) ? j.detail.map((d) => d.msg).join("; ") : j.detail;
    } catch { /* body was not JSON */ }
    throw new Error(detail || `Request failed (${r.status})`);
  }
  return r.json();
}

/* ------------------------------------------------- station drop-downs */
function combo(key, input, list) {
  let items = [], active = -1, seq = 0, timer = null;

  const close = () => { list.hidden = true; active = -1; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); };
  const hl = (name, q) => {
    const i = fold(name).indexOf(fold(q));
    return i < 0 ? esc(name) : `${esc(name.slice(0, i))}<b>${esc(name.slice(i, i + q.length))}</b>${esc(name.slice(i + q.length))}`;
  };
  const render = () => {
    const q = input.value.trim();
    list.innerHTML = items.length
      ? items.map((s, i) => `<li role="option" id="${key}-o${i}" data-i="${i}"${i === active ? ' aria-selected="true"' : ""}>${hl(s.name, q)}</li>`).join("")
      : `<li class="empty">No station found</li>`;
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    if (active >= 0) input.setAttribute("aria-activedescendant", `${key}-o${active}`);
  };
  const pick = (s) => { state[key] = s; input.value = s.name; close(); hideMsg(); };

  input.addEventListener("input", () => {
    state[key] = null;                       // typing again invalidates the pick: it must come from the list
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      let found = [];
      try { found = await api("/api/stations", { q }); } catch { /* show "No station found" */ }
      if (mine !== seq) return;              // a newer keystroke already asked again
      items = found; active = items.length ? 0 : -1; render();
    }, 120);
  });
  input.addEventListener("keydown", (e) => {
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); active = (active + 1) % items.length; render(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = (active - 1 + items.length) % items.length; render(); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); pick(items[active]); }
    else if (e.key === "Escape") close();
  });
  input.addEventListener("blur", close);
  list.addEventListener("mousedown", (e) => {   // mousedown, not click: the input's blur would close the list first
    const li = e.target.closest("li[data-i]");
    if (li) { e.preventDefault(); pick(items[Number(li.dataset.i)]); }
  });
}

/* ------------------------------------------------------- small helpers */
function showMsg(t) { const m = $("#msg"); m.textContent = t; m.hidden = !t; }
function hideMsg() { showMsg(""); }
const hhmm = (iso) => (iso ? iso.slice(11, 16) : "");
const minutesBetween = (a, b) => Math.round((new Date(b) - new Date(a)) / 60000);

function niceMax(v, steps) { for (const s of steps) if (v <= s) return s; return Math.ceil(v / 10) * 10; }

function dateLabel(iso) {
  return new Date(iso + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}

/* ------------------------------------------------------- overview chart */
function drawOverview(ov) {
  $("#ov-legend").innerHTML = SERIES.map((s) =>
    `<span><i style="background:${COLORS[s]};height:${s === "All" ? 4 : 3}px"></i>${s === "All" ? "All trains" : s}</span>`).join("");
  $("#ov-sub").textContent =
    `Median arrival delay by planned arrival hour, ${ov.start} to ${ov.end}, all stations. Cancelled trains excluded.`;

  const W = 700, H = 260, x0 = 48, x1 = 648, y0 = 220, y1 = 20;
  const all = SERIES.flatMap((s) => ov.series[s]).filter(Boolean).map((p) => p.median);
  const top = niceMax(Math.max(8, ...all), [8, 12, 16, 20, 24, 32, 40, 60]);
  const X = (h) => x0 + (h * (x1 - x0)) / 23;
  const Y = (v) => y0 - (v * (y0 - y1)) / top;

  let g = "";
  for (let i = 0; i <= 4; i++) {
    const v = (top * i) / 4, y = Y(v);
    g += `<line x1="${x0}" y1="${y}" x2="${x1}" y2="${y}" stroke="${i ? "#E1E5EA" : "#C3C9D0"}"/>`;
    g += `<text x="${x0 - 8}" y="${y + 4}" font-size="11" fill="#8A94A0" text-anchor="end">${v}${i === 4 ? " min" : ""}</text>`;
  }
  g += `<line x1="${x0}" y1="${Y(6)}" x2="${x1}" y2="${Y(6)}" stroke="#D03B3B" stroke-dasharray="4 4"/>`;
  g += `<text x="${x1 + 4}" y="${Y(6) + 4}" font-size="11" fill="#B02F2F">6 min</text>`;
  for (let h = 0; h < 24; h += 3) g += `<text x="${X(h)}" y="238" font-size="11" fill="#8A94A0" text-anchor="middle">${h}:00</text>`;
  g += `<text x="348" y="256" font-size="11" fill="#8A94A0" text-anchor="middle">Planned arrival hour</text>`;

  // one polyline per run of consecutive hours: an hour with too little data is a gap, not a zero
  for (const s of [...SERIES].reverse()) {                       // "All" is drawn last = on top
    let seg = [];
    const flush = () => { if (seg.length > 1) g += `<polyline fill="none" stroke="${COLORS[s]}" stroke-width="${s === "All" ? 3 : 2}" stroke-linejoin="round" stroke-linecap="round" points="${seg.join(" ")}"/>`; else if (seg.length === 1) g += `<circle cx="${seg[0].split(",")[0]}" cy="${seg[0].split(",")[1]}" r="2.5" fill="${COLORS[s]}"/>`; seg = []; };
    ov.series[s].forEach((p, h) => { if (p) seg.push(`${X(h)},${Y(p.median)}`); else flush(); });
    flush();
  }
  g += `<g id="ov-cross" visibility="hidden"><line y1="${y1}" y2="${y0}" stroke="#8A94A0"/></g>`;

  const box = $("#ov-chart");
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Line chart: median arrival delay in minutes by planned arrival hour, one line per train type.">${g}</svg><div class="ctip" hidden></div>`;
  const svg = $("svg", box), tip = $(".ctip", box), cross = $("#ov-cross", box);

  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const h = Math.max(0, Math.min(23, Math.round(((px - x0) * 23) / (x1 - x0))));
    cross.setAttribute("visibility", "visible");
    $("line", cross).setAttribute("x1", X(h)); $("line", cross).setAttribute("x2", X(h));
    const rows = SERIES.filter((s) => ov.series[s][h]).sort((a, b) => ov.series[b][h].median - ov.series[a][h].median);
    tip.innerHTML = `<b>${ov.hours[h]} to ${pad(h)}:59</b>` + (rows.map((s) =>
      `<div><span><i style="background:${COLORS[s]}"></i>${s === "All" ? "All trains" : s}</span><span>${ov.series[s][h].median} min</span></div>`).join("") || "Not enough data");
    tip.hidden = false;
    const left = (X(h) / W) * r.width;
    tip.style.left = `${left > r.width / 2 ? left - tip.offsetWidth - 12 : left + 12}px`;
    tip.style.top = "8px";
  });
  svg.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

/* ---------------------------------------------------- persistence chart */
function drawPersistence(p) {
  const body = $("#pe-body");
  if (!p.available) { body.innerHTML = `<p class="loading">Needs two full months of data (${esc(p.reason || "")}).</p>`; $("#pe-sub").textContent = ""; return; }
  const month = (ym) => new Date(`${ym}-15T12:00:00`).toLocaleDateString("en-GB", { month: "long" });
  $("#pe-sub").textContent =
    `Is a train that was late in ${month(p.x_month)} late again in ${month(p.y_month)}? Each dot is one train at one station (median delay).`;

  const xs = p.points.map((d) => Math.max(d.x, d.y)).sort((a, b) => a - b);
  const q99 = xs.length ? xs[Math.floor(xs.length * 0.99)] : 10;
  const top = niceMax(Math.max(10, q99), [10, 20, 30, 40, 60, 80, 100]);
  const x0 = 48, x1 = 408, y0 = 220, y1 = 20;
  const X = (v) => x0 + (v * (x1 - x0)) / top, Y = (v) => y0 - (v * (y0 - y1)) / top;

  let g = "";
  for (let i = 0; i <= 4; i++) {
    const v = (top * i) / 4;
    g += `<line x1="${x0}" y1="${Y(v)}" x2="${x1}" y2="${Y(v)}" stroke="${i ? "#E1E5EA" : "#C3C9D0"}"/>`;
    if (i) g += `<line x1="${X(v)}" y1="${y1}" x2="${X(v)}" y2="${y0}" stroke="#E1E5EA"/>`;
    g += `<text x="${x0 - 8}" y="${Y(v) + 4}" font-size="11" fill="#8A94A0" text-anchor="end">${v}${i === 4 ? " min" : ""}</text>`;
    g += `<text x="${X(v)}" y="238" font-size="11" fill="#8A94A0" text-anchor="middle">${v}${i === 4 ? " min" : ""}</text>`;
  }
  g += `<text x="228" y="258" font-size="11" fill="#8A94A0" text-anchor="middle">Median delay, ${month(p.x_month)}</text>`;
  g += `<text transform="translate(12 120) rotate(-90)" font-size="11" fill="#8A94A0" text-anchor="middle">Median delay, ${month(p.y_month)}</text>`;
  let omitted = 0;
  g += `<g fill="#2a78d6" fill-opacity="0.45" stroke="#fff" stroke-width="1">`;
  for (const d of p.points) { if (d.x > top || d.y > top) { omitted++; continue; } g += `<circle cx="${X(d.x)}" cy="${Y(d.y)}" r="3.2"/>`; }
  g += `</g>`;
  // linear fit, cut to the visible square
  const f = (x) => p.intercept + p.slope * x;
  let a = 0, b = top;
  if (f(a) < 0) a = -p.intercept / p.slope;
  if (f(b) > top) b = (top - p.intercept) / p.slope;
  if (b > a) g += `<line x1="${X(a)}" y1="${Y(f(a))}" x2="${X(b)}" y2="${Y(f(b))}" stroke="#121821" stroke-width="2" stroke-dasharray="6 4"/><text x="${X(b) - 4}" y="${Y(f(b)) - 8}" font-size="12" font-weight="600" fill="#121821" text-anchor="end">linear fit</text>`;

  const pct = p.repeat_rate == null ? "–" : `${Math.round(p.repeat_rate * 100)}%`;
  body.innerHTML = `
    <svg viewBox="0 0 460 270" role="img" aria-label="Scatter plot of median delay in the earlier month against the later month, with a linear fit.">${g}</svg>
    <div class="tiles">
      <div class="tile"><div>Slope</div><div>${p.slope}</div></div>
      <div class="tile"><div>Correlation</div><div>${p.pearson_r}</div></div>
      <div class="tile"><div>Late again (6+ min)</div><div>${pct}</div></div>
      <div class="tile"><div>Trains compared</div><div>${p.n_trains.toLocaleString("en-US")}</div></div>
    </div>`;
}

/* ---------------------------------------------------------- results */
/* "+1" next to a time that falls on a later calendar day than the journey's first departure (night trains) */
function plusDays(iso, base) {
  if (!iso || !base) return "";
  const d = Math.round((Date.parse(`${iso.slice(0, 10)}T00:00:00Z`) - Date.parse(`${base.slice(0, 10)}T00:00:00Z`)) / 864e5);
  return d > 0 ? `<sup class="plus" title="${d === 1 ? "Next day" : `${d} days later`}">+${d}</sup>` : "";
}

function timeView(planned, live, mode, base) {
  if (mode === "live" && live && live !== planned)
    return `<span class="struck">${hhmm(planned)}</span><span class="t" style="color:${live > planned ? "#C62828" : "#0B7A3E"}">${hhmm(live)}${plusDays(live, base)}</span>`;
  const shown = mode === "history" && live ? live : planned;
  return `<span class="t">${hhmm(shown)}${plusDays(shown, base)}</span>`;
}

function duration(j) {
  const s = j.duration_seconds ?? (new Date(j.arrival) - new Date(j.departure)) / 1000;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return `${h} h ${pad(m)}`;
}

function euro(p) {
  return new Intl.NumberFormat("de-DE", { style: "currency", currency: p.currency }).format(p.amount);
}

function chipsFor(leg, mode) {
  const out = [];
  for (const f of leg.badge.flags || [])
    out.push(`<span class="chip cancel"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="#A32828" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M2 2l8 8M10 2l-8 8"/></svg>${esc(f)}</span>`);
  return out.join("");
}

function dayMap(leg, key, r) {
  const n = r.window, end = new Date(`${r.data_to}T00:00:00Z`);
  const days = Array.from({ length: n }, (_, i) => { const d = new Date(end); d.setUTCDate(end.getUTCDate() - (n - 1 - i)); return d.toISOString().slice(0, 10); });
  const byDay = new Map(leg.bars.map((b) => [b.day, b]));
  const nums = leg.bars.filter((b) => !b.cancelled).map((b) => b.delay);
  const top = Math.max(12, Math.ceil(Math.max(6, ...nums) / 10) * 10);
  const yOf = (v) => 190 - (v * 170) / top;
  const slot = 560 / n, bw = Math.min(36, slot * 0.62);

  let g = "";
  for (const v of [0, top / 2, top]) g += `<line x1="40" y1="${yOf(v)}" x2="600" y2="${yOf(v)}" stroke="#E1E5EA"/><text x="34" y="${yOf(v) + 4}" font-size="11" fill="#8A94A0" text-anchor="end">${v}${v === top ? " min" : ""}</text>`;
  g += `<line x1="40" y1="190" x2="600" y2="190" stroke="#C3C9D0"/><line x1="40" y1="${yOf(6)}" x2="600" y2="${yOf(6)}" stroke="#D03B3B" stroke-dasharray="4 4"/><text x="604" y="${yOf(6) + 4}" font-size="11" fill="#B02F2F">6 min</text>`;

  let tipHtml = "";
  days.forEach((day, i) => {
    const cx = 40 + slot * (i + 0.5), b = byDay.get(day), x0 = cx - bw / 2, x1 = cx + bw / 2;
    const label = n <= 7 ? `${new Date(day + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short" })} ${Number(day.slice(8))}` : String(Number(day.slice(8)));
    let note = n <= 10 ? "no service" : "";
    if (b && b.cancelled) {
      note = n <= 10 ? "cancelled" : "";
      g += `<path d="M${cx - 10} 168 l20 20 M${cx + 10} 168 l-20 20" stroke="#52606D" stroke-width="2.5" stroke-linecap="round" fill="none"/>`;
    } else if (b) {
      note = "";
      const y = Math.min(188, yOf(b.delay));
      g += `<path d="${190 - y >= 8 ? `M${x0} 190 V${y + 4} a4 4 0 0 1 4 -4 H${x1 - 4} a4 4 0 0 1 4 4 V190 Z` : `M${x0} 190 V${y} H${x1} V190 Z`}" fill="${b.delay >= 6 ? RED : GRAY}"/>`;
      if (n <= 15) g += `<text x="${cx}" y="${y - 6}" font-size="12" font-weight="600" fill="#121821" text-anchor="middle">${b.delay}</text>`;
    }
    g += `<text x="${cx}" y="208" font-size="11" fill="#52606D" text-anchor="middle">${label}</text>`;
    if (note) g += `<text x="${cx}" y="222" font-size="11" fill="#8A94A0" text-anchor="middle">${note}</text>`;
    if (b) {
      const id = `${key}|${day}`;
      g += `<g class="hit" data-tip="${esc(id)}" tabindex="0" role="button" aria-label="${esc(dateLabel(day))}: ${b.cancelled ? "cancelled" : `+${b.delay} min`}. Show details"><rect x="${cx - slot / 2}" y="20" width="${slot}" height="190" fill="transparent"/></g>`;
      if (state.tip === id) {
        const left = Math.min(80, Math.max(20, (cx / 650) * 100));
        tipHtml = `<div class="dtip" style="left:${left}%"><b>${esc(dateLabel(day))} · ${b.cancelled ? "cancelled" : `+${b.delay} min`}</b>${b.cause ? `${esc(b.cause)}<br><span>${esc(b.cause_category || "")}</span>` : "No cause recorded"}</div>`;
      }
    }
  });
  return `<div class="daymap">
    <div class="cap">Arrival delay per day at ${esc(leg.destination)}, last ${n} days</div>
    <div class="svgwrap"><svg viewBox="0 0 650 236" role="img" aria-label="Bar chart of the arrival delay per day for ${esc(leg.name)}.">${g}</svg>${tipHtml}</div>
    <p class="hint">✕ = cancelled. No bar = no record. Click a bar for the cause.</p>
  </div>`;
}

/* A median-delay badge. `key` names the leg whose per-day map it opens. Small ones sit at the end of a train
   row; the normal-size one sits in the journey's top line and shows the delay at the FINAL stop (what the
   traveller arrives with), so it opens the final train's map. The "7/7 days" detail lives in the tooltip. */
function pillFor(badge, key, { small = false, title = "", label = "" } = {}) {
  if (badge.state === "no_history") return `<span class="nohist${small ? " sm" : ""}" title="${esc(title)}">No history</span>`;
  const open = state.open === key;
  const red = badge.state === "ok" && badge.red;
  const detail = badge.state === "all_cancelled" ? `All ${badge.runs} tracked runs cancelled`
    : `Median over ${badge.runs} of ${badge.window} days with a record`;
  return `<button type="button" class="pill${small ? " sm" : ""} ${red ? "red" : "gray"}" data-toggle="${key}" aria-expanded="${open}" title="${esc(title ? `${title}. ${detail}` : detail)}" aria-label="${esc(label)} ${badge.state === "ok" ? `median delay ${badge.median} minutes` : "all runs cancelled"}. ${open ? "Hide" : "Show"} per-day map">
    <span>${esc(badge.text)}</span>
    <svg width="10" height="7" viewBox="0 0 12 8" aria-hidden="true"><path d="${open ? "M1 7 L6 1.5 L11 7 Z" : "M1 1 L6 6.5 L11 1 Z"}"/></svg></button>`;
}

function legHtml(leg, li, ji, j, r) {
  const mode = r.mode, key = `${ji}-${li}`, b = leg.badge;
  const t = j.transfers.length === j.legs.length - 1 ? j.transfers[li - 1] : j.transfers.find((x) => x.station === leg.origin);
  let transfer = "";
  if (li > 0 && t) {
    const eff = t.effective_minutes, plan = t.planned_minutes, walk = t.walk_minutes;
    const tip = "Timetable time between the two trains, walking included.";
    const bits = [`<span style="font-weight:600">Transfer at ${esc(t.station)}</span>`,
                  `<span title="${tip}"><b style="font-weight:600">${plan} min</b>${walk ? ` · walk ~${walk} min` : ""}</span>`];
    if (mode === "live" && eff !== plan) bits.push(`<span class="left">${eff} min left</span><span>earlier train ${plan - eff} min late</span>`);
    if (mode === "history" && eff !== plan) bits.push(`<span>actual: ${eff} min</span>`);
    const missed = t.likely_missed && mode !== "future" ? "Connection missed"
      : t.p_miss != null ? `Missed on ${Math.round(t.p_miss * 100)}% of past days` : "";
    const missTip = t.p_miss != null ? `On ${Math.round(t.p_miss * 100)}% of tracked days the earlier train arrived more than ${t.slack_minutes} min late${walk ? " (time left after the walk)" : ""}, which breaks this connection.` : "";
    transfer = `<div class="transfer">${bits.join("")}<span class="miss" title="${esc(missTip)}">${esc(missed)}</span></div>`;
  }

  const open = state.open === key;
  return `<div class="leg">${transfer}
    <div class="legbox${open ? " open" : ""}">
      <div class="legrow">
        <span class="lname">${esc(leg.name)}</span>
        <span class="route"><span>${esc(leg.origin)}</span>${timeView(leg.planned_departure, leg.live_departure, mode, j.departure)}<span class="arrow">→</span><span>${esc(leg.destination)}</span>${timeView(leg.planned_arrival, leg.live_arrival, mode, j.departure)}</span>
        <span class="badges">${chipsFor(leg, mode)}${pillFor(leg.badge, key, { small: true, label: leg.name })}</span>
      </div>
      ${open ? dayMap(leg, key, r) : ""}
    </div></div>`;
}

function journeyHtml(j, ji, r) {
  const mode = r.mode, first = j.legs[0], last = j.legs[j.legs.length - 1];
  const chips = [];
  if (j.tight_transfer)
    chips.push(`<span class="chip big tight" title="Less than 5 min to spare after walking"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#6B4A00" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.2"/><path d="M8 4.6V8l2.3 1.5"/></svg>Tight transfer</span>`);
  if (j.price && mode !== "history" && !j.dticket_covered)
    chips.push(`<span class="price">${j.price.from ? "from " : ""}${euro(j.price)}${j.price_partial ? " <small>paid legs only</small>" : ""}</span>`);
  if (j.dticket_covered) chips.push(`<span class="chip big dt">Included in Deutschlandticket</span>`);
  return `<article class="card journey">
    <div class="jtop">
      <div class="times">${timeView(j.departure, first.live_departure, mode, j.departure)}<span class="arrow">→</span>${timeView(j.arrival, last.live_arrival, mode, j.departure)}</div>
      <div class="meta">${duration(j)} · ${j.transfer_count ? `${j.transfer_count} transfer${j.transfer_count > 1 ? "s" : ""}` : "direct"}</div>
      <span class="jright">${pillFor(last.badge, `${ji}-${j.legs.length - 1}`, { title: `Whole journey: arrival at ${last.destination}`, label: "Whole journey" })}${chips.join("")}</span>
    </div>
    ${j.legs.map((l, li) => legHtml(l, li, ji, j, r)).join("")}
  </article>`;
}

function renderResults() {
  const r = state.result, q = r.query;
  const modeText = { history: "actual times" }[r.mode];
  const head = `<div class="rhead"><h2>${esc(q.from.name)} to ${esc(q.to.name)}</h2>
    <p class="sub">${r.source === "fixture" ? `<span class="chip big tight">Demo data, not a real timetable</span> ` : ""}${esc(dateLabel(q.date))}${r.mode === "live" ? " (today)" : ""}, from ${esc(q.time)}${modeText ? ` · ${modeText}` : ""}${r.dticket === "only" ? " · Deutschlandticket only" : ""}${r.data_to ? ` · delay data to ${esc(dateLabel(r.data_to))}` : ""}${r.source === "bahn" ? " · timetable and prices: bahn.de" : ""}</p></div>`;
  const body = r.journeys.length
    ? (() => {            // a date heading whenever the trains leave on another day than the one searched
        let day = q.date;
        return r.journeys.map((j, i) => {
          const d = (j.departure || "").slice(0, 10);
          const head = d && d !== day ? `<h3 class="dayhead">${esc(dateLabel(d))}</h3>` : "";
          if (d) day = d;
          return head + journeyHtml(j, i, r);
        }).join("");
      })()
    : `<div class="card empty-card">${esc(r.message || "No results found")}</div>`;
  const box = $("#results");
  const focus = document.activeElement?.dataset?.toggle || document.activeElement?.dataset?.tip;
  box.innerHTML = head + body;
  if (focus) (box.querySelector(`[data-toggle="${CSS.escape(focus)}"]`) || box.querySelector(`[data-tip="${CSS.escape(focus)}"]`))?.focus();
}

function showView(name) {
  $("#home").hidden = name !== "home";
  $("#results").hidden = name !== "results";
  $("#back").hidden = name !== "results";
}

/* ------------------------------------------------------------- search */
async function runSearch() {
  if (state.loading) return;
  if (!state.from || !state.to) { showMsg("Pick both stations from the list."); return; }
  const date = $("#date").value, time = $("#time").value;
  if (!date || !time) { showMsg("Choose a date and a departure time."); return; }
  hideMsg();
  state.loading = true; $("#go").disabled = true; $("#go").textContent = "Searching…";
  try {
    const res = await api("/api/journeys", {
      from_id: state.from.id, to_id: state.to.id, date, time, window: state.window, dticket: state.dt ? "only" : "off",
    });
    state.result = { ...res, query: { from: state.from, to: state.to, date, time } };
    state.open = null; state.tip = null;
    showView("results"); renderResults();
  } catch (e) {
    showMsg(e.message);
  } finally {
    state.loading = false; $("#go").disabled = false; $("#go").textContent = "Search";
  }
}

/* ------------------------------------------------------------ startup */
function init() {
  const now = new Date();
  $("#date").value = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  $("#time").value = `${pad(now.getHours())}:${pad(Math.floor(now.getMinutes() / 5) * 5)}`;

  combo("from", $("#from"), $("#from-list"));
  combo("to", $("#to"), $("#to-list"));
  $("#form").addEventListener("submit", (e) => { e.preventDefault(); runSearch(); });

  // changing the period or the ticket switch after a search simply repeats it
  $("#window").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-w]"); if (!b) return;
    state.window = Number(b.dataset.w);
    document.querySelectorAll("#window button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    if (state.result) runSearch();
  });
  $("#dt").addEventListener("click", () => {
    state.dt = !state.dt; $("#dt").setAttribute("aria-checked", String(state.dt));
    if (state.result) runSearch();
  });
  $("#back").addEventListener("click", (e) => { e.preventDefault(); showView("home"); });

  // one open map at a time; clicking the open one folds it; a bar click shows its cause
  $("#results").addEventListener("click", (e) => {
    const t = e.target.closest("[data-toggle]");
    if (t) { state.open = state.open === t.dataset.toggle ? null : t.dataset.toggle; state.tip = null; renderResults(); return; }
    const h = e.target.closest("[data-tip]");
    if (h) { state.tip = state.tip === h.dataset.tip ? null : h.dataset.tip; renderResults(); }
  });
  $("#results").addEventListener("keydown", (e) => {
    const h = e.target.closest?.("[data-tip]");
    if (h && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); h.dispatchEvent(new MouseEvent("click", { bubbles: true })); }
  });

  $("#rp-btn").addEventListener("click", async () => {
    const btn = $("#rp-btn"), box = $("#rp-body");
    btn.disabled = true; box.textContent = "Writing the report…";
    try {
      const r = await api("/api/report");
      box.innerHTML = r.text.split(/\n\s*\n/).map((t) => `<p style="margin:0 0 10px">${esc(t)}</p>`).join("");
    } catch (e) { box.textContent = `Could not create the report: ${e.message}`; }
    btn.disabled = false;
  });
  api("/api/overview").then(drawOverview).catch((e) => { $("#ov-chart").innerHTML = `<p class="loading">Could not load the overview: ${esc(e.message)}</p>`; });
  api("/api/persistence").then(drawPersistence).catch((e) => { $("#pe-body").innerHTML = `<p class="loading">Could not load: ${esc(e.message)}</p>`; });
}
init();
