const THEME_STORAGE_KEY = "paper-daily-theme";
const LANG_STORAGE_KEY = "paper-daily-lang";
const THEMES = new Set(["dark", "light", "eye"]);

const state = {
  datasets: {
    daily: null,
    conference: null,
  },
  theme: "dark",
  lang: "zh",
  filters: {
    query: "",
    topic: "all",
    level: "all",
    collection: "daily",
    view: "daily",
    date: "",
  },
};

const nodes = {
  updatedAt: document.querySelector("#updatedAt"),
  paperCount: document.querySelector("#paperCount"),
  weekCount: document.querySelector("#weekCount"),
  monthCount: document.querySelector("#monthCount"),
  topScore: document.querySelector("#topScore"),
  resultCount: document.querySelector("#resultCount"),
  viewTitle: document.querySelector("#viewTitle"),
  listTitle: document.querySelector("#listTitle"),
  scopeLabel: document.querySelector("#scopeLabel"),
  paperList: document.querySelector("#paperList"),
  topicFilter: document.querySelector("#topicFilter"),
  levelFilter: document.querySelector("#levelFilter"),
  dateFilter: document.querySelector("#dateFilter"),
  searchInput: document.querySelector("#searchInput"),
  langToggle: document.querySelector("#langToggle"),
  themeOptions: document.querySelectorAll("[data-theme-option]"),
  collectionTabs: document.querySelectorAll("[data-collection]"),
  tabs: document.querySelectorAll(".tab"),
  template: document.querySelector("#paperTemplate"),
};

function activeData() {
  return state.datasets[state.filters.collection] || state.datasets.daily || { papers: [], topics: [], stats: {} };
}

function storedTheme() {
  try {
    const theme = localStorage.getItem(THEME_STORAGE_KEY);
    return THEMES.has(theme) ? theme : "dark";
  } catch {
    return "dark";
  }
}

function storedLang() {
  try {
    const lang = localStorage.getItem(LANG_STORAGE_KEY);
    return lang === "en" ? "en" : "zh";
  } catch {
    return "zh";
  }
}

function applyLang(lang) {
  state.lang = lang === "en" ? "en" : "zh";
  nodes.langToggle.textContent = state.lang === "en" ? "EN" : "中";
  try {
    localStorage.setItem(LANG_STORAGE_KEY, state.lang);
  } catch {}
  render();
}

function applyTheme(theme) {
  state.theme = THEMES.has(theme) ? theme : "dark";
  document.body.dataset.theme = state.theme;
  for (const option of nodes.themeOptions) {
    const active = option.dataset.themeOption === state.theme;
    option.classList.toggle("active", active);
    option.setAttribute("aria-checked", String(active));
  }
  try {
    localStorage.setItem(THEME_STORAGE_KEY, state.theme);
  } catch {
    // localStorage may be blocked in privacy-focused browser modes.
  }
}

function parseDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDate(value) {
  const date = parseDate(value);
  if (!date) return value ? String(value).slice(0, 10) : "-";
  return date.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
}

