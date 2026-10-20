// Arbiter workbook. WebSocket push is primary for the paper engine; REST
// polling takes over whenever the socket is unavailable. Linked venues and
// live markets refresh on their own polls. Tables render as spreadsheet grids:
// click a cell to see it in the formula bar, shift/cmd-click to sum a range.
const FALLBACK_POLL_MS = 2500;
const SECONDARY_POLL_MS = 5000;
const VENUE_POLL_MS = 20000;
const MARKETS_POLL_MS = 3000;
const WS_RECONNECT_BASE_MS = 1000;
const WS_RECONNECT_MAX_MS = 15000;
const MAX_CHART_SAMPLES = 120;
const COLUMN_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
const GAUGE_CIRCUMFERENCE = 2 * Math.PI * 26;

const byId = (id) => document.getElementById(id);
const els = {
  banner: byId("banner"),
  greeting: byId("greeting"),
  clock: byId("system-clock"),
  modeLabel: byId("mode-label"),
  runConfig: byId("run-config"),
  btnStart: byId("btn-start"),
  btnStop: byId("btn-stop"),
  fbarName: byId("fbar-name"),
  fbarValue: byId("fbar-value"),
  nowSummary: byId("now-summary"),
  nowCount: byId("now-count"),
  liveMarkets: byId("live-markets"),
  paperEquity: byId("paper-equity"),
  paperBankroll: byId("paper-bankroll"),
  pnlSession: byId("pnl-session"),
  headroomBar: byId("headroom-bar"),
  headroomText: byId("headroom-text"),
  paperVolume: byId("paper-volume"),
  paperVolumeToday: byId("paper-volume-today"),
  paperFills: byId("paper-fills"),
  paperFees: byId("paper-fees"),
  paperOrders: byId("paper-orders"),
  paperOpps: byId("paper-opps"),
  strategyPhase: byId("strategy-phase"),
  firstLegPrice: byId("first-leg-price"),
  firstLegMeta: byId("first-leg-meta"),
  secondLegPrice: byId("second-leg-price"),
  secondLegMeta: byId("second-leg-meta"),
  hedgeTimer: byId("hedge-timer"),
  expectedNet: byId("expected-net"),
  depthState: byId("depth-state"),
  sequenceId: byId("sequence-id"),
  openSequenceCount: byId("open-sequence-count"),
  gaugeUpLabel: byId("gauge-up-label"),
  gaugeDownLabel: byId("gauge-down-label"),
  safetyChip: byId("safety-chip"),
  runStateDot: byId("run-state-dot"),
  runStateLabel: byId("run-state-label"),
  uptime: byId("uptime"),
  checkEngine: byId("check-engine"),
  connection: byId("connection-state"),
  linkDot: byId("link-dot"),
  liveCheckText: byId("live-check-text"),
  checkLive: byId("check-live"),
  ownerState: byId("owner-state"),
  checkOwner: byId("check-owner"),
  controlRevision: byId("control-revision"),
  refreshVenues: byId("btn-refresh-venues"),
  totalValue: byId("total-value"),
  totalNote: byId("total-note"),
  totalCash: byId("total-cash"),
  totalPositions: byId("total-positions"),
  totalOpen: byId("total-open"),
  totalVolume: byId("total-volume"),
  totalUnrealized: byId("total-unrealized"),
  totalRealized: byId("total-realized"),
  accountTotalShort: byId("account-total-short"),
  positionsCount: byId("positions-count"),
  positionsBody: byId("positions-body"),
  pnlLocked: byId("pnl-locked"),
  pnlDeployed: byId("pnl-deployed"),
  strategyExposure: byId("strategy-exposure"),
  strategyPairs: byId("strategy-pairs"),
  diagTimeouts: byId("diag-timeouts"),
  diagExits: byId("diag-exits"),
  modelRegime: byId("model-regime"),
  modelConfidence: byId("model-confidence"),
  modelStatus: byId("model-status"),
  modelAge: byId("model-age"),
  sampleCount: byId("sample-count"),
  pnlLine: byId("pnl-line"),
  pnlZero: byId("pnl-zero"),
  chartEmpty: byId("chart-empty"),
  phaseWaiting: byId("phase-waiting"),
  phaseSeeking: byId("phase-seeking"),
  phasePaired: byId("phase-paired"),
  phaseAborted: byId("phase-aborted"),
  phaseTerminal: byId("phase-terminal"),
  rejectionTotal: byId("rejection-total"),
  rejectionList: byId("rejection-list"),
  logTail: byId("log-tail"),
  clearLog: byId("btn-clear-log"),
  statusReady: byId("status-ready"),
  statusCalc: byId("status-calc"),
  tickerPnl: byId("ticker-pnl"),
  toast: byId("toast"),
};

const chartSamples = [];
let lastSampleSignature = null;
let lastStrategySnapshot = null;
let lastStatus = null;
let lastPaper = null;
let lastPnl = null;
let toastTimer = null;
let toastIsError = false;
let startRequestedAt = 0;

// ---------------------------------------------------------------- formatting

