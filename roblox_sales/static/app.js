"use strict";
// Sales Analytics dashboard. Everything user-supplied (item and store names, file names,
// error text) is rendered with textContent — never as HTML.

let ROWS = [];               // {id, t, rbx, src, item, loc}
let STORES = [];             // [{source, name, combine, group}]
let FIN = {};                // {source: {funds, pending, sales_month, updated}}
let CAT = {};                // {source: {items, as_of, attempt, error}} — items on sale
let BAL = null;              // {robux, ts} — the latest balance, for the pill
let curSrc = "All", curDays = 30, metric = "robux", sortKey = "rbx";
const PALETTE = ["#57a9ff", "#ffb74d", "#4dd8a3", "#f76fbe", "#ff8a3d", "#ffe066", "#63e0e0", "#ff8a65"];
const STYLES = ["circle", "rectRot", "triangle", "rect", "star", "crossRot"];
const TOTAL_COLOR = "#3b6dff", OTHER_COLOR = "#b18cff", ATH_COLOR = "#ff4d4d";
const $ = id => document.getElementById(id);
const fmt = n => Math.round(n).toLocaleString("en-US");
const fmtDate = k => { const [y, m, d] = k.split("-"); return `${+d}/${+m}/${y}`; };   // 4/7/2026

// The page must keep working if Chart.js fails to load: cards, tables and import still render.
const HAVE_CHART = typeof Chart !== "undefined";
if (!HAVE_CHART) console.error("Chart.js failed to load — rendering the dashboard without graphs");
if (HAVE_CHART) {
  Chart.defaults.color = "#9fa2a8";
  Chart.defaults.borderColor = "#33353a";
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.font.family = '"Builder Sans","Segoe UI",system-ui,sans-serif';
}
let chart = null;
let _athDay = null, _chartSig = "", _compareMode = false;
let _epoch = "", _maxRowid = 0, _count = 0, _baseAt = 0;
let _loading = false, _loadAgain = false, _stopped = false;
let customRange = null;      // {s, e} epoch-ms — a selected window, overrides curDays
let _rangeSelInit = false, _showcase = false;
const FULL_REFRESH_MS = 15 * 60 * 1000;   // re-download everything now and then (row edits)