function dateKey(value) {
  const date = parseDate(value);
  if (!date) return "";
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function collectionTime(paper) {
  // Prioritise the paper's real publication date so that daily /
  // weekly / monthly views show the actual submission rhythm.
  // last_seen_at / first_seen_at are collection timestamps (all
  // the same for a single run) — use them only as fallback.
  return paper.published || paper.last_seen_at || paper.first_seen_at || paper.updated || "";
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function startOfWeek(date) {
  const day = startOfDay(date);
  const offset = (day.getDay() + 6) % 7;
  day.setDate(day.getDate() - offset);
  return day;
}

function endOfWeek(date) {
  const end = startOfWeek(date);
  end.setDate(end.getDate() + 7);
  return end;
}

// window sizes for "this week" / "this month" — papers published
// within the last N days from the selected date.
const WEEK_WINDOW_DAYS = 7;
const MONTH_WINDOW_DAYS = 30;
const HIGHLIGHTS_WINDOW_DAYS = 7;

function startOfWindow(date, days) {
  const d = startOfDay(date);
  d.setDate(d.getDate() - days);
  return d;
}

function endOfWindow(date) {
  // "now" is always start of the next day after the selected date
  const d = startOfDay(date);
  d.setDate(d.getDate() + 1);
  return d;
}

function startOfMonth(date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function endOfMonth(date) {
  return new Date(date.getFullYear(), date.getMonth() + 1, 1);
}

function inRange(value, start, end) {
  const date = parseDate(value);
  return Boolean(date && date >= start && date < end);
}

function selectedDate() {
  return parseDate(`${state.filters.date}T12:00:00`) || new Date();
}

// When showing "all" topics, display the best_match score (includes
// bridge bonus for ranking).  When a specific topic is selected, use
// base_score so the list is sorted by raw relevance to that topic.
function scoreOf(paper) {
  if (state.filters.topic !== "all") {
    const label = topicLabel(paper);
    return label ? (label.base_score ?? label.score ?? 0) : Number(paper.best_match?.score || 0);
  }
  return Number(paper.best_match?.score || 0);
}

function levelOf(paper) {
  if (state.filters.topic !== "all") {
    return topicLevel(paper);
  }
  return String(paper.best_match?.level || "low").toLowerCase();
}

// Topic tag eligibility:
//   strong: base_score >= 0.10 → always in
//   weak:   base_score >= 0.06 AND has keyword hits → in
const TOPIC_STRONG = {
  motivic_k_theory: 0.08,
  algebraic_geometry: 0.08,
  arithmetic_geometry: 0.08,
  homotopy_theory: 0.08,
};
const MIN_TOPIC_BASE_WEAK = 0.04;

// Return the best matching label for the currently selected topic filter.
// When "all" is selected, use best_match (overall winner).  When a specific
// topic is chosen, look up its entry in top_labels to get the per-topic
// base_score and level, which are not inflated by cross-domain bridge bonus.
function topicLabel(paper) {
  const filterTopic = state.filters.topic;
  if (filterTopic === "all") return paper.best_match || null;
  if (paper.best_match?.topic_id === filterTopic) return paper.best_match;
  const extra = paper.top_labels || [];
  return extra.find(l => l.topic_id === filterTopic) || null;
}

function topicScore(paper) {
  const label = topicLabel(paper);
  return label ? (label.base_score ?? label.score ?? 0) : 0;
}

function topicLevel(paper) {
  const label = topicLabel(paper);
  return label ? String(label.level || "low").toLowerCase() : "low";
}

function textIncludes(paper, query) {
  if (!query) return true;
  const haystack = [
    paper.title,
    paper.summary,
    (paper.authors || []).join(" "),
    (paper.categories || []).join(" "),
    paper.best_match?.reason,
    paper.chinese_summary?.innovation,
    paper.chinese_summary?.evidence,
    paper.chinese_summary?.limitations,
    paper.chinese_summary?.why_relevant,
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query.toLowerCase());
}

function enField(paper, field) {
  // Generate English summary from raw paper metadata
  const enSummary = paper.summary || "";
  const firstSent = enSummary.split(/[.!?]\s+/)[0] || "";
  const fields = {
    problem: firstSent || enSummary.slice(0, 300) || "See paper for details.",
    method: enSummary.slice(0, 400) || "Please open the paper link to view method details.",
    innovation: firstSent ? firstSent.slice(0, 300) : enSummary.slice(0, 300) || "Core contribution extracted from abstract.",
    evidence: enSummary ? enSummary.slice(0, 250) : "Evidence not available in metadata.",
    limitations: "Full-text reading required for comprehensive evaluation.",
    why_relevant: (paper.best_match?.reason || "Matched by keyword / category overlap."),
  };
  return fields[field] || "See paper for details.";
}

function enRelReason(paper, best) {
  // Pair English abstract with the topic classification reason
  const topicName = best.topic_name || "Uncategorized";
  const reason = best.reason || "Matched by keyword / category overlap.";
  return `Matched to "${topicName}": ${reason}. See abstract for full details.`;
}

function matchesBaseFilters(paper) {
  if (!textIncludes(paper, state.filters.query)) return false;

  if (state.filters.topic !== "all") {
    const label = topicLabel(paper);
    if (!label) return false;
    // Must have per-topic strong score, OR weak + keyword hits
    const base = label.base_score ?? label.score ?? 0;
    const hasHits = (label.keyword_hits || []).length > 0;
    const tid = label.topic_id || "";
    const strong = TOPIC_STRONG[tid] ?? 0.22;
    if (base >= strong) { /* passes */ }
    else if (base >= MIN_TOPIC_BASE_WEAK && hasHits) { /* passes */ }
    else return false;
  }

  if (state.filters.level !== "all") {
    if (state.filters.topic !== "all") {
      if (topicLevel(paper) !== state.filters.level) return false;
    } else {
      if (levelOf(paper) !== state.filters.level) return false;
    }
  }

  return true;
}

function matchesView(paper) {
  if (state.filters.view === "all") return true;
  const date = selectedDate();
  const pubDate = parseDate(paper.published || paper.last_seen_at || "");
  if (!pubDate) return false;
  const windowEnd = endOfWindow(date);

  if (state.filters.view === "daily") return dateKey(paper.published || paper.last_seen_at) === state.filters.date;
  if (state.filters.view === "week") return pubDate >= startOfWindow(date, WEEK_WINDOW_DAYS) && pubDate < windowEnd;
  if (state.filters.view === "month") return pubDate >= startOfMonth(date) && pubDate < endOfMonth(date);
  if (state.filters.view === "highlights") {
    return pubDate >= startOfWindow(date, HIGHLIGHTS_WINDOW_DAYS) && pubDate < windowEnd && topicScore(paper) >= 0.42;
  }
  return true;
}

function filteredPapers() {
  return (activeData().papers || [])
    .filter((paper) => matchesBaseFilters(paper) && matchesView(paper))
    .sort((a, b) => topicScore(b) - topicScore(a) || String(b.published || "").localeCompare(String(a.published || "")));
}

function setText(parent, selector, text) {
  parent.querySelector(selector).textContent = text || "暂无";
}

function safeFilename(paper) {
  const title = String(paper.title || paper.id || "paper")
    .replace(/[\\/:*?"<>|]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120);
  return `${title || "paper"}.pdf`;
}

function renderPaper(paper) {
  const node = nodes.template.content.firstElementChild.cloneNode(true);
  const best = paper.best_match || {};
  const summary = paper.chinese_summary || {};
  const badge = node.querySelector(".match-badge");

  // When a topic filter is active, show per-topic score/level;
  // otherwise show the overall best_match.
  const showScore = topicScore(paper);
  const showLevel = topicLevel(paper);

  badge.textContent = `${showLevel} ${showScore.toFixed(2)}`;
  badge.classList.add(showLevel);

  // Multi-topic labels — use base_score so labels aren't bridge-inflated
  const topLabels = paper.top_labels || [];
  const allLabels = topLabels.slice(0, 4);
  // If best_match isn't already in top_labels (rare), prepend it
  if (!allLabels.some(l => l.topic_id === best.topic_id)) {
    allLabels.unshift({
      topic_id: best.topic_id,
      topic_name: best.topic_name,
      base_score: best.base_score ?? best.score,
      score: best.score,
      level: best.level,
    });
  }

  const isZh = state.lang === "zh";
  setText(node, ".paper-date", `发布 ${formatDate(paper.published)} · 收录 ${formatDate(collectionTime(paper))}`);
  setText(node, ".paper-source", paper.source || "paper");
  setText(node, ".paper-title", paper.title);
  setText(node, ".paper-authors", (paper.authors || []).slice(0, 8).join(", "));

  setText(node, ".summary-problem", isZh ? summary.problem : enField(paper, "problem"));
  setText(node, ".summary-method", isZh ? summary.method : enField(paper, "method"));
  setText(node, ".summary-innovation", isZh ? summary.innovation : enField(paper, "innovation"));
  setText(node, ".summary-evidence", isZh ? summary.evidence : enField(paper, "evidence"));
  setText(node, ".summary-limitations", isZh ? summary.limitations : enField(paper, "limitations"));
  setText(node, ".summary-relevant", isZh ? summary.why_relevant : enRelReason(paper, best));

  setText(node, ".match-reason", `${best.topic_name || "未分类"}：${best.reason || ""}`);

  const tags = node.querySelector(".paper-tags");
  // Show multi-topic labels as colored tags
  const labelColors = ["#2dd4bf", "#fbbf24", "#fb7185", "#a78bfa"];
  for (let i = 0; i < allLabels.length; i++) {
    const lbl = allLabels[i];
    const tag = document.createElement("span");
    tag.className = "topic-label";
    tag.textContent = lbl.topic_name;
    tag.style.cssText = `border:1px solid ${labelColors[i]};color:${labelColors[i]};border-radius:999px;padding:2px 8px;font-size:11px;font-weight:700;margin-right:4px;`;
    tags.appendChild(tag);
  }
  for (const category of (paper.categories || []).slice(0, 6)) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = category;
    tags.appendChild(tag);
  }

  const absLink = node.querySelector(".abs-link");
  const pdfLink = node.querySelector(".pdf-link");
  const downloadLink = node.querySelector(".download-link");
  const pdfUrl = paper.pdf_url || paper.paper_url || "#";
  absLink.href = paper.paper_url || "#";
  pdfLink.href = pdfUrl;
  downloadLink.href = pdfUrl;
  downloadLink.setAttribute("download", safeFilename(paper));
  downloadLink.setAttribute("target", "_blank");
  downloadLink.setAttribute("rel", "noreferrer");
  return node;
}

function viewLabels() {
  const date = selectedDate();
  const dayLabel = formatDate(date.toISOString());
  const weekStart = formatDate(startOfWeek(date).toISOString());
  const weekEndDate = endOfWeek(date);
  weekEndDate.setDate(weekEndDate.getDate() - 1);
  const weekEnd = formatDate(weekEndDate.toISOString());
  const monthLabel = `${date.getFullYear()} 年 ${String(date.getMonth() + 1).padStart(2, "0")} 月`;
  return {
    all: [state.filters.collection === "conference" ? "顶会精品" : "全部论文", "全部已收录论文"],
    daily: ["当日论文", dayLabel],
    week: ["本周论文", `${weekStart} - ${weekEnd}`],
    month: ["月度论文", monthLabel],
    highlights: ["本周精选", `${weekStart} - ${weekEnd}`],
  };
}

function updateHeadings(papers) {
  const labels = viewLabels()[state.filters.view];
  nodes.viewTitle.textContent = labels[0];
  nodes.listTitle.textContent = labels[0];
  nodes.scopeLabel.textContent = labels[1];
  nodes.resultCount.textContent = `${papers.length} 篇`;
}

function render() {
  const papers = filteredPapers();
  updateHeadings(papers);
  nodes.paperList.textContent = "";

  if (!papers.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "当前筛选条件下没有论文。";
    nodes.paperList.appendChild(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const paper of papers) fragment.appendChild(renderPaper(paper));
  nodes.paperList.appendChild(fragment);
}

function hydrateTopicFilter() {
  nodes.topicFilter.innerHTML = '<option value="all">全部方向</option>';
  for (const topic of activeData().topics || []) {
    const option = document.createElement("option");
    option.value = topic.id;
    option.textContent = topic.name;
    nodes.topicFilter.appendChild(option);
  }
}

function hydrateDateFilter() {
  const data = activeData();
  const now = new Date();

  // Build synthetic date options for daily/weekly/monthly views
  // so the user can always go back through the last 7 days.
  const recent = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    recent.push(dateKey(d.toISOString()));
  }

  // Merge with actual paper dates so nothing is hidden
  const paperDates = [...new Set((data.papers || []).map((paper) => dateKey(collectionTime(paper))).filter(Boolean))];
  const all = [...new Set([...recent, ...paperDates])].sort().reverse();

  const fallback = dateKey(data.generated_at_iso || now.toISOString());
  const options = all.length ? all : [fallback];
  state.filters.date = options[0];
  nodes.dateFilter.textContent = "";
  for (const key of options) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = formatDate(`${key}T12:00:00`);
    nodes.dateFilter.appendChild(option);
  }
}

function updateStats() {
  const papers = activeData().papers || [];
  const date = selectedDate();
  const windowEnd = endOfWindow(date);
  const weekPapers = papers.filter((paper) => {
    const d = parseDate(paper.published || paper.last_seen_at || "");
    return d && d >= startOfWindow(date, WEEK_WINDOW_DAYS) && d < windowEnd;
  });
  const monthPapers = papers.filter((paper) => {
    const d = parseDate(paper.published || paper.last_seen_at || "");
    return d && d >= startOfMonth(date) && d < endOfMonth(date);
  });
  const top = papers.reduce((max, paper) => Math.max(max, topicScore(paper)), 0);
  nodes.paperCount.textContent = String(papers.length);
  nodes.weekCount.textContent = String(weekPapers.length);
  nodes.monthCount.textContent = String(monthPapers.length);
  nodes.topScore.textContent = top.toFixed(2);
}

function bindEvents() {
  for (const option of nodes.themeOptions) {
    option.addEventListener("click", () => {
      applyTheme(option.dataset.themeOption);
    });
  }
  nodes.langToggle.addEventListener("click", () => {
    applyLang(state.lang === "zh" ? "en" : "zh");
  });
  nodes.searchInput.addEventListener("input", (event) => {
    state.filters.query = event.target.value.trim();
    render();
  });
  nodes.topicFilter.addEventListener("change", (event) => {
    state.filters.topic = event.target.value;
    render();
  });
  nodes.levelFilter.addEventListener("change", (event) => {
    state.filters.level = event.target.value;
    render();
  });
  for (const tab of nodes.collectionTabs) {
    tab.addEventListener("click", () => {
      state.filters.collection = tab.dataset.collection;
      state.filters.view = state.filters.collection === "conference" ? "all" : "daily";
      state.filters.topic = "all";
      for (const item of nodes.collectionTabs) item.classList.toggle("active", item === tab);
      for (const item of nodes.tabs) item.classList.toggle("active", item.dataset.view === state.filters.view);
      hydrateTopicFilter();
      hydrateDateFilter();
      updateStats();
      updateUpdatedAt();
      render();
    });
  }
  nodes.dateFilter.addEventListener("change", (event) => {
    state.filters.date = event.target.value;
    updateStats();
    render();
  });
  for (const tab of nodes.tabs) {
    tab.addEventListener("click", () => {
      state.filters.view = tab.dataset.view;
      for (const item of nodes.tabs) item.classList.toggle("active", item === tab);
      render();
    });
  }
}

async function loadData() {
  const response = await fetch("./data/papers.json", { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function loadOptionalData(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) return { generated_at_iso: new Date().toISOString(), topics: [], papers: [], stats: {} };
  return response.json();
}

function updateUpdatedAt(message = "") {
  if (message) {
    nodes.updatedAt.textContent = message;
    return;
  }
  const data = activeData();
  const stats = data.stats || {};
  const mode = stats.collection_mode === "incremental" ? "增量" : "初始化";
  const kind = state.filters.collection === "conference" ? "顶会精品" : "每日新论文";
  nodes.updatedAt.textContent = `${kind} · 更新于 ${formatDate(data.generated_at_iso)} · ${mode} · ${stats.llm_enabled ? "LLM" : "基础"}`;
}

async function main() {
  applyTheme(storedTheme());
  applyLang(storedLang());
  bindEvents();
  try {
    state.datasets.daily = await loadData();
    state.datasets.conference = await loadOptionalData("./data/conference_papers.json");
  } catch (error) {
    state.datasets.daily = {
      generated_at_iso: new Date().toISOString(),
      topics: [],
      papers: [],
      stats: { llm_enabled: false },
    };
    state.datasets.conference = {
      generated_at_iso: new Date().toISOString(),
      topics: [],
      papers: [],
      stats: { llm_enabled: false },
    };
    updateUpdatedAt(`数据读取失败：${error.message}`);
  }

  updateUpdatedAt();
  hydrateTopicFilter();
  hydrateDateFilter();
  updateStats();
  render();
}

main();
