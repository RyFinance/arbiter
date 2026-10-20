// PolyUni sequential operator console. WebSocket push is primary; resilient
// REST polling takes over whenever the socket is unavailable.
const FALLBACK_POLL_MS = 2500;
const SECONDARY_POLL_MS = 5000;
const WS_RECONNECT_BASE_MS = 1000;
const WS_RECONNECT_MAX_MS = 15000;
const MAX_CHART_SAMPLES = 48;

const byId = (id) => document.getElementById(id);
const els = {
  banner: byId("banner"),
  modeLabel: byId("mode-label"),
  runStateDot: byId("run-state-dot"),
  runStateLabel: byId("run-state-label"),
  ownerState: byId("owner-state"),
  uptime: byId("uptime"),
  clock: byId("system-clock"),
  connection: byId("connection-state"),
  btnStart: byId("btn-start"),
  btnStop: byId("btn-stop"),
  liveToggle: byId("live-toggle-checkbox"),
  controlRevision: byId("control-revision"),
  controlDetail: byId("control-detail"),
  pnlSession: byId("pnl-session"),
  pnlLocked: byId("pnl-locked"),
  pnlDeployed: byId("pnl-deployed"),
  strategyExposure: byId("strategy-exposure"),
  strategyPairs: byId("strategy-pairs"),
  pnlLine: byId("pnl-line"),
  pnlArea: byId("pnl-area"),
  pnlPoint: byId("pnl-point"),
  chartEmpty: byId("chart-empty"),
  sampleCount: byId("sample-count"),
  strategyPhase: byId("strategy-phase"),
  strategyPhaseCore: byId("strategy-phase-core"),
  exposureRoute: byId("exposure-route"),
  firstLegPrice: byId("first-leg-price"),
  firstLegMeta: byId("first-leg-meta"),
  secondLegPrice: byId("second-leg-price"),
  secondLegMeta: byId("second-leg-meta"),
  hedgeProgress: byId("hedge-progress"),
  hedgeTimer: byId("hedge-timer"),
  expectedNet: byId("expected-net"),
  depthState: byId("depth-state"),
  sequenceId: byId("sequence-id"),
  openSequenceCount: byId("open-sequence-count"),
  modelRegime: byId("model-regime"),
  modelStatus: byId("model-status"),
  modelConfidence: byId("model-confidence"),
  modelConfidenceBar: byId("model-confidence-bar"),
  modelSource: byId("model-source"),
  modelAge: byId("model-age"),
  diagPaired: byId("diag-paired"),
  diagTimeouts: byId("diag-timeouts"),
  diagExits: byId("diag-exits"),
  phaseWaiting: byId("phase-waiting"),
  phaseSeeking: byId("phase-seeking"),
  phasePaired: byId("phase-paired"),
  phaseAborted: byId("phase-aborted"),
  phaseTerminal: byId("phase-terminal"),
  rejectionTotal: byId("rejection-total"),
  rejectionList: byId("rejection-list"),
  logTail: byId("log-tail"),
  clearLog: byId("btn-clear-log"),
  toast: byId("toast"),
};

const strategySteps = [1, 2, 3, 4].map((number) => byId(`strategy-step-${number}`));
const loopNodes = [1, 2, 3, 4].map((number) => byId(`loop-node-${number}`));
const chartSamples = [];
let lastSampleSignature = null;
let lastStrategySnapshot = null;
let toastTimer = null;
let toastIsError = false;