function asFinite(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

const usdFormat = new Intl.NumberFormat("en-US", {style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2});

function fmtMoney(value) {
  const number = asFinite(value);
  return number === null ? "--" : usdFormat.format(number);
}

function fmtUsd(value) {
  const number = asFinite(value);
  if (number === null) return "--";
  const sign = number < 0 ? "−" : number > 0 ? "+" : "";
  return `${sign}${usdFormat.format(Math.abs(number))}`;
}

function fmtCompact(value) {
  const number = asFinite(value);
  if (number === null) return "--";
  if (Math.abs(number) >= 1e6) return `$${(number / 1e6).toFixed(2)}M`;
  if (Math.abs(number) >= 1e4) return `$${(number / 1e3).toFixed(1)}K`;
  return usdFormat.format(number);
}

function fmtPrice(value) {
  const number = asFinite(value);
  return number === null ? "--" : number.toFixed(3);
}

function fmtCount(value) {
  const number = asFinite(value);
  return number === null ? "--" : Math.round(number).toLocaleString("en-US");
}

function fmtAge(seconds) {
  const value = asFinite(seconds);
  if (value === null || value < 0) return "";
  if (value < 60) return `${Math.round(value)}s old`;
  if (value < 3600) return `${Math.round(value / 60)}m old`;
  return `${(value / 3600).toFixed(1)}h old`;
}

function fmtTimer(seconds) {
  const value = asFinite(seconds);
  if (value === null) return "--";
  if (value <= 0) return "expired";
  return `${Math.ceil(value)}s left`;
}

function fmtClock(seconds) {
  if (seconds === null || seconds === undefined) return "--";
  const value = Math.max(0, Math.round(seconds));
  const h = Math.floor(value / 3600), m = Math.floor((value % 3600) / 60), s = value % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function pnlClass(value) {
  const number = asFinite(value);
  if (number === null || number === 0) return "";
  return number > 0 ? "positive" : "negative";
}

function setPnl(element, value, base = "") {
  element.textContent = fmtUsd(value);
  element.className = `${base} ${pnlClass(value)}`.trim();
}

function clearToast() {
  clearTimeout(toastTimer);
  toastIsError = false;
  els.toast.className = "toast";
}

function showToast(message, error = false) {
  clearTimeout(toastTimer);
  toastIsError = error;
  els.toast.textContent = message;
  els.toast.className = `toast is-visible${error ? " is-error" : ""}`;
  toastTimer = setTimeout(clearToast, 3800);
}

function updateClock() {
  const now = new Date();
  els.clock.textContent = `${now.toISOString().slice(11, 19)} UTC`;
  const hour = now.getHours();
  els.greeting.textContent = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${url} → HTTP ${response.status}`);
  }
  return response.json();
}

// ---------------------------------------------------------------- spreadsheet grids
// A cell is a string or {text, value, num, cls, href, tag, formula}.

const selected = new Map(); // "sheet!A2" -> numeric value or null

function cellRef(col, row) {
  return `${COLUMN_LETTERS[col] || "?"}${row}`;
}

function renderGrid(table, sheet, headers, rows, emptyText) {
  table.replaceChildren();
  const thead = document.createElement("thead");
  const letters = document.createElement("tr");
  const corner = document.createElement("th");
  corner.className = "rn";
  letters.appendChild(corner);
  headers.forEach((_, index) => {
    const th = document.createElement("th");
    th.textContent = COLUMN_LETTERS[index];
    letters.appendChild(th);
  });
  const names = document.createElement("tr");
  names.className = "cols";
  const one = document.createElement("th");
  one.className = "rn";
  one.textContent = "1";
  names.appendChild(one);
  headers.forEach((name) => {
    const th = document.createElement("th");
    th.textContent = name;
    names.appendChild(th);
  });
  thead.append(letters, names);

  const tbody = document.createElement("tbody");
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.className = "empty";
    const td = document.createElement("td");
    td.colSpan = headers.length + 1;
    td.textContent = emptyText;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
  rows.forEach((cells, rowIndex) => {
    const rowNumber = rowIndex + 2;
    const tr = document.createElement("tr");
    const rn = document.createElement("td");
    rn.className = "rn";
    rn.textContent = String(rowNumber);
    tr.appendChild(rn);
    cells.forEach((raw, colIndex) => {
      const cell = typeof raw === "object" && raw !== null ? raw : {text: raw};
      const td = document.createElement("td");
      const ref = cellRef(colIndex, rowNumber);
      const key = `${sheet}!${ref}`;
      td.className = [cell.num ? "num" : "", cell.cls || "", selected.has(key) ? "sel" : ""].join(" ").trim();
      if (cell.tag) {
        const tag = document.createElement("span");
        tag.className = cell.tagCls ? `tag ${cell.tagCls}` : "tag";
        tag.textContent = cell.tag;
        td.append(tag, " ");
      }
      if (cell.href && /^https:\/\//.test(cell.href)) {
        const link = document.createElement("a");
        link.href = cell.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = cell.text ?? "";
        td.appendChild(link);
      } else {
        td.append(cell.text ?? "");
      }
      td.dataset.key = key;
      td.dataset.ref = ref;
      td.dataset.sheet = sheet;
      td.dataset.formula = cell.formula || String(cell.text ?? "");
      if (asFinite(cell.value) !== null) td.dataset.value = String(cell.value);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.append(thead, tbody);
}

function updateCalc() {
  const values = [...selected.values()].filter((value) => value !== null);
  if (!selected.size) {
    els.statusCalc.textContent = "";
    return;
  }
  const sum = values.reduce((total, value) => total + value, 0);
  els.statusCalc.textContent = values.length
    ? `Sum ${sum.toFixed(2)} · Avg ${(sum / values.length).toFixed(3)} · Count ${selected.size}`
    : `Count ${selected.size}`;
}

document.addEventListener("click", (event) => {
  const td = event.target.closest("td[data-key]");
  if (!td || event.target.closest("a")) return;
  const multi = event.shiftKey || event.metaKey || event.ctrlKey;
  if (!multi) {
    selected.clear();
    document.querySelectorAll("td.sel").forEach((cell) => cell.classList.remove("sel"));
  }
  if (selected.has(td.dataset.key) && multi) {
    selected.delete(td.dataset.key);
    td.classList.remove("sel");
  } else {
    selected.set(td.dataset.key, asFinite(td.dataset.value));
    td.classList.add("sel");
  }
  els.fbarName.textContent = td.dataset.ref;
  els.fbarValue.textContent = `=${td.dataset.sheet}!${td.dataset.ref} → ${td.dataset.formula}`;
  updateCalc();
});

// ---------------------------------------------------------------- sheet tabs

const tabs = Array.from(document.querySelectorAll(".tab"));
function showSheet(name) {
  tabs.forEach((tab) => tab.classList.toggle("is-active", tab.dataset.tab === name));
  document.querySelectorAll(".sheet-page").forEach((page) => page.classList.toggle("is-active", page.dataset.sheet === name));
  try { localStorage.setItem("arbiter.sheet", name); } catch (_) {}
}
tabs.forEach((tab) => tab.addEventListener("click", () => showSheet(tab.dataset.tab)));
try {
  const saved = localStorage.getItem("arbiter.sheet");
  if (saved && tabs.some((tab) => tab.dataset.tab === saved)) showSheet(saved);
} catch (_) {}

// ---------------------------------------------------------------- gauges

function setGauge(name, fraction, display) {
  const gauge = document.querySelector(`[data-gauge="${name}"]`);
  if (!gauge) return;
  const value = fraction === null ? 0 : Math.max(0, Math.min(1, fraction));
  gauge.querySelector(".gauge__arc").style.strokeDasharray = `${(value * GAUGE_CIRCUMFERENCE).toFixed(1)} 999`;
  gauge.querySelector("b").textContent = display;
}

function renderCapital() {
  const bankroll = asFinite(lastPaper && lastPaper.bankroll_usd);
  const deployed = asFinite(lastPnl && lastPnl.deployed_usd) ?? 0;
  setGauge("deployed", bankroll ? deployed / bankroll : null, bankroll ? String(Math.round((deployed / bankroll) * 100)) : "--");

  const limit = asFinite(lastStatus && lastStatus.risk && lastStatus.risk.daily_kill_switch_usd);
  const session = asFinite(lastPnl && lastPnl.session_pnl) ?? 0;
  if (limit) {
    const headroom = Math.max(0, 1 - Math.max(0, -session) / limit);
    setGauge("headroom", headroom, String(Math.round(headroom * 100)));
    els.headroomBar.style.width = `${(headroom * 100).toFixed(1)}%`;
    els.headroomText.textContent = `${fmtMoney(limit - Math.max(0, -session))} of ${fmtMoney(limit)}`;
  } else {
    setGauge("headroom", null, "--");
    els.headroomText.textContent = "--";
  }
}

// ---------------------------------------------------------------- bot status

function check(element, state, text) {
  element.className = state;
  element.textContent = text;
}

function renderStatus(status) {
  lastStatus = status;
  const state = String(status.run_state || "STOPPED").toUpperCase();
  const external = Array.isArray(status.external_bots) ? status.external_bots : [];
  const ownership = String(status.ownership || "none").toLowerCase();
  const pid = status.pid === null || status.pid === undefined ? null : status.pid;
  const config = status.run_config || {};
  const running = state !== "STOPPED";

  els.runStateDot.className = `tile-icon tile-icon--sm ${state === "RUNNING" ? "is-ok" : running ? "is-warn" : ""}`;
  els.runStateLabel.textContent = state.toLowerCase();
  els.uptime.textContent = status.uptime_s !== null && status.uptime_s !== undefined ? `· ${Math.round(status.uptime_s)}s` : "";
  check(els.checkEngine, state === "RUNNING" ? "ok" : running ? "warn" : "", state === "RUNNING" ? "✓" : running ? "…" : "–");

  let ownerLabel = "no process";
  if (external.length) ownerLabel = `conflict · ${external.length} extra bot${external.length === 1 ? "" : "s"}`;
  else if (ownership === "managed") ownerLabel = `managed${pid === null ? "" : ` · pid ${pid}`}`;
  else if (ownership === "recovered") ownerLabel = `attached${pid === null ? "" : ` · pid ${pid}`}`;
  els.ownerState.textContent = ownerLabel;
  els.ownerState.title = external.length ? `Duplicate bot process IDs: ${external.join(", ")}` : `Dashboard ownership: ${ownership}`;
  check(els.checkOwner, external.length ? "bad" : ownership === "none" ? "" : "ok", external.length ? "✗" : ownership === "none" ? "–" : "✓");

  const mode = running && config.mode ? config.mode : null;
  els.runConfig.textContent = mode ? `${mode === "dry_run" ? "dry-run" : mode} · ${config.scope === "btc" ? "BTC only" : "universe"}` : "idle";
  const live = mode === "live";
  els.safetyChip.textContent = live ? "LIVE" : mode === "dry_run" ? "DRY-RUN" : "PAPER";
  els.safetyChip.className = `chip ${live ? "chip--red" : "chip--green"}`;
  els.liveCheckText.textContent = live ? "LIVE · real orders within caps" : "off · paper only";
  check(els.checkLive, live ? "bad" : "ok", live ? "!" : "✓");

  els.btnStart.disabled = running || external.length > 0;
  els.btnStop.disabled = state === "STOPPED" || state === "STOPPING";
  renderScope(running && config.scope ? config.scope : selectedScope, running);
  els.statusReady.textContent = state === "RUNNING" ? "Calculating" : running ? state.toLowerCase() : "Ready";

  // A bot that dies right after Start almost always failed its startup config
  // check; say so instead of silently flipping back to "stopped".
  if (state === "STOPPED" && startRequestedAt && Date.now() - startRequestedAt < 30000
      && status.exit_code !== null && status.exit_code !== undefined && status.exit_code !== 0) {
    startRequestedAt = 0;
    showToast(`Bot exited on startup (code ${status.exit_code}). See the Log sheet.`, true);
  } else if (state === "RUNNING") {
    startRequestedAt = 0;
  }
  if (toastIsError && (state === "STARTING" || state === "RUNNING")) clearToast();
  renderCapital();
}

function renderLiveControl(control) {
  const isLive = Boolean(control.effective_live_submission_enabled);
  els.banner.className = `head ${isLive ? "banner--live" : "banner--paper"}`;
  els.modeLabel.textContent = isLive ? "LIVE" : "PAPER";
  els.controlRevision.textContent = `rev ${control.revision ?? "--"}`;
}

// ---------------------------------------------------------------- scope

const scopeButtons = Array.from(document.querySelectorAll(".seg__opt[data-scope]"));
let selectedScope = "universe";
try { selectedScope = localStorage.getItem("arbiter.scope") === "btc" ? "btc" : "universe"; } catch (_) {}

function renderScope(active, locked) {
  scopeButtons.forEach((button) => {
    const on = button.dataset.scope === active;
    button.classList.toggle("is-on", on);
    button.setAttribute("aria-checked", String(on));
    button.disabled = locked;
  });
}

scopeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    selectedScope = button.dataset.scope;
    try { localStorage.setItem("arbiter.scope", selectedScope); } catch (_) {}
    renderScope(selectedScope, false);
  });
});
renderScope(selectedScope, false);

// ---------------------------------------------------------------- paper P&L

function renderChart(value, signature) {
  const number = asFinite(value);
  if (number === null || signature === lastSampleSignature) return;
  lastSampleSignature = signature;
  chartSamples.push(number);
  if (chartSamples.length > MAX_CHART_SAMPLES) chartSamples.shift();
  const display = chartSamples.length === 1 ? [chartSamples[0], chartSamples[0]] : chartSamples;
  const min = Math.min(...display, 0);
  const max = Math.max(...display, 0);
  const range = Math.max(max - min, 0.01);
  const y = (sample) => 186 - ((sample - min) / range) * 172;
  const line = display
    .map((sample, index) => `${index ? "L" : "M"}${((index / (display.length - 1)) * 800).toFixed(1)} ${y(sample).toFixed(1)}`)
    .join(" ");
  els.pnlLine.setAttribute("d", line);
  els.pnlZero.setAttribute("y1", y(0).toFixed(1));
  els.pnlZero.setAttribute("y2", y(0).toFixed(1));
  els.chartEmpty.style.display = "none";
  els.sampleCount.textContent = `${chartSamples.length} sample${chartSamples.length === 1 ? "" : "s"}`;
}

function renderPnl(pnl) {
  if (!pnl.available) {
    [els.pnlSession, els.pnlLocked, els.pnlDeployed].forEach((element) => { element.textContent = "--"; });
    return;
  }
  lastPnl = pnl;
  setPnl(els.pnlSession, pnl.session_pnl, "tile__value mono");
  setPnl(els.tickerPnl, pnl.session_pnl);
  setPnl(els.pnlLocked, pnl.locked_pnl, "tile__value mono");
  els.pnlDeployed.textContent = fmtMoney(pnl.deployed_usd);
  els.paperOrders.textContent = fmtCount(pnl.orders_sent);
  els.paperOpps.textContent = fmtCount(pnl.opportunities);
  renderChart(pnl.session_pnl, `${pnl.generated_at ?? ""}:${pnl.session_pnl}`);
  renderCapital();
}

function renderPaper(paper) {
  lastPaper = paper;
  els.paperEquity.textContent = fmtMoney(paper.equity_usd);
  els.paperBankroll.textContent = fmtMoney(paper.bankroll_usd);
  els.paperVolume.textContent = fmtCompact(paper.volume_usd);
  els.paperVolumeToday.textContent = fmtCompact(paper.volume_today_usd);
  els.paperFees.textContent = fmtMoney(paper.fees_usd);
  els.paperFills.textContent = fmtCount(paper.fills);
  renderCapital();
}

// ---------------------------------------------------------------- live markets

let liveMarketRows = [];
let liveMarketsAt = 0;

function mid(side) {
  if (!side) return null;
  const bid = asFinite(side.bid), ask = asFinite(side.ask);
  if (bid === null && ask === null) return null;
  return bid !== null && ask !== null ? (bid + ask) / 2 : (ask ?? bid);
}

function quote(side) {
  const value = mid(side);
  if (value === null) return {text: "--"};
  return {
    text: `${Math.round(value * 100)}¢`, value, num: true,
    formula: `MID(bid ${fmtPrice(side.bid)}, ask ${fmtPrice(side.ask)})`,
  };
}

function renderLiveMarkets() {
  const elapsed = liveMarketsAt ? (Date.now() - liveMarketsAt) / 1000 : 0;
  els.nowCount.textContent = String(liveMarketRows.length);
  const rows = liveMarketRows.map((market) => {
    const left = market.seconds_left === null ? null : Math.max(0, market.seconds_left - elapsed);
    const position = market.position;
    return [
      {
        text: market.question || market.market_id, href: market.url, cls: "wrap",
        tag: market.in_trade ? "IN TRADE" : "WATCH", tagCls: market.in_trade ? "" : "tag--watch",
        formula: `MARKET("${market.market_id}")`,
      },
      {...quote(market.yes), text: `${market.outcome_yes} ${quote(market.yes).text}`},
      {...quote(market.no), text: `${market.outcome_no} ${quote(market.no).text}`},
      {text: fmtClock(left), value: left, num: true, cls: left !== null && left < 60 ? "ending" : "", formula: `TIMELEFT() = ${Math.round(left ?? 0)}s`},
      position
        ? {text: `${position.side || "--"} @ ${fmtPrice(position.entry)} × ${position.size ?? "--"}`, formula: `POSITION(${position.phase || "open"})`}
        : {text: "—"},
    ];
  });
  renderGrid(els.liveMarkets, "Desk", ["Market", "Yes side", "No side", "Time left", "Position"], rows,
    "No active market yet. Start the loop to see what it is in, time left and live odds.");

  const lead = liveMarketRows[0];
  if (!lead) {
    els.nowSummary.textContent = "waiting for an active market";
    setGauge("up", null, "--");
    setGauge("down", null, "--");
    return;
  }
  const leadLeft = lead.seconds_left === null ? null : lead.seconds_left - elapsed;
  els.nowSummary.textContent = `${lead.in_trade ? "in trade" : "watching"} · ${lead.question} · ${fmtClock(leadLeft)}`;
  const up = mid(lead.yes), down = mid(lead.no);
  els.gaugeUpLabel.textContent = lead.outcome_yes || "Yes";
  els.gaugeDownLabel.textContent = lead.outcome_no || "No";
  setGauge("up", up, up === null ? "--" : String(Math.round(up * 100)));
  setGauge("down", down, down === null ? "--" : String(Math.round(down * 100)));
}

async function pollLiveMarkets() {
  try {
    const data = await fetchJson("/api/live_markets");
    liveMarketRows = Array.isArray(data.markets) ? data.markets : [];
    liveMarketsAt = Date.now();
    renderLiveMarkets();
  } catch (error) {
    console.warn("live markets poll failed", error);
  }
}

// ---------------------------------------------------------------- venues

const VENUE_FIELDS = {
  account_value_usd: fmtMoney,
  cash_usd: fmtMoney,
  positions_value_usd: fmtMoney,
  cost_basis_usd: fmtMoney,
  unrealized_pnl: fmtUsd,
  realized_pnl: fmtUsd,
  volume_usd: fmtCompact,
  open_positions: fmtCount,
};

function renderVenueCard(venue) {
  const card = document.querySelector(`.venue[data-venue="${venue.venue}"]`);
  if (!card) return;
  const field = (name) => card.querySelector(`[data-field="${name}"]`);
  const state = venue.linked ? venue.status : "unlinked";
  card.dataset.state = state;
  card.dataset.linked = String(Boolean(venue.linked));
  field("state").textContent = {ok: "linked", error: "link error", unlinked: "not linked"}[state] || state;
  Object.entries(VENUE_FIELDS).forEach(([name, format]) => {
    const element = field(name);
    if (!element) return;
    element.textContent = venue.linked ? format(venue[name]) : "--";
    element.className = name.endsWith("pnl") ? pnlClass(venue[name]) : "";
  });
  if (venue.volume_note) field("volume_usd").title = venue.volume_note;
  const meta = [];
  if (venue.identity) meta.push(venue.identity);
  if (venue.environment) meta.push(venue.environment);
  if (venue.latency_ms !== null && venue.latency_ms !== undefined) meta.push(`${venue.latency_ms} ms`);
  if (venue.checked_at) meta.push(`checked ${new Date(venue.checked_at * 1000).toLocaleTimeString()}`);
  if (venue.env_managed) meta.push("set in .env");
  field("meta").textContent = meta.join(" · ");
  field("error").textContent = venue.error || "";
  card.querySelector('[data-action="unlink"]').hidden = !venue.linked || Boolean(venue.env_managed);
}

const MODE_LABELS = {read_only: "read-only", paper: "paper", live: "LIVE"};

function renderVenueMode(venue) {
  const box = document.querySelector(`[data-mode-for="${venue.venue}"]`);
  if (!box) return;
  const modes = venue.modes || {};
  const current = venue.mode || "read_only";
  box.querySelectorAll(".seg__opt").forEach((button) => {
    const option = modes[button.dataset.mode] || {};
    button.classList.toggle("is-on", button.dataset.mode === current);
    button.disabled = option.available === false && button.dataset.mode !== current;
    button.title = option.reason || "";
  });
  const note = box.querySelector('[data-field="mode_note"]');
  note.textContent = (modes[current] || {}).note || "";
  note.className = current === "live" ? "hint error" : "hint";
}

function renderPositions(venues) {
  const rows = [];
  venues.forEach((venue) => {
    (venue.positions || []).forEach((position) => {
      rows.push([
        venue.label,
        {text: position.market, href: position.url, cls: "wrap"},
        position.outcome || "--",
        {text: fmtCount(position.size), value: position.size, num: true},
        {text: fmtPrice(position.avg_price), value: position.avg_price, num: true},
        {text: fmtMoney(position.cost_usd), value: position.cost_usd, num: true},
        {text: fmtMoney(position.value_usd), value: position.value_usd, num: true},
        {text: fmtUsd(position.unrealized_pnl), value: position.unrealized_pnl, num: true, cls: pnlClass(position.unrealized_pnl)},
        {text: fmtUsd(position.realized_pnl), value: position.realized_pnl, num: true, cls: pnlClass(position.realized_pnl)},
      ]);
    });
  });
  els.positionsCount.textContent = String(rows.length);
  renderGrid(els.positionsBody, "Venues", ["Venue", "Market", "Side", "Size", "Avg", "Cost", "Value", "Unrealized", "Realized"],
    rows, "Nothing open on linked venues.");
}

// ---------------------------------------------------------------- live setup reminder

const reminder = byId("live-reminder");
let reminderHidden = false;
try { reminderHidden = sessionStorage.getItem("arbiter.reminder") === "hidden"; } catch (_) {}

function renderLiveSetup(setup) {
  if (!setup || !Array.isArray(setup.steps)) return;
  const steps = setup.steps;
  const next = steps.find((step) => !step.done);
  const list = byId("setup-steps");
  list.replaceChildren();
  steps.forEach((step, index) => {
    const li = document.createElement("li");
    li.className = step.done ? "done" : step === next ? "next" : "";
    const num = document.createElement("span");
    num.className = "num";
    num.textContent = String(index + 1);
    const body = document.createElement("div");
    const title = document.createElement("b");
    title.textContent = step.label;
    const how = document.createElement("small");
    how.textContent = step.how;
    body.append(title, how);
    const mark = document.createElement("span");
    mark.className = "mark";
    mark.textContent = step.done ? "✓" : "–";
    li.append(num, body, mark);
    list.appendChild(li);
  });
  const doneCount = steps.filter((step) => step.done).length;
  byId("setup-chip").textContent = setup.live ? "LIVE ON" : `${doneCount} / ${steps.length}`;
  byId("setup-chip").className = `chip ${setup.live ? "chip--red" : doneCount === steps.length ? "chip--green" : ""}`;
  byId("setup-dot").hidden = Boolean(setup.live);
  byId("reminder-next").textContent = next ? `Next step: ${next.label.charAt(0).toLowerCase()}${next.label.slice(1)}.` : "";
  reminder.hidden = Boolean(setup.live) || reminderHidden;
}

byId("reminder-open").addEventListener("click", () => {
  showSheet("setup");
  byId("live-setup").scrollIntoView({behavior: "smooth", block: "start"});
});
byId("reminder-close").addEventListener("click", () => {
  reminderHidden = true;
  reminder.hidden = true;
  try { sessionStorage.setItem("arbiter.reminder", "hidden"); } catch (_) {}
});

function renderVenues(snapshot) {
  renderLiveSetup(snapshot.live_setup);
  const venues = Array.isArray(snapshot.venues) ? snapshot.venues : [];
  const totals = snapshot.totals || {};
  venues.forEach(renderVenueCard);
  venues.forEach(renderVenueMode);
  renderPositions(venues.filter((venue) => venue.linked && venue.status === "ok"));
  const linked = asFinite(totals.linked) || 0;
  els.totalValue.textContent = linked ? fmtMoney(totals.account_value_usd) : "--";
  els.totalCash.textContent = linked ? fmtMoney(totals.cash_usd) : "--";
  els.totalPositions.textContent = linked ? fmtMoney(totals.positions_value_usd) : "--";
  setPnl(els.totalUnrealized, linked ? totals.unrealized_pnl : null);
  setPnl(els.totalRealized, linked ? totals.realized_pnl : null);
  els.totalVolume.textContent = linked ? fmtCompact(totals.volume_usd) : "--";
  els.totalOpen.textContent = linked ? fmtCount(totals.open_positions) : "--";
  els.accountTotalShort.textContent = linked ? fmtCompact(totals.account_value_usd) : "--";
  const names = venues.filter((venue) => venue.linked).map((venue) => venue.label);
  els.totalNote.textContent = linked
    ? `Across ${names.join(" + ")}; refreshed every ${VENUE_POLL_MS / 1000}s.`
    : "No venue linked yet. Link Polymarket or Kalshi below.";
}

async function pollVenues(refresh = false) {
  try {
    renderVenues(await fetchJson(`/api/venues${refresh ? "?refresh=true" : ""}`));
  } catch (error) {
    console.warn("venue poll failed", error);
  }
}

document.querySelectorAll(".venue__form").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const venue = form.dataset.form;
    const payload = Object.fromEntries(new FormData(form).entries());
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      renderVenues(await fetchJson(`/api/venues/${venue}/link`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      }));
      form.reset();
      showToast(`${venue === "kalshi" ? "Kalshi" : "Polymarket"} linked`);
    } catch (error) {
      showToast(error.message, true);
    }
    button.disabled = false;
  });
});

document.querySelectorAll('.venue [data-action="unlink"]').forEach((button) => {
  button.addEventListener("click", async () => {
    const venue = button.closest(".venue").dataset.venue;
    try {
      renderVenues(await fetchJson(`/api/venues/${venue}/unlink`, {method: "POST"}));
      showToast("Unlinked");
    } catch (error) {
      showToast(error.message, true);
    }
  });
});

document.querySelectorAll("[data-mode-for]").forEach((box) => {
  const venue = box.dataset.modeFor;
  box.querySelectorAll(".seg__opt").forEach((button) => {
    button.addEventListener("click", async () => {
      const mode = button.dataset.mode;
      const body = {mode};
      if (mode === "live") {
        const typed = prompt(
          `LIVE mode places REAL orders on ${venue} with real money, within the caps in .env.\n` +
          `Type LIVE to confirm.`);
        if (typed !== "LIVE") return;
        body.confirm = "LIVE";
      }
      try {
        renderVenues(await fetchJson(`/api/venues/${venue}/mode`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        }));
        showToast(`${venue} set to ${MODE_LABELS[mode]}`, mode === "live");
      } catch (error) {
        showToast(error.message, true);
      }
    });
  });
});

// ---------------------------------------------------------------- strategy

function normalizePhase(value) {
  const phase = String(value || "WAITING_FIRST").trim().toUpperCase().replace(/[ -]+/g, "_");
  if (phase === "CONSUMED") return "TERMINAL";
  if (["SEEKING_SECOND", "SECOND_SUBMITTING"].includes(phase) || phase.includes("SEEK")) return "SEEKING_OPPOSITE";
  if (phase.includes("PAIR") || phase.includes("COMPLETE")) return "PAIRED";
  if (["EXIT_SUBMITTING", "RELEASING", "RELEASED", "MANUAL_REVIEW"].includes(phase) || phase.includes("ABORT") || phase.includes("TIMEOUT") || phase.includes("UNRESOLVED")) return "ABORTED";
  return "WAITING_FIRST";
}

function phaseTotals(rawCounts) {
  const totals = {WAITING_FIRST: 0, SEEKING_OPPOSITE: 0, PAIRED: 0, ABORTED: 0, TERMINAL: 0};
  Object.entries(rawCounts || {}).forEach(([rawPhase, rawCount]) => {
    totals[normalizePhase(rawPhase)] += Math.max(0, Math.round(asFinite(rawCount) || 0));
  });
  return totals;
}

function timestampSeconds(value) {
  const number = asFinite(value);
  if (number !== null) return number > 1e12 ? number / 1000 : number;
  const parsed = Date.parse(String(value || ""));
  return Number.isNaN(parsed) ? null : parsed / 1000;
}

function remainingSeconds(sequence) {
  const supplied = asFinite(sequence.seconds_remaining);
  if (supplied !== null) {
    const generated = timestampSeconds(lastStrategySnapshot && lastStrategySnapshot.generated_at);
    return Math.max(0, supplied - (generated === null ? 0 : Math.max(0, Date.now() / 1000 - generated)));
  }
  const deadline = timestampSeconds(sequence.deadline_at);
  return deadline === null ? null : Math.max(0, deadline - Date.now() / 1000);
}

function renderOpenSequence(sequence, openCount) {
  if (!sequence) {
    els.firstLegPrice.textContent = "none";
    els.firstLegMeta.textContent = "waiting for qualified volatility";
    els.secondLegPrice.textContent = "awaiting";
    els.secondLegMeta.textContent = "fee-adjusted cap --";
    els.hedgeTimer.textContent = "--";
    els.expectedNet.textContent = "--";
    els.expectedNet.className = "";
    els.depthState.textContent = "idle";
    els.sequenceId.textContent = "none";
    els.openSequenceCount.textContent = "0";
    return;
  }
  const firstSide = String(sequence.first_side || "LEG").toUpperCase();
  const oppositeSide = firstSide === "YES" || firstSide === "UP"
    ? (firstSide === "UP" ? "DOWN" : "NO")
    : (firstSide === "DOWN" ? "UP" : "YES");
  const currentPrice = asFinite(sequence.current_second_price);
  const maxPrice = asFinite(sequence.max_second_price);
  const expectedNet = asFinite(sequence.expected_net_edge);
  const eligible = currentPrice !== null && maxPrice !== null && currentPrice <= maxPrice
    && (expectedNet === null || expectedNet >= 0);
  const fee = asFinite(sequence.first_fee);
  const size = asFinite(sequence.size);
  const rawId = String(sequence.sequence_id || "ACTIVE");
  els.firstLegPrice.textContent = `${firstSide} @ ${fmtPrice(sequence.first_price)}`;
  els.firstLegMeta.textContent = `size ${size === null ? "--" : size} · buy fee ${fee === null ? "--" : fmtUsd(fee)}`;
  els.secondLegPrice.textContent = currentPrice === null ? `${oppositeSide} · scanning` : `${oppositeSide} @ ${fmtPrice(currentPrice)}`;
  els.secondLegMeta.textContent = `max ${fmtPrice(maxPrice)} · depth precheck`;
  els.hedgeTimer.textContent = fmtTimer(remainingSeconds(sequence));
  els.expectedNet.textContent = fmtUsd(expectedNet);
  els.expectedNet.className = pnlClass(expectedNet);
  els.depthState.textContent = eligible ? "eligible" : "waiting";
  els.sequenceId.textContent = `#${rawId.slice(-12)}`;
  els.sequenceId.title = rawId;
  els.openSequenceCount.textContent = String(openCount);
}

function renderManualReview(review, reviewCount) {
  const side = String(review.side || review.first_side || "LEG").toUpperCase();
  const rawId = String(review.sequence_id || "RECOVERED");
  els.firstLegPrice.textContent = `${side} · unmatched`;
  els.firstLegMeta.textContent = `recovered cost ${fmtUsd(review.cost)}`;
  els.secondLegPrice.textContent = "operator review";
  els.secondLegMeta.textContent = "no automatic fill assumption";
  els.hedgeTimer.textContent = "hold";
  els.expectedNet.textContent = "--";
  els.expectedNet.className = "";
  els.depthState.textContent = "MANUAL REVIEW";
  els.sequenceId.textContent = `#${rawId.slice(-12)}`;
  els.sequenceId.title = rawId;
  els.openSequenceCount.textContent = String(reviewCount);
}

function renderModel(model) {
  const present = model && typeof model === "object";
  const rawConfidence = present ? asFinite(model.confidence) : null;
  const confidence = rawConfidence === null
    ? null
    : Math.max(0, Math.min(1, rawConfidence > 1 ? rawConfidence / 100 : rawConfidence));
  const usable = Boolean(present && model.usable);
  els.modelRegime.textContent = present ? String(model.regime || "--").toUpperCase() : "--";
  els.modelConfidence.textContent = confidence === null ? "" : `${Math.round(confidence * 100)}% conf ·`;
  els.modelStatus.textContent = usable ? "fresh" : (present ? String(model.status || "unavailable").toLowerCase() : "unavailable");
  els.modelAge.textContent = present ? fmtAge(model.age_s) : "";
}

function renderRejections(rejections) {
  const rows = Object.entries(rejections || {})
    .map(([reason, count]) => [reason, Math.max(0, Math.round(asFinite(count) || 0))])
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1]);
  const total = rows.reduce((sum, [, count]) => sum + count, 0);
  els.rejectionTotal.textContent = `${total.toLocaleString("en-US")} total`;
  renderGrid(els.rejectionList, "Engine", ["Reason", "Count", "Share"], rows.map(([reason, count]) => [
    {text: String(reason).replace(/[_-]+/g, " "), formula: `REJECT("${reason}")`},
    {text: count.toLocaleString("en-US"), value: count, num: true},
    {text: `${total ? Math.round((count / total) * 100) : 0}%`, value: total ? count / total : 0, num: true, formula: `=B/${total}`},
  ]), "No rejected entry frames yet");
}

