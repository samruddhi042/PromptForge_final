"use strict";
/* PromptForge front end: plain JS, no build step. All model text is escaped before display.
   Colours come from the CSS theme tokens (theme.css), so light/dark switching restyles everything. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = x => Math.round((x || 0) * 100) + "%";
const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const STAGES = ["Generate variants", "Evaluate", "Red-team", "Harden", "Re-attack", "Re-evaluate"];
const LABEL = { zero_shot: "Zero-shot", few_shot: "Few-shot", cot: "Chain-of-Thought", role: "Role-based", self_consistency: "Self-consistency" };
const VCOLOR = { zero_shot: "--chart-4", few_shot: "--chart-1", cot: "--chart-3", role: "--chart-2", self_consistency: "--muted-foreground", hardened: "--destructive" };
const colorOf = id => cssv(VCOLOR[id] || "--foreground");
const ARENA_HINT = "Composite = 0.5 accuracy + 0.2 relevance + 0.2 coherence + 0.1 cost-efficiency. Relevance and coherence come from an LLM judge, so treat them as indicative.";

const S = {};
const charts = {};

function spec() { return S.pack.modes ? S.pack.modes.find(m => m.id === S.mode) : S.pack; }

function resetRun() {
  Object.assign(S, { runId: null, stage: 0, variants: [], metrics: {}, winner: null, attacks: [], attackTypes: S.pack ? spec().attack_types : [],
    hardened: null, hardMetrics: null, result: null, running: false, mock: null, basis: null });
  if (charts.radar) { charts.radar.destroy(); delete charts.radar; }
  $("#term").innerHTML = "";
  $("#feed").innerHTML = '<p class="empty">Attacks stream in here while the pipeline runs.</p>';
  $$(".dot").forEach(d => d.classList.remove("on"));
  renderAll();
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = await r.text();
    try { msg = JSON.parse(msg).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

// ------------------------------------------------------------------ theme
function setTheme(t, save = true) {
  const dark = t === "dark";
  document.documentElement.classList.toggle("dark", dark);
  $("#theme").textContent = dark ? "Light" : "Dark";
  if (save) { try { localStorage.setItem("pf-theme", t); } catch (e) {} }
  if (charts.radar) { charts.radar.destroy(); delete charts.radar; }
  renderAll();
}

// ------------------------------------------------------------------ navigation
function showPage(name) {
  $$(".page").forEach(p => p.classList.toggle("active", p.id === "page-" + name));
  $$("#nav button").forEach(b => b.classList.toggle("active", b.dataset.page === name));
  const dot = $(`#nav button[data-page="${name}"] .dot`);
  if (dot) dot.classList.remove("on");
  if (name === "library") loadLibrary();
  renderAll();
}
function flag(name) {
  const cur = $("#nav button.active").dataset.page;
  if (cur === name) return;
  const dot = $(`#nav button[data-page="${name}"] .dot`);
  if (dot) dot.classList.add("on");
}

// ------------------------------------------------------------------ studio
function renderPacks() {
  $("#packs").innerHTML = S.packs.map(p =>
    `<div class="pack ${S.pack && S.pack.id === p.id ? "active" : ""}" data-id="${esc(p.id)}">
       <span class="ic">${esc(p.icon)}</span><b>${esc(p.name)}</b><small>${esc(p.audience)}</small></div>`).join("");
  $$(".pack").forEach(el => el.onclick = () => selectPack(el.dataset.id));
}
function selectPack(id) {
  S.pack = S.packs.find(p => p.id === id);
  S.mode = S.pack.modes ? S.pack.modes[0].id : null;
  const box = $("#modebox");
  box.hidden = !S.pack.modes;
  if (S.pack.modes) {
    $("#mode").innerHTML = S.pack.modes.map(m => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
    $("#mode").value = S.mode;
  }
  renderPacks();
  applySpec();
}
function applySpec() {
  const sp = spec();
  S.attackTypes = sp.attack_types;
  $("#task").value = sp.task;
  $("#context").value = sp.context;          // default context for this domain (editable)
  const how = sp.kind === "score" ? "numeric score vs expected range" + (S.pack.modes ? " (expected range comes from really running each submission)" : "")
    : sp.kind === "decision" ? "decision vs expected decision"
    : "the returned code is RUN against test cases in a restricted sandbox";
  const warn = (S.pack.modes && S.health && S.health.code_runner === false)
    ? '<div class="note">The code runner does not work on this computer, so Code Assistant runs will fail.</div>' : "";
  $("#packinfo").innerHTML = `${warn}<div>Audience: ${esc(sp.audience || S.pack.audience)}</div>
    <div>Attacks tested: <span>${sp.attack_types.map(esc).join(", ")}</span></div>
    <div>Scoring: ${esc(how)} &middot; ${sp.n_tests} test inputs</div>`;
  ctxState();
}
function ctxState() {
  const edited = $("#context").value.trim() !== spec().context.trim();
  const pill = $("#ctxpill");
  pill.textContent = edited ? "edited" : "default";
  pill.classList.toggle("edited", edited);
  $("#ctxreset").disabled = !edited;
}
function log(msg, cls = "") {
  const t = $("#term");
  const d = document.createElement("div");
  d.className = "t-line " + cls;
  d.textContent = msg;
  t.appendChild(d);
  t.scrollTop = t.scrollHeight;
}
function renderStepper() {
  $("#stepper").innerHTML = STAGES.map((s, i) => {
    const n = i + 1, cls = S.stage === n && S.running ? "active" : (S.stage > n || (S.result && S.stage >= n)) ? "done" : "";
    return `<li class="${cls}"><b>STAGE ${n}</b>${esc(s)}</li>`;
  }).join("");
}
function renderVariants() {
  if (!S.variants.length) return;
  $("#variants").innerHTML = S.variants.map(v =>
    `<div class="vcard"><header><span><i class="sw" style="background:${colorOf(v.id)}"></i>${esc(v.label)}</span>${v.id === S.winner ? '<span class="winmark">WINNER</span>' : ""}</header>
     <pre>${esc(v.template)}</pre></div>`).join("");
}

// ------------------------------------------------------------------ arena
function sortedMetrics() {
  return Object.entries(S.metrics).sort((a, b) => (b[1].composite ?? b[1].accuracy) - (a[1].composite ?? a[1].accuracy));
}
function renderArena() {
  $("#arena-hint").textContent = ARENA_HINT + (S.basis ? " " + S.basis : "");
  if (!Object.keys(S.metrics).length) return;
  const rows = sortedMetrics().map(([id, m], i) => `
    <tr class="${id === S.winner ? "win" : ""}">
      <td>${i + 1}</td><td><i class="sw" style="background:${colorOf(id)}"></i>${esc(LABEL[id] || id)}</td>
      <td>${pct(m.accuracy)}<div class="bar"><i style="width:${pct(m.accuracy)}"></i></div></td>
      <td>${m.relevance.toFixed(1)}</td><td>${m.coherence.toFixed(1)}</td>
      <td>${Math.round(m.avg_tokens)}</td><td>${pct(m.format_valid)}</td>
      <td><b>${m.composite != null ? m.composite.toFixed(2) : "..."}</b></td></tr>`).join("");
  $("#board").innerHTML = `<table><tr><th>#</th><th>Variant</th><th>Accuracy</th><th>Rel /5</th><th>Coh /5</th><th>Tokens</th><th>Valid</th><th>Score</th></tr>${rows}</table>`;
  $("#winner-badge").textContent = S.winner ? "WINNER: " + (LABEL[S.winner] || S.winner) : "";
  if (!window.Chart || !$("#page-arena").classList.contains("active")) return;
  const fg = cssv("--foreground"), grid = cssv("--border"), dim = cssv("--muted-foreground");
  const datasets = Object.entries(S.metrics).map(([id, m]) => ({
    label: LABEL[id] || id, data: [m.accuracy, m.relevance / 5, m.coherence / 5, m.cost_score ?? 1],
    borderColor: colorOf(id), backgroundColor: colorOf(id) + "22", pointBackgroundColor: colorOf(id), borderWidth: 2,
    borderDash: id === "self_consistency" ? [6, 4] : [] }));
  if (charts.radar) { charts.radar.data.datasets = datasets; charts.radar.update(); return; }
  charts.radar = new Chart($("#radar"), {
    type: "radar",
    data: { labels: ["Accuracy", "Relevance", "Coherence", "Cost-efficiency"], datasets },
    options: { maintainAspectRatio: false,
      scales: { r: { min: 0, max: 1, ticks: { display: false }, grid: { color: grid }, angleLines: { color: grid }, pointLabels: { color: dim, font: { size: 12 } } } },
      plugins: { legend: { labels: { color: fg } } } } });
}

// ------------------------------------------------------------------ attack lab
function asrFor(phase, variant) {
  const rows = S.attacks.filter(a => a.phase === phase && a.variant === variant);
  return rows.length ? rows.filter(a => a.success).length / rows.length : null;
}
function gauge(v, label) {
  const C = 2 * Math.PI * 54, off = C * (1 - (v || 0));
  const col = v > 0.5 ? "var(--destructive)" : v > 0.2 ? "var(--warn)" : "var(--primary)";
  return `<div class="gauge"><svg viewBox="0 0 140 140"><circle cx="70" cy="70" r="54" fill="none" stroke-width="12" style="stroke:var(--muted)"/>
    <circle cx="70" cy="70" r="54" fill="none" stroke-width="12" stroke-linecap="round" stroke-dasharray="${C}" stroke-dashoffset="${off}"
    transform="rotate(-90 70 70)" style="stroke:${col};transition:all .6s"/></svg><b>${pct(v)}</b>${esc(label)}</div>`;
}
function renderLab() {
  if (!S.attacks.length) return;
  const b = asrFor("before", S.winner), a = asrFor("after", "hardened");
  $("#gauges").innerHTML = b == null ? '<p class="empty">Waiting for winner...</p>' :
    gauge(b, "BEFORE hardening") + (a != null ? '<span class="arrow">&rarr;</span>' + gauge(a, "AFTER hardening") : "");
  const cols = [...S.variants.map(v => v.id), "hardened"];
  let html = `<div class="heat" style="grid-template-columns:130px repeat(${cols.length},1fr)"><div></div>` +
    cols.map(c => `<div class="h">${esc(c === "hardened" ? "HARDENED" : (LABEL[c] || c))}</div>`).join("");
  for (const t of S.attackTypes) {
    html += `<div class="r">${esc(t)}</div>`;
    for (const c of cols) {
      const x = S.attacks.find(z => z.atype === t && z.variant === c && z.phase === (c === "hardened" ? "after" : "before"));
      html += x ? `<div class="cell ${x.success ? "hit" : "ok"}">${x.success ? "BREACHED" : "blocked"}</div>` : '<div class="cell na">-</div>';
    }
  }
  $("#heat").innerHTML = html + "</div>";
}
function appendFeed(a) {
  const feed = $("#feed");
  if (feed.querySelector(".empty")) feed.innerHTML = "";
  const d = document.createElement("div");
  d.className = "f " + (a.success ? "hit" : "ok");
  d.innerHTML = `<span class="tag">${a.success ? "BREACHED" : "BLOCKED"}</span>
    <span class="meta">${esc(a.phase)} &middot; ${esc(LABEL[a.variant] || a.variant)} &middot; ${esc(a.atype)}</span>
    <details><summary>payload &amp; model output</summary><pre>payload: ${esc(a.payload)}\n\noutput: ${esc(a.output)}</pre></details>`;
  feed.prepend(d);
}

// ------------------------------------------------------------------ hardening
function renderHardening() {
  if (!S.hardened) return;
  $("#defenses").innerHTML = S.hardened.defenses.map(d => `<div class="chip">+ ${esc(d)}</div>`).join("");
  $("#diff").innerHTML = S.hardened.diff.map(l =>
    `<div class="d ${l.op === "+" ? "add" : l.op === "-" ? "del" : ""}"><span class="g">${l.op === " " ? "" : l.op}</span>${esc(l.text) || "&nbsp;"}</div>`).join("");
  const w = S.metrics[S.winner], h = S.hardMetrics;
  const b = asrFor("before", S.winner), a = asrFor("after", "hardened");
  if (!w || !h) return;
  const dTok = Math.round(h.avg_tokens - w.avg_tokens);
  $("#tradeoff").innerHTML = `
    <div class="cmp-top"><div><div class="big" style="color:var(--destructive)">${pct(b)}</div><small>attack success BEFORE</small></div>
      <span class="arrow">&rarr;</span><div><div class="big" style="color:var(--strong)">${a != null ? pct(a) : "..."}</div><small>AFTER</small></div></div>
    <table class="cmp"><tr><th>Metric</th><th>Before</th><th>After</th></tr>
    <tr><td>Accuracy</td><td>${pct(w.accuracy)}</td><td>${pct(h.accuracy)}</td></tr>
    <tr><td>Avg tokens / call</td><td>${Math.round(w.avg_tokens)}</td><td>${Math.round(h.avg_tokens)} (${dTok >= 0 ? "+" : ""}${dTok})</td></tr>
    <tr><td>Composite quality</td><td>${w.composite.toFixed(2)}</td><td>${h.composite.toFixed(2)}</td></tr></table>
    <p class="hint">Defenses add tokens (cost) and can change accuracy. Showing this trade-off is part of responsible prompt engineering.</p>`;
}

// ------------------------------------------------------------------ library
async function loadLibrary() {
  const runs = await api("/api/runs");
  $("#library").innerHTML = runs.length ? runs.map(r => `
    <div class="lcard"><b>${esc(r.pack_name)}</b>${esc(r.created)} ${r.mock ? "&middot; SIMULATED" : ""}<br>
      winner: ${esc(LABEL[r.winner] || r.winner)}<br>attack success: ${pct(r.asr_before)} &rarr; ${pct(r.asr_after)}<br>
      <button class="btn" data-open="${esc(r.id)}">Open</button>
      <button class="btn" data-copy="${esc(r.id)}">Copy template</button>
      <a class="btn" href="/api/runs/${esc(r.id)}/report.md">Report .md</a></div>`).join("")
    : '<p class="empty">No saved runs yet. Press FORGE in the Studio.</p>';
  $$("[data-open]").forEach(b => b.onclick = async () => { loadResult(await api("/api/runs/" + b.dataset.open)); showPage("report"); });
  $$("[data-copy]").forEach(b => b.onclick = async () => {
    const r = await api("/api/runs/" + b.dataset.copy);
    await navigator.clipboard.writeText(r.hardened.template); b.textContent = "Copied!";
  });
}
function loadResult(r) {
  Object.assign(S, { result: r, variants: r.variants, metrics: r.metrics, winner: r.winner, attacks: r.attacks,
    attackTypes: r.attack_types, hardened: r.hardened, hardMetrics: r.hardened.metrics, mock: r.mock, runId: r.run_id, stage: 6,
    running: false, basis: r.accuracy_basis || null });
  if (charts.radar) { charts.radar.destroy(); delete charts.radar; }
  $("#feed").innerHTML = "";
  r.attacks.forEach(appendFeed);
  renderVariants();
}

// ------------------------------------------------------------------ report
function renderReport() {
  const r = S.result;
  if (!r) return;
  const w = r.metrics[r.winner], h = r.hardened.metrics;
  const labels = Object.fromEntries(r.variants.map(v => [v.id, v.label]));
  const board = Object.entries(r.metrics).sort((a, b) => b[1].composite - a[1].composite).map(([id, m], i) =>
    `<tr class="${id === r.winner ? "win" : ""}"><td>${i + 1}</td><td>${esc(labels[id])}</td><td>${pct(m.accuracy)}</td><td>${m.relevance.toFixed(1)}</td>
     <td>${m.coherence.toFixed(1)}</td><td>${Math.round(m.avg_tokens)}</td><td>${m.composite.toFixed(2)}</td></tr>`).join("");
  $("#report").innerHTML = `
    ${r.mock ? '<div class="note">SIMULATED RUN: this report came from the offline mock engine, not a real model. Add a Gemini API key for real results.</div>' : ""}
    ${(r.models_used || []).length > 1 ? `<div class="note">This run used several models (${esc(r.models_used.join(", "))}) because the primary one was overloaded, so styles were not all measured on the same model.</div>` : ""}
    <h2>PromptForge report &middot; ${esc(r.pack_name)}</h2>
    <p class="hint">Run ${esc(r.run_id)} &middot; ${esc(r.created)} &middot; model: ${esc(r.model)} &middot; tokens used: ${r.tokens_total}</p>
    <p><b>Task:</b> ${esc(r.task)}</p>
    <p><b>Context</b> <span class="pill ${r.context_edited ? "edited" : ""}">${r.context_edited ? "edited by user" : "default"}</span><br>
       <span class="mono" style="font-size:12px;color:var(--muted-foreground)">${esc(r.context || "")}</span></p>
    <p class="hint">${esc(r.accuracy_basis || "")}</p>
    <h3>1 &middot; Quality leaderboard</h3>
    <table><tr><th>#</th><th>Variant</th><th>Accuracy</th><th>Rel</th><th>Coh</th><th>Tokens</th><th>Score</th></tr>${board}</table>
    <h3>2 &middot; Security result</h3>
    <p>Winner <b>${esc(labels[r.winner])}</b>: attack success <b style="color:var(--destructive)">${pct(r.asr_before)}</b> before hardening &rarr; <b style="color:var(--strong)">${pct(r.asr_after)}</b> after.</p>
    <h3>3 &middot; Cost of security</h3>
    <p>Accuracy ${pct(w.accuracy)} &rarr; ${pct(h.accuracy)}; tokens per call ${Math.round(w.avg_tokens)} &rarr; ${Math.round(h.avg_tokens)}.</p>
    <h3>4 &middot; Final hardened template</h3><pre class="tpl">${esc(r.hardened.template)}</pre>
    <h3>5 &middot; Limitations</h3>
    <ul><li>Relevance/coherence use an LLM judge, which can favour longer or more confident answers.</li>
    <li>Small test sets give noisy accuracy; attack results are a sample, not proof of safety.</li>
    <li>Hardening makes attacks harder, not impossible.</li>
    ${r.kind === "code" ? "<li>Generated code runs in a restricted runner (import whitelist, timeout, stripped environment): defence in depth, not a perfect sandbox.</li>" : ""}</ul>
    <a class="btn" href="/api/runs/${esc(r.run_id)}/report.md">Download .md</a>
    <button class="btn" id="copytpl">Copy template</button>
    <button class="btn" onclick="window.print()">Print / save as PDF</button>`;
  $("#copytpl").onclick = async e => { await navigator.clipboard.writeText(r.hardened.template); e.target.textContent = "Copied!"; };
}

function renderAll() {
  renderStepper(); renderVariants(); renderArena(); renderLab(); renderHardening(); renderReport();
}

// ------------------------------------------------------------------ run + events
function handle(ev) {
  switch (ev.type) {
    case "start": S.mock = ev.mock; S.basis = ev.accuracy_basis; log(`Run started - ${ev.pack} - model: ${ev.model}${ev.mock ? " (SIMULATED)" : ""}`); break;
    case "stage": S.stage = ev.n; renderStepper(); break;
    case "log": log(ev.msg); break;
    case "variants": S.variants = ev.variants; renderVariants(); break;
    case "eval": S.metrics[ev.variant] = ev.metrics; renderArena(); flag("arena"); break;
    case "leaderboard": S.metrics = ev.metrics; S.winner = ev.winner; renderArena(); renderVariants(); break;
    case "attack": S.attacks.push(ev); appendFeed(ev); renderLab(); flag("lab"); break;
    case "hardened": S.hardened = { template: ev.template, diff: ev.diff, defenses: ev.defenses }; renderHardening(); flag("hardening"); break;
    case "reeval": S.hardMetrics = ev.metrics; renderHardening(); break;
    case "done": S.result = ev.result; S.stage = 6; log("Done. Report ready.", "ok"); renderAll(); flag("report"); break;
    case "error": log("ERROR: " + ev.msg, "err"); break;
  }
}

function updateForge() {
  const b = $("#forge");
  b.disabled = S.running;
  b.textContent = S.running ? "FORGING..." : "FORGE ▸";
  renderStepper();
}

async function startRun() {
  if (S.running) return;
  resetRun();
  const body = { domain: S.pack.id, mode: S.mode, task: $("#task").value.trim() || null,
    context: $("#context").value.trim() || null,
    self_consistency: $("#opt-sc").checked, n_tests: +$("#opt-n").value };
  try {
    const { run_id } = await api("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    S.runId = run_id; S.running = true; updateForge();
    const es = new EventSource("/api/stream/" + run_id);
    es.onmessage = e => {
      const ev = JSON.parse(e.data);
      handle(ev);
      if (ev.type === "done" || ev.type === "error") { es.close(); S.running = false; updateForge(); }
    };
    es.onerror = () => { es.close(); if (S.running) { log("Connection lost.", "err"); S.running = false; updateForge(); } };
  } catch (e) { log("Could not start: " + e.message, "err"); S.running = false; updateForge(); }
}

async function init() {
  S.health = await api("/api/health");
  S.packs = await api("/api/packs");
  const b = $("#badge");
  b.className = "badge " + S.health.mode;
  b.textContent = S.health.mode === "live" ? "LIVE · " + S.health.model : "MOCK MODE · simulated";
  $$("#nav button").forEach(btn => btn.onclick = () => showPage(btn.dataset.page));
  $("#forge").onclick = startRun;
  $("#theme").onclick = () => setTheme(document.documentElement.classList.contains("dark") ? "light" : "dark");
  $("#theme").textContent = document.documentElement.classList.contains("dark") ? "Light" : "Dark";
  $("#mode").onchange = e => { S.mode = e.target.value; applySpec(); };
  $("#context").oninput = ctxState;
  $("#ctxreset").onclick = () => { $("#context").value = spec().context; ctxState(); };
  selectPack(S.packs[0].id);
  resetRun();
}
init().catch(e => { document.body.insertAdjacentHTML("afterbegin", `<div class="note">Backend not reachable: ${esc(e.message)}</div>`); });