const ageText = ts => {
  if (!ts) return "–";
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago`
    : new Date(ts * 1000).toISOString().slice(0, 10);
};
// golden.js defines the formula; if it failed to load the card shows "–" instead of breaking
const goldenSafe = (...a) => typeof goldenRatio === "function" ? goldenRatio(...a) : null;
const formatGoldenSafe = g => typeof formatGolden === "function" ? formatGolden(g) : "–";
const storeName = src => (STORES.find(s => s.source === src) || {}).name || src;
const combined = src => (STORES.find(s => s.source === src) || { combine: true }).combine;

// the ATH ("all-time high") badge, drawn on the main chart
const athLabel = {
  id: "athLabel",
  afterDatasetsDraw(c) {
    const i = c.data.labels.indexOf(_athDay);
    if (i < 0 || _compareMode) return;
    const pt = c.getDatasetMeta(0).data[i];
    if (!pt) return;
    const { ctx, chartArea } = c;
    ctx.save();
    ctx.font = "700 10px 'Builder Sans','Segoe UI',sans-serif";
    const padX = 7, h = 18, w = ctx.measureText("ATH").width + padX * 2;
    const bx = Math.min(Math.max(pt.x, chartArea.left + w / 2), chartArea.right - w / 2);
    let by = pt.y - 14 - h;
    if (by < chartArea.top) by = pt.y + 14;
    ctx.beginPath(); ctx.arc(pt.x, pt.y, 9, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(255,77,77,.22)"; ctx.fill();
    ctx.beginPath(); ctx.roundRect(bx - w / 2, by, w, h, 9);
    ctx.fillStyle = ATH_COLOR; ctx.fill();
    const tipY = by < pt.y ? by + h : by, dir = by < pt.y ? 1 : -1;
    ctx.beginPath(); ctx.moveTo(pt.x - 4, tipY); ctx.lineTo(pt.x + 4, tipY);
    ctx.lineTo(pt.x, tipY + 5 * dir); ctx.closePath(); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("ATH", bx, by + h / 2 + .5);
    ctx.restore();
  },
};

// timestamps are stored as UTC ISO strings; a value without a zone is read as UTC too
const parseTs = s => {
  if (typeof s !== "string" || !s) return NaN;
  const hasZone = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(s);
  return new Date((s.includes("T") ? s : s.replace(" ", "T")) + (hasZone ? "" : "Z")).getTime();
};
// UTC day buckets: Roblox's analytics day resets at midnight UTC, and the current
// (incomplete) UTC day is left off the chart
const dayKey = d => new Date(d).toISOString().slice(0, 10);
const todayKey = () => new Date().toISOString().slice(0, 10);
const val = r => metric === "robux" ? r.rbx : 1;

function finFor() {
  let srcs;
  if (curSrc === "All" || curSrc === "Compare") srcs = STORES.filter(s => s.group && s.combine).map(s => s.source);
  else srcs = STORES.filter(s => s.group && s.source === curSrc).map(s => s.source);
  const pick = k => {
    const vals = srcs.map(n => FIN[n] && FIN[n][k]).filter(v => v != null);
    return vals.length ? vals.reduce((a, b) => a + b, 0) : null;
  };
  // Items on sale: a combined figure needs a COMPLETE count for every store in it — summing
  // only the stores that have one would quietly undercount
  let items = null, asOf = null, catErr = false;
  if (srcs.length && srcs.every(n => CAT[n] && CAT[n].items != null)) {
    items = srcs.reduce((a, n) => a + CAT[n].items, 0);
    asOf = Math.min(...srcs.map(n => CAT[n].as_of || 0));
  }
  catErr = srcs.some(n => CAT[n] && CAT[n].error);
  return { pending: pick("pending"), funds: pick("funds"), groups: srcs.length, items, asOf, catErr };
}

function filtered() {
  const srcOk = r => (curSrc === "All" || curSrc === "Compare") ? combined(r.src) : r.src === curSrc;
  if (customRange) return ROWS.filter(r => srcOk(r) && r.t >= customRange.s && r.t <= customRange.e);
  let cut = 0;
  if (curDays) {           // aligned to UTC midnight so the first day is a whole day
    const d = new Date(); d.setUTCHours(0, 0, 0, 0);
    cut = d.getTime() - curDays * 864e5;
  }
  return ROWS.filter(r => srcOk(r) && r.t >= cut);
}

// tab strips are rebuilt on every render; keep keyboard focus on the same tab
function mkTabs(el, opts, cur, cb) {
  const had = el.contains(document.activeElement) ? document.activeElement.textContent : null;
  el.replaceChildren();
  for (const o of opts) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = o.label;
    if (o.value === cur) b.classList.add("on");
    b.setAttribute("aria-pressed", String(o.value === cur));
    b.onclick = () => cb(o.value);
    el.appendChild(b);
    if (had !== null && had === o.label) b.focus({ preventScroll: true });
  }
}

function chartLabel(days, totals, title) {
  if (!days.length) return title + ": no data in this range";
  const sum = totals.reduce((a, b) => a + (b || 0), 0);
  let pk = 0; totals.forEach((v, i) => { if ((v || 0) > (totals[pk] || 0)) pk = i; });
  const span = `${days.length} day${days.length === 1 ? "" : "s"}, ${days[0]} to ${days[days.length - 1]}`;
  if (_showcase) return `${title} per day, ${span} (numbers hidden)`;
  return `${title} per day, ${span}: total ${fmt(sum)}, highest ${fmt(totals[pk] || 0)} on ${days[pk]}. ` +
         "The Items table below lists the same range.";
}

// drag across the chart to select a date range
function setupRangeSelect() {
  if (_rangeSelInit || !chart) return;
  _rangeSelInit = true;
  const canvas = $("chMain"), wrap = canvas.parentElement;
  const sel = document.createElement("div");
  sel.className = "sd-band";
  Object.assign(sel.style, { position: "absolute", background: "rgba(59,109,255,.14)",
    borderLeft: "1.5px solid #3b6dff", borderRight: "1.5px solid #3b6dff",
    pointerEvents: "none", display: "none", zIndex: "2", borderRadius: "2px" });
  wrap.appendChild(sel);
  const area = () => chart.chartArea;
  const showBand = () => {
    sel.style.top = area().top + "px";
    sel.style.height = (area().bottom - area().top) + "px";
    sel.style.display = "block";
  };
  let dragging = false, startX = 0;
  const clampX = x => Math.min(Math.max(x, area().left), area().right);
  const dateAt = px => {
    const n = chart.data.labels.length;
    let i = Math.round(chart.scales.x.getValueForPixel(px));
    i = Math.min(Math.max(i, 0), n - 1);
    return chart.data.labels[i];
  };
  canvas.style.cursor = "crosshair";
  canvas.addEventListener("pointerdown", e => {
    if (e.button !== 0 || !chart.chartArea || _compareMode) return;
    if (e.offsetY > area().bottom + 2) return;          // leave the legend clickable
    dragging = true; startX = clampX(e.offsetX);
    sel.style.left = startX + "px"; sel.style.width = "0"; showBand();
    try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* older browsers */ }
  });
  canvas.addEventListener("pointermove", e => {
    if (!dragging) return;
    const x = clampX(e.offsetX);
    sel.style.left = Math.min(startX, x) + "px";
    sel.style.width = Math.abs(x - startX) + "px";
  });
  canvas.addEventListener("pointerup", e => {
    if (!dragging) return;
    dragging = false; sel.style.display = "none";
    const x = clampX(e.offsetX);
    if (Math.abs(x - startX) < 8) return;               // a click, not a drag
    const a = dateAt(Math.min(startX, x)), b = dateAt(Math.max(startX, x));
    customRange = { s: new Date(a + "T00:00:00Z").getTime(), e: new Date(b + "T00:00:00Z").getTime() + 864e5 - 1 };
    render();
  });
  canvas.addEventListener("pointercancel", () => { dragging = false; sel.style.display = "none"; });
}

function render() {
  const data = filtered();
  const withRows = new Set(ROWS.map(r => r.src));
  const sources = STORES.map(s => s.source).filter(s => withRows.has(s));
  mkTabs($("srcTabs"),
    [{ label: "All stores", value: "All" },
     ...sources.map(s => ({ label: storeName(s), value: s })),
     ...(sources.length >= 2 ? [{ label: "Compare", value: "Compare" }] : [])],
    curSrc, v => { curSrc = v; customRange = null; render(); });
  const cmp = curSrc === "Compare";
  $("rangeTabs").style.display = cmp ? "none" : "";
  if (cmp) $("rangeTabs").replaceChildren();
  else {
    mkTabs($("rangeTabs"),
      [{ label: "7D", value: 7 }, { label: "30D", value: 30 }, { label: "90D", value: 90 }, { label: "All", value: 0 }],
      customRange ? null : curDays, v => { curDays = v; customRange = null; render(); });
    if (customRange) {       // a selected range shows as a clearable chip
      const chip = document.createElement("button");
      chip.type = "button"; chip.className = "on"; chip.setAttribute("aria-pressed", "true");
      chip.textContent = "✕ " + fmtDate(dayKey(customRange.s)) + " – " + fmtDate(dayKey(customRange.e));
      chip.title = "Clear selected range";
      chip.setAttribute("aria-label", "Clear the selected range " + chip.textContent.slice(2));
      chip.onclick = () => { customRange = null; render(); };
      $("rangeTabs").appendChild(chip);
    }
  }

  const dayset = {};
  for (const r of data) dayset[dayKey(r.t)] = 1;
  let days = Object.keys(dayset).sort().filter(k => k < todayKey());
  if (days.length > 1) {     // fill empty days so the line does not skip them
    const filled = [];
    for (let d = new Date(days[0]); dayKey(d) <= days[days.length - 1]; d.setUTCDate(d.getUTCDate() + 1))
      filled.push(dayKey(d));
    days = filled;
  }
  const nDays = Math.max(days.length, 1);

  const rbx = data.reduce((s, r) => s + r.rbx, 0);
  const fin = finFor();
  $("vRobux").textContent = fmt(rbx);
  $("vSales").textContent = fmt(data.length);
  $("vPending").textContent = fin.pending != null ? fmt(fin.pending) : "–";
  $("vFunds").textContent = fin.funds != null ? fmt(fin.funds) : "–";
  const noGroup = !fin.groups;
  $("sPending").textContent = noGroup ? "groups only" : (curSrc === "All" ? "combined groups, per Roblox" : "per Roblox");
  $("sFunds").textContent = noGroup ? "groups only" : "current balance";
  $("vItems").textContent = fin.items != null ? fmt(fin.items) : "–";
  $("sItems").textContent = noGroup ? "groups only"
    : fin.items == null ? (fin.catErr ? "count incomplete — retrying" : "counting…")
    : "as of " + ageText(fin.asOf) + (fin.catErr ? " · last recount incomplete" : "");
  $("vGolden").textContent = formatGoldenSafe(goldenSafe(fin.pending, fin.items, rbx, data.length));

  let game = 0, web = 0, unk = 0;       // only CSV-imported rows carry a location
  for (const r of data) {
    if (r.loc === "Game") game++; else if (r.loc === "Website") web++; else unk++;
  }
  const known = game + web;
  $("vLoc").textContent = known ? `${Math.round(100 * game / known)}% / ${Math.round(100 * web / known)}%` : "–";
  $("sLoc").textContent = known ? `${fmt(game)} in-game · ${fmt(web)} website` + (unk ? ` · ${fmt(unk)} unknown` : "")
                                : "no location data in range";
  $("vAvg").textContent = metric === "robux" ? fmt(rbx / nDays) : fmt(data.length / nDays);
  $("sAvg").textContent = metric === "robux" ? "Robux per day" : "sales per day";
  $("vAvgSale").textContent = data.length ? fmt(rbx / data.length) : "–";
  $("sRobux").textContent = `${fmt(rbx / nDays)} / day`;
  $("sSales").textContent = `${fmt(data.length / nDays)} / day`;
  for (const [id, m] of [["cardRobux", "robux"], ["cardSales", "sales"]]) {
    $(id).classList.toggle("sel", metric === m);
    $(id).setAttribute("aria-pressed", String(metric === m));
  }
  $("chartTitle").textContent = metric === "robux" ? "Robux Earned" : "Sales";
  $("chartSub").textContent = `per day · ${curSrc === "All" ? "all stores" : storeName(curSrc)}` +
    " · top items broken out, rest grouped as Other" + (customRange ? "" : " · drag to select a range");

  const itemTotals = {}, itemDay = {};
  for (const r of data) {
    const k = dayKey(r.t);
    itemTotals[r.item] = (itemTotals[r.item] || 0) + val(r);
    const m = itemDay[r.item] || (itemDay[r.item] = {});
    m[k] = (m[k] || 0) + val(r);
  }
  const top = Object.keys(itemTotals).sort((a, b) => itemTotals[b] - itemTotals[a]).slice(0, 3);
  const dayTotal = {};
  for (const r of data) dayTotal[dayKey(r.t)] = (dayTotal[dayKey(r.t)] || 0) + val(r);
  const totals = days.map(k => dayTotal[k] || 0);
  const otherSeries = days.map((k, i) => totals[i] - top.reduce((s, it) => s + ((itemDay[it] || {})[k] || 0), 0));

  // the all-time-high day for this store + metric, over the whole history
  const athTotal = {};
  for (const r of ROWS) {
    if ((curSrc === "All" || curSrc === "Compare") ? !combined(r.src) : r.src !== curSrc) continue;
    const k = dayKey(r.t);
    if (k < todayKey()) athTotal[k] = (athTotal[k] || 0) + val(r);
  }
  const athDay = Object.keys(athTotal).reduce((b, k) => athTotal[k] > (athTotal[b] ?? -1) ? k : b, null);

  const manyDays = days.length > 45;
  let ds = [
    { label: "Total", data: totals, borderColor: TOTAL_COLOR, backgroundColor: TOTAL_COLOR,
      borderWidth: 2.5, pointStyle: "circle",
      pointRadius: days.map(k => k === athDay ? 5 : (manyDays ? 2 : 3)),
      pointBackgroundColor: days.map(k => k === athDay ? ATH_COLOR : TOTAL_COLOR),
      pointBorderColor: days.map(k => k === athDay ? "#fff" : TOTAL_COLOR),
      pointBorderWidth: days.map(k => k === athDay ? 2 : 1), pointHitRadius: 8, tension: .3 },
    { label: "Other", data: otherSeries, borderColor: OTHER_COLOR, backgroundColor: OTHER_COLOR,
      borderWidth: 2.5, pointStyle: "rectRot", pointRadius: manyDays ? 2.5 : 3.5, pointHitRadius: 8, tension: .3 },
    ...top.map((it, i) => ({
      label: it, data: days.map(k => (itemDay[it] || {})[k] || 0), borderColor: PALETTE[i],
      backgroundColor: PALETTE[i], borderWidth: 1.5, pointStyle: STYLES[i % STYLES.length],
      pointRadius: manyDays ? 1.5 : 2.5, pointHitRadius: 8, tension: .3 })),
  ];

  // Compare: each store's daily curve aligned to ITS OWN first sale (day 0), so stores
  // launched at different times can be compared from the same starting point
  _compareMode = cmp;
  if (cmp) {
    const CMP = [TOTAL_COLOR, "#ffb74d", "#4dd8a3", "#f76fbe", "#63e0e0"];
    let maxAge = 0;
    const per = sources.map(g => {
      const gr = ROWS.filter(r => r.src === g && dayKey(r.t) < todayKey());
      if (!gr.length) return null;
      const first = gr.reduce((m, r) => Math.min(m, +new Date(dayKey(r.t) + "T00:00:00Z")), Infinity);
      const byAge = {}; let gMax = 0;
      for (const r of gr) {
        const age = Math.round((+new Date(dayKey(r.t) + "T00:00:00Z") - first) / 864e5);
        byAge[age] = (byAge[age] || 0) + val(r);
        gMax = Math.max(gMax, age);
      }
      maxAge = Math.max(maxAge, gMax);
      return { name: storeName(g), byAge, gMax };
    }).filter(Boolean);
    const ages = Array.from({ length: maxAge + 1 }, (_, i) => i);
    days = ages.map(String);
    const smooth = arr => arr.map((v, i) => {     // 7-day rolling average
      if (v === null) return null;
      let s = 0, n = 0;
      for (let j = Math.max(0, i - 6); j <= i; j++) if (arr[j] != null) { s += arr[j]; n++; }
      return n ? Math.round(s / n) : null;
    });
    ds = [];
    per.forEach((p, i) => {
      const color = CMP[i % CMP.length];
      const raw = ages.map(a => a <= p.gMax ? (p.byAge[a] || 0) : null);
      ds.push({ label: p.name, data: smooth(raw), _raw: raw, borderColor: color,
        backgroundColor: c2 => {
          const { ctx, chartArea } = c2.chart;
          if (!chartArea) return "rgba(0,0,0,0)";
          const g = ctx.createLinearGradient(0, chartArea.bottom, 0, chartArea.top);
          g.addColorStop(0, color + "00"); g.addColorStop(1, color + "2e");
          return g;
        },
        fill: "origin", borderWidth: 2.5, pointRadius: 0, pointHitRadius: 10,
        pointBackgroundColor: color, tension: .35, spanGaps: false });
      ds.push({ label: p.name + " (daily)", data: raw, borderColor: color + "38", backgroundColor: "transparent",
        borderWidth: 1.2, pointRadius: 0, pointHitRadius: 0, tension: .3, spanGaps: false });
    });
    $("chartTitle").textContent = (metric === "robux" ? "Robux" : "Sales") + " per day — stores aligned to day 0";
    $("chartSub").textContent = "7-day average (bold) over raw daily (faint) · aligned to each store's first sale";
  }

  _athDay = athDay;
  $("chMain").setAttribute("aria-label", chartLabel(days, ds.length ? ds[0].data : [], $("chartTitle").textContent));
  const sig = days.join(",") + "|" + ds.map(d => d.label + ":" + d.data.join(",")).join(";");
  if (chart) {
    if (sig !== _chartSig) {           // update in place — no flicker, no rebuild
      chart.data.labels = days; chart.data.datasets = ds; chart.update("none"); _chartSig = sig;
    }
  } else if (HAVE_CHART) {
    _chartSig = sig;
    chart = new Chart($("chMain"), {
      type: "line", plugins: [athLabel], data: { labels: days, datasets: ds },
      options: {
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "bottom",
            onClick: (e, item, legend) => {     // a series toggles its faint "(daily)" twin too
              const c = legend.chart, base = c.data.datasets[item.datasetIndex].label;
              const hide = !c.getDatasetMeta(item.datasetIndex).hidden;
              c.data.datasets.forEach((d, i) => {
                if (d.label === base || d.label === base + " (daily)") c.getDatasetMeta(i).hidden = hide;
              });
              c.update();
            },
            labels: { usePointStyle: true, pointStyle: "circle", boxWidth: 7, boxHeight: 7, padding: 14,
                      filter: item => !item.text.endsWith("(daily)") } },
          tooltip: {
            backgroundColor: "#26282c", borderColor: "#3a3c41", borderWidth: 1, titleFont: { weight: "700" },
            padding: 12, usePointStyle: true, boxWidth: 7, boxHeight: 7,
            filter: item => !item.dataset.label.endsWith("(daily)"),
            itemSort: (a, b) => b.parsed.y - a.parsed.y,
            callbacks: {
              title: items => _compareMode ? ("Day " + items[0].label)
                : fmtDate(items[0].label) + (items[0].label === _athDay ? "  ★ all-time high" : ""),
              label: c => {
                if (_showcase) return ` ${c.dataset.label}   •••`;
                const raw = _compareMode && c.dataset._raw ? c.dataset._raw[c.dataIndex] : null;
                return ` ${c.dataset.label}   ${fmt(raw != null ? raw : c.parsed.y)}`;
              },
            },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 12,
               callback: function (v) { const l = this.getLabelForValue(v); return _compareMode ? ("Day " + l) : fmtDate(l); } } },
          y: { beginAtZero: true, ticks: { maxTicksLimit: 6, display: !_showcase },
               grid: { color: "#2b2e34" }, border: { dash: [4, 4], display: false } },
        },
      },
    });
  }
  setupRangeSelect();

  if (athDay && !cmp) {
    const sp = document.createElement("span");
    sp.className = "sd-money";
    sp.textContent = ` · ★ all-time high ${fmt(athTotal[athDay])} on ${fmtDate(athDay)}`;
    $("chartSub").appendChild(sp);
  }

  // items table
  const items = {};
  for (const r of data) {
    const it = items[r.item] || (items[r.item] = { n: 0, rbx: 0, last: r.t });
    it.n++; it.rbx += r.rbx;
    if (r.t > it.last) it.last = r.t;
  }
  const order = { rbx: (a, b) => b[1].rbx - a[1].rbx, n: (a, b) => b[1].n - a[1].n,
                  last: (a, b) => b[1].last - a[1].last }[sortKey];
  document.querySelectorAll("th.sortable").forEach(th => {
    th.classList.toggle("on", th.dataset.sort === sortKey);
    th.setAttribute("aria-sort", th.dataset.sort === sortKey ? "descending" : "none");
  });
  const tb = $("tbl").querySelector("tbody");
  tb.replaceChildren();
  Object.entries(items).sort(order).slice(0, 100).forEach(([name, it]) => {
    const tr = document.createElement("tr");
    const td0 = document.createElement("td");
    const dot = document.createElement("span");
    dot.className = "swatch";
    const i = top.indexOf(name);
    dot.style.background = i >= 0 ? PALETTE[i] : "#3a3c41";
    td0.append(dot, document.createTextNode(name));
    tr.appendChild(td0);
    for (const v of [fmt(it.n), fmt(it.rbx), fmt(it.rbx / nDays),
                     (rbx ? (100 * it.rbx / rbx).toFixed(1) : "0") + "%", fmtDate(dayKey(it.last))]) {
      const td = document.createElement("td"); td.className = "num"; td.textContent = v; tr.appendChild(td);
    }
    tb.appendChild(tr);
  });
  const wrap = $("itemsWrap"), toggle = $("itemsToggle"), count = tb.children.length;
  if (count <= 7) { wrap.classList.add("short"); toggle.hidden = true; }
  else {
    wrap.classList.remove("short"); toggle.hidden = false;
    toggle.textContent = wrap.classList.contains("open") ? "Show less ▴" : `Show all (${count}) ▾`;
  }
}
$("itemsToggle").addEventListener("click", () => {
  const wrap = $("itemsWrap"), open = wrap.classList.toggle("open");
  $("itemsToggle").setAttribute("aria-expanded", String(open));
  $("itemsToggle").textContent = open ? "Show less ▴" : `Show all (${wrap.querySelectorAll("tbody tr").length}) ▾`;
});
document.querySelectorAll("button.card[data-metric]").forEach(b =>
  b.addEventListener("click", () => { metric = b.dataset.metric; render(); }));
document.querySelectorAll("th.sortable .sd-sort").forEach(b =>
  b.addEventListener("click", () => { sortKey = b.closest("th").dataset.sort; render(); }));

// ── current Robux balance (top-bar pill) ─────────────────────────────────────
const fmtShort = n => n >= 1000 ? (Math.floor(n / 10) / 100).toFixed(2) + "K+" : n.toLocaleString();
function renderBalance() {
  if (!BAL || BAL.robux == null) return;
  $("robuxVal").textContent = fmtShort(BAL.robux);
  $("robuxPill").title = `Your account's Robux balance: ${fmt(BAL.robux)}, read ${ageText(BAL.ts)}`;
  $("robuxPill").hidden = false;
}

