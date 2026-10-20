/* PM CANVAS — terminal canvas frontend. Zero dependencies, REST polling.
 * Layout: panels are absolutely positioned on #canvas; drag via panel header,
 * resize via bottom-right handle; positions/stacking persist in localStorage.
 * All panel content comes from the project backend /api endpoints.
 */
"use strict";

(function () {
  const POLL_MS = 2000;
  const SOCIAL_POLL_MS = 5000;
  const LAYOUT_KEY = "pm-canvas-layout-v5";
  const TAB_KEY = "pm-canvas-tab-v4";
  const LAYOUT_VERSION = 5;
  const MIN_W = 200;
  const MIN_H = 120;
  const GAP = 16;

  const ageOf = {};
  let layout = {};
  let maxZ = 10;
  let resetRequested = false;
  let activeTab = "bot";
  let activeSocialSource = "reddit";

  /* ---------------- helpers ---------------- */

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function fmtUsd(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    const num = Number(value);
    return (num > 0 ? "+" : "") + num.toFixed(4);
  }

  function fmtMs(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    return Number(value).toFixed(2) + "ms";
  }

  function fmtCount(value) {
    if (value === null || value === undefined) return "—";
    return String(value);
  }

  function timeHMS(iso) {
    if (!iso) return "--:--:--";
    const match = String(iso).match(/T(\d{2}:\d{2}:\d{2})/);
    if (match) return match[1];
    if (String(iso).match(/^\d{2}:\d{2}:\d{2}/)) return String(iso).slice(0, 8);
    return "--:--:--";
  }

  function ageString(endpoint) {
    const last = ageOf[endpoint];
    if (last === undefined) return { text: "OFFLINE", cls: "offline" };
    const seconds = Math.round((Date.now() - last) / 1000);
    if (seconds < 10) return { text: "LIVE", cls: "live" };
    if (seconds < 30) return { text: "STALE " + seconds + "s", cls: "stale" };
    return { text: "OFFLINE " + seconds + "s", cls: "offline" };
  }

  function refreshAgeBadges() {
    Object.keys(ageOf).forEach(function (endpoint) {
      const badge = document.getElementById("age-" + endpoint);
      if (!badge) return;
      const status = ageString(endpoint);
      badge.className = "panel-age " + status.cls;
      badge.textContent = status.text;
    });
  }

  async function fetchJson(url, timeoutMs) {
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = controller && timeoutMs ? setTimeout(function () { controller.abort(); }, timeoutMs) : null;
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller ? controller.signal : undefined });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      const key = url.split("?")[0].replace("/api/", "");
      ageOf[key] = Date.now();
      return data;
    } catch (error) {
      return null;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function poll(endpoint, renderFn) {
    const data = await fetchJson(endpoint);
    renderFn(data);
  }

  /* ---------------- form editing helpers ----------------
   * Both control panels re-render on a 2s poll. While the user is editing,
   * typed values live in `formDirty` and are re-applied on every rebuild, so
   * a poll can never wipe an in-progress edit. `savedFlash` gives instant
   * visual confirmation ("APPLIED ✓") right after a save POST returns.
   */

  const formDirty = {};
  const savedFlash = {};
  const savedFlashText = {};
  const inFlight = {};

  function markDirty(input) {
    if (!input || !input.id) return;
    formDirty[input.id] = input.type === "checkbox" ? input.checked : input.value;
  }

  function clearDirty(ids) {
    ids.forEach(function (id) { delete formDirty[id]; });
  }

  function flashNow(key) { savedFlash[key] = Date.now() + 1800; }

  function isFlashed(key) { return savedFlash[key] > Date.now(); }

  function flashButton(btn, text, ok) {
    if (!btn) return;
    flashNow("btn-" + btn.id);
    savedFlashText[btn.id] = { text: text, ok: !!ok };
  }

  function applyButtonState(btn, baseText) {
    if (!btn) return;
    const pending = savedFlashText[btn.id];
    if (pending && isFlashed("btn-" + btn.id)) {
      btn.textContent = pending.text;
      btn.classList.add(pending.ok ? "flash-ok" : "flash-bad");
    } else {
      btn.textContent = baseText;
      btn.classList.remove("flash-ok", "flash-bad");
      delete savedFlashText[btn.id];
    }
  }

  function setInFlight(btn) {
    if (!btn) return;
    btn.dataset.inflight = "1";
    btn.disabled = true;
    btn.textContent = "…";
  }

  /* ---------------- layout engine ---------------- */

  function canvasSize() {
    const canvas = document.getElementById("canvas");
    return { w: canvas.clientWidth || window.innerWidth, h: canvas.clientHeight || 600 };
  }

  function defaultLayout() {
    const x1 = 16, x2 = 596, x3 = 1176;
    const w = 564;
    // Each tab shows only its own group, so panels from different tabs can
    // safely share coordinates — every tab opens on a clean, uncluttered grid.
    // BOT uses the same 16px-gutter aligned grid as LOOP BOT: full-width rows
    // on top/bottom, paired side-by-side panels, cards in one even row.
    return {
      // BOT
      "panel-mind":       { x: 16,  y: 16,   w: 1748, h: 330, z: 1 },
      "panel-pnl":        { x: 16,  y: 362,  w: 860,  h: 576, z: 2 },
      "panel-botctl":     { x: 892, y: 362,  w: 872,  h: 576, z: 14 },
      "panel-exec":       { x: 16,  y: 954,  w: 278,  h: 130, z: 3 },
      "panel-depth":      { x: 310, y: 954,  w: 278,  h: 130, z: 8 },
      "panel-alerts":     { x: 604, y: 954,  w: 278,  h: 130, z: 4 },
      "panel-models":     { x: 898, y: 954,  w: 278,  h: 130, z: 5 },
      "panel-latency":    { x: 1192, y: 954, w: 278,  h: 130, z: 6 },
      "panel-gates":      { x: 1486, y: 954, w: 278,  h: 130, z: 7 },
      "panel-sentiment":  { x: 16,  y: 1100, w: 1748, h: 304, z: 9 },
      // MARKETS
      "panel-cross":      { x: x1, y: 16, w: w, h: 640, z: 9 },
      "panel-indices":    { x: x2, y: 16, w: w, h: 640, z: 10 },
      "panel-sectorheat": { x: x3, y: 16, w: w, h: 640, z: 11 },
      // NEWS
      "panel-news":       { x: x1, y: 16, w: 1724, h: 900, z: 12 },
      // LOOP BOT
      "panel-loopctl":    { x: x1, y: 16, w: 700, h: 660, z: 16 },
      "panel-loopchart":  { x: 732, y: 16, w: 1032, h: 470, z: 17 },
      "panel-loopevents": { x: 732, y: 502, w: 1032, h: 340, z: 18 },
      // SOCIAL
      "panel-social":     { x: x1, y: 16, w: 1724, h: 900, z: 13 },
      // CHARTS
      "panel-chart":      { x: x1, y: 16, w: 1724, h: 940, z: 15 },
    };
  }

  function clampLayout() {
    const { w, h } = canvasSize();
    Object.keys(layout).forEach(function (id) {
      const box = layout[id];
      box.w = Math.max(MIN_W, Math.min(box.w, w));
      box.h = Math.max(MIN_H, Math.min(box.h, h));
      box.x = Math.max(0, Math.min(box.x, Math.max(0, w - box.w)));
      box.y = Math.max(0, Math.min(box.y, Math.max(0, h - box.h)));
    });
  }

  function applyLayout() {
    const defaults = defaultLayout();
    Object.keys(defaults).forEach(function (id) {
      if (!layout[id]) layout[id] = { ...defaults[id] };
      layout[id].x = layout[id].x === undefined ? defaults[id].x : layout[id].x;
      layout[id].y = layout[id].y === undefined ? defaults[id].y : layout[id].y;
      layout[id].w = layout[id].w === undefined ? defaults[id].w : layout[id].w;
      layout[id].h = layout[id].h === undefined ? defaults[id].h : layout[id].h;
      layout[id].z = layout[id].z === undefined ? defaults[id].z : layout[id].z;
    });
    Object.keys(layout).forEach(function (id) {
      const panel = document.getElementById(id);
      if (!panel) return;
      const box = layout[id];
      panel.style.left = box.x + "px";
      panel.style.top = box.y + "px";
      panel.style.width = box.w + "px";
      panel.style.height = box.h + "px";
      panel.style.zIndex = String(box.z);
      panel.classList.toggle("minimized", !!box.minimized);
      panel.classList.toggle("closed", !!box.closed);
      panel.classList.toggle("maximized", !!box.maximized);
      maxZ = Math.max(maxZ, box.z);
    });
  }

  function loadLayout() {
    try {
      const raw = localStorage.getItem(LAYOUT_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && parsed.version === LAYOUT_VERSION && parsed.panels) {
          layout = parsed.panels;
        }
      }
    } catch (error) {
      layout = {};
    }
    if (!layout || !Object.keys(layout).length) {
      layout = defaultLayout();
      persistLayout();
    }
    clampLayout();
    applyLayout();
    applyTab();
  }

  function persistLayout() {
    // After RESET LAYOUT, the stale in-memory layout must not be written back
    // (the beforeunload handler would otherwise resurrect it during reload).
    if (resetRequested) return;
    try {
      localStorage.setItem(LAYOUT_KEY, JSON.stringify({ version: LAYOUT_VERSION, panels: layout }));
    } catch (error) {
      /* storage unavailable — layout just won't survive a reload */
    }
  }

  /* ---------------- tab groups ---------------- */

  const TAB_HINTS = {
    bot: "bot state · execution · pnl · depth · alerts · models · latency · gates · control",
    loop: "isolated paper loop bot — trades ALL timeframes (5m/15m/30m), own PnL + decision chart",
    markets: "cross-asset regime · global indices · sector heatmap — click any symbol for its chart",
    news: "massive headlines · finnhub world news · X/deitaone feed — auto-refreshing",
    social: "reddit hot posts (live) + X scraper feed — toggle source above, refresh every 5s",
    charts: "search any ticker (finnhub) — daily candles + live quote",
  };

  function groupOf(id) {
    const panel = document.getElementById(id);
    if (panel) return panel.getAttribute("data-group") || "bot";
    if (id === "panel-social") return "social";
    if (id === "panel-chart") return "charts";
    if (id === "panel-loopctl" || id === "panel-loopchart" || id === "panel-loopevents") return "loop";
    if (id === "panel-cross" || id === "panel-indices" || id === "panel-sectorheat") return "markets";
    if (id === "panel-news") return "news";
    if (id === "panel-sentiment") return "bot";
    return "bot";
  }

  function applyTab() {
    try {
      const saved = localStorage.getItem(TAB_KEY);
      if (saved && TAB_HINTS[saved]) activeTab = saved;
    } catch (error) {
      /* ignore */
    }
    document.querySelectorAll(".tab-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-tab") === activeTab);
    });
    Object.keys(layout).forEach(function (id) {
      const panel = document.getElementById(id);
      if (!panel) return;
      panel.classList.toggle("tab-hidden", groupOf(id) !== activeTab);
    });
    const hint = document.getElementById("tab-hint");
    if (hint) hint.textContent = TAB_HINTS[activeTab] || "";
    redrawCharts();
  }

  function setTab(tab) {
    if (!TAB_HINTS[tab]) return;
    activeTab = tab;
    try { localStorage.setItem(TAB_KEY, tab); } catch (error) { /* ignore */ }
    applyTab();
  }

  function wireTabs() {
    document.querySelectorAll(".tab-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setTab(btn.getAttribute("data-tab"));
      });
    });
  }

  function bringToFront(id) {
    maxZ += 1;
    layout[id].z = maxZ;
    const panel = document.getElementById(id);
    if (panel) panel.style.zIndex = String(maxZ);
  }

  function startDrag(event, panelId, mode) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    event.preventDefault();
    bringToFront(panelId);
    const startX = event.clientX;
    const startY = event.clientY;
    const startBox = { x: layout[panelId].x, y: layout[panelId].y, w: layout[panelId].w, h: layout[panelId].h };
    panel.classList.add(mode === "resize" ? "resizing" : "dragging");

    function onMove(moveEvent) {
      const dx = moveEvent.clientX - startX;
      const dy = moveEvent.clientY - startY;
      if (mode === "resize") {
        layout[panelId].w = Math.max(MIN_W, startBox.w + dx);
        layout[panelId].h = Math.max(MIN_H, startBox.h + dy);
      } else {
        layout[panelId].x = startBox.x + dx;
        layout[panelId].y = startBox.y + dy;
      }
      clampLayout();
      applyLayout();
      redrawCharts();
    }

    function onUp() {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      panel.classList.remove("dragging", "resizing");
      persistLayout();
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function wireLayout() {
    document.querySelectorAll("[data-drag]").forEach(function (header) {
      header.addEventListener("pointerdown", function (event) {
        if (event.button !== 0) return;
        if (event.target && event.target.closest && event.target.closest(".pb")) return;
        const panel = header.closest("[data-panel]");
        if (panel) startDrag(event, panel.id, "drag");
      });
    });
    document.querySelectorAll("[data-resize]").forEach(function (handle) {
      handle.addEventListener("pointerdown", function (event) {
        if (event.button !== 0) return;
        const panel = handle.closest("[data-panel]");
        if (panel) startDrag(event, panel.id, "resize");
      });
    });
    document.querySelectorAll(".pb").forEach(function (button) {
      button.addEventListener("pointerdown", function (event) {
        event.preventDefault();
        event.stopPropagation();
        const action = button.getAttribute("data-act");
        const panel = button.closest("[data-panel]");
        if (!panel) return;
        const id = panel.id;
        if (action === "min") {
          layout[id].minimized = !layout[id].minimized;
        } else if (action === "max") {
          if (!layout[id].maximized) {
            layout[id]._prev = { x: layout[id].x, y: layout[id].y, w: layout[id].w, h: layout[id].h };
            const { w, h } = canvasSize();
            layout[id].x = 2;
            layout[id].y = 2;
            layout[id].w = w - 4;
            layout[id].h = h - 4;
            layout[id].maximized = true;
            layout[id].minimized = false;
          } else {
            const prev = layout[id]._prev;
            if (prev) {
              layout[id].x = prev.x;
              layout[id].y = prev.y;
              layout[id].w = prev.w;
              layout[id].h = prev.h;
            }
            layout[id].maximized = false;
          }
        } else if (action === "close") {
          layout[id].closed = true;
        }
        bringToFront(id);
        applyLayout();
        persistLayout();
        redrawCharts();
      });
    });
    document.getElementById("cmd-restore").addEventListener("click", function () {
      Object.keys(layout).forEach(function (id) {
        if (layout[id].closed) layout[id].closed = false;
      });
      applyLayout();
      persistLayout();
    });
    document.getElementById("cmd-reset").addEventListener("click", function () {
      resetRequested = true;
      localStorage.removeItem(LAYOUT_KEY);
      localStorage.removeItem(TAB_KEY);
      location.reload();
    });
    const heatBody = document.getElementById("body-sectorheat");
    if (heatBody) heatBody.addEventListener("click", onChipClick);
    wireTabs();
    wireSocialToolbar();
    wireChartSearch();
    window.addEventListener("resize", function () {
      clampLayout();
      applyLayout();
      redrawCharts();
    });
    window.addEventListener("beforeunload", persistLayout);
  }

  /* ---------------- 01 execution feed ---------------- */

  function renderExec(data) {
    const body = document.getElementById("detail-exec");
    const sum = document.getElementById("sum-exec");
    clear(body);
    if (!data) {
      body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      if (sum) { sum.querySelector(".card-num").textContent = "OFFLINE"; sum.querySelector(".card-label").textContent = "backend unreachable"; }
      return;
    }
    if (data.error) {
      body.appendChild(el("div", "empty", "READER ERROR: " + data.error));
      if (sum) { sum.querySelector(".card-num").textContent = "ERR"; sum.querySelector(".card-label").textContent = "reader error"; }
      return;
    }
    const items = data.items || [];
    if (!items.length) {
      body.appendChild(el("div", "empty", "NO DATA — 0 FILLS / REJECTS (0 OPPORTUNITIES THIS SESSION)"));
      if (sum) { sum.querySelector(".card-num").textContent = "0"; sum.querySelector(".card-label").textContent = "fills / rejects"; }
      return;
    }
    let fills = 0, partials = 0, rejects = 0;
    items.forEach(function (item) {
      const kind = item.kind || "reject";
      if (kind === "fill") fills += 1;
      else if (kind === "partial") partials += 1;
      else rejects += 1;
    });
    if (sum) {
      sum.querySelector(".card-num").textContent = String(fills);
      sum.querySelector(".card-label").textContent = "fills · " + partials + " partial · " + rejects + " rejects";
    }
    const maxEdge = items.reduce(function (max, item) {
      const edge = Math.abs(item.edge === null || item.edge === undefined ? 0 : item.edge);
      return Math.max(max, edge);
    }, 0.001);
    items.forEach(function (item) {
      const kind = item.kind || "reject";
      const edge = Math.abs(item.edge === null || item.edge === undefined ? 0 : item.edge);
      const row = el("div", "exec-line " + kind);
      const block = el("span", "exec-block");
      block.style.opacity = String((0.35 + 0.65 * (edge / maxEdge)).toFixed(2));
      row.appendChild(block);
      const text = el("span", "exec-text");
      text.appendChild(el("span", "exec-time", timeHMS(item.ts_utc) + " "));
      text.appendChild(el("span", "", (kind === "fill" ? "FILLED" : kind === "partial" ? "PARTIAL" : "REJECTED") + " "));
      text.appendChild(el("span", "", String(item.market_id || "?").slice(-20) + " "));
      text.appendChild(el("span", "", "edge=" + (item.edge === null || item.edge === undefined ? "—" : Number(item.edge).toFixed(3)) + " "));
      if (item.pnl !== null && item.pnl !== undefined) text.appendChild(el("span", "", "pnl=" + fmtUsd(item.pnl) + " "));
      if (item.size !== null && item.size !== undefined) text.appendChild(el("span", "", "size=" + fmtCount(item.size) + " "));
      if (item.reason) text.appendChild(el("span", "exec-reason", item.reason));
      row.title = item.ts_utc + " " + (item.market_id || "") + " edge=" + (item.edge || "—") + " pnl=" + fmtUsd(item.pnl) + (item.reason ? " " + item.reason : "");
      row.appendChild(text);
      body.appendChild(row);
    });
    if (data.source) {
      body.appendChild(el("div", "empty", "SOURCE: " + data.source + " (" + items.length + " events)"));
    }
    body.scrollTop = body.scrollHeight;
  }

  /* ---------------- 02 pnl chart ---------------- */

  let lastPnlSeries = [];

  function drawPnl() {
    const canvas = document.getElementById("pnl-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 400;
    const height = canvas.clientHeight || 150;
    if (canvas.width !== width * dpr) { canvas.width = width * dpr; canvas.height = height * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const series = lastPnlSeries.filter(function (point) {
      return point.locked !== null && point.locked !== undefined;
    });
    if (!series.length) return;
    const padL = 44, padR = 8, padT = 8, padB = 14;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;
    const values = [];
    series.forEach(function (p) { values.push(p.locked || 0, p.session || 0, p.residual || 0); });
    let minV = Math.min(0, Math.min.apply(null, values));
    let maxV = Math.max(0, Math.max.apply(null, values));
    if (maxV - minV < 0.0001) maxV = minV + 1;
    const x = function (i) { return series.length === 1 ? padL + plotW / 2 : padL + plotW * (i / (series.length - 1)); };
    const y = function (v) { return padT + plotH * (1 - (v - minV) / (maxV - minV)); };
    ctx.strokeStyle = "#1a1a1a";
    ctx.beginPath(); ctx.moveTo(padL, y(0)); ctx.lineTo(padL + plotW, y(0)); ctx.stroke();
    ctx.fillStyle = "#666";
    ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";
    ctx.textAlign = "right";
    ctx.fillText(maxV.toFixed(2), padL - 4, padT + 8);
    ctx.fillText(minV.toFixed(2), padL - 4, padT + plotH);
    ctx.textAlign = "left";
    ctx.fillText(timeHMS(series[0].ts_utc), padL, height - 3);
    ctx.textAlign = "right";
    ctx.fillText(timeHMS(series[series.length - 1].ts_utc), padL + plotW, height - 3);
    [["locked", "#00ff41"], ["session", "#ffb000"], ["residual", "#ff4136"]].forEach(function (pair) {
      ctx.strokeStyle = pair[1];
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      series.forEach(function (point, i) {
        const v = point[pair[0]];
        if (v === null || v === undefined) return;
        if (i === 0) ctx.moveTo(x(i), y(v)); else ctx.lineTo(x(i), y(v));
      });
      ctx.stroke();
    });
  }

  function renderPnl(data) {
    const readout = document.getElementById("pnl-readout");
    clear(readout);
    if (!data) {
      lastPnlSeries = [];
      drawPnl();
      readout.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      return;
    }
    if (data.error) {
      lastPnlSeries = [];
      drawPnl();
      readout.appendChild(el("div", "empty", "READER ERROR: " + data.error));
      return;
    }
    lastPnlSeries = data.series || [];
    drawPnl();
    const latest = data.latest || {};
    const cells = [
      ["LOCKED", latest.locked],
      ["SESSION", latest.session],
      ["RESIDUAL", latest.residual],
      ["WOULD-LOCK", latest.would_lock],
    ];
    const hasAny = cells.some(function (c) { return c[1] !== null && c[1] !== undefined; });
    if (!hasAny) {
      readout.appendChild(el("div", "empty", "NO DATA — 0 METRIC POINTS (" + (data.notes || "") + ")"));
      return;
    }
    cells.forEach(function (c) {
      const cell = el("div", "pnl-cell");
      cell.appendChild(el("div", "lbl", c[0]));
      const cls = c[1] === null || c[1] === undefined ? "" : (c[1] >= 0 ? "pos" : "neg");
      cell.appendChild(el("div", "val " + cls, fmtUsd(c[1])));
      readout.appendChild(cell);
    });
  }

  /* ---------------- 03 depth ---------------- */

  function renderDepth(data) {
    const body = document.getElementById("detail-depth");
    const sum = document.getElementById("sum-depth");
    clear(body);
    if (!data) {
      body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      if (sum) { sum.querySelector(".card-num").textContent = "OFFLINE"; sum.querySelector(".card-label").textContent = "backend unreachable"; }
      return;
    }
    if (data.error) {
      body.appendChild(el("div", "empty", "READER ERROR: " + data.error));
      if (sum) { sum.querySelector(".card-num").textContent = "ERR"; sum.querySelector(".card-label").textContent = "reader error"; }
      return;
    }
    const continuity = data.continuity || {};
    if (sum) {
      sum.querySelector(".card-num").textContent = fmtCount(continuity.complete) + "/" + fmtCount(continuity.windows);
      sum.querySelector(".card-label").textContent = "windows complete (" + String(Math.round((continuity.ratio || 0) * 1000) / 10) + "%)";
    }
    const breadth = el("div", "breadth");
    const healthy = el("div", "breadth-box ok");
    healthy.appendChild(el("div", "num", fmtCount(continuity.complete)));
    healthy.appendChild(el("div", "lbl", "HEALTHY WINDOWS"));
    breadth.appendChild(healthy);
    const broken = el("div", "breadth-box bad");
    broken.appendChild(el("div", "num", fmtCount(continuity.broken)));
    broken.appendChild(el("div", "lbl", "BROKEN WINDOWS"));
    breadth.appendChild(broken);
    const snaps = el("div", "breadth-box");
    snaps.appendChild(el("div", "num", fmtCount(data.snapshot_count)));
    snaps.appendChild(el("div", "lbl", "SNAPSHOTS"));
    breadth.appendChild(snaps);
    body.appendChild(breadth);

    const bar = el("div", "breadth-bar");
    const okFrac = continuity.ratio === null || continuity.ratio === undefined ? 0 : continuity.ratio;
    const okPart = el("div", "ok", "");
    okPart.style.width = Math.round(okFrac * 1000) / 10 + "%";
    bar.appendChild(okPart);
    const badPart = el("div", "bad", "");
    badPart.style.flex = "1";
    bar.appendChild(badPart);
    body.appendChild(bar);

    const lines = el("div", "depth-lines");
    const line = function (key, value, cls) {
      const wrap = el("div");
      wrap.appendChild(el("span", "k", key + " "));
      wrap.appendChild(el("span", cls || "", value));
      lines.appendChild(wrap);
    };
    line("CONTINUITY", fmtCount(continuity.complete) + "/" + fmtCount(continuity.windows) + " windows (" + String(Math.round(okFrac * 1000) / 10) + "%)", okFrac > 0 ? "v-pos" : "v-neg");
    if (continuity.scope) line("SCOPE", String(continuity.scope));
    if (data.last_30_windows) {
      line("LAST 30", fmtCount(data.last_30_windows.complete) + "/" + fmtCount(data.last_30_windows.windows) + " complete");
    }
    line("SNAPSHOTS", fmtCount(data.snapshot_count) + " (" + (data.snapshot_count_scope || "—") + ")");
    if (data.scanning) line("FULL SCAN", "SCANNING FULL HISTORY…", "v-neg");
    if (data.history && data.history.continuity) {
      const hist = data.history.continuity;
      line("FULL HISTORY", fmtCount(hist.complete) + "/" + fmtCount(hist.windows) + " windows complete");
    }
    const age = data.last_capture_age_s;
    line("LAST CAPTURE", age === null || age === undefined ? "—" : String(Math.round(age)) + "s ago", age !== null && age !== undefined && age < 30 ? "v-pos" : "v-neg");
    const proc = data.process || {};
    line("CAPTURE PROC", (proc.alive ? "ALIVE" : "DEAD") + " pid=" + fmtCount(proc.pid) + " lock=" + (proc.lock_held ? "held" : "missing"), proc.alive ? "v-pos" : "v-neg");
    (data.shards || []).forEach(function (shard) {
      line("SHARD " + shard.name, fmtCount(shard.rows) + " rows · " + (shard.age_s === null || shard.age_s === undefined ? "—" : String(Math.round(shard.age_s)) + "s ago"));
    });
    body.appendChild(lines);
  }

  /* ---------------- 04 alerts ---------------- */

  function renderAlerts(data) {
    const body = document.getElementById("detail-alerts");
    const sum = document.getElementById("sum-alerts");
    clear(body);
    if (!data) {
      body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      if (sum) { sum.querySelector(".card-num").textContent = "OFFLINE"; sum.querySelector(".card-label").textContent = "backend unreachable"; }
      return;
    }
    if (data.error) {
      body.appendChild(el("div", "empty", "READER ERROR: " + data.error));
      if (sum) { sum.querySelector(".card-num").textContent = "ERR"; sum.querySelector(".card-label").textContent = "reader error"; }
      return;
    }
    const items = data.items || [];
    if (!items.length) {
      body.appendChild(el("div", "empty", "NO ALERTS RECORDED"));
      if (sum) { sum.querySelector(".card-num").textContent = "0"; sum.querySelector(".card-label").textContent = "alerts recorded"; }
      return;
    }
    if (sum) {
      const latest = items[0];
      sum.querySelector(".card-num").textContent = String(items.length);
      sum.querySelector(".card-label").textContent = "alerts · latest: " + String(latest.kind || "?").slice(0, 14);
    }
    items.forEach(function (item) {
      const row = el("div", "wire-line");
      row.appendChild(el("span", "wire-time", timeHMS(item.ts_utc) + " "));
      row.appendChild(el("span", "wire-kind " + (item.kind || ""), String(item.kind || "alert").toUpperCase()));
      row.appendChild(el("span", "", " " + (item.message || "")));
      row.title = item.ts_utc + " [" + (item.kind || "") + "] " + (item.message || "") + (item.session_id ? " (" + item.session_id + ")" : "");
      body.appendChild(row);
    });
    body.scrollTop = body.scrollHeight;
  }

  /* ---------------- 05 models ---------------- */

  function renderModels(data) {
    const body = document.getElementById("detail-models");
    const sum = document.getElementById("sum-models");
    clear(body);
    if (!data) {
      body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      if (sum) { sum.querySelector(".card-num").textContent = "OFFLINE"; sum.querySelector(".card-label").textContent = "backend unreachable"; }
      return;
    }
    if (data.error) {
      body.appendChild(el("div", "empty", "PARSER UNAVAILABLE: " + data.error));
      if (sum) { sum.querySelector(".card-num").textContent = "ERR"; sum.querySelector(".card-label").textContent = "parser unavailable"; }
      return;
    }
    const models = data.models || [];
    if (!models.length) {
      body.appendChild(el("div", "empty", "NO MODEL ARTIFACTS FOUND"));
      if (sum) { sum.querySelector(".card-num").textContent = "0"; sum.querySelector(".card-label").textContent = "model artifacts"; }
      return;
    }
    if (sum) {
      let pass = 0;
      models.forEach(function (m) {
        if (/pass|verified/i.test(m.verdict || "")) pass += 1;
      });
      sum.querySelector(".card-num").textContent = String(models.length);
      sum.querySelector(".card-label").textContent = "models · " + pass + " pass";
    }
    const table = el("table", "model-table");
    const head = el("tr");
    head.appendChild(el("th", "", "MODEL"));
    head.appendChild(el("th", "", "VERDICT"));
    head.appendChild(el("th", "", "KEY METRICS"));
    table.appendChild(head);
    models.forEach(function (model) {
      const tr = el("tr");
      tr.appendChild(el("td", "", model.name || model.id));
      const verdictText = model.verdict || "—";
      const verdictClass = /not deployable|fail|superseded|artifact missing/i.test(verdictText)
        ? "verdict-fail" : (/pass|verified/i.test(verdictText) ? "verdict-pass" : "");
      tr.appendChild(el("td", verdictClass, verdictText));
      const metrics = model.metrics || {};
      const metricLines = Object.keys(metrics).slice(0, 6).map(function (key) {
        const value = metrics[key];
        if (value === null || value === undefined) return null;
        const shown = typeof value === "object" ? JSON.stringify(value).slice(0, 60) : String(value);
        return key + "=" + shown;
      }).filter(Boolean);
      const metricCell = el("td", "metric-list", metricLines.join("  ") || "—");
      if (model.source) metricCell.title = model.source;
      tr.appendChild(metricCell);
      table.appendChild(tr);
    });
    body.appendChild(table);
  }

  /* ---------------- 06 latency ---------------- */

  let lastLatencySeries = [];

  function drawLatency() {
    const canvas = document.getElementById("lat-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 300;
    const height = canvas.clientHeight || 80;
    if (canvas.width !== width * dpr) { canvas.width = width * dpr; canvas.height = height * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const points = lastLatencySeries.filter(function (p) { return p.p50_ms !== null && p.p50_ms !== undefined; });
    if (!points.length) return;
    const padL = 6, padR = 6, padT = 6, padB = 12;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;
    let maxV = 1;
    points.forEach(function (p) {
      [p.p50_ms, p.p95_ms, p.p99_ms].forEach(function (v) {
        if (v !== null && v !== undefined) maxV = Math.max(maxV, v);
      });
    });
    const groupW = plotW / points.length;
    const barW = Math.max(1, groupW / 4);
    const colors = ["#ffb000", "#ff7f00", "#ff4136"];
    points.forEach(function (point, i) {
      ["p50_ms", "p95_ms", "p99_ms"].forEach(function (key, j) {
        const v = point[key];
        if (v === null || v === undefined) return;
        ctx.fillStyle = colors[j];
        ctx.fillRect(padL + i * groupW + j * barW, padT + plotH - (v / maxV) * plotH, barW, (v / maxV) * plotH);
      });
    });
    ctx.fillStyle = "#666";
    ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";
    ctx.textAlign = "left";
    ctx.fillText(timeHMS(points[0].ts_utc), padL, height - 2);
    ctx.textAlign = "right";
    ctx.fillText(timeHMS(points[points.length - 1].ts_utc), padL + plotW, height - 2);
    ctx.fillText(maxV.toFixed(0) + "ms", padL + plotW, padT + 8);
  }

  function renderLatency(data) {
    const body = document.getElementById("detail-latency");
    const sum = document.getElementById("sum-latency");
    clear(body);
    if (!data) {
      body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      if (sum) { sum.querySelector(".card-num").textContent = "OFFLINE"; sum.querySelector(".card-label").textContent = "backend unreachable"; }
      return;
    }
    if (data.error) {
      body.appendChild(el("div", "empty", "READER ERROR: " + data.error));
      if (sum) { sum.querySelector(".card-num").textContent = "ERR"; sum.querySelector(".card-label").textContent = "reader error"; }
      return;
    }
    const latest = data.latest || {};
    const hasAny = latest.p50_ms !== null && latest.p50_ms !== undefined || latest.p95_ms !== null && latest.p95_ms !== undefined;
    if (!hasAny) {
      body.appendChild(el("div", "empty", "NO DATA — n=" + fmtCount(latest.n) + " FIRES"));
      if (sum) { sum.querySelector(".card-num").textContent = "—"; sum.querySelector(".card-label").textContent = "no fires yet"; }
      return;
    }
    if (sum) {
      sum.querySelector(".card-num").textContent = fmtMs(latest.p50_ms);
      sum.querySelector(".card-label").textContent = "opp→order p50 · avg " + fmtMs(latest.opp2order_avg_ms);
    }
    const readout = el("div", "lat-readout");
    const cell = function (label, value) {
      const box = el("div", "lat-cell");
      box.appendChild(el("div", "lbl", label));
      box.appendChild(el("div", "val", value));
      readout.appendChild(box);
    };
    cell("N", fmtCount(latest.n));
    cell("AVG", fmtMs(latest.opp2order_avg_ms));
    cell("P50", fmtMs(latest.p50_ms));
    cell("P95", fmtMs(latest.p95_ms));
    cell("P99", fmtMs(latest.p99_ms));
    cell("ACK AVG", fmtMs(latest.ack_avg_ms));
    body.appendChild(readout);
    const wrap = el("div", "lat-chart");
    const canvas = el("canvas");
    canvas.id = "lat-canvas";
    wrap.appendChild(canvas);
    body.appendChild(wrap);
    lastLatencySeries = data.series || [];
    drawLatency();
  }

  /* ---------------- 07 gates ---------------- */

  function renderGates(data) {
    const body = document.getElementById("detail-gates");
    const sum = document.getElementById("sum-gates");
    clear(body);
    if (!data) {
      body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      if (sum) { sum.querySelector(".card-num").textContent = "OFFLINE"; sum.querySelector(".card-label").textContent = "backend unreachable"; }
      return;
    }
    if (data.error) {
      body.appendChild(el("div", "empty", "PARSER UNAVAILABLE: " + data.error));
      if (sum) { sum.querySelector(".card-num").textContent = "ERR"; sum.querySelector(".card-label").textContent = "parser unavailable"; }
      return;
    }
    const gates = data.gates || [];
    if (!gates.length) {
      body.appendChild(el("div", "empty", "NO GATE ROWS PARSED"));
      if (sum) { sum.querySelector(".card-num").textContent = "0"; sum.querySelector(".card-label").textContent = "gates parsed"; }
      return;
    }
    const summary = data.summary || {};
    if (sum) {
      sum.querySelector(".card-num").textContent = fmtCount(summary.verified_pass);
      sum.querySelector(".card-label").textContent = "gates passed · " + fmtCount(summary.verified_fail) + " failed";
    }
    const counts = el("div", "ck-summary");
    const countBox = function (label, key, cls) {
      const box = el("div", "ck-count " + cls);
      box.appendChild(el("span", "num", fmtCount(summary[key])));
      box.appendChild(el("span", "", " " + label));
      counts.appendChild(box);
    };
    countBox("PASS", "verified_pass", "pass");
    countBox("FAIL", "verified_fail", "fail");
    countBox("BLOCKED", "blocked_external", "blocked");
    countBox("UNTESTED", "not_yet_tested", "untested");
    counts.appendChild(el("span", "", "doc: " + (data.doc || "—")));
    body.appendChild(counts);

    const table = el("table", "ck-table");
    const head = el("tr");
    head.appendChild(el("th", "", "#"));
    head.appendChild(el("th", "", "GATE"));
    head.appendChild(el("th", "", "STATUS"));
    head.appendChild(el("th", "", "FINDING"));
    table.appendChild(head);
    gates.forEach(function (gate) {
      const tr = el("tr");
      tr.appendChild(el("td", "", fmtCount(gate.n)));
      tr.appendChild(el("td", "", gate.name || ""));
      const statusCell = el("td");
      statusCell.appendChild(el("span", "ck-badge " + (gate.status || "unknown"), (gate.status || "unknown").toUpperCase()));
      tr.appendChild(statusCell);
      tr.appendChild(el("td", "", gate.finding || ""));
      tr.title = (gate.criterion || "") + " — " + (gate.evidence || "");
      table.appendChild(tr);
    });
    body.appendChild(table);
    if (data.verdict_line) body.appendChild(el("div", "ck-verdict", data.verdict_line));
  }

  /* ---------------- 08 cross-asset ---------------- */

  function renderCross(data) {
    const body = document.getElementById("body-cross");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    if (data.error && !(data.items || []).length) { body.appendChild(el("div", "empty", "READER ERROR: " + data.error)); return; }
    const items = data.items || [];
    if (!items.length) { body.appendChild(el("div", "empty", "NO DATA — LSE VAULT RETURNED NOTHING")); return; }
    const table = el("table", "cross-table");
    const head = el("tr");
    ["SYMBOL", "CLOSE", "DAY %", "CORR vs BTC 30D"].forEach(function (label) { head.appendChild(el("th", "", label)); });
    table.appendChild(head);
    items.forEach(function (item) {
      const tr = el("tr", "clickable-row");
      tr.setAttribute("data-symbol", item.symbol);
      tr.title = "click for chart — " + item.symbol;
      tr.appendChild(el("td", "", item.symbol));
      tr.appendChild(el("td", "", item.close === null || item.close === undefined ? "—" : Number(item.close).toFixed(2)));
      const change = item.change_pct;
      tr.appendChild(el("td", change === null || change === undefined ? "" : (change >= 0 ? "corr-pos" : "corr-neg"),
        change === null || change === undefined ? "—" : (change >= 0 ? "+" : "") + Number(change).toFixed(2) + "%"));
      const corr = item.corr_vs_btc_30d;
      tr.appendChild(el("td", corr === null || corr === undefined ? "corr-nil" : (corr >= 0 ? "corr-pos" : "corr-neg"),
        corr === null || corr === undefined ? "n/a" : Number(corr).toFixed(3)));
      table.appendChild(tr);
    });
    body.appendChild(table);
    if (data.matrix && data.matrix.window) body.appendChild(el("div", "heat-caption", data.matrix.window + " — " + (data.source || "")));
    else if (data.source) body.appendChild(el("div", "heat-caption", data.source));
    (data.errors || []).slice(0, 3).forEach(function (err) {
      body.appendChild(el("div", "empty", "ERR: " + err));
    });
  }

  /* ---------------- 09 indices ---------------- */

  function renderIndices(data) {
    const body = document.getElementById("body-indices");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    const items = data.items || [];
    if (!items.length) {
      body.appendChild(el("div", "empty", (data.error || "NO DATA — MASSIVE RETURNED NOTHING") + (data.quota && data.quota.next_free_in_s ? " — RATE LIMITED, NEXT REFRESH IN " + data.quota.next_free_in_s + "s" : "")));
      return;
    }
    const strip = el("div", "idx-strip");
    items.forEach(function (item) {
      const row = el("div", "idx-row clickable-row");
      row.setAttribute("data-symbol", item.symbol);
      row.title = "click for chart — " + item.symbol;
      row.appendChild(el("span", "idx-sym", item.symbol));
      row.appendChild(el("span", "idx-name", item.name));
      row.appendChild(el("span", "idx-val", item.close === null || item.close === undefined ? "—" : Number(item.close).toFixed(2)));
      const change = item.change_pct;
      row.appendChild(el("span", "idx-chg " + (change === null || change === undefined ? "" : (change >= 0 ? "pos" : "neg")),
        change === null || change === undefined ? "—" : (change >= 0 ? "+" : "") + Number(change).toFixed(2) + "%"));
      row.title = item.ts_utc ? "bar ts " + item.ts_utc : "";
      strip.appendChild(row);
    });
    body.appendChild(strip);
    body.appendChild(el("div", "heat-caption", data.source || ""));
    if (data.quota) {
      const q = data.quota;
      body.appendChild(el("div", "heat-caption",
        "quota: " + q.remaining_this_minute + "/" + q.limit_per_min + " left this minute" + (q.next_free_in_s ? " · next slot " + q.next_free_in_s + "s" : "")));
    }
  }

  /* ---------------- 10 sector heatmap ---------------- */

  let activeSymbol = null;

  function renderSectorHeatmap(data) {
    const body = document.getElementById("body-sectorheat");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    if (data.error && !(data.items || []).length && !(data.sectors || []).length) {
      body.appendChild(el("div", "empty", "READER ERROR: " + data.error));
      return;
    }
    const sectors = data.sectors || [];
    if (!sectors.length) { body.appendChild(el("div", "empty", "NO DATA — LSE VAULT RETURNED NOTHING")); return; }
    const allChanges = [];
    sectors.forEach(function (sector) {
      (sector.items || []).forEach(function (item) {
        if (item.change_pct !== null && item.change_pct !== undefined) {
          allChanges.push(Math.abs(item.change_pct));
        }
      });
    });
    const maxAbs = Math.max.apply(null, allChanges.concat([0.001]));

    sectors.forEach(function (sector) {
      const block = el("div", "sector-block");
      const head = el("div", "sector-head");
      const sectorChange = sector.change_pct;
      head.appendChild(el("span", "sector-name", sector.sector));
      head.appendChild(el("span", "sector-chg " + (sectorChange === null || sectorChange === undefined ? "" : (sectorChange >= 0 ? "pos" : "neg")),
        sectorChange === null || sectorChange === undefined ? "—" : (sectorChange >= 0 ? "+" : "") + Number(sectorChange).toFixed(2) + "%"));
      head.appendChild(el("span", "sector-count", "(" + sector.count + ")"));
      block.appendChild(head);
      const chips = el("div", "sector-chips");
      (sector.items || []).forEach(function (item) {
        const change = item.change_pct === null || item.change_pct === undefined ? 0 : item.change_pct;
        const alpha = 0.25 + 0.75 * (Math.abs(change) / maxAbs);
        const chip = el("div", "stock-chip");
        chip.setAttribute("data-symbol", item.symbol);
        chip.style.background = change >= 0 ? "rgba(0,255,65," + alpha.toFixed(2) + ")" : "rgba(255,65,54," + alpha.toFixed(2) + ")";
        chip.appendChild(el("span", "h-sym", item.symbol));
        chip.appendChild(el("span", "h-chg", (change >= 0 ? "+" : "") + Number(change).toFixed(2) + "%"));
        chip.title = item.symbol + " close " + (item.close === null || item.close === undefined ? "—" : Number(item.close).toFixed(2)) + " · bar " + (item.ts || "—") + " — click for chart";
        if (activeSymbol === item.symbol) chip.classList.add("active");
        chips.appendChild(chip);
      });
      block.appendChild(chips);
      body.appendChild(block);
    });
    body.appendChild(el("div", "heat-caption", data.source || ""));
    (data.errors || []).slice(0, 3).forEach(function (err) {
      body.appendChild(el("div", "empty", "ERR: " + err));
    });
  }

  function openChart(symbol) {
    if (!symbol) return;
    if (activeTab !== "charts") setTab("charts");
    wsLoadChart(symbol, ws.resolution);
  }

  /* ---------------- chart symbol search ---------------- */

  let searchTimer = null;
  let searchSeq = 0;

  function wireChartSearch() {
    const input = document.getElementById("chart-search");
    const go = document.getElementById("chart-go");
    const suggest = document.getElementById("chart-suggest");
    if (!input || !suggest) return;

    function showSuggestions(items) {
      clear(suggest);
      if (!items || !items.length) { suggest.hidden = true; return; }
      items.forEach(function (item) {
        const row = el("div", "suggest-row");
        row.appendChild(el("span", "suggest-sym", item.symbol));
        row.appendChild(el("span", "suggest-desc", (item.description || "") + (item.type ? " · " + item.type : "")));
        row.addEventListener("click", function () {
          suggest.hidden = true;
          openChart(item.symbol);
        });
        suggest.appendChild(row);
      });
      suggest.hidden = false;
    }

    function runSearch() {
      const query = input.value.trim();
      if (!query) { suggest.hidden = true; return; }
      if (/^[A-Z]{1,5}(\/[A-Z]{3})?$/.test(query)) {
        openChart(query);
        return;
      }
      const seq = ++searchSeq;
      fetchJson("/api/finnhub_search?q=" + encodeURIComponent(query), 15000).then(function (data) {
        if (seq !== searchSeq) return;
        showSuggestions(data && !data.error ? data.items || [] : []);
      });
    }

    if (go) go.addEventListener("click", function () {
      const query = input.value.trim();
      if (query) openChart(query);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        const query = input.value.trim();
        if (query) openChart(query);
      }
    });
    input.addEventListener("input", function () {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(runSearch, 350);
    });
    document.addEventListener("click", function (event) {
      if (!event.target.closest("#chart-search") && !event.target.closest("#chart-suggest")) {
        suggest.hidden = true;
      }
    });
  }

  function onChipClick(event) {
    const chip = event.target.closest(".stock-chip");
    if (!chip) return;
    openChart(chip.getAttribute("data-symbol"));
  }

  /* ---------------- 13 chart workspace ---------------- */

  let ws = {
    symbol: null,
    resolution: "15m",
    chartType: "candles",
    inds: { sma: false, ema: true, bb: false, vwap: false, vol: true, rsi: true, macd: false },
    candles: [],
    quote: null,
    news: [],
    watchlist: [],
    theme: "dark",
    viewStart: 0,
    viewEnd: 0,
    xhair: null,
    lastDraw: 0,
  };
  const WS_COLORS = {
    up: "#00c853", down: "#ff5252", dim: "#888", grid: "rgba(255,255,255,0.06)",
    gridL: "rgba(0,0,0,0.10)", text: "#e6e6e6", textL: "#333",
    ma: "#ffb000", bb: "#5aa0ff", vwap: "#b967ff", vol: "rgba(90,160,255,0.55)",
  };
  const WATCHLIST_DEFAULT = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "SPY", "QQQ", "AMD", "JPM", "XOM"];

  function sma(values, period) {
    const out = new Array(values.length).fill(null);
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
      sum += values[i];
      if (i >= period) sum -= values[i - period];
      if (i >= period - 1) out[i] = sum / period;
    }
    return out;
  }

  function ema(values, period) {
    const out = new Array(values.length).fill(null);
    const k = 2 / (period + 1);
    let prev = null;
    for (let i = 0; i < values.length; i++) {
      prev = prev === null ? values[i] : values[i] * k + prev * (1 - k);
      if (i >= period - 1) out[i] = prev;
    }
    return out;
  }

  function bollinger(values, period) {
    const mid = sma(values, period);
    const up = new Array(values.length).fill(null);
    const lo = new Array(values.length).fill(null);
    for (let i = period - 1; i < values.length; i++) {
      let sum = 0;
      for (let j = i - period + 1; j <= i; j++) sum += Math.pow(values[j] - mid[i], 2);
      const sd = Math.sqrt(sum / period);
      up[i] = mid[i] + 2 * sd;
      lo[i] = mid[i] - 2 * sd;
    }
    return { mid, up, lo };
  }

  function vwapSeries(candles) {
    const out = new Array(candles.length).fill(null);
    let cumPV = 0, cumV = 0;
    candles.forEach(function (c, i) {
      const tp = (c.h + c.l + c.c) / 3;
      cumPV += tp * c.v;
      cumV += c.v;
      out[i] = cumV > 0 ? cumPV / cumV : null;
    });
    return out;
  }

  function rsiSeries(values, period) {
    const out = new Array(values.length).fill(null);
    let gain = 0, loss = 0;
    for (let i = 1; i < values.length; i++) {
      const diff = values[i] - values[i - 1];
      const g = diff > 0 ? diff : 0;
      const l = diff < 0 ? -diff : 0;
      if (i <= period) { gain += g; loss += l; }
      else {
        gain = (gain * (period - 1) + g) / period;
        loss = (loss * (period - 1) + l) / period;
      }
      if (i >= period && gain + loss > 0) out[i] = 100 - 100 / (1 + gain / loss);
    }
    return out;
  }

  function macdSeries(values) {
    const e12 = ema(values, 12), e26 = ema(values, 26);
    const line = new Array(values.length).fill(null);
    for (let i = 0; i < values.length; i++) {
      if (e26[i] !== null) line[i] = e12[i] - e26[i];
    }
    const signal = ema(line.map(function (v) { return v === null ? 0 : v; }), 9);
    const hist = new Array(values.length).fill(null);
    for (let i = 0; i < values.length; i++) {
      if (line[i] !== null) hist[i] = line[i] - signal[i];
    }
    return { line, signal, hist };
  }

  function wsQuoteLine() {
    const quoteBox = document.getElementById("chart-quote");
    clear(quoteBox);
    const candles = ws.candles;
    if (!candles.length) {
      quoteBox.appendChild(el("div", "empty", "LOADING…"));
      return;
    }
    const last = candles[candles.length - 1];
    const prev = candles.length > 1 ? candles[candles.length - 2] : last;
    const chg = prev.c > 0 ? (last.c - prev.c) / prev.c * 100 : 0;
    quoteBox.appendChild(el("span", "quote-price " + (chg >= 0 ? "pos" : "neg"), Number(last.c).toFixed(2)));
    quoteBox.appendChild(el("span", "quote-chg " + (chg >= 0 ? "pos" : "neg"), (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%"));
    quoteBox.appendChild(el("span", "quote-meta",
      " o " + Number(last.o).toFixed(2) + " h " + Number(last.h).toFixed(2) +
      " l " + Number(last.l).toFixed(2) + " v " + (last.v > 0 ? Math.round(last.v).toLocaleString() : "—") +
      " · " + ws.symbol + " " + ws.resolution + " · " + (ws.candleSource || "")));
  }

  function wsSetView() {
    const n = ws.candles.length;
    if (!n) { ws.viewStart = 0; ws.viewEnd = 0; return; }
    ws.viewEnd = n;
    ws.viewStart = Math.max(0, n - 120);
  }

  function wsXScale(index, w, padL, padR) {
    const count = ws.viewEnd - ws.viewStart;
    if (count <= 1) return padL + (w - padL - padR) / 2;
    return padL + (index - ws.viewStart) / (count - 1) * (w - padL - padR);
  }

  function drawStockChart() {
    const canvas = document.getElementById("stock-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 800;
    const height = canvas.clientHeight || 320;
    if (canvas.width !== width * dpr) { canvas.width = width * dpr; canvas.height = height * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const candles = ws.candles;
    if (!candles.length) {
      ctx.fillStyle = "#666";
      ctx.font = "11px SFMono-Regular, Menlo, Monaco, monospace";
      ctx.textAlign = "center";
      ctx.fillText("NO DATA — search a symbol above", width / 2, height / 2);
      return;
    }
    const light = ws.theme === "light";
    const col = WS_COLORS;
    const textCol = light ? col.textL : col.text;
    const gridCol = light ? col.gridL : col.grid;
    if (ws.viewStart === 0 && ws.viewEnd === 0) wsSetView();
    const padL = 56, padR = 10, padT = 8, padB = 20;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;
    let minV = Infinity, maxV = -Infinity;
    for (let i = ws.viewStart; i < ws.viewEnd; i++) {
      const c = candles[i];
      minV = Math.min(minV, c.l); maxV = Math.max(maxV, c.h);
    }
    const span = maxV - minV || 1;
    minV -= span * 0.06; maxV += span * 0.06;
    const y = function (v) { return padT + plotH * (1 - (v - minV) / (maxV - minV)); };

    ctx.fillStyle = gridCol;
    for (let g = 0; g <= 4; g++) {
      const gy = padT + plotH * g / 4;
      ctx.fillRect(padL, gy, plotW, 1);
      ctx.fillStyle = textCol;
      ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";
      ctx.textAlign = "right";
      ctx.fillStyle = light ? "#999" : "#666";
      ctx.fillText((maxV - (maxV - minV) * g / 4).toFixed(2), padL - 4, gy + 8);
      ctx.fillStyle = gridCol;
    }
    ctx.fillStyle = light ? "#999" : "#666";
    ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";
    ctx.textAlign = "left";
    if (candles[ws.viewStart] && candles[ws.viewEnd - 1]) {
      const t0 = new Date(candles[ws.viewStart].t * 1000);
      const t1 = new Date(candles[ws.viewEnd - 1].t * 1000);
      ctx.fillText(t0.toISOString().slice(0, 16).replace("T", " "), padL, height - 6);
      ctx.textAlign = "right";
      ctx.fillText(t1.toISOString().slice(0, 16).replace("T", " "), padL + plotW, height - 6);
    }

    const bandW = plotW / Math.max(1, ws.viewEnd - ws.viewStart);
    const bodyW = Math.max(1.5, bandW * 0.62);
    const closes = candles.map(function (c) { return c.c; });

    if (ws.chartType === "candles" || ws.chartType === "bars") {
      for (let i = ws.viewStart; i < ws.viewEnd; i++) {
        const c = candles[i];
        const x = wsXScale(i, width, padL, padR);
        const up = c.c >= c.o;
        ctx.strokeStyle = up ? col.up : col.down;
        ctx.fillStyle = up ? col.up : col.down;
        ctx.beginPath();
        ctx.moveTo(x, y(c.h));
        ctx.lineTo(x, y(c.l));
        ctx.stroke();
        if (ws.chartType === "candles") {
          const yo = y(c.o), yc = y(c.c);
          ctx.fillRect(x - bodyW / 2, Math.min(yo, yc), bodyW, Math.max(1, Math.abs(yo - yc)));
        } else {
          const bw = Math.max(1.5, bodyW * 0.5);
          ctx.fillRect(x - bw / 2, y(c.o), bw, 1.5);
          ctx.fillRect(x - bw / 2, y(c.c), bw, 1.5);
        }
      }
    } else if (ws.chartType === "line" || ws.chartType === "area") {
      ctx.beginPath();
      for (let i = ws.viewStart; i < ws.viewEnd; i++) {
        const x = wsXScale(i, width, padL, padR);
        if (i === ws.viewStart) ctx.moveTo(x, y(candles[i].c));
        else ctx.lineTo(x, y(candles[i].c));
      }
      if (ws.chartType === "area") {
        ctx.lineTo(wsXScale(ws.viewEnd - 1, width, padL, padR), y(minV));
        ctx.lineTo(wsXScale(ws.viewStart, width, padL, padR), y(minV));
        ctx.closePath();
        ctx.globalAlpha = 0.25;
        ctx.fillStyle = candles[candles.length - 1].c >= candles[ws.viewStart].c ? col.up : col.down;
        ctx.fill();
        ctx.globalAlpha = 1;
      }
      ctx.strokeStyle = candles[candles.length - 1].c >= candles[ws.viewStart].c ? col.up : col.down;
      ctx.lineWidth = 1.4;
      ctx.stroke();
    }

    // overlays on price
    if (ws.inds.sma) drawOverlay(ctx, sma(closes, 20), y, width, padL, padR, "#ffb000", "SMA20");
    if (ws.inds.ema) drawOverlay(ctx, ema(closes, 20), y, width, padL, padR, "#00e5ff", "EMA20");
    if (ws.inds.bb) {
      const bb = bollinger(closes, 20);
      drawOverlay(ctx, bb.up, y, width, padL, padR, "rgba(90,160,255,0.55)", "BB up");
      drawOverlay(ctx, bb.lo, y, width, padL, padR, "rgba(90,160,255,0.55)", "BB low");
      drawOverlay(ctx, bb.mid, y, width, padL, padR, "rgba(90,160,255,0.8)", "BB mid");
    }
    if (ws.inds.vwap) drawOverlay(ctx, vwapSeries(candles), y, width, padL, padR, col.vwap, "VWAP");

    // crosshair
    if (ws.xhair) {
      const cx = ws.xhair.x, cy = ws.xhair.y;
      ctx.strokeStyle = "rgba(255,255,255,0.35)";
      ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(cx, padT); ctx.lineTo(cx, padT + plotH); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(padL, cy); ctx.lineTo(padL + plotW, cy); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#111";
      ctx.fillRect(cx - 24, Math.max(padT, cy - 10), 48, 14);
      ctx.fillStyle = "#fff";
      ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";
      ctx.textAlign = "center";
      ctx.fillText((maxV - (cy - padT) / plotH * (maxV - minV)).toFixed(2), cx, Math.max(padT + 9, cy + 1));
    }
  }

  function drawOverlay(ctx, series, y, width, padL, padR, color, label) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    let started = false;
    series.forEach(function (v, i) {
      if (v === null) return;
      const x = wsXScale(i, width, padL, padR);
      if (!started) { ctx.moveTo(x, y(v)); started = true; }
      else ctx.lineTo(x, y(v));
    });
    ctx.stroke();
  }

  function drawSubChart() {
    const canvas = document.getElementById("chart-sub-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 800;
    const height = canvas.clientHeight || 140;
    if (canvas.width !== width * dpr) { canvas.width = width * dpr; canvas.height = height * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const candles = ws.candles;
    if (!candles.length) return;
    const light = ws.theme === "light";
    const textCol = light ? "#999" : "#666";
    const padL = 56, padR = 10, padT = 6, padB = 10;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;
    const closes = candles.map(function (c) { return c.c; });
    let paneH = plotH;
    const panes = [];
    if (ws.inds.vol) panes.push("vol");
    if (ws.inds.rsi) panes.push("rsi");
    if (ws.inds.macd) panes.push("macd");
    if (!panes.length) return;
    paneH = plotH / panes.length;
    ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";

    panes.forEach(function (pane, pi) {
      const py0 = padT + pi * paneH;
      const py1 = padT + (pi + 1) * paneH - 3;
      ctx.fillStyle = light ? "rgba(0,0,0,0.10)" : "rgba(255,255,255,0.06)";
      ctx.fillRect(padL, py0, plotW, 1);
      ctx.fillStyle = textCol;
      ctx.textAlign = "left";
      ctx.fillText(pane.toUpperCase(), padL + 3, py0 + 9);

      if (pane === "vol") {
        let maxV = 0;
        for (let i = ws.viewStart; i < ws.viewEnd; i++) maxV = Math.max(maxV, candles[i].v || 0);
        for (let i = ws.viewStart; i < ws.viewEnd; i++) {
          const c = candles[i];
          const x = wsXScale(i, width, padL, padR);
          const h = maxV > 0 ? (c.v / maxV) * (py1 - py0 - 6) : 0;
          ctx.fillStyle = c.c >= c.o ? "rgba(0,200,83,0.55)" : "rgba(255,82,82,0.55)";
          ctx.fillRect(x - 3, py1 - h, 6, h);
        }
      } else if (pane === "rsi") {
        const rsi = rsiSeries(closes, 14);
        ctx.fillStyle = "rgba(255,255,255,0.12)";
        ctx.fillRect(padL, py0 + (py1 - py0) * 0.7, plotW, 1);   // 70
        ctx.fillRect(padL, py0 + (py1 - py0) * 0.3, plotW, 1);   // 30
        ctx.strokeStyle = "#ffb000";
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        let started = false;
        for (let i = ws.viewStart; i < ws.viewEnd; i++) {
          const v = rsi[i];
          if (v === null) continue;
          const x = wsXScale(i, width, padL, padR);
          const yy = py0 + (py1 - py0) * (1 - v / 100);
          if (!started) { ctx.moveTo(x, yy); started = true; }
          else ctx.lineTo(x, yy);
        }
        ctx.stroke();
        ctx.fillStyle = textCol;
        ctx.textAlign = "right";
        ctx.fillText("70", padL - 4, py0 + (py1 - py0) * 0.7 + 8);
        ctx.fillText("30", padL - 4, py0 + (py1 - py0) * 0.3 + 8);
      } else if (pane === "macd") {
        const m = macdSeries(closes);
        let maxA = 0;
        for (let i = ws.viewStart; i < ws.viewEnd; i++) {
          const h = Math.abs(m.hist[i] || 0) * 2;
          maxA = Math.max(maxA, h);
        }
        maxA = maxA || 1;
        const mid = (py0 + py1) / 2;
        const scale = (py1 - py0 - 8) / 2 / maxA;
        for (let i = ws.viewStart; i < ws.viewEnd; i++) {
          const h = m.hist[i];
          if (h === null || h === undefined) continue;
          const x = wsXScale(i, width, padL, padR);
          ctx.fillStyle = h >= 0 ? "rgba(0,200,83,0.6)" : "rgba(255,82,82,0.6)";
          const hh = Math.max(1, Math.abs(h) * scale);
          ctx.fillRect(x - 2, h >= 0 ? mid - hh : mid, 4, hh);
        }
        drawSubLine(ctx, m.line, mid, scale, padL, padR, width, "#00e5ff");
        drawSubLine(ctx, m.signal, mid, scale, padL, padR, width, "#ffb000");
      }
    });
  }

  function drawSubLine(ctx, series, midY, scale, padL, padR, width, color) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    let started = false;
    series.forEach(function (v, i) {
      if (v === null || v === undefined) return;
      const x = wsXScale(i, width, padL, padR);
      const yy = midY - v * scale;
      if (!started) { ctx.moveTo(x, yy); started = true; }
      else ctx.lineTo(x, yy);
    });
    ctx.stroke();
  }

  function drawCrosshair() {
    const canvas = document.getElementById("chart-xhair");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 800;
    const height = canvas.clientHeight || 320;
    if (canvas.width !== width * dpr) { canvas.width = width * dpr; canvas.height = height * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!ws.xhair) return;
    const readout = document.getElementById("xhair-readout");
    const idx = Math.round((ws.xhair.x - 56) / (canvas.width - 66) * (ws.viewEnd - ws.viewStart - 1) + ws.viewStart);
    const c = ws.candles[idx];
    if (c && readout) {
      readout.hidden = false;
      readout.textContent =
        new Date(c.t * 1000).toISOString().slice(0, 16).replace("T", " ") +
        "  O " + c.o.toFixed(2) + " H " + c.h.toFixed(2) +
        " L " + c.l.toFixed(2) + " C " + c.c.toFixed(2) +
        " V " + (c.v > 0 ? Math.round(c.v).toLocaleString() : "—");
      readout.style.left = Math.min(ws.xhair.x + 10, canvas.width - 380) + "px";
      readout.style.top = "4px";
    }
  }

  function wsRender() {
    drawStockChart();
    drawSubChart();
    drawCrosshair();
  }

  function renderStockChart(symbol, data) {
    ws.symbol = symbol;
    ws.candles = (data && data.items) || [];
    ws.candleSource = (data && data.source) || "";
    const quoteBox = document.getElementById("chart-quote");
    clear(quoteBox);
    const sourceLine = document.getElementById("chart-source");
    if (!data || data.error) {
      quoteBox.appendChild(el("div", "empty", "CHART UNAVAILABLE: " + ((data && data.error) || "backend unreachable")));
      sourceLine.textContent = "";
      return;
    }
    wsSetView();
    wsQuoteLine();
    sourceLine.textContent = "source: " + (data.source || "");
    wsRender();
    const badge = document.getElementById("age-stock_chart");
    if (badge) { badge.className = "panel-age live"; badge.textContent = "LIVE"; }
  }

  function wsLoadChart(symbol, resolution) {
    ws.symbol = symbol;
    ws.resolution = resolution;
    activeSymbol = symbol;
    const title = document.getElementById("chart-title");
    if (title) title.textContent = "13 CHART WORKSPACE — " + symbol;
    const search = document.getElementById("chart-search");
    if (search) search.value = symbol;
    document.querySelectorAll(".stock-chip").forEach(function (c) {
      c.classList.toggle("active", c.getAttribute("data-symbol") === symbol);
    });
    renderWatchlist();
    fetchJson("/api/ohlcv?symbol=" + encodeURIComponent(symbol) + "&resolution=" + encodeURIComponent(resolution), 60000).then(function (data) {
      renderStockChart(symbol, data);
    });
    fetchJson("/api/company_news?symbol=" + encodeURIComponent(symbol)).then(renderCompanyNews);
    document.querySelectorAll("#chart-tf-btns .tf-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-tf") === resolution);
    });
  }

  function renderCompanyNews(data) {
    const head = document.getElementById("chart-news-head");
    const body = document.getElementById("chart-news");
    clear(body);
    if (!data || data.error) {
      if (head) head.textContent = "COMPANY NEWS — " + ((data && data.error) || "unavailable");
      body.appendChild(el("div", "empty", "news unavailable: " + ((data && data.error) || "no data")));
      return;
    }
    const items = data.items || [];
    if (head) head.textContent = "COMPANY NEWS — " + items.length + " headlines · " + (data.source || "finnhub") + " · click to open";
    if (!items.length) { body.appendChild(el("div", "empty", "no news in the last 3 days")); return; }
    items.slice(0, 14).forEach(function (item) {
      const row = el("div", "news-line cnews");
      row.appendChild(el("span", "news-time", (item.ts_utc || "").slice(0, 16).replace("T", " ") + " "));
      row.appendChild(el("span", "news-pub", "[" + item.source + "] "));
      row.appendChild(el("span", "", item.headline));
      row.title = item.summary + (item.url ? " — " + item.url : "");
      if (item.url) {
        row.addEventListener("click", function () { window.open(item.url, "_blank"); });
        row.classList.add("cnews-click");
      }
      body.appendChild(row);
    });
  }

  function renderWatchlist() {
    const list = document.getElementById("chart-watchlist");
    const meta = document.getElementById("chart-wl-meta");
    if (!list) return;
    clear(list);
    const syms = ws.watchlist.length ? ws.watchlist : WATCHLIST_DEFAULT;
    syms.forEach(function (sym) {
      const row = el("div", "wl-row" + (sym === ws.symbol ? " active" : ""));
      row.appendChild(el("span", "wl-sym", sym));
      row.appendChild(el("span", "wl-go", "▸"));
      row.addEventListener("click", function () {
        wsLoadChart(sym, ws.resolution);
      });
      list.appendChild(row);
    });
    if (meta) meta.textContent = "click a symbol to chart it";
  }

  function wireChartWorkspace() {
    document.querySelectorAll("#chart-tf-btns .tf-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (ws.symbol) wsLoadChart(ws.symbol, btn.getAttribute("data-tf"));
      });
    });
    document.querySelectorAll("#chart-type-btns .tf-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        ws.chartType = btn.getAttribute("data-ctype");
        document.querySelectorAll("#chart-type-btns .tf-btn").forEach(function (b) {
          b.classList.toggle("active", b === btn);
        });
        wsRender();
      });
    });
    document.querySelectorAll("#chart-ind-btns .ind-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const key = btn.getAttribute("data-ind");
        ws.inds[key] = !ws.inds[key];
        btn.classList.toggle("active", ws.inds[key]);
        wsRender();
      });
    });
    const themeBtn = document.getElementById("chart-theme");
    if (themeBtn) {
      themeBtn.addEventListener("click", function () {
        ws.theme = ws.theme === "dark" ? "light" : "dark";
        themeBtn.textContent = ws.theme === "dark" ? "LIGHT" : "DARK";
        document.body.classList.toggle("chart-light", ws.theme === "light");
        wsRender();
      });
    }
    const resetBtn = document.getElementById("chart-reset");
    if (resetBtn) {
      resetBtn.addEventListener("click", function () { wsSetView(); wsRender(); });
    }
    const xhair = document.getElementById("chart-xhair");
    if (xhair) {
      xhair.addEventListener("pointermove", function (event) {
        const rect = xhair.getBoundingClientRect();
        ws.xhair = { x: event.clientX - rect.left, y: event.clientY - rect.top };
        drawCrosshair();
        drawStockChart();
      });
      xhair.addEventListener("pointerleave", function () {
        ws.xhair = null;
        const readout = document.getElementById("xhair-readout");
        if (readout) readout.hidden = true;
        drawStockChart();
      });
      xhair.addEventListener("wheel", function (event) {
        event.preventDefault();
        const count = ws.viewEnd - ws.viewStart;
        const factor = event.deltaY > 0 ? 1.12 : 0.89;
        const newCount = Math.max(20, Math.min(ws.candles.length, Math.round(count * factor)));
        const center = (ws.viewStart + ws.viewEnd) / 2;
        ws.viewStart = Math.round(center - newCount / 2);
        ws.viewStart = Math.max(0, Math.min(ws.viewStart, ws.candles.length - newCount));
        ws.viewEnd = ws.viewStart + newCount;
        wsRender();
      }, { passive: false });
    }
  }

  function wsAutoRefresh() {
    if (ws.symbol) {
      fetchJson("/api/ohlcv?symbol=" + encodeURIComponent(ws.symbol) + "&resolution=" + encodeURIComponent(ws.resolution), 60000).then(function (data) {
        if (data && !data.error) {
          ws.candles = data.items || [];
          wsSetView();
          wsQuoteLine();
          wsRender();
        }
      });
    }
  }

  /* ---------------- 11 headlines wire ---------------- */

  function renderNews(data) {
    const body = document.getElementById("body-news");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    const massive = data.massive || {};
    const finnhub = data.finnhub || {};
    const deitaone = data.deitaone || {};
    const massiveItems = massive.items || [];
    const finnhubItems = finnhub.items || [];
    const deitaoneItems = deitaone.items || [];
    if (!massiveItems.length && !finnhubItems.length && !deitaoneItems.length) {
      body.appendChild(el("div", "empty", "NO HEADLINES — " + (massive.error || finnhub.error || deitaone.reason || "sources empty")));
      return;
    }
    body.appendChild(el("div", "news-src", "— MASSIVE.COM V2 NEWS (LIVE) —"));
    if (massive.error) body.appendChild(el("div", "empty", "MASSIVE: " + massive.error));
    massiveItems.slice(0, 12).forEach(function (item) {
      const row = el("div", "news-line");
      row.appendChild(el("span", "news-time", (item.ts_utc || "").slice(0, 16).replace("T", " ") + " "));
      row.appendChild(el("span", "news-pub", "[" + (item.publisher || "?") + "] "));
      row.appendChild(el("span", "", item.title));
      row.title = (item.ts_utc || "") + " [" + (item.publisher || "") + "] " + item.title + (item.url ? " — " + item.url : "");
      body.appendChild(row);
    });
    body.appendChild(el("div", "news-src", "— FINNHUB WORLD NEWS (LIVE) —"));
    if (finnhub.error) body.appendChild(el("div", "empty", "FINNHUB: " + finnhub.error));
    finnhubItems.slice(0, 12).forEach(function (item) {
      const row = el("div", "news-line");
      row.appendChild(el("span", "news-time", (item.ts_utc || "").slice(0, 16).replace("T", " ") + " "));
      row.appendChild(el("span", "news-pub", "[" + (item.publisher || "finnhub") + "] "));
      row.appendChild(el("span", "", item.title));
      row.title = (item.ts_utc || "") + " [" + (item.publisher || "") + "] " + item.title + (item.url ? " — " + item.url : "");
      body.appendChild(row);
    });
    body.appendChild(el("div", "news-src", "— X.COM/DEITAONE (LIVE PROFILE SCRAPE, NO API KEY) —"));
    if (deitaone.status !== "ok") {
      body.appendChild(el("div", "empty", "DEITAONE FEED: " + (deitaone.status || "unknown") + (deitaone.reason ? " — " + deitaone.reason : "")));
    } else {
      deitaoneItems.forEach(function (item) {
        const row = el("div", "news-line");
        row.appendChild(el("span", "news-time", (item.ts_utc || "").slice(0, 16).replace("T", " ") + " "));
        row.appendChild(el("span", "", item.text.split("\n")[0]));
        row.title = (item.ts_utc || "") + " " + item.text + (item.url ? " — " + item.url : "");
        body.appendChild(row);
      });
    }
    if (deitaone.scraped_at_utc) {
      body.appendChild(el("div", "news-src",
        (deitaone.live ? "deitaone scraped live: " : "deitaone last run: ") + deitaone.scraped_at_utc +
        (deitaone.live ? " · x.com profile page" : " (" + (deitaone.source_file || "").split("/").pop() + ")")));
    }
    body.scrollTop = body.scrollHeight;
  }

  /* ---------------- 12 social feed (reddit + X combined) ---------------- */

  let lastSocialData = null;

  function renderSocial(data) {
    lastSocialData = data;
    const body = document.getElementById("body-social");
    const status = document.getElementById("social-status");
    clear(body);
    if (!data) {
      body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      status.textContent = "OFFLINE";
      status.className = "social-status offline";
      return;
    }
    if (data.error) {
      body.appendChild(el("div", "empty", "READER ERROR: " + data.error));
      status.textContent = "ERROR";
      status.className = "social-status offline";
      return;
    }
    const reddit = data.reddit || {};
    const x = data.x || {};
    const refreshed = new Date();
    const stamp = refreshed.toISOString().slice(11, 19) + "Z";
    if (activeSocialSource === "reddit") {
      const mode = reddit.live
        ? (reddit.degraded ? "cached (throttled)" : "reddit.com public JSON + session cookie")
        : "stale scraper-file fallback";
      status.textContent = "REDDIT · refreshed " + stamp + " · " + mode;
      status.className = "social-status " + (reddit.live ? (reddit.degraded ? "warn" : "live") : "warn");
      if (!reddit.items || !reddit.items.length) {
        body.appendChild(el("div", "empty", "NO REDDIT POSTS — " + (reddit.error || "sources empty")));
        return;
      }
      if (reddit.degraded) {
        body.appendChild(el("div", "news-src", "reddit throttled — showing the last live snapshot"));
      }
      if (!reddit.live) {
        const badge = el("span", "stale-badge", "LIVE SCRAPE UNREACHABLE — SHOWING LAST SCRAPER RUN");
        body.appendChild(el("div", "news-src", "").appendChild(badge));
        if (reddit.last_run_utc) {
          body.appendChild(el("div", "news-src", "last run: " + reddit.last_run_utc + " · " + (reddit.source_file || "").split("/").pop()));
        }
      }
      reddit.items.forEach(function (item) {
        const row = el("div", "rdt-line");
        row.appendChild(el("span", "rdt-sub", item.subreddit));
        row.appendChild(el("span", "rdt-score", String(item.score)));
        row.appendChild(el("span", "", " " + item.title));
        row.title = item.title + " — r/" + item.subreddit + " · score " + item.score + " · comments " + (item.num_comments || 0);
        body.appendChild(row);
      });
      body.appendChild(el("div", "news-src", "top " + reddit.items.length + " hot posts — " + (reddit.source || "scraper file")));
    } else {
      status.textContent = "X/DEITAONE · refreshed " + stamp + (x.live ? " · x.com profile scrape (no API key)" : " · stale file");
      status.className = "social-status " + (x.status === "ok" && x.live ? "live" : "warn");
      if (!x.items || !x.items.length) {
        body.appendChild(el("div", "empty", "NO X POSTS — " + (x.reason || "profile scrape empty")));
        return;
      }
      if (x.status !== "ok") {
        body.appendChild(el("div", "empty", "DEITAONE FEED: " + (x.status || "unknown") + (x.reason ? " — " + x.reason : "")));
      }
      if (x.scraped_at_utc) {
        const badge = el("span", "stale-badge", x.live ? "SCRAPED LIVE: " + x.scraped_at_utc : "LAST SCRAPED: " + x.scraped_at_utc);
        body.appendChild(el("div", "news-src", "").appendChild(badge));
        if (!x.live) {
          body.appendChild(el("div", "news-src", "live x.com scrape failed — showing the stale scraper file"));
        }
      }
      x.items.forEach(function (item) {
        const row = el("div", "news-line");
        row.appendChild(el("span", "news-time", (item.ts_utc || "").slice(0, 16).replace("T", " ") + " "));
        row.appendChild(el("span", "", item.text.split("\n")[0]));
        row.title = (item.ts_utc || "") + " " + item.text + (item.url ? " — " + item.url : "");
        body.appendChild(row);
      });
    }
    body.scrollTop = body.scrollHeight;
  }

  function wireSocialToolbar() {
    const redditBtn = document.getElementById("social-src-reddit");
    const xBtn = document.getElementById("social-src-x");
    const refreshBtn = document.getElementById("social-refresh");
    if (redditBtn) {
      redditBtn.addEventListener("click", function () {
        activeSocialSource = "reddit";
        redditBtn.classList.add("active");
        if (xBtn) xBtn.classList.remove("active");
        if (lastSocialData) renderSocial(lastSocialData);
      });
    }
    if (xBtn) {
      xBtn.addEventListener("click", function () {
        activeSocialSource = "x";
        xBtn.classList.add("active");
        if (redditBtn) redditBtn.classList.remove("active");
        if (lastSocialData) renderSocial(lastSocialData);
      });
    }
    if (refreshBtn) {
      refreshBtn.addEventListener("click", function () {
        refreshBtn.disabled = true;
        fetchJson("/api/social").then(function (data) {
          renderSocial(data);
          refreshBtn.disabled = false;
        });
      });
    }
  }

  /* ---------------- globe background ---------------- */

  let globeBits = null;
  let globeRotation = 0;
  let globeRunning = true;

  function initGlobe() {
    const mask = window.LANDMASK;
    if (!mask) return;
    const hex = mask.hex;
    const count = mask.width * mask.height;
    globeBits = new Uint8Array(count);
    for (let i = 0; i < count; i++) {
      const byte = parseInt(hex.substr(Math.floor(i / 8) * 2, 2), 16);
      globeBits[i] = (byte >> (7 - (i % 8))) & 1;
    }
  }

  function drawGlobe() {
    if (!globeRunning) return;
    const canvas = document.getElementById("globe-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const width = window.innerWidth;
    const height = window.innerHeight;
    if (canvas.width !== width * dpr) { canvas.width = width * dpr; canvas.height = height * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!globeBits) return;
    const mask = window.LANDMASK;
    const radius = Math.min(width, height) * 0.33;
    const cx = width / 2;
    const cy = height * 0.42;
    const stepDeg = 2;
    const latStep = (180 / mask.height) * (stepDeg / 2);
    const lngStep = (360 / mask.width) * (stepDeg / 2);
    globeRotation += 0.0012;
    const theta = globeRotation;
    const cosT = Math.cos(theta);
    const sinT = Math.sin(theta);

    ctx.lineWidth = 0.6;
    // graticule: meridians every 30 degrees
    for (let lng = -180; lng < 180; lng += 30) {
      ctx.beginPath();
      let started = false;
      for (let lat = -90; lat <= 90; lat += 4) {
        const la = lat * Math.PI / 180;
        const lo = lng * Math.PI / 180;
        const x = Math.cos(la) * Math.sin(lo);
        const z = Math.cos(la) * Math.cos(lo);
        const y = Math.sin(la);
        const rx = x * cosT - z * sinT;
        const rz = x * sinT + z * cosT;
        if (rz > 0.02) {
          const sx = cx + radius * rx;
          const sy = cy - radius * y;
          if (!started) { ctx.moveTo(sx, sy); started = true; }
          else ctx.lineTo(sx, sy);
        } else {
          started = false;
        }
      }
      ctx.strokeStyle = "rgba(80,140,240,0.20)";
      ctx.stroke();
    }
    // parallels every 30 degrees
    for (let lat = -60; lat <= 60; lat += 30) {
      ctx.beginPath();
      let started = false;
      for (let lng = -180; lng <= 180; lng += 4) {
        const la = lat * Math.PI / 180;
        const lo = lng * Math.PI / 180;
        const x = Math.cos(la) * Math.sin(lo);
        const z = Math.cos(la) * Math.cos(lo);
        const y = Math.sin(la);
        const rx = x * cosT - z * sinT;
        const rz = x * sinT + z * cosT;
        if (rz > 0.02) {
          const sx = cx + radius * rx;
          const sy = cy - radius * y;
          if (!started) { ctx.moveTo(sx, sy); started = true; }
          else ctx.lineTo(sx, sy);
        } else {
          started = false;
        }
      }
      ctx.strokeStyle = "rgba(80,140,240,0.12)";
      ctx.stroke();
    }
    // land dots
    for (let row = 0; row < mask.height; row += 1) {
      const lat = 90 - (row + 0.5) * (180 / mask.height);
      const la = lat * Math.PI / 180;
      const cosLa = Math.cos(la);
      const y = Math.sin(la);
      for (let col = 0; col < mask.width; col += 1) {
        if (!globeBits[row * mask.width + col]) continue;
        const lng = -180 + (col + 0.5) * (360 / mask.width);
        const lo = lng * Math.PI / 180;
        const x = cosLa * Math.sin(lo);
        const z = cosLa * Math.cos(lo);
        const rx = x * cosT - z * sinT;
        const rz = x * sinT + z * cosT;
        if (rz <= 0.02) continue;
        const sx = cx + radius * rx;
        const sy = cy - radius * y;
        const alpha = 0.35 + 0.65 * rz;
        ctx.fillStyle = "rgba(90,150,255," + alpha.toFixed(2) + ")";
        ctx.fillRect(sx, sy, 1.6, 1.6);
      }
    }
    requestAnimationFrame(drawGlobe);
  }

  function startGlobe() {
    initGlobe();
    document.addEventListener("visibilitychange", function () {
      globeRunning = !document.hidden;
      if (globeRunning) requestAnimationFrame(drawGlobe);
    });
    requestAnimationFrame(drawGlobe);
  }

  /* ---------------- 00 bot mind ---------------- */

  function renderMind(data) {
    const body = document.getElementById("mind-main");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    if (data.error) { body.appendChild(el("div", "empty", "ERROR: " + data.error)); return; }

    const managed = data.managed || {};
    const running = !!managed.managed_running;
    const statusRow = el("div", "mind-status");
    statusRow.appendChild(el("span", "mind-state " + (running ? "run" : "stop"), running ? "BOT RUNNING" : "BOT STOPPED"));
    if (managed.pid !== null && managed.pid !== undefined) statusRow.appendChild(el("span", "mind-meta", " pid=" + managed.pid));
    if (managed.session_id) statusRow.appendChild(el("span", "mind-meta", " " + managed.session_id));
    if (managed.uptime_s !== null && managed.uptime_s !== undefined) statusRow.appendChild(el("span", "mind-meta", " up " + Math.round(managed.uptime_s) + "s"));
    if (managed.fill_source) statusRow.appendChild(el("span", "mind-meta", " fill=" + managed.fill_source));
    if (data.kill_switch === "halted") statusRow.appendChild(el("span", "mind-halt", "KILL SWITCH HALTED"));
    if (managed.emergency_stop) statusRow.appendChild(el("span", "mind-halt", "EMERGENCY STOP FILE"));
    body.appendChild(statusRow);

    const verdict = data.verdict || {};
    const verdictLine = el("div", "mind-verdict v-" + (verdict.level || "idle"), verdict.text || "—");
    body.appendChild(verdictLine);

    const focus = data.focus || {};
    const focusLine = el("div", "mind-focus");
    const focusParts = [];
    if (focus.window) focusParts.push("watching " + focus.window);
    if (focus.combined !== null && focus.combined !== undefined) focusParts.push("book combined " + focus.combined);
    if (focus.macro_high !== null && focus.macro_high !== undefined) focusParts.push(focus.macro_high + " high-risk macro windows");
    if (focus.close_epoch) {
      const secondsLeft = focus.close_epoch - Math.floor(Date.now() / 1000);
      if (secondsLeft > 0 && secondsLeft < 3600) focusParts.push("window closes in " + secondsLeft + "s");
    }
    focusLine.appendChild(el("span", "mind-focus-text", focusParts.length ? focusParts.join(" · ") : "focus: waiting for window data"));
    if (data.source) focusLine.appendChild(el("span", "mind-meta", "  [" + data.source + "]"));
    body.appendChild(focusLine);

    const regime = data.regime;
    if (regime) {
      const regimeLine = el("div", "mind-focus");
      regimeLine.appendChild(el("span", "mind-focus-text",
        "regime " + regime.regime + " (" + regime.source + ", conf " + Number(regime.confidence).toFixed(2) + ")"
        + " → no_entry=" + regime.no_entry_seconds + "s max_quote_age=" + regime.max_quote_age_ms + "ms"));
      body.appendChild(regimeLine);
    }

    const funnelData = data.funnel || {};
    if (funnelData.available) {
      const funnelRow = el("div", "mind-funnel");
      const segments = [
        ["opps", funnelData.opps, "f-opps", "OPPS"],
        ["fires", funnelData.fires, "f-fires", "FIRED"],
        ["fills", funnelData.fills, "f-fills", "CLEAN"],
        ["partial", funnelData.partial, "f-partial", "PARTIAL"],
      ];
      const total = Math.max(1, funnelData.opps || 0);
      segments.forEach(function (seg) {
        const box = el("div", "mind-seg " + seg[2]);
        box.style.width = Math.max(2, Math.round(100 * (seg[1] || 0) / total)) + "%";
        box.title = seg[3] + ": " + seg[1];
        box.appendChild(el("span", "", seg[3] + " " + seg[1]));
        funnelRow.appendChild(box);
      });
      body.appendChild(funnelRow);
      const stats = el("div", "mind-stats",
        "fired " + fmtCount(funnelData.fires) + "/" + fmtCount(funnelData.opps) +
        " opps (" + (funnelData.fire_rate_pct === null || funnelData.fire_rate_pct === undefined ? "—" : funnelData.fire_rate_pct + "%") + ")" +
        " · clean " + fmtCount(funnelData.fills) + " (" + (funnelData.clean_rate_pct === null || funnelData.clean_rate_pct === undefined ? "—" : funnelData.clean_rate_pct + "%") + ")" +
        " · partial " + fmtCount(funnelData.partial) +
        " · resolved " + fmtCount(funnelData.resolved) +
        " · pnl " + fmtUsd(funnelData.session_pnl));
      body.appendChild(stats);
    }

    const thoughtsBox = el("div", "mind-thoughts");
    const thoughtList = data.thoughts || [];
    if (!thoughtList.length) {
      thoughtsBox.appendChild(el("div", "empty", "no thoughts yet — start the bot from panel 14"));
    } else {
      thoughtList.slice(0, 14).forEach(function (thought) {
        const line = el("div", "mind-thought t-" + (thought.tag || ""));
        line.appendChild(el("span", "mind-tag", "[" + thought.tag + "] "));
        line.appendChild(el("span", "", thought.text));
        thoughtsBox.appendChild(line);
      });
    }
    body.appendChild(thoughtsBox);
  }

  /* ---------------- 14 bot control ---------------- */

  /* ---------------- 00b multi-timeframe window grid ---------------- */

  function renderWindows(data) {
    const box = document.getElementById("mind-windows");
    if (!box) return;
    clear(box);
    if (!data || data.error) {
      box.appendChild(el("div", "empty", "window grid: " + ((data && data.error) || "backend unreachable")));
      return;
    }
    const tfs = data.timeframes || {};
    const table = el("table", "loop-table mind-tf-table");
    const head = el("tr");
    ["TF", "WINDOW", "YES", "NO", "COMBINED", "BEST", "POSITION"].forEach(function (label) {
      head.appendChild(el("th", "", label));
    });
    table.appendChild(head);
    Object.keys(tfs).forEach(function (tf) {
      const st = tfs[tf] || {};
      const tr = el("tr");
      const tfCell = el("td", "loop-tf");
      tfCell.style.color = TF_COLORS[tf] || "#fff";
      tfCell.textContent = tf;
      tr.appendChild(tfCell);
      tr.appendChild(el("td", "", st.slug ? String(st.slug).slice(-12) : "resolving…"));
      tr.appendChild(el("td", "", st.yes === null || st.yes === undefined ? "—" : Number(st.yes).toFixed(3)));
      tr.appendChild(el("td", "", st.no === null || st.no === undefined ? "—" : Number(st.no).toFixed(3)));
      const combined = st.combined;
      const combinedCell = el("td");
      combinedCell.textContent = combined === null || combined === undefined ? "—" : Number(combined).toFixed(3);
      if (combined !== null && combined !== undefined) {
        combinedCell.className = combined < 0.97 ? "corr-pos" : "corr-neg";
      }
      tr.appendChild(combinedCell);
      tr.appendChild(el("td", "loop-dim", st.best_combined === null || st.best_combined === undefined ? "—" : Number(st.best_combined).toFixed(3)));
      const posCell = el("td");
      if (st.position && st.position !== "—") {
        posCell.appendChild(el("span", "loop-pos", st.position));
      } else {
        posCell.appendChild(el("span", "loop-dim", "—"));
      }
      tr.appendChild(posCell);
      table.appendChild(tr);
    });
    box.appendChild(table);
    box.appendChild(el("div", "heat-caption", "live public books · edge threshold YES+NO < 0.97 (3% gross) · " + (data.fetched_at_utc || "")));
  }

  /* ---------------- cutout cards + detail modal ---------------- */

  const CARD_MODAL = {
    "panel-exec": { title: "01 EXECUTION FEED — DETAIL", detail: "detail-exec" },
    "panel-depth": { title: "03 DEPTH CAPTURE HEALTH — DETAIL", detail: "detail-depth" },
    "panel-alerts": { title: "04 ALERTS WIRE — DETAIL", detail: "detail-alerts" },
    "panel-models": { title: "05 MODEL STATUS — DETAIL", detail: "detail-models" },
    "panel-latency": { title: "06 LATENCY — DETAIL", detail: "detail-latency" },
    "panel-gates": { title: "07 GO-LIVE GATES — DETAIL", detail: "detail-gates" },
  };
  let modalOpenPanel = null;
  let modalHomeParent = null;

  function openCardModal(panelId) {
    const spec = CARD_MODAL[panelId];
    if (!spec) return;
    const detail = document.getElementById(spec.detail);
    const modal = document.getElementById("detail-modal");
    const modalBody = document.getElementById("modal-body");
    const title = document.getElementById("modal-title");
    if (!detail || !modal || !modalBody) return;
    if (modalOpenPanel) closeCardModal();
    modalHomeParent = detail.parentNode;
    modalBody.appendChild(detail);
    title.textContent = spec.title;
    modal.hidden = false;
    modalOpenPanel = panelId;
    const owner = document.getElementById(panelId);
    if (owner) owner.classList.add("card-open");
  }

  function closeCardModal() {
    const modal = document.getElementById("detail-modal");
    if (!modal) return;
    if (modalOpenPanel && modalHomeParent) {
      const spec = CARD_MODAL[modalOpenPanel];
      const detail = spec ? document.getElementById(spec.detail) : null;
      if (detail && modalHomeParent) modalHomeParent.appendChild(detail);
      const owner = document.getElementById(modalOpenPanel);
      if (owner) owner.classList.remove("card-open");
    }
    modal.hidden = true;
    modalOpenPanel = null;
    modalHomeParent = null;
    redrawCharts();
  }

  function wireCardModals() {
    Object.keys(CARD_MODAL).forEach(function (panelId) {
      const panel = document.getElementById(panelId);
      if (!panel) return;
      const body = panel.querySelector(".card-body");
      if (body) body.addEventListener("click", function (event) {
        if (event.target && event.target.closest && event.target.closest(".pb")) return;
        openCardModal(panelId);
      });
    });
    const closeBtn = document.getElementById("modal-close");
    if (closeBtn) closeBtn.addEventListener("click", closeCardModal);
    const modal = document.getElementById("detail-modal");
    if (modal) {
      modal.addEventListener("click", function (event) {
        if (event.target === modal) closeCardModal();
      });
    }
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && modalOpenPanel) closeCardModal();
    });
  }

  /* ---------------- 08 btc sentiment gauge ---------------- */

  function renderSentiment(data) {
    const body = document.getElementById("body-sentiment");
    clear(body);
    if (!data || data.error) {
      body.appendChild(el("div", "empty", "SENTIMENT UNAVAILABLE: " + ((data && data.error) || "backend unreachable")));
      return;
    }
    const score = data.score;
    const wrap = el("div", "sentiment-wrap");
    const gauge = el("div", "sentiment-gauge");
    const fill = el("div", "sentiment-fill");
    fill.style.width = Math.round(score) + "%";
    gauge.appendChild(fill);
    const marker = el("div", "sentiment-marker");
    marker.style.left = Math.round(score) + "%";
    gauge.appendChild(marker);
    wrap.appendChild(gauge);
    const labels = el("div", "sentiment-labels");
    labels.appendChild(el("span", "s-label", "0 EXTREME FEAR"));
    labels.appendChild(el("span", "s-label mid", "50 NEUTRAL"));
    labels.appendChild(el("span", "s-label end", "100 EXTREME GREED"));
    wrap.appendChild(labels);
    const readout = el("div", "sentiment-readout");
    const value = el("span", "sentiment-value", String(score));
    const label = el("span", "sentiment-label", data.label || "—");
    readout.appendChild(value);
    readout.appendChild(label);
    if (data.price !== null && data.price !== undefined) {
      readout.appendChild(el("span", "sentiment-meta", "BTC " + Number(data.price).toLocaleString(undefined, { maximumFractionDigits: 0 })));
    }
    wrap.appendChild(readout);
    const comps = data.components || {};
    const compLine = el("div", "sentiment-meta");
    compLine.textContent =
      "momentum(30d) " + comps.momentum_30d_pct + "% · SMA50 dist " + comps.sma50_dist_pct +
      "% · realized vol " + comps.realized_vol_ann_pct + "% ann";
    wrap.appendChild(compLine);
    const srcLine = el("div", "sentiment-meta src");
    srcLine.textContent = (data.source || "") + " · updated " + (data.updated_at_utc || "—");
    wrap.appendChild(srcLine);
    body.appendChild(wrap);
  }

  let lastBotctlSig = null;

  function renderBotctl(data) {
    const body = document.getElementById("body-botctl");
    if (!data) {
      clear(body);
      body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      lastBotctlSig = null;
      return;
    }
    if (data.error) {
      clear(body);
      body.appendChild(el("div", "empty", "ERROR: " + data.error));
      lastBotctlSig = null;
      return;
    }

    const running = !!data.managed_running;
    const external = (data.external_paper_bots || []).length > 0;
    const risk = data.risk || {};
    // Rebuild only when the interactive shell changes (running state, saved
    // risk values, emergency flag) — never on the 2s poll — so typed values
    // and mid-click buttons are never swallowed by a re-render.
    const sig = JSON.stringify({
      running: running, external: external, emergency: !!data.emergency_stop,
      risk: Object.keys(risk).sort().map(function (k) { return k + "=" + risk[k]; }).join(","),
    });
    if (sig !== lastBotctlSig) {
      lastBotctlSig = sig;
      clear(body);

      const badge = el("div", "bot-badge paper", "PAPER TRADE ONLY — REAL ORDERS: 0");
      body.appendChild(badge);

      const statusRow = el("div", "bot-status", "");
      statusRow.id = "bot-status-row";
      body.appendChild(statusRow);

      const warnRow = el("div", "bot-warn", "EMERGENCY STOP FILE ACTIVE — state/EMERGENCY_STOP exists");
      warnRow.id = "bot-warn-row";
      body.appendChild(warnRow);

      const metricsRow = el("div", "bot-metrics", "");
      metricsRow.id = "bot-metrics-row";
      body.appendChild(metricsRow);

      const form = el("div", "risk-form");
      const fields = [
        ["order_size", "order size"], ["min_edge", "min edge"], ["max_position_per_market", "max pos/market"],
        ["max_total_capital", "max total capital"], ["daily_kill_switch_usd", "daily kill switch $"],
        ["no_entry_seconds", "no-entry s"], ["max_quote_age_ms", "quote age ms"],
      ];
      fields.forEach(function (pair) {
        const key = pair[0];
        const wrap = el("label", "risk-field");
        wrap.appendChild(el("span", "risk-lbl", pair[1]));
        const input = el("input", "risk-input");
        input.type = "number";
        input.id = "risk-" + key;
        input.step = "any";
        input.value = formDirty["risk-" + key] !== undefined ? formDirty["risk-" + key] :
          (risk[key] === undefined ? "" : risk[key]);
        wrap.appendChild(input);
        form.appendChild(wrap);
      });
      body.appendChild(form);

      const controls = el("div", "bot-controls");
      const duration = el("select", "bot-select");
      duration.id = "bot-duration";
      [15, 30, 60].forEach(function (minutes) {
        const option = el("option", "", minutes + " min");
        option.value = String(minutes);
        duration.appendChild(option);
      });
      const fillSource = el("select", "bot-select");
      fillSource.id = "bot-fill-source";
      [["rest_book", "rest_book (live book fills)"], ["decision_snapshot", "decision_snapshot (mechanical)"]].forEach(function (pair) {
        const option = el("option", "", pair[1]);
        option.value = pair[0];
        fillSource.appendChild(option);
      });
      controls.appendChild(duration);
      controls.appendChild(fillSource);
      const startBtn = el("button", "bot-btn start", "START BOT");
      startBtn.id = "bot-start";
      startBtn.disabled = running || external;
      startBtn.title = external ? "an external paper bot is already running" : "";
      controls.appendChild(startBtn);
      const stopBtn = el("button", "bot-btn stop", "STOP + EMERGENCY");
      stopBtn.id = "bot-stop";
      stopBtn.disabled = !running || isFlashed("btn-bot-stop");
      controls.appendChild(stopBtn);
      const saveBtn = el("button", "bot-btn save", "SAVE RISK");
      saveBtn.id = "bot-save-risk";
      applyButtonState(saveBtn, "SAVE RISK");
      controls.appendChild(saveBtn);
      body.appendChild(controls);
      body.appendChild(el("div", "bot-note", running
        ? "running session keeps its startup values — SAVE RISK stages changes for the next START"
        : "SAVE RISK stages values for the next session start (PAPER_TRADE only; kill switch, alerts and archives all active)"));
      const logWrap = el("div", "bot-log-wrap");
      logWrap.appendChild(el("div", "bot-log", ""));
      logWrap.firstChild.id = "bot-log";
      body.appendChild(logWrap);
    }

    // dynamic in-place updates (every poll, no DOM rebuild)
    const statusRow = document.getElementById("bot-status-row");
    if (statusRow) {
      clear(statusRow);
      statusRow.appendChild(el("span", "bot-state " + (running ? "run" : (external ? "ext" : "stop")),
        running ? "RUNNING" : (external ? "EXTERNAL BOT" : "STOPPED")));
      if (data.pid !== null && data.pid !== undefined) statusRow.appendChild(el("span", "", " pid=" + data.pid));
      if (data.uptime_s !== null && data.uptime_s !== undefined) statusRow.appendChild(el("span", "", " up=" + Math.round(data.uptime_s) + "s"));
      if (data.session_id) statusRow.appendChild(el("span", "bot-meta", " " + data.session_id));
    }
    const warnRow = document.getElementById("bot-warn-row");
    if (warnRow) warnRow.style.display = data.emergency_stop ? "" : "none";
    const metricsRow = document.getElementById("bot-metrics-row");
    if (metricsRow) {
      clear(metricsRow);
      const metrics = data.metrics || {};
      if (Object.keys(metrics).length) {
        metricsRow.appendChild(el("div", "", "loops " + fmtCount(metrics.loops) + " · opps " + fmtCount(metrics.opps) +
          " · sent " + fmtCount(metrics.sent) + " · fills " + fmtCount(metrics.fills) +
          " · partial " + fmtCount(metrics.partial) + " · missed " + fmtCount(metrics.missed)));
        metricsRow.appendChild(el("div", "bot-pnl", "session pnl " + fmtUsd(metrics.session_pnl) +
          " (locked " + fmtUsd(metrics.locked_pnl) + " / residual " + fmtUsd(metrics.residual_pnl) + ")"));
      }
    }
    const startBtn = document.getElementById("bot-start");
    if (startBtn) startBtn.disabled = running || external;
    const stopBtn = document.getElementById("bot-stop");
    if (stopBtn) stopBtn.disabled = !running || isFlashed("btn-bot-stop");
    const saveBtn = document.getElementById("bot-save-risk");
    if (saveBtn) applyButtonState(saveBtn, "SAVE RISK");
  }

  function renderBotLog(data) {
    const logBox = document.getElementById("bot-log");
    if (!logBox) return;
    clear(logBox);
    if (!data || !data.lines || !data.lines.length) {
      logBox.appendChild(el("div", "empty", data && data.note ? data.note : "NO MANAGED SESSION LOG YET"));
      return;
    }
    data.lines.slice(-10).forEach(function (line) {
      logBox.appendChild(el("div", "bot-log-line", String(line).slice(0, 200)));
    });
    logBox.scrollTop = logBox.scrollHeight;
  }

  function collectRiskForm() {
    const risk = {};
    ["order_size", "min_edge", "max_position_per_market", "max_total_capital", "daily_kill_switch_usd", "no_entry_seconds", "max_quote_age_ms"].forEach(function (key) {
      const input = document.getElementById("risk-" + key);
      if (input && input.value !== "") risk[key] = Number(input.value);
    });
    return risk;
  }

  function wireBotctl() {
    document.addEventListener("click", function (event) {
      const id = event.target.id;
      if (id === "bot-save-risk") {
        const btn = document.getElementById("bot-save-risk");
        setInFlight(btn);
        postJson("/api/botctl/risk", collectRiskForm()).then(function (resp) {
          if (resp && !resp.error) {
            clearDirty(["risk-order_size", "risk-min_edge", "risk-max_position_per_market", "risk-max_total_capital",
              "risk-daily_kill_switch_usd", "risk-no_entry_seconds", "risk-max_quote_age_ms"]);
            flashButton(btn, "SAVED ✓", true);
          } else {
            flashButton(btn, "SAVE FAILED", false);
          }
          pollBotctl();
        });
      } else if (id === "bot-start") {
        const btn = document.getElementById("bot-start");
        setInFlight(btn);
        const duration = document.getElementById("bot-duration");
        const fillSource = document.getElementById("bot-fill-source");
        postJson("/api/botctl/start", {
          duration_minutes: Number(duration ? duration.value : 15),
          fill_source: fillSource ? fillSource.value : "rest_book",
          risk: collectRiskForm(),
        }).then(function (resp) {
          if (resp && resp.error) flashButton(document.getElementById("bot-start"), "FAILED", false);
          pollBotctl();
        });
      } else if (id === "bot-stop") {
        const btn = document.getElementById("bot-stop");
        setInFlight(btn);
        flashNow("btn-bot-stop");
        postJson("/api/botctl/stop", {}).then(function (resp) {
          if (resp && resp.error) flashButton(document.getElementById("bot-stop"), "FAILED", false);
          pollBotctl();
        });
      }
      const row = event.target.closest && event.target.closest(".clickable-row");
      if (row && row.getAttribute("data-symbol")) {
        openChart(row.getAttribute("data-symbol"));
      }
    });
  }

  async function postJson(url, payload) {
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        cache: "no-store",
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      ageOf[url.split("?")[0].replace("/api/", "")] = Date.now();
      return data;
    } catch (error) {
      return null;
    }
  }

  function pollBotctl() {
    fetchJson("/api/botctl/status").then(renderBotctl);
  }

  /* ---------------- isolated loop bot (15-17) ---------------- */

  const TF_COLORS = { "5m": "#ffb000", "15m": "#5aa0ff", "30m": "#b967ff" };

  let lastLoopctlSig = null;

  function renderLoopctl(data) {
    const body = document.getElementById("body-loopctl");
    if (!data) {
      clear(body);
      body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      lastLoopctlSig = null;
      return;
    }
    if (data.error) {
      clear(body);
      body.appendChild(el("div", "empty", "ERROR: " + data.error));
      lastLoopctlSig = null;
      return;
    }
    const running = !!data.managed_running;
    const config = data.config || {};
    const mode = config.mode || "auto";
    // Hoisted to function scope (not just the sig-rebuild block below) — the
    // per-poll dynamic-update section at the bottom reads `manual` too (for
    // saveBtn.disabled), and referencing a block-scoped const from outside
    // its block is a ReferenceError that was silently killing every one of
    // those per-poll updates (saveBtn's disabled/label reset never ran).
    const manual = mode === "manual";
    // Signature of everything INTERACTIVE — the panel re-renders only when
    // these change, so clicks on buttons/inputs are never swallowed by a
    // mid-click re-render. Per-timeframe rows update in place every poll, so
    // entering/exiting positions never rebuild (and never wipe) the form.
    const sig = JSON.stringify({
      running: running,
      mode: mode,
      cfg: [
        config.order_size, config.min_edge, config.no_entry_seconds,
        config.entry_window_seconds, config.first_price_min, config.first_price_max,
        config.hedge_timeout_seconds, config.force_exit_before_close_seconds,
        config.local_vol_lookback_seconds, config.local_vol_threshold,
        (config.timeframes || []).join(","),
      ].join("/"),
      stop: !!data.stop_file,
    });
    if (sig !== lastLoopctlSig) {
      lastLoopctlSig = sig;
      clear(body);

      const badge = el("div", "bot-badge paper", "SEQUENTIAL_V2 · ISOLATED PAPER · PUBLIC DEPTH + MODELED FEES · REAL ORDERS: 0");
      body.appendChild(badge);

      const statusRow = el("div", "bot-status", "");
      statusRow.id = "loop-status-row";
      body.appendChild(statusRow);

      const pnlRow = el("div", "loop-pnl", "");
      pnlRow.id = "loop-pnl-row";
      body.appendChild(pnlRow);
      const roiRow = el("div", "loop-roi-row", "");
      roiRow.id = "loop-roi-row";
      body.appendChild(roiRow);
      const regimeRow = el("div", "loop-seq-line", "");
      regimeRow.id = "loop-regime-row";
      body.appendChild(regimeRow);
      const rejectionRow = el("div", "loop-seq-line rejection", "");
      rejectionRow.id = "loop-rejection-row";
      body.appendChild(rejectionRow);

      // per-timeframe state table
      const tfState = data.timeframes || {};
      const table = el("table", "loop-table");
      const head = el("tr");
      ["TF", "PHASE", "YES", "NO", "LOCAL VOL", "SECOND / CAP", "POSITION"].forEach(function (label) {
        head.appendChild(el("th", "", label));
      });
      table.appendChild(head);
      const tbody = el("tbody", "", "");
      tbody.id = "loop-tf-body";
      Object.keys(tfState).forEach(function (tf) {
        tbody.appendChild(el("tr", "", "").appendChild(el("td", "", "")).parentNode);
        tbody.lastChild.id = "loop-tf-" + tf;
      });
      table.appendChild(tbody);
      body.appendChild(table);

      // config + controls — AUTO locks the pre-tuned defaults, MANUAL unlocks editing
      // (`manual` is declared at function scope above)
      const modeBar = el("div", "loop-mode-bar");
      modeBar.appendChild(el("span", "loop-mode-lbl", "MODE:"));
      const autoBtn = el("button", "mode-btn" + (!manual ? " active" : ""), "AUTO — pre-tuned defaults");
      autoBtn.id = "loop-mode-auto";
      applyButtonState(autoBtn, "AUTO — pre-tuned defaults");
      modeBar.appendChild(autoBtn);
      const manualBtn = el("button", "mode-btn" + (manual ? " active" : ""), "MANUAL — edit fields");
      manualBtn.id = "loop-mode-manual";
      applyButtonState(manualBtn, "MANUAL — edit fields");
      modeBar.appendChild(manualBtn);
      body.appendChild(modeBar);

      const form = el("div", "risk-form");
      const fields = [
        ["order_size", "order size"], ["min_edge", "min net edge"],
        ["no_entry_seconds", "entry delay s"], ["entry_window_seconds", "entry window s"],
        ["first_price_min", "first price min"], ["first_price_max", "first price max"],
        ["hedge_timeout_seconds", "hedge timeout s"], ["force_exit_before_close_seconds", "force-exit lead s"],
        ["local_vol_lookback_seconds", "vol lookback s"], ["local_vol_threshold", "local vol trigger"],
      ];
      fields.forEach(function (pair) {
        const key = pair[0];
        const wrap = el("label", "risk-field");
        wrap.appendChild(el("span", "risk-lbl", pair[1]));
        const input = el("input", "risk-input");
        input.type = "number";
        input.id = "loop-" + key;
        input.step = "any";
        input.value = formDirty["loop-" + key] !== undefined ? formDirty["loop-" + key] :
          (config[key] === undefined ? "" : config[key]);
        input.disabled = !manual;
        if (!manual) input.title = "switch to MANUAL to edit — AUTO uses pre-tuned defaults";
        wrap.appendChild(input);
        form.appendChild(wrap);
      });
      body.appendChild(form);
      const tfPicks = el("div", "loop-tf-picks");
      ["5m", "15m", "30m"].forEach(function (tf) {
        const label = el("label", "loop-tf-check");
        const box = el("input");
        box.type = "checkbox";
        box.id = "loop-tf-" + tf;
        box.value = tf;
        box.checked = formDirty["loop-tf-" + tf] !== undefined ? formDirty["loop-tf-" + tf] :
          (!config.timeframes || config.timeframes.indexOf(tf) !== -1);
        box.disabled = !manual;
        label.appendChild(box);
        label.appendChild(el("span", "", tf));
        tfPicks.appendChild(label);
      });
      body.appendChild(tfPicks);

      const controls = el("div", "bot-controls");
      const startBtn = el("button", "bot-btn start", "START LOOP BOT");
      startBtn.id = "loop-start";
      startBtn.disabled = running;
      controls.appendChild(startBtn);
      const stopBtn = el("button", "bot-btn stop", "STOP LOOP BOT");
      stopBtn.id = "loop-stop";
      stopBtn.disabled = !running || isFlashed("btn-loop-stop");
      controls.appendChild(stopBtn);
      const saveBtn = el("button", "bot-btn save", "APPLY CONFIG");
      saveBtn.id = "loop-save";
      saveBtn.disabled = !manual;
      applyButtonState(saveBtn, "APPLY CONFIG");
      if (!manual) saveBtn.title = "switch to MANUAL to change settings";
      controls.appendChild(saveBtn);
      body.appendChild(controls);
      body.appendChild(el("div", "bot-note",
        "sequential loop: volatility-gated near-even first leg, then a later opposite leg only when full-size depth and taker fees preserve the minimum net edge. A timed protective sell realizes the paper loss if the hedge never qualifies. " +
        (running ? "CONFIG HOT-RELOADS into the running bot on APPLY — no restart needed." :
                   "LightGBM v3 adjusts first-leg sensitivity only; it never blocks a hedge or exit.")));
    }

    // dynamic in-place updates (every poll, no DOM rebuild)
    const statusRow = document.getElementById("loop-status-row");
    if (statusRow) {
      clear(statusRow);
      statusRow.appendChild(el("span", "bot-state " + (running ? "run" : "stop"), running ? "RUNNING" : "STOPPED"));
      if (data.pid !== null && data.pid !== undefined) statusRow.appendChild(el("span", "", " pid=" + data.pid));
      if (data.uptime_s !== null && data.uptime_s !== undefined) statusRow.appendChild(el("span", "", " up=" + Math.round(data.uptime_s) + "s"));
      statusRow.appendChild(el("span", "bot-meta",
        (data.controller_owned ? " MANAGED" : (running ? " ADOPTED" : " NO PROCESS")) +
        " · state/loop_bot/ · isolated from the main bot"));
    }
    const pnlRow = document.getElementById("loop-pnl-row");
    if (pnlRow) {
      clear(pnlRow);
      const totals = data.strategy_totals || data.totals || {};
      pnlRow.appendChild(el("span", "loop-pnl-lbl", "REALIZED " + fmtUsd(totals.realized_pnl)));
      pnlRow.appendChild(el("span", "loop-pnl-lbl open", "OPEN EXPOSURE " + fmtUsd(totals.open_exposure)));
      pnlRow.appendChild(el("span", "loop-pnl-lbl", "PAIRS " + fmtCount(totals.completed) +
        " · PROTECTIVE " + fmtCount(totals.forced_exits) + " · FEES " + fmtUsd(totals.fees)));
    }
    const roiRow = document.getElementById("loop-roi-row");
    if (roiRow) {
      clear(roiRow);
      const perf = data.performance || {};
      if (perf.roi_pct === null || perf.roi_pct === undefined) {
        roiRow.appendChild(el("span", "loop-roi-dim", "ROI — · RISK-ADJ — · no settled trades yet"));
      } else {
        roiRow.appendChild(el("span", "loop-roi-val " + (perf.roi_pct >= 0 ? "pos" : "neg"), "ROI " + (perf.roi_pct >= 0 ? "+" : "") + Number(perf.roi_pct).toFixed(2) + "%"));
        roiRow.appendChild(el("span", "loop-roi-lbl", "realized ÷ capital deployed"));
        const sharpe = perf.sharpe;
        roiRow.appendChild(el("span", "loop-roi-val " + (sharpe === null || sharpe >= 0 ? "pos" : "neg"),
          "RISK-ADJ " + (sharpe === null ? "—" : (sharpe >= 0 ? "+" : "") + sharpe + "×")));
        roiRow.appendChild(el("span", "loop-roi-lbl", "per-trade return ÷ std (" + fmtCount(perf.trades) + " trades · " +
          (perf.mean_trade_ret_pct === null || perf.mean_trade_ret_pct === undefined ? "—" : perf.mean_trade_ret_pct + "% mean") +
          " · cost $" + fmtCount(perf.total_cost) + ")"));
      }
    }
    const regimeRow = document.getElementById("loop-regime-row");
    if (regimeRow) {
      clear(regimeRow);
      const regime = data.regime || {};
      const confidence = regime.confidence === null || regime.confidence === undefined
        ? "—" : Math.round(Number(regime.confidence) * 100) + "%";
      regimeRow.appendChild(el("span", "loop-seq-key", "LIGHTGBM V3 · ENTRY ONLY"));
      regimeRow.appendChild(el("span", regime.status === "active" ? "loop-seq-good" : "loop-seq-warn",
        String(regime.regime || "—") + " · " + String(regime.status || "missing").toUpperCase() + " · " + confidence));
      regimeRow.appendChild(el("span", "loop-dim", "local-vol trigger " +
        (regime.adjusted_local_vol_threshold === null || regime.adjusted_local_vol_threshold === undefined
          ? "—" : Number(regime.adjusted_local_vol_threshold).toFixed(4)) +
        " · source " + String(regime.source || "—") +
        " · age " + (regime.age_s === null || regime.age_s === undefined ? "—" : Math.round(regime.age_s) + "s")));
    }
    const rejectionRow = document.getElementById("loop-rejection-row");
    if (rejectionRow) {
      clear(rejectionRow);
      const reasons = Object.entries(data.rejections || {}).sort(function (a, b) { return Number(b[1]) - Number(a[1]); });
      rejectionRow.appendChild(el("span", "loop-seq-key", "REJECTIONS"));
      rejectionRow.appendChild(el("span", "loop-dim", reasons.length
        ? reasons.slice(0, 4).map(function (item) { return item[0].replaceAll("_", " ") + "=" + item[1]; }).join(" · ")
        : "none yet"));
    }
    const tfState = data.timeframes || {};
    Object.keys(tfState).forEach(function (tf) {
      const tr = document.getElementById("loop-tf-" + tf);
      if (!tr) return;
      const st = tfState[tf] || {};
      clear(tr);
      const tfCell = el("td", "loop-tf");
      tfCell.style.color = TF_COLORS[tf] || "#fff";
      tfCell.textContent = tf;
      tr.appendChild(tfCell);
      const phase = String(st.phase || "waiting_market");
      const phaseCell = el("td", "loop-phase loop-phase-" + phase, phase.replaceAll("_", " ").toUpperCase());
      tr.appendChild(phaseCell);
      tr.appendChild(el("td", "", st.yes === null || st.yes === undefined ? "—" : Number(st.yes).toFixed(3)));
      tr.appendChild(el("td", "", st.no === null || st.no === undefined ? "—" : Number(st.no).toFixed(3)));
      const localVol = st.local_vol || {};
      tr.appendChild(el("td", "loop-dim", "Y " + Number(localVol.yes || 0).toFixed(3) + " · N " + Number(localVol.no || 0).toFixed(3)));
      const preview = st.second_preview || {};
      const previewCell = el("td", "loop-dim");
      if (preview.vwap !== null && preview.vwap !== undefined) {
        previewCell.textContent = String(preview.side || "?").toUpperCase() + " " + Number(preview.vwap).toFixed(3) +
          " · net " + fmtUsd(preview.net_pnl) + " / req " + Number(preview.required_net_edge || 0).toFixed(3);
        if (Number(preview.net_edge) >= Number(preview.required_net_edge)) previewCell.className = "corr-pos";
      } else {
        previewCell.textContent = "—";
      }
      tr.appendChild(previewCell);
      const posCell = el("td");
      const position = st.position || {};
      const first = position.first || {};
      if (first.vwap !== null && first.vwap !== undefined) {
        const remaining = position.deadline_at ? Math.max(0, Number(position.deadline_at) - Date.now() / 1000) : null;
        let detail = "FIRST " + String(position.side || "?").toUpperCase() + " " + Number(first.vwap).toFixed(3) +
          " × " + fmtCount(first.shares);
        if (phase === "waiting_second") detail += " · " + (remaining === null ? "—" : Math.ceil(remaining) + "s");
        else if (phase === "paired") detail = "PAIRED · net " + fmtUsd(position.pnl);
        else if (phase === "aborted") detail = "PROTECTIVE EXIT · " + fmtUsd(position.pnl);
        else if (phase === "manual_review") detail = "MANUAL REVIEW · exposure " + fmtUsd(Number(first.notional || 0) + Number(first.fee || 0));
        posCell.appendChild(el("span", phase === "paired" ? "loop-pos" : (phase === "manual_review" ? "corr-neg" : "loop-dim"), detail));
      } else {
        posCell.appendChild(el("span", "loop-dim", st.last_rejection ? "flat · " + String(st.last_rejection).replaceAll("_", " ") : "flat"));
      }
      tr.appendChild(posCell);
    });
    // Every poll (not just on a full panel rebuild) must restore the real
    // label via applyButtonState — otherwise a click's setInFlight() "…" text
    // survives forever whenever `running` doesn't change afterward (e.g. a
    // start that fails silently, or a bot that dies right back to stopped),
    // since a same-signature poll never rebuilds the DOM. Mirrors saveBtn
    // below, which already did this correctly.
    const startBtn = document.getElementById("loop-start");
    if (startBtn) {
      startBtn.disabled = running;
      applyButtonState(startBtn, "START LOOP BOT");
    }
    const stopBtn = document.getElementById("loop-stop");
    if (stopBtn) {
      stopBtn.disabled = !running || isFlashed("btn-loop-stop");
      applyButtonState(stopBtn, "STOP LOOP BOT");
    }
    const saveBtn = document.getElementById("loop-save");
    if (saveBtn) {
      saveBtn.disabled = !manual;
      applyButtonState(saveBtn, "APPLY CONFIG");
    }
  }

  function collectLoopForm() {
    const config = {};
    const modeAuto = document.getElementById("loop-mode-auto");
    config.mode = modeAuto && modeAuto.classList.contains("active") ? "auto" : "manual";
    [
      "order_size", "min_edge", "no_entry_seconds", "entry_window_seconds",
      "first_price_min", "first_price_max", "hedge_timeout_seconds",
      "force_exit_before_close_seconds", "local_vol_lookback_seconds", "local_vol_threshold",
    ].forEach(function (key) {
      const input = document.getElementById("loop-" + key);
      if (input && input.value !== "") config[key] = Number(input.value);
    });
    const tfs = [];
    ["5m", "15m", "30m"].forEach(function (tf) {
      const box = document.getElementById("loop-tf-" + tf);
      if (box && box.checked) tfs.push(tf);
    });
    if (tfs.length) config.timeframes = tfs;
    return config;
  }

  function wireLoopctl() {
    document.addEventListener("click", function (event) {
      const id = event.target.id;
      const LOOP_IDS = [
        "loop-order_size", "loop-min_edge", "loop-no_entry_seconds", "loop-entry_window_seconds",
        "loop-first_price_min", "loop-first_price_max", "loop-hedge_timeout_seconds",
        "loop-force_exit_before_close_seconds", "loop-local_vol_lookback_seconds", "loop-local_vol_threshold",
        "loop-tf-5m", "loop-tf-15m", "loop-tf-30m",
      ];
      const refresh = function () {
        fetchJson("/api/loopbot/status").then(renderLoopctl);
      };
      if (id === "loop-mode-auto" || id === "loop-mode-manual") {
        // switching back to AUTO restores the pre-tuned defaults
        const target = id === "loop-mode-auto" ? "auto" : "manual";
        const btn = document.getElementById(id);
        setInFlight(btn);
        flashNow("btn-" + id);
        postJson("/api/loopbot/config", { mode: target }).then(function (resp) {
          if (resp && !resp.error) {
            clearDirty(LOOP_IDS);
            flashButton(document.getElementById(id), target === "auto" ? "AUTO ✓" : "MANUAL ✓", true);
          } else {
            flashButton(document.getElementById(id), "FAILED", false);
          }
          refresh();
        });
      } else if (id === "loop-save") {
        const btn = document.getElementById("loop-save");
        setInFlight(btn);
        postJson("/api/loopbot/config", collectLoopForm()).then(function (resp) {
          if (resp && !resp.error) {
            clearDirty(LOOP_IDS);
            flashButton(btn, "APPLIED ✓", true);
          } else {
            flashButton(btn, "SAVE FAILED", false);
          }
          refresh();
        });
      } else if (id === "loop-start") {
        const btn = document.getElementById("loop-start");
        setInFlight(btn);
        postJson("/api/loopbot/start", { config: collectLoopForm() }).then(function (resp) {
          if (resp && resp.error) flashButton(document.getElementById("loop-start"), "FAILED", false);
          refresh();
        });
      } else if (id === "loop-stop") {
        const btn = document.getElementById("loop-stop");
        setInFlight(btn);
        flashNow("btn-loop-stop");
        postJson("/api/loopbot/stop", {}).then(function (resp) {
          if (resp && resp.error) flashButton(document.getElementById("loop-stop"), "FAILED", false);
          refresh();
        });
      }
    });
  }

  function renderLoopEvents(data) {
    const body = document.getElementById("body-loopevents");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    const rows = data.events || [];
    if (!rows.length) { body.appendChild(el("div", "empty", "NO EVENTS — START THE LOOP BOT")); return; }
    rows.slice(-60).forEach(function (row) {
      const kind = row.kind || "?";
      const line = el("div", "wire-line");
      line.appendChild(el("span", "wire-time", timeHMS(new Date(row.t * 1000).toISOString()) + " "));
      line.appendChild(el("span", "wire-kind loop-k-" + kind, "[" + String(kind).toUpperCase() + "]"));
      let text = "";
      if (kind === "btc") text = "BTC " + Number(row.price).toFixed(2);
      else if (kind === "first_filled") text = row.tf + " FIRST " + String(row.side || "?").toUpperCase() +
        " vwap=" + Number(row.vwap).toFixed(4) + " size=" + row.size + " fee=" + fmtUsd(row.fee) +
        " vol=" + Number(row.local_vol || 0).toFixed(4);
      else if (kind === "second_candidate") text = row.tf + " OPPOSITE " + String(row.side || "?").toUpperCase() +
        " vwap=" + Number(row.vwap).toFixed(4) + " net=" + fmtUsd(row.net_pnl) +
        " edge=" + Number(row.net_edge).toFixed(4);
      else if (kind === "pair_completed") text = row.tf + " PAIRED combined=" + Number(row.combined).toFixed(4) +
        " fees=" + fmtUsd(row.fees) + " net=" + fmtUsd(row.pnl) + " wait=" + Number(row.wait_seconds).toFixed(1) + "s";
      else if (kind === "protective_exit") text = row.tf + " PROTECTIVE EXIT " + String(row.side || "?").toUpperCase() +
        " buy=" + Number(row.first_vwap).toFixed(4) + " sell=" + Number(row.exit_vwap).toFixed(4) +
        " net=" + fmtUsd(row.pnl) + " reason=" + row.reason;
      else if (kind === "rejected") text = row.tf + " " + String(row.reason || "unknown").replaceAll("_", " ");
      else if (kind === "phase") text = row.tf + " → " + String(row.phase || "?").replaceAll("_", " ").toUpperCase();
      else if (kind === "manual_review" || kind === "exit_failed" || kind === "sequence_aborted") {
        text = row.tf + " " + String(row.reason || kind).replaceAll("_", " ").toUpperCase();
      }
      else if (kind === "enter") text = row.tf + " " + row.slug + " yes=" + Number(row.yes).toFixed(3) + " no=" + Number(row.no).toFixed(3) + " combined=" + Number(row.combined).toFixed(3) + " size=" + row.size + " locked=" + fmtUsd(row.locked);
      else if (kind === "exit") text = row.tf + " " + row.slug + " pnl=" + fmtUsd(row.pnl);
      else if (kind === "edge") text = row.tf + " " + row.slug + " combined=" + Number(row.combined).toFixed(3);
      else if (kind === "watch") text = row.tf + " " + row.slug;
      else if (kind === "error") text = row.message;
      else if (kind === "meta") text = row.meta_kind + " " + JSON.stringify({ tf: row.timeframes, size: row.order_size, edge: row.min_edge });
      line.appendChild(el("span", "", " " + text));
      line.title = JSON.stringify(row).slice(0, 300);
      body.appendChild(line);
    });
    body.scrollTop = body.scrollHeight;
  }

  /* ---------------- 16 loop decision chart ---------------- */

  let lastLoopChart = { btc: [], entries: [], exits: [] };

  function drawLoopChart() {
    const canvas = document.getElementById("loop-chart-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 800;
    const height = canvas.clientHeight || 280;
    if (canvas.width !== width * dpr) { canvas.width = width * dpr; canvas.height = height * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const btc = lastLoopChart.btc || [];
    const entries = lastLoopChart.entries || [];
    const exits = lastLoopChart.exits || [];
    const hasBtc = btc.length >= 2;
    if (!hasBtc && !entries.length && !exits.length) {
      ctx.fillStyle = "#666";
      ctx.font = "11px SFMono-Regular, Menlo, Monaco, monospace";
      ctx.textAlign = "center";
      ctx.fillText("NO DATA — START THE LOOP BOT TO STREAM DECISIONS", width / 2, height / 2);
      return;
    }
    const padL = 52, padR = 90, padT = 10, padB = 18;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;
    const tMin = Math.min.apply(null, btc.map(function (p) { return p.t; }).concat(entries.map(function (e) { return e.t; })).concat(exits.map(function (e) { return e.t; })));
    const tMax = Math.max.apply(null, btc.map(function (p) { return p.t; }).concat(entries.map(function (e) { return e.t; })).concat(exits.map(function (e) { return e.t; })).concat([Date.now() / 1000]));
    const x = function (t) { return padL + plotW * ((t - tMin) / (tMax - tMin || 1)); };
    if (hasBtc) {
      let minV = Math.min.apply(null, btc.map(function (p) { return p.price; }));
      let maxV = Math.max.apply(null, btc.map(function (p) { return p.price; }));
      const span = maxV - minV || 1;
      minV -= span * 0.08;
      maxV += span * 0.08;
      const y = function (v) { return padT + plotH * (1 - (v - minV) / (maxV - minV)); };
      ctx.fillStyle = "#666";
      ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";
      ctx.textAlign = "right";
      ctx.fillText(maxV.toFixed(0), padL - 4, padT + 8);
      ctx.fillText(minV.toFixed(0), padL - 4, padT + plotH);
      ctx.strokeStyle = "rgba(0,255,65,0.85)";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      btc.forEach(function (p, i) {
        if (i === 0) ctx.moveTo(x(p.t), y(p.price)); else ctx.lineTo(x(p.t), y(p.price));
      });
      ctx.stroke();
      ctx.fillStyle = "#3a3a3a";
      ctx.textAlign = "left";
      ctx.fillText("BTC/USD (finnhub spot)", padL + 4, padT + 10);
      // entry/exit markers at BTC price
      entries.forEach(function (e) {
        if (e.btc === null || e.btc === undefined) return;
        const px = x(e.t), py = y(e.btc);
        ctx.fillStyle = TF_COLORS[e.tf] || "#ffb000";
        ctx.beginPath();
        ctx.moveTo(px, py - 7);
        ctx.lineTo(px + 6, py + 2);
        ctx.lineTo(px - 6, py + 2);
        ctx.closePath();
        ctx.fill();
        ctx.fillStyle = "#fff";
        ctx.font = "8px SFMono-Regular, Menlo, Monaco, monospace";
        ctx.textAlign = "center";
        ctx.fillText(e.tf, px, py - 9);
      });
      exits.forEach(function (e) {
        const px = x(e.t);
        const py = e.btc !== null && e.btc !== undefined ? y(e.btc) : padT + 6;
        ctx.strokeStyle = "#fff";
        ctx.strokeRect(px - 3, py - 3, 6, 6);
        const label = fmtUsd(e.pnl);
        ctx.fillStyle = e.pnl >= 0 ? "#00ff41" : "#ff4136";
        ctx.font = "8px SFMono-Regular, Menlo, Monaco, monospace";
        ctx.textAlign = "left";
        ctx.fillText(label, Math.min(px + 5, width - 70), Math.max(10, py - 3));
      });
    } else {
      // no BTC samples: plot markers on a combined-price axis
      ctx.fillStyle = "#666";
      ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";
      ctx.textAlign = "left";
      ctx.fillText("axis: book combined (no BTC samples yet)", padL, padT + 8);
      const yCombined = function (c) { return padT + plotH * (1 - ((c || 0.99) - 0.9) / 0.1); };
      entries.forEach(function (e) {
        const px = x(e.t), py = yCombined(e.combined);
        ctx.fillStyle = TF_COLORS[e.tf] || "#ffb000";
        ctx.beginPath();
        ctx.arc(px, py, 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "#fff";
        ctx.font = "8px SFMono-Regular, Menlo, Monaco, monospace";
        ctx.textAlign = "center";
        ctx.fillText(e.tf + " " + Number(e.combined).toFixed(3), px, py - 7);
      });
    }
    ctx.fillStyle = "#666";
    ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";
    ctx.textAlign = "left";
    ctx.fillText(timeHMS(new Date(tMin * 1000).toISOString()), padL, height - 3);
    ctx.textAlign = "right";
    ctx.fillText(timeHMS(new Date(tMax * 1000).toISOString()), padL + plotW, height - 3);
    // per-tf legend
    let legendX = padL + plotW + 8;
    ctx.textAlign = "left";
    Object.keys(TF_COLORS).forEach(function (tf) {
      ctx.fillStyle = TF_COLORS[tf];
      ctx.fillText("▲ " + tf + " enter", legendX, padT + 14 * (["5m", "15m", "30m"].indexOf(tf) + 1));
    });
  }

  function renderLoopChart(data) {
    const caption = document.getElementById("loop-chart-caption");
    if (!data) return;
    lastLoopChart = { btc: data.btc || [], entries: data.entries || [], exits: data.exits || [] };
    drawLoopChart();
    const totals = data.totals || {};
    const perTf = data.per_tf || {};
    let text = "pnl " + fmtUsd(totals.realized_pnl) + " · entries " + fmtCount(totals.entries) + " · exits " + fmtCount(totals.exits);
    const perf = data.performance || {};
    if (perf.roi_pct !== null && perf.roi_pct !== undefined) {
      text += " · ROI " + (perf.roi_pct >= 0 ? "+" : "") + Number(perf.roi_pct).toFixed(2) + "%";
      if (perf.sharpe !== null && perf.sharpe !== undefined) {
        text += " · RISK-ADJ " + (perf.sharpe >= 0 ? "+" : "") + perf.sharpe + "×";
      }
    }
    Object.keys(perTf).forEach(function (tf) {
      text += " · " + tf + ": " + perTf[tf].entries + " entries / " + perTf[tf].exits + " exits / " + fmtUsd(perTf[tf].realized);
    });
    if (caption) caption.textContent = text;
  }

  /* ---------------- command bar / status ---------------- */

  function tickClock() {
    const now = new Date();
    const utc = document.getElementById("cmd-utc");
    if (utc) utc.textContent = now.toISOString().slice(0, 19).replace("T", " ") + "Z";
    const footer = document.getElementById("footer-ts");
    if (footer) footer.textContent = now.toISOString().slice(0, 19).replace("T", " ") + "Z";
  }

  function renderCommand(data) {
    const conn = document.getElementById("cmd-conn-state");
    if (!data) {
      conn.className = "cmd-badge offline";
      conn.textContent = "OFFLINE";
      return;
    }
    conn.className = "cmd-badge " + (data.bot_log_fresh ? "live" : "stale");
    conn.textContent = data.bot_log_fresh ? "CONNECTED" : "STALE";
    document.getElementById("cmd-mode").textContent = data.mode || "—";
    const kill = document.getElementById("cmd-kill");
    const killState = (data.kill_switch || {}).state || "unknown";
    kill.className = "cmd-badge " + (killState === "halted" ? "halted" : "");
    kill.textContent = killState.toUpperCase();
    const session = data.session || {};
    document.getElementById("cmd-sess").textContent = session.elapsed_s === null || session.elapsed_s === undefined
      ? "—" : String(Math.round(session.elapsed_s)) + "s";
    document.getElementById("cmd-loops").textContent = fmtCount(session.loops);
  }

  function redrawCharts() {
    drawPnl();
    drawLatency();
    drawStockChart();
    drawLoopChart();
  }

  /* ---------------- boot ---------------- */

  function start() {
    loadLayout();
    wireLayout();
    tickClock();
    setInterval(tickClock, 1000);
    setInterval(refreshAgeBadges, 1000);
    const loop = function (endpoint, renderFn) {
      const run = function () { poll(endpoint, renderFn); };
      run();
      setInterval(run, POLL_MS);
    };
    loop("/api/heatmap?limit=48", renderExec);
    loop("/api/pnl", renderPnl);
    loop("/api/depth", renderDepth);
    loop("/api/alerts?limit=50", renderAlerts);
    loop("/api/models", renderModels);
    loop("/api/latency", renderLatency);
    loop("/api/checklist", renderGates);
    loop("/api/command", renderCommand);
    loop("/api/windows", renderWindows);
    const sentimentLoop = function () { poll("/api/btc_sentiment", renderSentiment); };
    sentimentLoop();
    setInterval(sentimentLoop, 60000);
    wireCardModals();
    loop("/api/lse_cross_asset", renderCross);
    loop("/api/indices", renderIndices);
    loop("/api/sector_heatmap", renderSectorHeatmap);
    loop("/api/news_wire", renderNews);
    const socialLoop = function () { poll("/api/social", renderSocial); };
    socialLoop();
    setInterval(socialLoop, SOCIAL_POLL_MS);
    wireBotctl();
    loop("/api/botmind", renderMind);
    wireLoopctl();
    // a poll rebuild must never wipe an in-progress edit
    document.addEventListener("input", function (event) {
      if (event.target && event.target.classList && event.target.classList.contains("risk-input")) markDirty(event.target);
    });
    document.addEventListener("change", function (event) {
      if (event.target && event.target.type === "checkbox") markDirty(event.target);
    });
    loop("/api/loopbot/status", renderLoopctl);
    loop("/api/loopbot/events?limit=80", renderLoopEvents);
    loop("/api/loopbot/chart", renderLoopChart);
    wireChartWorkspace();
    renderWatchlist();
    setInterval(wsAutoRefresh, 60000);
    const botctlLoop = function () {
      pollBotctl();
      fetchJson("/api/botctl/log?lines=60").then(renderBotLog);
    };
    botctlLoop();
    setInterval(botctlLoop, 4000);
    startGlobe();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
