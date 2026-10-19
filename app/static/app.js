"use strict";

const $ = (s) => document.querySelector(s);
const state = { cats: {}, extraction: null, sha: null, viewer: "", charts: {}, loaded: {} };
const money = (n, d = 2) =>
  (n < 0 ? "-$" : "$") + Math.abs(Number(n) || 0).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const u = new URL(path, location.origin);
  if (state.viewer && !opts.mine) u.searchParams.set("user", state.viewer);
  const res = await fetch(u, opts);
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new Error(body?.detail || `Request failed (${res.status})`);
  return body;
}

function toast(msg, error = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (error ? " error" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = "toast"), error ? 5000 : 2600);
}

/* ---------------- tabs ---------------- */
function show(view) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.toggleAttribute("data-active", v.id === view));
  if (view === "spending") loadDashboard();
  if (view === "engine" && !state.loaded.engine) loadStats(true);
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

$("#viewer").addEventListener("change", (e) => {
  state.viewer = e.target.value;
  state.loaded = {};
  $("#month").innerHTML = "";
  loadRecent();
  const active = document.querySelector(".view[data-active]").id;
  if (active === "spending") loadDashboard();
  $("#answer").hidden = true;
});

/* ---------------- snap ---------------- */
const shutter = $("#shutter");
$("#file").addEventListener("change", (e) => e.target.files[0] && handleFile(e.target.files[0]));
["dragenter", "dragover"].forEach((ev) => shutter.addEventListener(ev, (e) => { e.preventDefault(); shutter.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => shutter.addEventListener(ev, (e) => { e.preventDefault(); shutter.classList.remove("drag"); }));
shutter.addEventListener("drop", (e) => e.dataTransfer.files[0] && handleFile(e.dataTransfer.files[0]));

async function handleFile(file) {
  const status = $("#snap-status");
  const prev = $("#preview");
  if (file.type.startsWith("image/")) { prev.src = URL.createObjectURL(file); prev.hidden = false; } else prev.hidden = true;
  $("#review").hidden = true;
  status.textContent = "Reading the receipt…";
  status.className = "status busy";
  const fd = new FormData();
  fd.append("file", file);
  try {
    const data = await api("/api/extract", { method: "POST", body: fd, mine: true });
    if (data.duplicate) {
      status.className = "status";
      status.textContent = `Already saved: ${data.duplicate.merchant}, ${money(data.duplicate.total)} on ${new Date(data.duplicate.purchased_at).toLocaleDateString()}.`;
      return;
    }
    status.className = "status";
    status.textContent = "Check every line, fix anything that's off, then save.";
    state.sha = data.image_sha256;
    state.extraction = data.extraction;
    renderReview(data);
  } catch (err) {
    status.className = "status";
    status.textContent = err.message;
  } finally {
    $("#file").value = "";
  }
}

function catOptions(selected) {
  return Object.entries(state.cats).map(([c, v]) =>
    `<optgroup label="${esc(c)}">${v.subs.map((s) => {
      const val = `${c}|${s}`;
      return `<option value="${esc(val)}"${val === selected ? " selected" : ""}>${esc(c)} › ${esc(s)}</option>`;
    }).join("")}</optgroup>`).join("");
}

function lineHTML(it = { description: "", amount: 0, quantity: 1, category: "Other", subcategory: "Uncategorized" }) {
  const val = `${it.category}|${it.subcategory}`;
  return `<li>
    <input class="desc" value="${esc(it.description)}" aria-label="Item" required>
    <input class="amt" type="number" step="0.01" inputmode="decimal" value="${Number(it.amount).toFixed(2)}" aria-label="Amount">
    <button type="button" class="x" aria-label="Remove line">×</button>
    <select class="cat" aria-label="Category">${catOptions(val)}</select>
    <input type="hidden" class="qty" value="${it.quantity ?? 1}">
  </li>`;
}

function renderReview({ extraction: x, checks, possible_duplicate }) {
  const form = $("#review");
  $("#r-merchant").value = x.merchant || "";
  $("#r-date").value = x.purchase_date || new Date().toISOString().slice(0, 10);
  $("#r-time").value = x.purchase_time || "";
  $("#r-pay").value = x.payment_method || "";
  $("#r-tax").value = x.tax ?? "";
  $("#r-tip").value = x.tip ?? "";
  $("#r-total").value = Number(x.total).toFixed(2);
  $("#r-items").innerHTML = x.items.map(lineHTML).join("");
  const alerts = [...(checks || []), ...(x.warnings || [])].map((m) => `<p class="alert">${esc(m)}</p>`);
  if (possible_duplicate) {
    alerts.unshift(`<p class="alert info">Looks like a receipt you already saved: ${esc(possible_duplicate.merchant)}, ${money(possible_duplicate.total)}. Save only if it's a separate purchase.</p>`);
  }
  $("#r-alerts").innerHTML = alerts.join("");
  form.hidden = false;
  form.classList.remove("printing");
  void form.offsetWidth;
  form.classList.add("printing");
  rebalance();
  form.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}

function rebalance() {
  const items = [...document.querySelectorAll("#r-items .amt")].reduce((s, i) => s + (parseFloat(i.value) || 0), 0);
  const tax = parseFloat($("#r-tax").value) || 0;
  const tip = parseFloat($("#r-tip").value) || 0;
  const total = parseFloat($("#r-total").value) || 0;
  $("#r-itemsum").textContent = items.toFixed(2);
  const diff = Math.round((items + tax + tip - total) * 100) / 100;
  const b = $("#r-balance");
  if (Math.abs(diff) <= 0.02) { b.textContent = "Lines add up to the total."; b.className = "balance on"; }
  else { b.textContent = `Lines are ${money(Math.abs(diff))} ${diff > 0 ? "over" : "under"} the total.`; b.className = "balance off"; }
}

$("#review").addEventListener("input", rebalance);
$("#r-items").addEventListener("click", (e) => {
  if (e.target.classList.contains("x")) { e.target.closest("li").remove(); rebalance(); }
});
$("#add-line").addEventListener("click", () => {
  $("#r-items").insertAdjacentHTML("beforeend", lineHTML());
  $("#r-items li:last-child .desc").focus();
});
$("#discard").addEventListener("click", () => {
  $("#review").hidden = true; $("#preview").hidden = true; $("#snap-status").textContent = "";
});

$("#review").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.submitter; btn.disabled = true;
  const num = (id) => ($(id).value === "" ? null : parseFloat($(id).value));
  const items = [...document.querySelectorAll("#r-items li")].map((li) => {
    const [category, subcategory] = li.querySelector(".cat").value.split("|");
    return { description: li.querySelector(".desc").value, amount: parseFloat(li.querySelector(".amt").value) || 0,
             quantity: parseFloat(li.querySelector(".qty").value) || 1, category, subcategory };
  });
  const body = {
    merchant: $("#r-merchant").value, purchase_date: $("#r-date").value || null, purchase_time: $("#r-time").value || null,
    payment_method: $("#r-pay").value || null, tax: num("#r-tax"), tip: num("#r-tip"), total: num("#r-total"),
    currency: state.extraction?.currency || "USD", subtotal: state.extraction?.subtotal ?? null,
    items, image_sha256: state.sha, raw: state.extraction,
  };
  try {
    await api("/api/receipts", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body), mine: true });
    toast(`Saved ${body.merchant}, ${money(body.total)}`);
    $("#review").hidden = true; $("#preview").hidden = true; $("#snap-status").textContent = "";
    state.loaded = {};
    loadRecent();
  } catch (err) { toast(err.message, true); }
  finally { btn.disabled = false; }
});