// ── CSV import + imported months ─────────────────────────────────────────────
function renderImportLog(imports) {
  const box = $("importLog");
  box.replaceChildren();
  if (!imports || !imports.length) {
    const d = document.createElement("div"); d.className = "sub"; d.textContent = "None yet."; box.appendChild(d); return;
  }
  const bySrc = {};
  imports.forEach(i => { (bySrc[i.name] || (bySrc[i.name] = [])).push(i); });
  Object.keys(bySrc).sort().forEach(name => {
    const wrap = document.createElement("div"); wrap.className = "imp-src";
    const nm = document.createElement("div"); nm.className = "imp-src-name"; nm.textContent = name; wrap.appendChild(nm);
    const chips = document.createElement("div"); chips.className = "imp-chips";
    bySrc[name].sort((a, b) => b.month.localeCompare(a.month)).forEach(i => {
      const c = document.createElement("span"); c.className = "imp-chip"; c.textContent = i.month;
      c.title = `${i.filename} · ${fmt(i.rows)} rows`;
      chips.appendChild(c);
    });
    wrap.appendChild(chips); box.appendChild(wrap);
  });
}

function wireCsv() {
  const inp = $("csvInput"), drop = $("csvDrop"), out = $("csvResult"), txt = $("csvDropText");
  const send = async files => {
    if (!files || !files.length) return;
    out.replaceChildren();
    let i = 0;
    for (const f of [...files]) {
      txt.textContent = `Importing ${++i} of ${files.length}…`;
      let line;
      try {
        const r = await fetch("/import-csv", { method: "POST", body: f,
          headers: { "X-Filename": encodeURIComponent(f.name), "X-RSA-Request": "1" } });
        const j = await r.json();
        line = j.ok ? `✓ ${j.filename} — ${fmt(j.rows)} rows for ${j.month}` : `✗ ${f.name} — ${j.error}`;
        if (j.imports) renderImportLog(j.imports);
      } catch (e) { line = `✗ ${f.name} — upload failed: ${e}`; }
      if (out.childNodes.length) out.appendChild(document.createElement("br"));
      out.appendChild(document.createTextNode(line));
    }
    txt.textContent = "Choose or drop CSV / .zip files"; inp.value = "";
    _epoch = "";                 // a month import rewrites rows: fetch everything again
    loadData();
  };
  inp.addEventListener("change", () => send(inp.files));
  drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", e => { e.preventDefault(); drop.classList.remove("drag"); send(e.dataTransfer.files); });
}