function renderStrategy(strategy) {
  if (!strategy || strategy.available === false) {
    lastStrategySnapshot = null;
    els.strategyExposure.textContent = "--";
    els.strategyPairs.textContent = "--";
    renderOpenSequence(null, 0);
    renderModel(null);
    return;
  }
  lastStrategySnapshot = strategy;
  const openSequences = Array.isArray(strategy.open_sequences) ? strategy.open_sequences : [];
  const manualReviews = Array.isArray(strategy.manual_review_sequences) ? strategy.manual_review_sequences : [];
  const sequence = openSequences[0] || null;
  const manualReview = manualReviews[0] || null;
  const phase = normalizePhase(sequence ? sequence.phase : (manualReview ? "MANUAL_REVIEW" : (strategy.phase || "WAITING_FIRST")));
  const totals = phaseTotals(strategy.phase_counts);
  els.strategyExposure.textContent = fmtMoney(strategy.open_exposure_usd ?? 0);
  els.strategyPairs.textContent = fmtCount(strategy.completed_pairs ?? 0);
  els.strategyPhase.textContent = phase;
  if (sequence) renderOpenSequence(sequence, openSequences.length + manualReviews.length);
  else if (manualReview) renderManualReview(manualReview, manualReviews.length);
  else renderOpenSequence(null, 0);
  renderModel(strategy.lightgbm);
  els.diagTimeouts.textContent = fmtCount(strategy.timeouts ?? 0);
  els.diagExits.textContent = fmtCount(strategy.exits ?? 0);
  els.phaseWaiting.textContent = String(totals.WAITING_FIRST);
  els.phaseSeeking.textContent = String(totals.SEEKING_OPPOSITE);
  els.phasePaired.textContent = String(totals.PAIRED);
  els.phaseAborted.textContent = String(totals.ABORTED);
  els.phaseTerminal.textContent = String(totals.TERMINAL);
  renderRejections(strategy.rejections);
}

