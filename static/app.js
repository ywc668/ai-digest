/* The AI Digest — dashboard logic (no build step, no framework) */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const CATEGORIES = [
  ["", "All"], ["arxiv", "Papers"], ["blogs", "Blogs"], ["labs", "Labs"],
  ["github", "Releases"], ["newsletters", "Newsletters"], ["podcasts", "Podcasts"],
];

const state = {
  tab: "today",
  search: "",
  category: "",
  sort: "score",
  days: "1",
  minScore: 5,
  starredOnly: false,
  showRejects: false,
  offset: 0,
  limit: 40,
  total: 0,
  pollTimer: null,
  wasRunning: false,
};

// ── helpers ──────────────────────────────────

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return resp.json();
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

function fmtNum(n) {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

// ── masthead ─────────────────────────────────

function renderMastheadDate() {
  const now = new Date();
  $("#masthead-date").textContent = now.toLocaleDateString(undefined, {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
  });
}

async function refreshStats() {
  try {
    const [stats, config] = await Promise.all([api("/api/stats"), api("/api/config")]);
    const s = config.parsed.scoring || {};
    const backend = s.backend || "anthropic";
    const model = backend === "ollama"
      ? (s.ollama || {}).model || "?"
      : (s.anthropic || {}).model || "?";
    $("#backend-chip").textContent = `${backend} · ${model}`;
    $("#cost-chip").textContent = `$${(stats.total_est_cost_usd || 0).toFixed(2)} lifetime`;

    const last = stats.last_run;
    const stages = last && last.stage_counts ? JSON.parse(last.stage_counts || "{}") : {};
    $("#stats-strip").innerHTML = `
      <div class="stat"><b>${fmtNum(last ? last.fetched : 0)}</b><span>fetched</span></div>
      <div class="stat"><b>${fmtNum(last ? last.new : 0)}</b><span>new</span></div>
      <div class="stat"><b>${fmtNum(last ? last.scored : 0)}</b><span>scored</span></div>
      <div class="stat"><b>${fmtNum(stages.stage1_filtered || 0)}</b><span>title-cut</span></div>
      <div class="stat"><b>${fmtNum(last ? last.sent : 0)}</b><span>in digest</span></div>
      <div class="stat"><b>${fmtNum((last ? last.input_tokens : 0) + (last ? last.output_tokens : 0))}</b><span>tokens (last run)</span></div>
      <div class="stat"><b>${fmtNum(stats.items)}</b><span>archived</span></div>`;
  } catch (e) {
    console.warn("stats failed", e);
  }
}

// ── feed ─────────────────────────────────────

function feedQuery() {
  const p = new URLSearchParams();
  if (state.search) p.set("search", state.search);
  if (state.category) p.set("category", state.category);
  if (state.minScore > 0 && !state.showRejects) p.set("min_score", state.minScore);
  if (state.starredOnly) p.set("starred", "true");
  if (state.tab === "today" && state.days) p.set("days", state.days);
  if (state.tab === "archive" && state.days) p.set("days", state.days);
  p.set("sort", state.sort);
  p.set("limit", state.limit);
  p.set("offset", state.offset);
  return p.toString();
}

function scoreClass(score) {
  if (score >= 7) return "s-high";
  if (score >= 5) return "s-mid";
  return "";
}

function renderItem(item, idx) {
  const score = item.score == null ? "·" : Math.round(item.score);
  const stageBadge =
    item.score_stage === "stage3" ? '<span class="badge deep">deep dive</span>' :
    item.score_stage === "stage1_filtered" ? '<span class="badge reject">title-cut</span>' :
    item.score_stage === "skipped" ? '<span class="badge reject">unscored</span>' : "";
  const authors = (item.authors || []).slice(0, 3).join(", ");
  const pub = item.published ? new Date(item.published).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
  const hasSummary = item.summary && item.summary.length > 10;

  return `<article class="item" style="--i:${idx}" data-id="${esc(item.id)}">
    <div class="item-score ${scoreClass(item.score || 0)}">${score}<small>${esc(item.score_stage || "")}</small></div>
    <div>
      <h3 class="item-title"><a href="${esc(item.url)}" target="_blank" rel="noopener">${esc(item.title)}</a></h3>
      <div class="item-meta">
        <span class="src">${esc(item.source_name)}</span>
        ${authors ? " · " + esc(authors) : ""}${pub ? " · " + pub : ""}${stageBadge}
      </div>
      ${item.score_reason ? `<p class="item-reason">${esc(item.score_reason)}</p>` : ""}
      ${hasSummary ? `<p class="item-summary">${esc(item.summary)}</p>
      <button class="item-expand" data-act="expand">read summary +</button>` : ""}
    </div>
    <div class="item-actions">
      <button class="act ${item.starred ? "starred" : ""}" data-act="star" title="Star">${item.starred ? "★" : "☆"}</button>
      <button class="act" data-act="hide" title="Hide">✕</button>
    </div>
  </article>`;
}

async function loadFeed(append = false) {
  if (!append) state.offset = 0;
  const data = await api(`/api/items?${feedQuery()}`);
  state.total = data.total;
  const html = data.items.map(renderItem).join("");
  const feed = $("#feed");
  if (append) feed.insertAdjacentHTML("beforeend", html);
  else feed.innerHTML = html || emptyState();
  const shown = feed.querySelectorAll(".item").length;
  $("#load-more").hidden = shown >= data.total;
  $("#feed-count").textContent = data.total ? `${shown} of ${data.total} items` : "";
}

function emptyState() {
  return `<div class="empty">
    <div class="empty-mark">∅</div>
    <p>Nothing here yet. Hit <b>Run now</b> to fetch &amp; score today's feeds,<br>
    or widen the window / lower the minimum score.</p>
  </div>`;
}

// ── feed events (delegated) ──────────────────

$("#feed").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const itemEl = btn.closest(".item");
  const id = itemEl.dataset.id;
  const act = btn.dataset.act;
  if (act === "expand") {
    itemEl.classList.toggle("open");
    btn.textContent = itemEl.classList.contains("open") ? "collapse −" : "read summary +";
  } else if (act === "star") {
    const on = !btn.classList.contains("starred");
    await api(`/api/items/${id}/flag`, { method: "POST", body: JSON.stringify({ field: "starred", value: on }) });
    btn.classList.toggle("starred", on);
    btn.textContent = on ? "★" : "☆";
  } else if (act === "hide") {
    await api(`/api/items/${id}/flag`, { method: "POST", body: JSON.stringify({ field: "hidden", value: true }) });
    itemEl.style.opacity = "0.25";
    setTimeout(() => itemEl.remove(), 250);
    toast("Hidden — it won't show again");
  }
});

