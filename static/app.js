/* The AI Digest — dashboard logic (no build step, no framework) */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const CATEGORIES = [
  ["", "All"], ["arxiv", "Papers"], ["blogs", "Blogs"], ["labs", "Labs"],
  ["github", "Releases"], ["newsletters", "Newsletters"], ["community", "Community"],
  ["podcasts", "Podcasts"],
];

const state = {
  tab: "today",
  search: "",
  category: "",
  topic: "",
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
  if (state.topic) p.set("topic", state.topic);
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

function regexEscape(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/* Escape the summary, then wrap each highlight phrase in <mark>.
   The (<tag>)|(phrase) alternation prevents matching inside HTML tags. */
function renderSummary(summary, highlights) {
  let html = esc(summary);
  const phrases = [...(highlights || [])].sort((a, b) => b.length - a.length);
  for (const h of phrases) {
    const eh = esc(h.trim());
    if (eh.length < 2) continue;
    html = html.replace(
      new RegExp(`(<[^>]+>)|(${regexEscape(eh)})`, "gi"),
      (m, tag, txt) => (tag ? tag : `<mark>${txt}</mark>`)
    );
  }
  return html;
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

/* Minimal markdown renderer — enough for LLM-generated reports/abstracts. */
function renderMd(md) {
  const lines = esc(md || "").split("\n");
  let html = "", inList = false;
  const inline = (s) => s
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\*([^*]+)\*/g, "<i>$1</i>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  for (const raw of lines) {
    const line = raw.trimEnd();
    const bullet = line.match(/^\s*[-*]\s+(.*)/);
    if (bullet) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(bullet[1])}</li>`;
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) html += `<h${h[1].length + 2}>${inline(h[2])}</h${h[1].length + 2}>`;
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  if (inList) html += "</ul>";
  return html;
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
  const host = hostOf(item.url);
  const highlights = item.highlights || [];

  return `<article class="item" style="--i:${idx}" data-id="${esc(item.id)}">
    <div class="item-score ${scoreClass(item.score || 0)}">${score}<small>${esc(item.score_stage || "")}</small></div>
    <div>
      <h3 class="item-title"><a href="${esc(item.url)}" target="_blank" rel="noopener">${esc(item.title)}</a></h3>
      <div class="item-meta">
        <span class="src">${esc(item.source_name)}</span>
        ${authors ? " · " + esc(authors) : ""}${pub ? " · " + pub : ""}
        ${host ? ` · <a class="link-out" href="${esc(item.url)}" target="_blank" rel="noopener" title="${esc(item.url)}">${esc(host)} ↗</a>` : ""}${stageBadge}
      </div>
      ${item.score_reason ? `<p class="item-reason">${esc(item.score_reason)}</p>` : ""}
      ${item.topic ? `<span class="topic-tag">${esc(item.topic)}</span>` : ""}
      ${highlights.length ? `<div class="item-highlights">${highlights.map((h) => `<span class="hl">${esc(h)}</span>`).join("")}</div>` : ""}
      ${hasSummary ? `<p class="item-summary">${renderSummary(item.summary, highlights)}</p>` : ""}
      <div class="item-buttons">
        ${hasSummary ? `<button class="item-expand" data-act="expand">read summary +</button>` : ""}
        <button class="item-expand" data-act="dig">${item.deep_dive ? "deep dive ▸" : "dig deeper ⛏"}</button>
      </div>
      <div class="dig-panel" hidden></div>
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

function renderDig(d) {
  return `
    <h4>Analysis ${d.used_full_article ? '<span class="badge deep">full article</span>' : '<span class="badge">from summary</span>'}</h4>
    <p>${esc(d.analysis || "")}</p>
    <h4>Implications</h4><p>${esc(d.implications || "")}</p>
    ${d.try_it ? `<h4>Try it</h4><p>${esc(d.try_it)}</p>` : ""}
    ${(d.questions || []).length ? `<h4>Dig further</h4><ul>${d.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>` : ""}`;
}

$("#feed").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const itemEl = btn.closest(".item");
  const id = itemEl.dataset.id;
  const act = btn.dataset.act;
  if (act === "expand") {
    itemEl.classList.toggle("open");
    btn.textContent = itemEl.classList.contains("open") ? "collapse −" : "read summary +";
  } else if (act === "dig") {
    const panel = itemEl.querySelector(".dig-panel");
    if (!panel.hidden) { panel.hidden = true; return; }
    if (panel.dataset.loaded) { panel.hidden = false; return; }
    btn.disabled = true;
    btn.textContent = "digging… (fetching article + analyzing)";
    try {
      const d = await api(`/api/items/${id}/dig`, { method: "POST" });
      panel.innerHTML = renderDig(d);
      panel.dataset.loaded = "1";
      panel.hidden = false;
      btn.textContent = "deep dive ▸";
    } catch (err) {
      toast(err.message, true);
      btn.textContent = "dig deeper ⛏";
    } finally {
      btn.disabled = false;
    }
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

let topicList = [];
async function renderTopicChips() {
  if (!topicList.length) {
    try { topicList = await api("/api/topics"); } catch { return; }
  }
  $("#topic-chips").innerHTML =
    `<button class="chip ${state.topic === "" ? "active" : ""}" data-topic="">All topics</button>` +
    topicList.filter((t) => t.count > 0 || true).map((t) =>
      `<button class="chip ${state.topic === t.topic ? "active" : ""}" data-topic="${esc(t.topic)}">${esc(t.topic)}${t.count ? ` <small>${t.count}</small>` : ""}</button>`
    ).join("");
}

$("#topic-chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  state.topic = chip.dataset.topic;
  renderTopicChips();
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
  $("#panel-stories").hidden = state.tab !== "stories";
  $("#panel-map").hidden = state.tab !== "map";
  $("#panel-reports").hidden = state.tab !== "reports";
  $("#panel-runs").hidden = state.tab !== "runs";
  $("#panel-settings").hidden = state.tab !== "settings";
  if (state.tab === "today") { state.days = "1"; $("#days").value = "1"; loadFeed(); }
  if (state.tab === "archive") { state.days = ""; $("#days").value = ""; loadFeed(); }
  if (state.tab === "stories") loadStories();
  if (state.tab === "map") loadMap();
  if (state.tab === "reports") loadReports();
  if (state.tab === "runs") loadRuns();
  if (state.tab === "settings") loadSettings();
});

// ── stories ──────────────────────────────────

let currentStoryId = null;
let storyPollTimer = null;

async function loadStories(selectId = null) {
  const stories = await api("/api/stories");
  $("#story-list").innerHTML = stories.map((s) => `
    <div class="story-card ${s.id === currentStoryId ? "sel" : ""}" data-id="${s.id}">
      <div class="story-card-title">${esc(s.title || s.prompt.slice(0, 50))}</div>
      <div class="story-card-meta">
        ${s.status === "building" ? "⏳ building…" : s.status === "error" ? "⚠ failed" : `${s.item_count} items`}
        ${s.updated ? " · " + fmtWhen(s.updated) : ""}
      </div>
    </div>`).join("") || `<p class="hint">No stories yet.</p>`;
  if (selectId) viewStory(selectId);
  else if (currentStoryId) viewStory(currentStoryId, false);
}

async function viewStory(id, rerenderList = true) {
  currentStoryId = id;
  if (rerenderList) $$("#story-list .story-card").forEach((c) => c.classList.toggle("sel", Number(c.dataset.id) === id));
  const s = await api(`/api/stories/${id}`);
  clearInterval(storyPollTimer); storyPollTimer = null;
  if (s.status === "building") {
    $("#story-view").innerHTML = `<p class="hint">⏳ Mining the archive and building the timeline… (~30–90s, local model)</p>`;
    storyPollTimer = setInterval(() => viewStory(id, false), 4000);
    return;
  }
  if (s.status === "error") {
    $("#story-view").innerHTML = `<p class="hint">⚠ Build failed: ${esc(s.error || "unknown")}</p>
      <button class="ctl-toggle" id="story-refresh">retry</button>`;
    $("#story-refresh").onclick = () => refreshStory(id);
    return;
  }
  $("#story-view").innerHTML = `
    <div class="story-head">
      <h3 class="story-title">${esc(s.title)}</h3>
      <div>
        <button class="ctl-toggle" id="story-refresh" title="Re-scan the archive">↻ refresh</button>
        <button class="ctl-toggle" id="story-delete">✕ delete</button>
      </div>
    </div>
    <p class="hint">“${esc(s.prompt)}”</p>
    <div class="story-abstract">${renderMd(s.abstract)}</div>
    <h4 class="section-head">Timeline — ${s.items.length} items</h4>
    <div class="timeline">
      ${s.items.map((i) => `
        <div class="tl-item">
          <div class="tl-date">${(i.published || i.first_seen || "").slice(0, 10)}</div>
          <div class="tl-body">
            <a href="${esc(i.url)}" target="_blank" rel="noopener" class="tl-title">${esc(i.title)}</a>
            <div class="tl-meta">${esc(i.source_name)}${i.score ? ` · scored ${Math.round(i.score)}` : ""}${i.story_relevance ? ` · relevance ${Math.round(i.story_relevance)}` : ""}</div>
            ${i.story_note ? `<div class="tl-note">${esc(i.story_note)}</div>` : ""}
          </div>
        </div>`).join("") || "<p class='hint'>No archive items match yet — the story will fill in as new items arrive (hit refresh after runs).</p>"}
    </div>`;
  $("#story-refresh").onclick = () => refreshStory(id);
  $("#story-delete").onclick = async () => {
    await api(`/api/stories/${id}`, { method: "DELETE" });
    currentStoryId = null;
    $("#story-view").innerHTML = `<p class="hint">Select a story.</p>`;
    loadStories();
    toast("Story deleted");
  };
}

async function refreshStory(id) {
  await api(`/api/stories/${id}/refresh`, { method: "POST" });
  viewStory(id, false);
}

$("#story-list").addEventListener("click", (e) => {
  const card = e.target.closest(".story-card");
  if (card) viewStory(Number(card.dataset.id));
});

$("#story-create-btn").addEventListener("click", async () => {
  const prompt = $("#story-prompt").value.trim();
  if (!prompt) { toast("Describe the story first", true); return; }
  const res = await api("/api/stories", { method: "POST", body: JSON.stringify({ prompt }) });
  $("#story-prompt").value = "";
  toast("Story building started");
  loadStories(res.id);
});

// ── knowledge map ────────────────────────────

const TOPIC_COLORS = {
  "llm-inference": "#b3380c", "training-finetuning": "#92580a",
  "agents-tooling": "#3f6212", "model-releases": "#185fa5",
  "ml-infrastructure": "#534ab7", "ai-safety": "#9d174d",
  "rag-retrieval": "#0e7490", "hardware": "#57534e",
  "industry-business": "#7c2d12", "other": "#a39a85",
};
let cyInstance = null;

async function loadMap() {
  const minCount = Number($("#map-min-count").value || 2);
  const g = await api(`/api/graph?min_count=${minCount}`);
  $("#map-stats").textContent = `${g.nodes.length} entities · ${g.edges.length} links`;
  if (!g.nodes.length) {
    $("#map-container").innerHTML =
      "<p class='hint' style='padding:30px'>No entities yet — they're extracted during scoring; run the pipeline or lower “min items”.</p>";
    return;
  }
  const maxEng = Math.max(1, ...g.nodes.map((n) => n.engagement));
  const elements = [
    ...g.nodes.map((n) => ({
      data: { id: n.id, label: n.label, count: n.count, topic: n.topic, engagement: n.engagement },
    })),
    ...g.edges.map((e, i) => ({ data: { id: `e${i}`, source: e.source, target: e.target, weight: e.weight } })),
  ];
  if (cyInstance) cyInstance.destroy();
  cyInstance = cytoscape({
    container: $("#map-container"),
    elements,
    style: [
      {
        selector: "node",
        style: {
          "background-color": (el) => TOPIC_COLORS[el.data("topic")] || TOPIC_COLORS.other,
          "background-opacity": (el) => 0.25 + 0.75 * Math.min(1, el.data("engagement") / maxEng),
          width: (el) => 14 + Math.sqrt(el.data("count")) * 9,
          height: (el) => 14 + Math.sqrt(el.data("count")) * 9,
          label: "data(label)",
          "font-family": "IBM Plex Mono, monospace",
          "font-size": "9px",
          color: "#211e18",
          "text-valign": "bottom",
          "text-margin-y": 4,
          "border-width": (el) => (el.data("engagement") > 0 ? 2 : 0.5),
          "border-color": (el) => TOPIC_COLORS[el.data("topic")] || TOPIC_COLORS.other,
        },
      },
      {
        selector: "edge",
        style: {
          width: (el) => Math.min(4, 0.6 * el.data("weight")),
          "line-color": "#d9d2bf",
          "curve-style": "haystack",
        },
      },
    ],
    layout: { name: "cose", animate: false, nodeRepulsion: 9000, idealEdgeLength: 70 },
    wheelSensitivity: 0.2,
  });
  cyInstance.on("tap", "node", (evt) => {
    const label = evt.target.data("label");
    // jump to the archive filtered by this entity
    state.tab = "archive"; state.search = label; state.days = "";
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === "archive"));
    $("#panel-feed").hidden = false; $("#panel-map").hidden = true;
    $("#search").value = label; $("#days").value = "";
    loadFeed();
  });
  // legend
  const topicsInUse = [...new Set(g.nodes.map((n) => n.topic))];
  $("#map-legend").innerHTML = topicsInUse.map((t) =>
    `<span class="legend-item"><span class="legend-dot" style="background:${TOPIC_COLORS[t] || TOPIC_COLORS.other}"></span>${esc(t)}</span>`
  ).join("");
}

$("#map-min-count").addEventListener("change", loadMap);

// ── reports ──────────────────────────────────

let currentReportId = null;
let reportPollTimer = null;

async function loadReports(selectId = null) {
  const reports = await api("/api/reports");
  $("#report-list").innerHTML = reports.map((r) => `
    <div class="story-card ${r.id === currentReportId ? "sel" : ""}" data-id="${r.id}">
      <div class="story-card-title">${esc(r.kind)} — ${(r.period_end || r.created).slice(0, 10)}</div>
      <div class="story-card-meta">${r.status === "building" ? "⏳ building…" : r.status === "error" ? "⚠ failed" : fmtWhen(r.created)}</div>
    </div>`).join("") || `<p class="hint">No reports yet — generate one above.</p>`;
  if (selectId) viewReport(selectId);
}

async function viewReport(id) {
  currentReportId = id;
  $$("#report-list .story-card").forEach((c) => c.classList.toggle("sel", Number(c.dataset.id) === id));
  const r = await api(`/api/reports/${id}`);
  clearInterval(reportPollTimer); reportPollTimer = null;
  if (r.status === "building") {
    $("#report-view").innerHTML = `<p class="hint">⏳ Synthesizing report… (~20–60s)</p>`;
    reportPollTimer = setInterval(() => { viewReport(id); loadReports(); }, 4000);
    return;
  }
  $("#report-view").innerHTML = r.status === "error"
    ? `<p class="hint">⚠ ${esc(r.error || "failed")}</p>`
    : `<div class="story-abstract">${renderMd(r.content_md)}</div>`;
}

$("#report-list").addEventListener("click", (e) => {
  const card = e.target.closest(".story-card");
  if (card) viewReport(Number(card.dataset.id));
});

$("#panel-reports .report-actions").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-report-kind]");
  if (!btn) return;
  const res = await api("/api/reports", { method: "POST", body: JSON.stringify({ kind: btn.dataset.reportKind }) });
  toast(`${btn.dataset.reportKind} report building`);
  loadReports(res.id);
});