let logCleared = false;
function renderLog(log) {
  const lines = logCleared ? [] : (log.lines || []).slice(-120);
  renderGrid(els.logTail, "Log", ["Event"], lines.map((line) => [{text: line}]),
    logCleared ? "View cleared. New events appear on the next refresh." : "Waiting for runtime events…");
  logCleared = false;
  const wrap = els.logTail.closest(".grid-wrap");
  if (wrap) wrap.scrollTop = wrap.scrollHeight;
}

// ---------------------------------------------------------------- setup sheet

async function loadSetup() {
  try {
    const data = await fetchJson("/api/setup");
    renderGrid(byId("env-grid"), "Setup", ["Setting", "Now", "Default", "What it does"],
      (data.settings || []).map((row) => [
        {text: row.name, cls: "setting", formula: `ENV("${row.name}")`},
        {text: row.now, cls: row.now === "set" || (!row.now.includes("default") && row.now !== "not set") ? "state-set" : "dim"},
        {text: row.default},
        {text: row.about, cls: "wrap"},
      ]), "No settings found.");
  } catch (error) {
    console.warn("setup info unavailable", error);
  }
}

document.querySelectorAll(".code .copy").forEach((button) => {
  button.addEventListener("click", async () => {
    const text = button.parentElement.querySelector("code").textContent;
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "copied";
    } catch (_) {
      button.textContent = "select + copy";
    }
    setTimeout(() => { button.textContent = "copy"; }, 1600);
  });
});