// ── controls ─────────────────────────────────

function renderChips() {
  $("#category-chips").innerHTML = CATEGORIES.map(
    ([val, label]) =>
      `<button class="chip ${state.category === val ? "active" : ""}" data-cat="${val}">${label}</button>`
  ).join("");
}

$("#category-chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  state.category = chip.dataset.cat;
  renderChips();
  loadFeed();
});

let searchTimer = null;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = e.target.value.trim();
    loadFeed();
  }, 280);
});

$("#sort").addEventListener("change", (e) => { state.sort = e.target.value; loadFeed(); });
$("#days").addEventListener("change", (e) => { state.days = e.target.value; loadFeed(); });
$("#min-score").addEventListener("input", (e) => {
  state.minScore = Number(e.target.value);
  $("#min-score-val").textContent = e.target.value;
});
$("#min-score").addEventListener("change", () => loadFeed());
$("#toggle-starred").addEventListener("click", (e) => {
  state.starredOnly = !state.starredOnly;
  e.target.classList.toggle("active", state.starredOnly);
  loadFeed();
});
$("#toggle-filtered").addEventListener("click", (e) => {
  state.showRejects = !state.showRejects;
  e.target.classList.toggle("active", state.showRejects);
  loadFeed();
});
$("#load-more").addEventListener("click", () => {
  state.offset += state.limit;
  loadFeed(true);
});

// ── tabs ─────────────────────────────────────

$("#tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  state.tab = tab.dataset.tab;
  $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  $("#panel-feed").hidden = !(state.tab === "today" || state.tab === "archive");
  $("#panel-runs").hidden = state.tab !== "runs";
  $("#panel-settings").hidden = state.tab !== "settings";
  if (state.tab === "today") { state.days = "1"; $("#days").value = "1"; loadFeed(); }
  if (state.tab === "archive") { state.days = ""; $("#days").value = ""; loadFeed(); }
  if (state.tab === "runs") loadRuns();
  if (state.tab === "settings") loadSettings();
});

// ── runs & tokens ────────────────────────────