function asFinite(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function fmtUsd(value) {
  const number = asFinite(value);
  if (number === null) return "--";
  const sign = number < 0 ? "-" : number > 0 ? "+" : "";
  return `${sign}$${Math.abs(number).toFixed(2)}`;
}

function fmtPrice(value) {
  const number = asFinite(value);
  return number === null ? "--" : `$${number.toFixed(4)}`;
}

function fmtAge(seconds) {
  const value = asFinite(seconds);
  if (value === null || value < 0) return "--";
  if (value < 60) return `${Math.round(value)} SEC`;
  if (value < 3600) return `${Math.round(value / 60)} MIN`;
  return `${(value / 3600).toFixed(1)} HR`;
}

function fmtTimer(seconds) {
  const value = asFinite(seconds);
  if (value === null) return "--";
  if (value <= 0) return "EXPIRED";
  return `${Math.ceil(value)} S`;
}

function pnlClass(value) {
  const number = asFinite(value);
  if (number === null || number === 0) return "";
  return number > 0 ? "positive" : "negative";
}

function setMetric(element, value) {
  element.textContent = fmtUsd(value);
  element.className = `metric-card__value ${pnlClass(value)}`;
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
  els.clock.textContent = `${new Date().toISOString().slice(11, 19)} UTC`;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${url} → HTTP ${response.status}`);
  }
  return response.json();
}

function renderStatus(status) {
  const state = String(status.run_state || "STOPPED").toUpperCase();
  const dotClass = {
    STOPPED: "dot--stopped",
    STARTING: "dot--starting",
    RUNNING: "dot--running",
    STOPPING: "dot--stopping",
  }[state] || "dot--stopped";
  const external = Array.isArray(status.external_bots) ? status.external_bots : [];
  const ownership = String(status.ownership || "none").toLowerCase();
  const pid = status.pid === null || status.pid === undefined ? null : status.pid;

  els.runStateDot.className = `dot ${dotClass}`;
  els.runStateLabel.textContent = state;
  els.uptime.textContent = status.uptime_s !== null && status.uptime_s !== undefined
    ? `${Math.round(status.uptime_s)} SEC`
    : "OFFLINE";

  let ownerLabel = "NO PROCESS";
  let ownerClass = "status-text--off";
  if (external.length) {
    ownerLabel = `CONFLICT · ${external.length} EXTRA`;
  } else if (ownership === "managed") {
    ownerLabel = `MANAGED${pid === null ? "" : ` · ${pid}`}`;
    ownerClass = "status-text--ok";
  } else if (ownership === "recovered") {
    ownerLabel = `ATTACHED${pid === null ? "" : ` · ${pid}`}`;
    ownerClass = "status-text--warn";
  }
  els.ownerState.textContent = ownerLabel;
  els.ownerState.className = `top-stat__value status-text ${ownerClass}`;
  els.ownerState.title = external.length
    ? `Duplicate bot process IDs: ${external.join(", ")}`
    : `Dashboard ownership: ${ownership}`;

  els.btnStart.disabled = state !== "STOPPED" || external.length > 0;
  els.btnStop.disabled = state === "STOPPED" || state === "STOPPING";
  document.body.dataset.engineState = state.toLowerCase();

  // A successful attach/start is authoritative and clears an obsolete
  // "another bot is running" action toast from the previous status frame.
  if (toastIsError && (state === "STARTING" || state === "RUNNING")) clearToast();
}

function renderLiveControl(control) {
  const isLive = Boolean(control.effective_live_submission_enabled);
  els.banner.className = `topbar ${isLive ? "banner--live" : "banner--paper"}`;
  els.modeLabel.textContent = isLive ? "LIVE MODE" : "PAPER ONLY";
  els.liveToggle.checked = Boolean(control.operator_enabled);
  els.controlRevision.textContent = `REV ${control.revision ?? "--"}`;
  els.controlDetail.textContent = control.operator_enabled
    ? `Intent armed · ${control.hard_block_reason || "submission hard-blocked"}`
    : "Live submission remains hard-blocked";
  els.liveToggle.title = control.operator_enabled
    ? `Operator intent armed — ${control.hard_block_reason}`
    : "Operator intent is disarmed";
}

function renderChart(value, signature) {
  const number = asFinite(value);
  if (number === null || signature === lastSampleSignature) return;
  lastSampleSignature = signature;
  chartSamples.push(number);
  if (chartSamples.length > MAX_CHART_SAMPLES) chartSamples.shift();

  const display = chartSamples.length === 1 ? [chartSamples[0], chartSamples[0]] : chartSamples;
  const min = Math.min(...display, 0);
  const max = Math.max(...display, 0);
  const range = Math.max(max - min, 1);
  const points = display.map((sample, index) => [
    (index / (display.length - 1)) * 800,
    210 - ((sample - min) / range) * 180,
  ]);
  const line = points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const last = points.at(-1);
  els.pnlLine.setAttribute("d", line);
  els.pnlArea.setAttribute("d", `${line} L800 220 L0 220 Z`);
  els.pnlPoint.setAttribute("cx", last[0].toFixed(1));
  els.pnlPoint.setAttribute("cy", last[1].toFixed(1));
  els.chartEmpty.style.display = "none";
  els.sampleCount.textContent = `${chartSamples.length} SAMPLE${chartSamples.length === 1 ? "" : "S"}`;
}

function renderPnl(pnl) {
  if (!pnl.available) {
    [els.pnlSession, els.pnlLocked, els.pnlDeployed].forEach((element) => {
      element.textContent = "--";
      element.className = "metric-card__value";
    });
    return;
  }
  setMetric(els.pnlSession, pnl.session_pnl);
  setMetric(els.pnlLocked, pnl.locked_pnl);
  setMetric(els.pnlDeployed, pnl.deployed_usd);
  renderChart(pnl.session_pnl, `${pnl.generated_at ?? ""}:${pnl.session_pnl}`);
}

function normalizePhase(value) {
  const phase = String(value || "WAITING_FIRST").trim().toUpperCase().replace(/[ -]+/g, "_");
  if (phase === "CONSUMED") return "TERMINAL";
  if (["SEEKING_SECOND", "SECOND_SUBMITTING"].includes(phase) || phase.includes("SEEK")) return "SEEKING_OPPOSITE";
  if (phase.includes("PAIR") || phase.includes("COMPLETE")) return "PAIRED";
  if (["EXIT_SUBMITTING", "RELEASING", "RELEASED", "MANUAL_REVIEW"].includes(phase) || phase.includes("ABORT") || phase.includes("TIMEOUT") || phase.includes("UNRESOLVED")) return "ABORTED";
  return "WAITING_FIRST";
}

function phaseClass(phase) {
  return {
    WAITING_FIRST: "phase--waiting",
    SEEKING_OPPOSITE: "phase--seeking",
    PAIRED: "phase--paired",
    ABORTED: "phase--aborted",
    TERMINAL: "phase--terminal",
  }[phase] || "phase--offline";
}

function renderStages(phase) {
  strategySteps.forEach((step) => { step.className = "sequence-step"; });
  loopNodes.forEach((node) => { node.className = node.className.replace(/\s(?:is-active|is-complete|is-alert)/g, ""); });

  const markComplete = (index) => {
    strategySteps[index].classList.add("is-complete");
    loopNodes[index].classList.add("is-complete");
  };
  const markActive = (index) => {
    strategySteps[index].classList.add("is-active");
    loopNodes[index].classList.add("is-active");
  };

  if (phase === "SEEKING_OPPOSITE") {
    markComplete(0);
    markComplete(1);
    markActive(2);
  } else if (phase === "PAIRED") {
    markComplete(0);
    markComplete(1);
    markComplete(2);
    markActive(3);
  } else if (phase === "ABORTED") {
    markComplete(0);
    markComplete(1);
    strategySteps[3].classList.add("is-alert");
    loopNodes[3].classList.add("is-alert");
  } else if (phase === "TERMINAL") {
    markComplete(0);
    markComplete(1);
    markComplete(2);
    markComplete(3);
  } else {
    markActive(0);
  }
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
    els.exposureRoute.className = "exposure-route is-idle";
    els.firstLegPrice.textContent = "NO OPEN LEG";
    els.firstLegMeta.textContent = "waiting for qualified volatility";
    els.secondLegPrice.textContent = "AWAITING";
    els.secondLegMeta.textContent = "fee-adjusted cap --";
    els.hedgeProgress.style.width = "0%";
    els.hedgeTimer.textContent = "--";
    els.expectedNet.textContent = "--";
    els.expectedNet.className = "";
    els.depthState.textContent = "IDLE";
    els.sequenceId.textContent = "NONE";
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
  const remaining = remainingSeconds(sequence);
  const opened = timestampSeconds(sequence.opened_at);
  const deadline = timestampSeconds(sequence.deadline_at);
  const duration = opened !== null && deadline !== null ? Math.max(1, deadline - opened) : null;
  const elapsedPercent = remaining !== null && duration !== null
    ? Math.max(0, Math.min(100, (1 - remaining / duration) * 100))
    : 0;
  const fee = asFinite(sequence.first_fee);
  const size = asFinite(sequence.size);
  const rawId = String(sequence.sequence_id || "ACTIVE");

  els.exposureRoute.className = `exposure-route is-active${eligible ? " is-eligible" : ""}`;
  els.firstLegPrice.textContent = `${firstSide} ${fmtPrice(sequence.first_price)}`;
  els.firstLegMeta.textContent = `SIZE ${size === null ? "--" : size} · BUY FEE ${fee === null ? "--" : fmtUsd(fee)}`;
  els.secondLegPrice.textContent = currentPrice === null
    ? `${oppositeSide} · SCANNING`
    : `${oppositeSide} ${fmtPrice(currentPrice)}`;
  els.secondLegMeta.textContent = `MAX ${fmtPrice(maxPrice)} · DEPTH PRECHECK`;
  els.hedgeProgress.style.width = `${elapsedPercent.toFixed(1)}%`;
  els.hedgeTimer.textContent = fmtTimer(remaining);
  els.expectedNet.textContent = fmtUsd(expectedNet);
  els.expectedNet.className = pnlClass(expectedNet);
  els.depthState.textContent = eligible ? "ELIGIBLE" : "WAITING";
  els.sequenceId.textContent = `#${rawId.slice(-12).toUpperCase()}`;
  els.sequenceId.title = rawId;
  els.openSequenceCount.textContent = String(openCount);
}

function renderManualReview(review, reviewCount) {
  const side = String(review.side || review.first_side || "LEG").toUpperCase();
  const rawId = String(review.sequence_id || "RECOVERED");
  els.exposureRoute.className = "exposure-route is-active is-review";
  els.firstLegPrice.textContent = `${side} · UNMATCHED`;
  els.firstLegMeta.textContent = `RECOVERED COST ${fmtUsd(review.cost)}`;
  els.secondLegPrice.textContent = "OPERATOR REVIEW";
  els.secondLegMeta.textContent = "NO AUTOMATIC FILL ASSUMPTION";
  els.hedgeProgress.style.width = "100%";
  els.hedgeTimer.textContent = "HOLD";
  els.expectedNet.textContent = "--";
  els.expectedNet.className = "";
  els.depthState.textContent = "MANUAL REVIEW";
  els.sequenceId.textContent = `#${rawId.slice(-12).toUpperCase()}`;
  els.sequenceId.title = rawId;
  els.openSequenceCount.textContent = String(reviewCount);
}

function renderModel(model) {
  const present = model && typeof model === "object";
  const regime = present ? String(model.regime || "--").toUpperCase() : "--";
  const rawConfidence = present ? asFinite(model.confidence) : null;
  const confidence = rawConfidence === null
    ? null
    : Math.max(0, Math.min(1, rawConfidence > 1 ? rawConfidence / 100 : rawConfidence));
  const status = present
    ? String(model.status || (model.usable ? "fresh" : "unavailable")).toUpperCase()
    : "UNAVAILABLE";
  const usable = Boolean(present && model.usable);

  els.modelRegime.textContent = regime;
  els.modelStatus.textContent = usable ? "FRESH" : status;
  els.modelStatus.className = usable ? "is-fresh" : (status.includes("STALE") ? "is-stale" : "");
  els.modelConfidence.textContent = confidence === null ? "--" : `${Math.round(confidence * 100)}%`;
  els.modelConfidenceBar.style.width = `${confidence === null ? 0 : confidence * 100}%`;
  els.modelSource.textContent = present ? String(model.source || "--").toUpperCase() : "--";
  els.modelAge.textContent = present ? fmtAge(model.age_s) : "--";
}

function renderRejections(rejections) {
  const rows = Object.entries(rejections || {})
    .map(([reason, count]) => [reason, Math.max(0, Math.round(asFinite(count) || 0))])
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1]);
  const total = rows.reduce((sum, [, count]) => sum + count, 0);
  els.rejectionTotal.textContent = `${total} TOTAL`;
  els.rejectionList.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "rejection-empty";
    empty.textContent = "No rejected entry frames yet";
    els.rejectionList.appendChild(empty);
    return;
  }
  rows.slice(0, 5).forEach(([reason, count]) => {
    const row = document.createElement("div");
    const label = document.createElement("span");
    const value = document.createElement("strong");
    row.className = "rejection-row";
    label.textContent = String(reason).replace(/[_-]+/g, " ").toUpperCase();
    label.title = String(reason);
    value.textContent = String(count);
    row.append(label, value);
    els.rejectionList.appendChild(row);
  });
}