// ── data loading ─────────────────────────────────────────────────────────────
let reconnectTimer = null;
const toRows = a => a.map(r => ({ id: r[0], t: parseTs(r[1]), rbx: r[2] || 0, src: r[3], item: r[4] || "?", loc: r[5] }))
                     .filter(r => !Number.isNaN(r.t));          // one bad row must not break the page
async function loadData(live) {
  if (_stopped) return;
  if (_loading) { _loadAgain = true; return; }                  // one request in flight
  _loading = true;
  try {
    const p = new URLSearchParams();
    if (live) p.set("live", "1");
    if (_epoch && Date.now() - _baseAt < FULL_REFRESH_MS) {
      p.set("epoch", _epoch); p.set("rowid", _maxRowid); p.set("count", _count);
    }
    const r = await fetch("/data?" + p, { cache: "no-store" });
    if (r.status === 401) {
      _stopped = true;
      $("status").className = "err";
      $("statusText").textContent = "signed out — reload the page to sign in again";
      return;
    }
    const j = await r.json();
    if (j.rows) { ROWS = toRows(j.rows); _baseAt = Date.now(); }
    else if (j.rows_add) ROWS = ROWS.concat(toRows(j.rows_add));
    if (j.epoch !== undefined && (j.rows || j.rows_add)) { _epoch = j.epoch; _maxRowid = j.max_rowid; _count = j.count; }
    if (j.stores) STORES = j.stores;
    if (j.financials) FIN = j.financials;
    if (j.balance) BAL = j.balance;           // null when never read: keep what we show
    if (j.catalog) CAT = j.catalog;
    const st = j.status || {};
    if (st.sync_error) {
      $("status").className = st.polling ? "err" : "warn";
      $("statusText").textContent = st.sync_error;
    } else {
      $("status").className = "";
      const ago = st.last_sync ? Math.round((Date.now() - new Date(st.last_sync)) / 1000) + "s ago" : "not yet";
      $("statusText").textContent = st.polling
        ? `live · ${ROWS.length.toLocaleString()} sales · synced ${ago}`
        : `${ROWS.length.toLocaleString()} sales · not polling Roblox (stored data only)`;
    }
    if (!j.error) {
      render(); renderBalance(); renderImportLog(j.imports);
      $("signOut").hidden = !st.auth;
      $("btnCatalog").hidden = !st.polling;
    }
    if (reconnectTimer) { clearInterval(reconnectTimer); reconnectTimer = null; }
  } catch (e) {
    // keep what is on screen and retry quietly — rides out a restart of the app
    $("status").className = "err";
    $("statusText").textContent = "reconnecting…";
    if (!reconnectTimer) reconnectTimer = setInterval(loadData, 4000);
  } finally {
    _loading = false;
    if (_loadAgain) { _loadAgain = false; loadData(); }
  }
}