// ---------------------------------------------------------------- transport

let wsConnected = false;
let fallbackTimer = null;

function setConnection(state) {
  const configs = {
    connected: ["live push", "ok", "✓"],
    fallback: ["rest poll", "warn", "!"],
    connecting: ["connecting", "warn", "…"],
    offline: ["offline", "bad", "✗"],
  };
  const [label, cls, mark] = configs[state];
  els.connection.textContent = label;
  check(els.linkDot, cls, mark);
}

async function fetchStrategySafely() {
  try {
    return await fetchJson("/api/strategy");
  } catch (error) {
    console.warn("strategy snapshot unavailable", error);
    return {available: false};
  }
}

async function pollPrimary() {
  try {
    const [status, pnl, strategy] = await Promise.all([
      fetchJson("/api/status"),
      fetchJson("/api/pnl"),
      fetchStrategySafely(),
    ]);
    renderStatus(status);
    renderPnl(pnl);
    renderStrategy(strategy);
    if (!wsConnected) setConnection("fallback");
  } catch (error) {
    console.error("fallback poll failed", error);
    if (!wsConnected) setConnection("offline");
  }
}

async function pollSecondary() {
  try {
    const [log, liveControl, paper] = await Promise.all([
      fetchJson("/api/log?lines=120"),
      fetchJson("/api/live_control"),
      fetchJson("/api/paper"),
    ]);
    renderLiveControl(liveControl);
    renderLog(log);
    renderPaper(paper);
  } catch (error) {
    console.error("secondary poll failed", error);
  }
}