function renderStrategy(strategy) {
  if (!strategy || strategy.available === false) {
    lastStrategySnapshot = null;
    els.strategyExposure.textContent = "--";
    els.strategyExposure.className = "metric-card__value";
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
  const paired = Math.max(0, Math.round(asFinite(strategy.completed_pairs) || 0));
  const timeouts = Math.max(0, Math.round(asFinite(strategy.timeouts) || 0));
  const exits = Math.max(0, Math.round(asFinite(strategy.exits) || 0));

  setMetric(els.strategyExposure, strategy.open_exposure_usd ?? 0);
  els.strategyPairs.textContent = String(paired);
  els.strategyPairs.className = "metric-card__value metric-card__value--count";
  els.strategyPhase.textContent = phase;
  els.strategyPhase.className = `phase-badge ${phaseClass(phase)}`;
  els.strategyPhaseCore.textContent = phase;
  renderStages(phase);
  if (sequence) renderOpenSequence(sequence, openSequences.length + manualReviews.length);
  else if (manualReview) renderManualReview(manualReview, manualReviews.length);
  else renderOpenSequence(null, 0);
  renderModel(strategy.lightgbm);

  els.diagPaired.textContent = String(paired);
  els.diagTimeouts.textContent = String(timeouts);
  els.diagExits.textContent = String(exits);
  els.phaseWaiting.textContent = String(totals.WAITING_FIRST);
  els.phaseSeeking.textContent = String(totals.SEEKING_OPPOSITE);
  els.phasePaired.textContent = String(totals.PAIRED);
  els.phaseAborted.textContent = String(totals.ABORTED);
  els.phaseTerminal.textContent = String(totals.TERMINAL);
  renderRejections(strategy.rejections);
}

function renderLog(log) {
  const lines = (log.lines || []).slice(-60);
  els.logTail.replaceChildren();
  if (!lines.length) {
    const placeholder = document.createElement("span");
    placeholder.className = "log-placeholder";
    placeholder.textContent = "Waiting for runtime events…";
    els.logTail.appendChild(placeholder);
    return;
  }
  lines.forEach((line) => {
    const row = document.createElement("span");
    row.className = "log-line";
    row.textContent = line;
    els.logTail.appendChild(row);
  });
  els.logTail.scrollTop = els.logTail.scrollHeight;
}

let wsConnected = false;
let fallbackTimer = null;

function setConnection(state) {
  const configs = {
    connected: ["LIVE / PUSH", "status-text--ok"],
    fallback: ["REST / POLL", "status-text--warn"],
    connecting: ["CONNECTING", "status-text--warn"],
    offline: ["OFFLINE", "status-text--off"],
  };
  const [label, className] = configs[state];
  els.connection.textContent = label;
  els.connection.className = `top-stat__value status-text ${className}`;
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
    const [log, liveControl] = await Promise.all([
      fetchJson("/api/log?lines=60"),
      fetchJson("/api/live_control"),
    ]);
    renderLiveControl(liveControl);
    renderLog(log);
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
      body: JSON.stringify({}),
    });
    showToast("Sequential paper loop start accepted");
  } catch (error) {
    showToast(`Start failed: ${error.message}`, true);
  }
  refreshNow();
});