// showcase mode — blur the numbers, keep the graphs; remembered across reloads
const _dash = $("dash");
function setShowcase(on) {
  _showcase = on;
  _dash.classList.toggle("showcase", on);
  $("btnShowcase").textContent = on ? "Show stats" : "Hide stats";
  $("btnShowcase").setAttribute("aria-pressed", String(on));
  if (chart) { chart.options.scales.y.ticks.display = !on; chart.update("none"); }
  try { localStorage.setItem("rsa_showcase", on ? "1" : "0"); } catch (_) { /* storage blocked */ }
}
$("btnShowcase").onclick = () => setShowcase(!_dash.classList.contains("showcase"));
try { if (localStorage.getItem("rsa_showcase") === "1") setShowcase(true); } catch (_) { /* storage blocked */ }

wireCsv();
// keep html{scroll-padding-top} equal to the sticky header's real height (it wraps on narrow screens)
(function () {
  const st = document.querySelector(".stickytop");
  if (!st || typeof ResizeObserver === "undefined") return;
  new ResizeObserver(() => document.documentElement.style.setProperty("--sticky-h", st.offsetHeight + "px")).observe(st);
})();
$("btnCatalog").addEventListener("click", async () => {
  const b = $("btnCatalog");
  b.disabled = true;
  try {
    await fetch("/refresh-catalog", { method: "POST", headers: { "X-RSA-Request": "1" } });
    $("sItems").textContent = "recount requested…";
  } catch (_) { /* the next poll shows the state */ }
  setTimeout(() => { b.disabled = false; loadData(); }, 60000);
});
loadData(true).finally(() => {
  setInterval(() => { if (!document.hidden) loadData(); }, 60000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadData(); });
});