// ── profile assistant ────────────────────────

$("#assist-edit-btn").addEventListener("click", async () => {
  const instruction = $("#assist-instruction").value.trim();
  if (!instruction) { toast("Type how you want the profile changed", true); return; }
  const btn = $("#assist-edit-btn");
  btn.disabled = true; btn.textContent = "✨ thinking…";
  try {
    const res = await api("/api/profile/assist", {
      method: "POST",
      body: JSON.stringify({ mode: "edit", instruction }),
    });
    $("#cfg-profile").value = res.profile || $("#cfg-profile").value;
    toast(res.changes || "Profile rewritten — review and Save");
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "✨ rewrite";
  }
});

$("#assist-feedback-btn").addEventListener("click", async () => {
  const btn = $("#assist-feedback-btn");
  btn.disabled = true; btn.textContent = "✨ analyzing your behavior…";
  try {
    const res = await api("/api/profile/feedback-suggest", { method: "POST" });
    $("#cfg-profile").value = res.profile || $("#cfg-profile").value;
    const obs = $("#feedback-observations");
    obs.innerHTML = `<b>Observed</b> (from ${res.signal.starred}★ / ${res.signal.hidden}✕):
      ${esc(res.observations || "")}<br><b>Changed:</b> ${esc(res.changes || "")}`;
    obs.hidden = false;
    toast("Amendments proposed — review and Save");
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "✨ learn from my feedback";
  }
});