async function refreshNow() {
  await Promise.all([pollPrimary(), pollSecondary()]);
}

function startFallbackPolling() {
  if (fallbackTimer !== null) return;
  setConnection("fallback");
  pollPrimary();
  fallbackTimer = setInterval(pollPrimary, FALLBACK_POLL_MS);
}

function stopFallbackPolling() {
  if (fallbackTimer !== null) {
    clearInterval(fallbackTimer);
    fallbackTimer = null;
  }
}

function connectWs(attempt = 0) {
  setConnection("connecting");
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws`);
  let opened = false;
  socket.onopen = () => {
    opened = true;
    wsConnected = true;
    stopFallbackPolling();
    setConnection("connected");
  };
  socket.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (message.type === "bot_status") renderStatus(message.data);
    else if (message.type === "pnl_update") renderPnl({...message.data, available: true});
    else if (message.type === "strategy_update") renderStrategy({...message.data, available: true});
  };
  socket.onerror = () => console.warn("dashboard websocket error");
  socket.onclose = () => {
    wsConnected = false;
    startFallbackPolling();
    const nextAttempt = opened ? 0 : attempt + 1;
    const delay = Math.min(WS_RECONNECT_BASE_MS * 2 ** nextAttempt, WS_RECONNECT_MAX_MS);
    setTimeout(() => connectWs(nextAttempt), delay);
  };
}

els.btnStart.addEventListener("click", async () => {
  els.btnStart.disabled = true;
  try {
    await fetchJson("/api/botctl/start", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({scope: selectedScope}),
    });
    startRequestedAt = Date.now();
    showToast(`Loop starting · ${selectedScope === "btc" ? "BTC only" : "universe"}`);
  } catch (error) {
    showToast(`Start failed: ${error.message}`, true);
  }
  refreshNow();
});

els.btnStop.addEventListener("click", async () => {
  els.btnStop.disabled = true;
  try {
    await fetchJson("/api/botctl/stop", {method: "POST"});
    showToast("Stopping");
  } catch (error) {
    showToast(`Stop failed: ${error.message}`, true);
  }
  refreshNow();
});

els.refreshVenues.addEventListener("click", () => pollVenues(true));
els.clearLog.addEventListener("click", () => {
  logCleared = true;
  renderLog({lines: []});
});

updateClock();
setInterval(updateClock, 1000);
setInterval(() => {
  if (lastStrategySnapshot) renderStrategy(lastStrategySnapshot);
}, 1000);
connectWs();
setInterval(pollSecondary, SECONDARY_POLL_MS);
setInterval(pollVenues, VENUE_POLL_MS);
setInterval(pollLiveMarkets, MARKETS_POLL_MS);
setInterval(renderLiveMarkets, 1000);
pollPrimary();
pollSecondary();
pollVenues();
pollLiveMarkets();
loadSetup();