async function loadRecent() {
  const ul = $("#recent");
  try {
    const rows = await api("/api/receipts?limit=20");
    if (!rows.length) { ul.innerHTML = `<li class="empty">Nothing saved yet. Photograph your first receipt above.</li>`; return; }
    ul.innerHTML = rows.map((r) => `<li><details data-id="${r.id}">
      <summary><strong>${esc(r.merchant)}</strong><span class="tot">${money(r.total)}</span>
      <span class="when">${new Date(r.purchased_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })} · ${r.items} lines</span></summary>
      <div class="items">Loading…</div></details></li>`).join("");
  } catch (err) { ul.innerHTML = `<li class="empty">${esc(err.message)}</li>`; }
}
$("#recent").addEventListener("toggle", async (e) => {
  const d = e.target;
  if (!d.open || d.dataset.loaded) return;
  d.dataset.loaded = 1;
  const box = d.querySelector(".items");
  try {
    const r = await api(`/api/receipts/${d.dataset.id}`);
    box.innerHTML = r.items.map((i) => `<div><span>${esc(i.description)}<br><small>${esc(i.category)} › ${esc(i.subcategory)}</small></span><span>${money(i.amount)}</span></div>`).join("")
      + (state.viewer ? "" : `<p><button class="ghost small del" data-id="${r.id}">Delete receipt</button></p>`);
  } catch (err) { box.textContent = err.message; }
}, true);
$("#recent").addEventListener("click", async (e) => {
  if (!e.target.classList.contains("del")) return;
  if (!confirm("Delete this receipt and all its lines?")) return;
  try { await api(`/api/receipts/${e.target.dataset.id}`, { method: "DELETE", mine: true }); toast("Deleted receipt"); state.loaded = {}; loadRecent(); }
  catch (err) { toast(err.message, true); }
});