els.btnStop.addEventListener("click", async () => {
  els.btnStop.disabled = true;
  try {
    await fetchJson("/api/botctl/stop", {method: "POST"});
    showToast("Safe-stop command accepted");
  } catch (error) {
    showToast(`Stop failed: ${error.message}`, true);
  }
  refreshNow();
});

els.liveToggle.addEventListener("change", async (event) => {
  const enabled = event.target.checked;
  const reason = prompt(enabled
    ? "Arm audited operator intent. Enter a reason for the audit trail:"
    : "Disarm operator intent. Enter a reason for the audit trail:");
  if (!reason || !reason.trim()) {
    event.target.checked = !enabled;
    return;
  }
  try {
    await fetchJson("/api/live_control/toggle", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({enabled, reason: reason.trim()}),
    });
    showToast(enabled
      ? "Operator intent armed; submission remains hard-blocked"
      : "Operator intent disarmed");
  } catch (error) {
    showToast(`Control update failed: ${error.message}`, true);
    event.target.checked = !enabled;
  }
  refreshNow();
});

els.clearLog.addEventListener("click", () => {
  els.logTail.textContent = "Log view cleared. New events will appear on the next refresh.";
});

updateClock();
setInterval(updateClock, 1000);
setInterval(() => {
  if (lastStrategySnapshot) renderStrategy(lastStrategySnapshot);
}, 1000);
connectWs();
setInterval(pollSecondary, SECONDARY_POLL_MS);
pollPrimary();
pollSecondary();