async function loadRuns() {
  const [runs, usage] = await Promise.all([api("/api/runs"), api("/api/usage")]);

  const totalIn = usage.reduce((a, u) => a + (u.input_tokens || 0), 0);
  const totalOut = usage.reduce((a, u) => a + (u.output_tokens || 0), 0);
  const cards = usage.map((u) => `
    <div class="usage-card">
      <h3>${esc(u.stage)} · ${esc(u.backend)}</h3>
      <b>${fmtNum((u.input_tokens || 0) + (u.output_tokens || 0))}</b>
      <span>${fmtNum(u.calls)} calls · ${fmtNum(u.input_tokens)} in / ${fmtNum(u.output_tokens)} out · ~${Math.round(u.avg_ms || 0)}ms</span>
    </div>`).join("");
  $("#usage-cards").innerHTML = (cards || "<p class='hint'>No LLM calls recorded yet.</p>") + (usage.length ? `
    <div class="usage-card">
      <h3>all stages</h3><b>${fmtNum(totalIn + totalOut)}</b>
      <span>${fmtNum(totalIn)} in / ${fmtNum(totalOut)} out, lifetime</span>
    </div>` : "");

  $("#runs-table tbody").innerHTML = runs.map((r) => {
    const st = r.stage_counts || {};
    return `<tr>
      <td>${r.id}</td><td>${fmtWhen(r.started)}</td><td>${esc(r.backend || "—")}</td>
      <td>${fmtNum(r.fetched)}</td><td>${fmtNum(r.new)}</td><td>${fmtNum(r.scored)}</td>
      <td>${fmtNum(st.stage1_filtered || 0)}</td><td>${fmtNum(st.stage2 || 0)}</td>
      <td>${fmtNum(st.stage3 || 0)}</td><td>${fmtNum(r.sent)}</td>
      <td>${fmtNum(r.input_tokens)} / ${fmtNum(r.output_tokens)}</td>
      <td>${r.est_cost_usd ? "$" + r.est_cost_usd.toFixed(3) : "free"}</td>
      <td class="${r.status === "ok" ? "ok" : "error"}">${esc(r.status || "?")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="13">No runs yet.</td></tr>`;
}

// ── pipeline runner ──────────────────────────

$("#run-now").addEventListener("click", async () => {
  try {
    await api("/api/run", { method: "POST", body: JSON.stringify({ no_email: false }) });
    toast("Pipeline started");
    startPolling();
  } catch (e) {
    toast(e.message, true);
  }
});

function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(pollStatus, 2000);
  pollStatus();
}

async function pollStatus() {
  let s;
  try { s = await api("/api/run/status"); } catch { return; }
  const btn = $("#run-now");
  const bar = $("#run-progress-bar");
  const line = $("#run-status-line");

  if (s.running) {
    state.wasRunning = true;
    btn.disabled = true;
    btn.querySelector(".run-button-label").textContent = "Running…";
    const pct = s.progress ? Math.round((s.progress.done / s.progress.total) * 100) : 4;
    bar.style.width = pct + "%";
    line.hidden = false;
    const lastLine = (s.log_tail || []).filter((l) => l.trim()).pop() || "";
    line.textContent = (s.progress ? `scoring ${s.progress.done}/${s.progress.total} — ` : "") + lastLine.slice(-110);
  } else {
    btn.disabled = false;
    btn.querySelector(".run-button-label").textContent = "Run now";
    bar.style.width = "0%";
    if (state.wasRunning) {
      state.wasRunning = false;
      line.textContent = s.exit_code === 0 ? "Run finished ✓" : `Run exited with code ${s.exit_code}`;
      toast(s.exit_code === 0 ? "Run complete — feed refreshed" : "Run failed — check the log in Runs", s.exit_code !== 0);
      refreshStats();
      loadFeed();
      setTimeout(() => { line.hidden = true; }, 8000);
    }
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  $("#run-log").textContent = (s.log_tail || []).join("\n") || "No run this session.";
  const log = $("#run-log");
  log.scrollTop = log.scrollHeight;
}

// ── settings ─────────────────────────────────

async function loadSettings() {
  const cfg = await api("/api/config");
  const p = cfg.parsed;
  const s = p.scoring || {};
  $("#cfg-backend").value = s.backend || "anthropic";
  $("#cfg-ollama-model").value = (s.ollama || {}).model || "";
  $("#cfg-s1").value = s.stage1_threshold ?? 3;
  $("#cfg-s3").value = s.stage3_threshold ?? 7;
  $("#cfg-min-score").value = s.min_score ?? 5;
  $("#cfg-max-items").value = s.max_items ?? 15;
  $("#cfg-max-score").value = s.max_items_to_score ?? 150;
  $("#cfg-email").checked = !!(p.email || {}).enabled;
  $("#cfg-profile").value = p.interest_profile || "";
  $("#cfg-raw").value = cfg.yaml;
}

$("#quick-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const updates = {
    "scoring.backend": $("#cfg-backend").value,
    "scoring.ollama.model": $("#cfg-ollama-model").value,
    "scoring.stage1_threshold": Number($("#cfg-s1").value),
    "scoring.stage3_threshold": Number($("#cfg-s3").value),
    "scoring.min_score": Number($("#cfg-min-score").value),
    "scoring.max_items": Number($("#cfg-max-items").value),
    "scoring.max_items_to_score": Number($("#cfg-max-score").value),
    "email.enabled": $("#cfg-email").checked,
    "interest_profile": $("#cfg-profile").value,
  };
  try {
    await api("/api/config", { method: "PATCH", body: JSON.stringify({ updates }) });
    toast("Settings saved");
    loadSettings();
    refreshStats();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#save-raw").addEventListener("click", async () => {
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify({ yaml: $("#cfg-raw").value }) });
    toast("config.yaml saved");
    loadSettings();
    refreshStats();
  } catch (err) {
    toast(err.message, true);
  }
});

// ── boot ─────────────────────────────────────

renderMastheadDate();
renderChips();
refreshStats();
loadFeed();
pollStatus(); // pick up an in-flight run if the page was reloaded mid-run