/* ---------------- spending ---------------- */
function chart(id, config) {
  state.charts[id]?.destroy();
  const ink = css("--faded"), rule = css("--rule");
  Chart.defaults.color = ink;
  Chart.defaults.font.family = css("--sans");
  Chart.defaults.borderColor = rule;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) Chart.defaults.animation = false;
  state.charts[id] = new Chart(document.getElementById(id), config);
}
const monthLabel = (iso) => new Date(iso.slice(0, 7) + "-15").toLocaleDateString(undefined, { month: "short", year: "2-digit" });

async function loadDashboard() {
  const sel = $("#month");
  let d;
  try { d = await api("/api/dashboard" + (sel.value ? `?month=${sel.value}` : "")); }
  catch (err) { toast(err.message, true); return; }
  if (!sel.options.length || !d.months_available.includes(sel.value)) {
    const months = d.months_available.length ? d.months_available : [d.month];
    sel.innerHTML = months.map((m) => `<option value="${m}">${new Date(m + "-15").toLocaleDateString(undefined, { month: "long", year: "numeric" })}</option>`).join("");
    sel.value = d.month;
  }
  $("#m-total").textContent = money(d.this_month, 0);
  const cmp = $("#m-compare");
  const partial = d.last_month_to_date !== null && d.last_month_to_date !== undefined;
  const base = partial ? d.last_month_to_date : d.last_month;
  if (base > 0) {
    const pct = Math.round((d.this_month / base - 1) * 100);
    cmp.textContent = partial
      ? `${pct >= 0 ? "up" : "down"} ${Math.abs(pct)}% from ${money(base, 0)} by this point last month`
      : `${pct >= 0 ? "up" : "down"} ${Math.abs(pct)}% from ${money(base, 0)} the month before`;
    cmp.className = "compare " + (pct > 0 ? "up" : "down");
  } else { cmp.textContent = "spent this month"; cmp.className = "compare"; }

  // stacked monthly bars
  const months = [...new Set(d.monthly.map((r) => r.month.slice(0, 7)))];
  const cats = Object.keys(state.cats).filter((c) => d.monthly.some((r) => r.category === c));
  chart("c-monthly", {
    type: "bar",
    data: { labels: months.map(monthLabel), datasets: cats.map((c) => ({
      label: c, backgroundColor: state.cats[c].color, borderRadius: 2,
      data: months.map((m) => d.monthly.filter((r) => r.month.startsWith(m) && r.category === c).reduce((s, r) => s + r.total, 0)),
    })) },
    options: { maintainAspectRatio: false, scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, ticks: { callback: (v) => money(v, 0) } } },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10 } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${money(c.raw)}` } } } },
  });

  // cumulative pace
  const m0 = d.month;
  const prev = new Date(m0 + "-15"); prev.setMonth(prev.getMonth() - 1);
  const p0 = prev.toISOString().slice(0, 7);
  const cum = (prefix) => { const arr = Array(31).fill(null); let s = 0;
    const byDay = {}; d.daily.filter((r) => r.day.startsWith(prefix)).forEach((r) => (byDay[+r.day.slice(8, 10)] = r.total));
    const last = prefix === m0 && prefix === new Date().toISOString().slice(0, 7) ? new Date().getDate() : 31;
    for (let i = 1; i <= last; i++) { s += byDay[i] || 0; arr[i - 1] = Math.round(s * 100) / 100; } return arr; };
  chart("c-pace", {
    type: "line",
    data: { labels: Array.from({ length: 31 }, (_, i) => i + 1), datasets: [
      { label: "This month", data: cum(m0), borderColor: css("--register"), borderWidth: 3, pointRadius: 0, tension: .2 },
      { label: "Last month", data: cum(p0), borderColor: css("--faded"), borderDash: [5, 4], borderWidth: 2, pointRadius: 0, tension: .2 },
    ] },
    options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: { x: { grid: { display: false }, title: { display: true, text: "Day of month" } }, y: { ticks: { callback: (v) => money(v, 0) } } },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10 } }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${money(c.raw)}` } } } },
  });

  // breakdown
  const byCat = {};
  d.by_subcategory.forEach((r) => { (byCat[r.category] ||= { total: 0, subs: [] }).total += r.total; byCat[r.category].subs.push(r); });
  const sorted = Object.entries(byCat).sort((a, b) => b[1].total - a[1].total);
  const max = sorted[0]?.[1].total || 1;
  $("#breakdown").innerHTML = sorted.length ? sorted.map(([c, v]) => `<li><details>
    <summary><span class="row"><span>${esc(c)}</span><span class="num">${money(v.total)}</span></span>
      <span class="meter"><i style="width:${(v.total / max) * 100}%;background:${state.cats[c]?.color}"></i></span></summary>
    <div class="subs">${v.subs.map((s) => `<div><span>${esc(s.subcategory)}</span><span class="num">${money(s.total)}</span></div>`).join("")}</div>
  </details></li>`).join("") : `<li class="empty">No purchases this month.</li>`;

  $("#merchants").innerHTML = d.merchants.length ? d.merchants.map((m) =>
    `<li><span>${esc(m.merchant)}<br><small>${m.visits} visit${m.visits === 1 ? "" : "s"}</small></span><span class="num">${money(m.total)}</span></li>`).join("")
    : `<li class="empty">No purchases this month.</li>`;
  $("#dash-ms").textContent = `All five dashboard queries ran against continuous aggregates in ${d.query_ms} ms.`;
}
$("#month").addEventListener("change", loadDashboard);