$("#assist-interview-btn").addEventListener("click", async () => {
  const box = $("#interview-box");
  const btn = $("#assist-interview-btn");
  btn.disabled = true; btn.textContent = "✨ preparing questions…";
  try {
    const res = await api("/api/profile/assist", {
      method: "POST", body: JSON.stringify({ mode: "questions" }),
    });
    box.innerHTML = (res.questions || []).map((q, i) => `
      <label class="field">${esc(q)}<input type="text" class="interview-answer" data-q="${esc(q)}"></label>
    `).join("") + `<button type="button" id="interview-draft-btn" class="ctl-toggle">✨ draft profile from answers</button>`;
    box.hidden = false;
    $("#interview-draft-btn").onclick = async () => {
      const answers = $$(".interview-answer")
        .map((el) => ({ q: el.dataset.q, a: el.value.trim() }))
        .filter((a) => a.a);
      if (!answers.length) { toast("Answer at least one question", true); return; }
      const dbtn = $("#interview-draft-btn");
      dbtn.disabled = true; dbtn.textContent = "✨ drafting…";
      try {
        const res2 = await api("/api/profile/assist", {
          method: "POST", body: JSON.stringify({ mode: "draft", answers }),
        });
        $("#cfg-profile").value = res2.profile || $("#cfg-profile").value;
        box.hidden = true;
        toast("Draft ready — review and Save");
      } catch (err) {
        toast(err.message, true);
      } finally {
        dbtn.disabled = false; dbtn.textContent = "✨ draft profile from answers";
      }
    };
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "✨ set up via interview";
  }
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

// ── sources editor ───────────────────────────

/* model: {category: [{name, url, enabled, custom}]} — catalog entries first,
   then any config feeds not in the catalog (custom). */
let sourcesModel = {};

async function loadSources() {
  const data = await api("/api/catalog");
  const enabledUrls = new Set(Object.keys(data.enabled));
  const catalogUrls = new Set();
  sourcesModel = {};
  for (const [cat, entries] of Object.entries(data.catalog || {})) {
    sourcesModel[cat] = entries.map((e) => {
      catalogUrls.add(e.url);
      return { name: e.name, url: e.url, enabled: enabledUrls.has(e.url), custom: false };
    });
  }
  const cfg = await api("/api/config");
  for (const [url, name] of Object.entries(data.enabled)) {
    if (catalogUrls.has(url)) continue;
    // find the category this custom feed lives in
    for (const [cat, feeds] of Object.entries(cfg.parsed.feeds || {})) {
      if (feeds.some((f) => f.url === url)) {
        (sourcesModel[cat] = sourcesModel[cat] || []).push({ name, url, enabled: true, custom: true });
        break;
      }
    }
  }
  renderSources();
}

function renderSources() {
  $("#sources-groups").innerHTML = Object.entries(sourcesModel)
    .filter(([, entries]) => entries.length)
    .map(([cat, entries]) => `
    <div class="source-group">
      <h3 class="source-group-head">${esc(cat)}</h3>
      ${entries.map((e, i) => `
        <label class="source-row ${e.enabled ? "on" : ""}">
          <input type="checkbox" data-cat="${esc(cat)}" data-i="${i}" ${e.enabled ? "checked" : ""}>
          <span class="source-name">${esc(e.name)}${e.custom ? ' <span class="badge">custom</span>' : ""}</span>
          <span class="source-url">${esc(hostOf(e.url))}</span>
        </label>`).join("")}
    </div>`).join("");
}

$("#sources-groups").addEventListener("change", (e) => {
  const cb = e.target;
  if (cb.type !== "checkbox") return;
  sourcesModel[cb.dataset.cat][Number(cb.dataset.i)].enabled = cb.checked;
  cb.closest(".source-row").classList.toggle("on", cb.checked);
});

$("#add-feed-btn").addEventListener("click", () => {
  const cat = $("#add-feed-cat").value;
  const name = $("#add-feed-name").value.trim();
  const url = $("#add-feed-url").value.trim();
  if (!name || !/^https?:\/\//.test(url)) { toast("Need a name and a valid http(s) URL", true); return; }
  (sourcesModel[cat] = sourcesModel[cat] || []).push({ name, url, enabled: true, custom: true });
  $("#add-feed-name").value = ""; $("#add-feed-url").value = "";
  renderSources();
});

$("#save-sources").addEventListener("click", async () => {
  const feeds = {};
  for (const [cat, entries] of Object.entries(sourcesModel)) {
    const on = entries.filter((e) => e.enabled).map((e) => ({ name: e.name, url: e.url }));
    if (on.length) feeds[cat] = on;
  }
  if (!Object.keys(feeds).length) { toast("Enable at least one source", true); return; }
  try {
    const res = await api("/api/feeds", { method: "PUT", body: JSON.stringify({ feeds }) });
    toast(`Sources saved — ${res.feed_count} feeds active`);
    loadSettings();
  } catch (err) {
    toast(err.message, true);
  }
});

// ── settings ─────────────────────────────────

async function loadSettings() {
  loadSources();
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
  $("#cfg-skip-patch").checked = (p.filters || {}).skip_patch_releases !== false;
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
    "filters.skip_patch_releases": $("#cfg-skip-patch").checked,
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
renderTopicChips();
refreshStats();
loadFeed();
pollStatus(); // pick up an in-flight run if the page was reloaded mid-run