/* ---------------- ask ---------------- */
const examples = [
  "How much did I spend on coffee each month this year?",
  "What are my top 5 grocery items by spend?",
  "Which weekday do I spend the most on dining out?",
  "How much have I paid in sales tax this year?",
  "Weekly grocery spending over the last 3 months",
];
$("#chips").innerHTML = examples.map((q) => `<button type="button">${esc(q)}</button>`).join("");
$("#chips").addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") { $("#q").value = e.target.textContent; $("#ask-form").requestSubmit(); } });

$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.submitter || $("#ask-form button.primary");
  btn.disabled = true; btn.textContent = "Thinking…";
  try {
    const r = await api("/api/ask", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ question: $("#q").value }) });
    renderAnswer(r);
  } catch (err) { toast(err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Ask"; }
});

function renderAnswer(r) {
  $("#answer").hidden = false;
  $("#a-text").textContent = r.answer;
  $("#a-sql").textContent = r.sql;
  $("#a-ms").textContent = `${r.rows.length} row${r.rows.length === 1 ? "" : "s"} · query ran in ${r.query_ms} ms`;
  const box = $("#c-answer").parentElement;
  const cols = r.rows.length ? Object.keys(r.rows[0]) : [];
  const x = cols.includes(r.x) ? r.x : cols[0];
  const y = cols.includes(r.y) ? r.y : cols.find((c) => typeof r.rows[0]?.[c] === "number");
  const plottable = ["bar", "line", "pie"].includes(r.chart) && r.rows.length > 1 && x && y;
  box.hidden = !plottable;
  if (plottable) {
    const labels = r.rows.map((row) => (typeof row[x] === "string" && /^\d{4}-\d\d-\d\dT/.test(row[x]) ? row[x].slice(0, 10) : row[x]));
    const palette = Object.values(state.cats).map((c) => c.color);
    chart("c-answer", {
      type: r.chart === "pie" ? "doughnut" : r.chart,
      data: { labels, datasets: [{ label: r.title, data: r.rows.map((row) => row[y]),
        backgroundColor: r.chart === "pie" ? palette : css("--register"), borderColor: css("--register"), borderWidth: r.chart === "line" ? 3 : 0, pointRadius: 2, tension: .2 }] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: r.chart === "pie", position: "bottom" }, title: { display: true, text: r.title } },
        scales: r.chart === "pie" ? {} : { x: { grid: { display: false } } } },
    });
  }
  $("#a-table").innerHTML = r.rows.length && r.chart !== "number"
    ? `<table><thead><tr>${cols.map((c) => `<th>${esc(c.replace(/_/g, " "))}</th>`).join("")}</tr></thead><tbody>${
        r.rows.slice(0, 50).map((row) => `<tr>${cols.map((c) => `<td>${esc(typeof row[c] === "number" ? (Number.isInteger(row[c]) ? row[c] : row[c].toFixed(2)) : row[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>`
    : "";
}

/* ---------------- engine ---------------- */
async function loadStats(bench) {
  state.loaded.engine = true;
  let s;
  try { s = await api("/api/stats?bench=" + bench, { mine: true }); } catch (err) { toast(err.message, true); return; }
  const mb = (b) => (b / 1e6).toLocaleString(undefined, { maximumFractionDigits: b > 1e8 ? 0 : 1 }) + " MB";
  const tiles = [
    [Number(s.line_items).toLocaleString(), "line items in the hypertable"],
    [Number(s.receipts).toLocaleString(), "receipts"],
    [Number(s.users).toLocaleString(), "people"],
    [`${s.compressed_chunks}/${s.chunks}`, "chunks compressed"],
    [mb(s.hypertable_bytes), "on disk now"],
  ];
  if (s.compression_ratio) tiles.push([`${s.space_saved_pct}%`, `smaller after compression (${mb(s.compressed_before_bytes)} → ${mb(s.compressed_after_bytes)})`]);
  $("#stats").innerHTML = tiles.map(([b, t]) => `<div class="stat"><b>${b}</b><span>${t}</span></div>`).join("");
  if (s.bench) {
    $("#bench-q").textContent = s.bench.question;
    const max = Math.max(s.bench.raw_ms, s.bench.cagg_ms);
    $("#b-raw").style.width = `${(s.bench.raw_ms / max) * 100}%`;
    $("#b-cagg").style.width = `${Math.max((s.bench.cagg_ms / max) * 100, 1)}%`;
    $("#b-raw-ms").textContent = `${s.bench.raw_ms.toLocaleString()} ms`;
    $("#b-cagg-ms").textContent = `${s.bench.cagg_ms.toLocaleString()} ms`;
  }
  return s;
}
$("#rerun").addEventListener("click", async (e) => { e.target.disabled = true; await loadStats(true); e.target.disabled = false; });

/* ---------------- boot ---------------- */
(async function boot() {
  try { state.cats = await api("/api/categories", { mine: true }); } catch (err) { toast(err.message, true); }
  loadRecent();
  try {
    const s = await api("/api/stats?bench=false", { mine: true });
    if (s.demo_user) $("#viewer").insertAdjacentHTML("beforeend", `<option value="${s.demo_user}">Demo user (sample data)</option>`);
  } catch { /* stats are optional on boot */ }
})();
