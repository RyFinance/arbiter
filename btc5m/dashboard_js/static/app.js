/* PM TERMINAL — frontend application. Zero dependencies, REST polling. */
"use strict";

(function () {
  const POLL_MS = 2000;
  const AGE_LIVE_MS = 10_000;
  const AGE_STALE_MS = 30_000;

  const ageOf = {};   // endpoint -> Date.now() of last successful fetch
  const panes = {};   // endpoint -> { data, error }

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
    const sign = num > 0 ? "+" : "";
    return sign + num.toFixed(4);
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
    if (seconds < AGE_LIVE_MS / 1000) return { text: "LIVE", cls: "live" };
    if (seconds < AGE_STALE_MS / 1000) return { text: "STALE " + seconds + "s", cls: "stale" };
    return { text: "OFFLINE " + seconds + "s", cls: "offline" };
  }

  function refreshAgeBadges() {
    Object.keys(panes).forEach(function (endpoint) {
      const badge = document.getElementById("age-" + endpoint);
      if (!badge) return;
      const status = ageString(endpoint);
      badge.className = "panel-age " + status.cls;
      badge.textContent = status.text;
    });
  }

  async function fetchJson(url) {
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      ageOf[url.split("?")[0].replace("/api/", "")] = Date.now();
      return data;
    } catch (error) {
      return null;
    }
  }

  async function poll(endpoint, renderFn) {
    const key = endpoint;
    panes[key] = panes[key] || {};
    try {
      const data = await fetchJson(endpoint);
      panes[key] = { data: data, error: data === null };
      renderFn(data, panes[key]);
    } catch (error) {
      panes[key] = { data: null, error: true };
      renderFn(null, panes[key]);
    }
  }

  /* ---------------- 01 session status ---------------- */

  function renderStatus(commandData) {
    const body = document.getElementById("body-status");
    clear(body);
    if (!commandData || commandData.error !== undefined && commandData.error) {
      body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      return;
    }
    const grid = el("div", "stat-grid");
    const row = function (key, value, cls) {
      grid.appendChild(el("span", "k", key));
      const v = el("span", "v" + (cls ? " " + cls : ""), value);
      grid.appendChild(v);
    };
    row("MODE", commandData.mode || "UNKNOWN");
    row("DRY_RUN", String(commandData.dry_run));
    row("BOT LOG", commandData.bot_log_fresh ? "FRESH " + String(Math.round(commandData.bot_log_age_s || 0)) + "s" : (commandData.bot_log_age_s === null || commandData.bot_log_age_s === undefined ? "NO FILE" : "STALE " + String(Math.round(commandData.bot_log_age_s)) + "s"),
        commandData.bot_log_fresh ? "pos" : "neg");
    const kill = commandData.kill_switch || {};
    row("KILL SWITCH", kill.state === "halted" ? "HALTED" : (kill.state || "UNKNOWN"), kill.state === "halted" ? "neg" : "");
    if (kill.last_message) row("KILL MSG", kill.last_message);
    if (kill.session_id) row("KILL SESS", kill.session_id);
    const session = commandData.session || {};
    row("SESS START", session.start_ts_utc || "—");
    row("SESS ELAPSED", session.elapsed_s === null || session.elapsed_s === undefined ? "—" : String(Math.round(session.elapsed_s)) + "s");
    row("LOOPS", fmtCount(session.loops));
    row("OPPS", fmtCount(session.opps));
    row("FILLS", fmtCount(session.fills));
    row("UTC NOW", commandData.utc_now || "—");
    body.appendChild(grid);
  }

  /* ---------------- 02 heatmap ---------------- */

  function renderHeatmap(data) {
    const body = document.getElementById("body-heatmap");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    if (data.error) { body.appendChild(el("div", "empty", "READER ERROR: " + data.error)); return; }
    const items = data.items || [];
    if (!items.length) { body.appendChild(el("div", "empty", "NO DATA — 0 FILLS / REJECTS")); return; }
    const wrap = el("div", "heat-wrap");
    const grid = el("div", "heat-grid");
    const newest = items.slice().reverse();
    const maxEdge = newest.reduce(function (m, item) {
      const e = Math.abs(item.edge === null || item.edge === undefined ? 0 : item.edge);
      return Math.max(m, e);
    }, 0.001);
    newest.forEach(function (item) {
      const edge = Math.abs(item.edge === null || item.edge === undefined ? 0 : item.edge);
      const size = 8 + Math.round(18 * (edge / maxEdge));
      const alpha = 0.35 + 0.65 * (edge / maxEdge);
      const block = el("div", "heat-block " + (item.kind || "reject"));
      block.style.width = size + "px";
      block.style.height = size + "px";
      block.style.opacity = String(alpha.toFixed(2));
      block.title = timeHMS(item.ts_utc) +
        " " + (item.market_id || "?") +
        " edge=" + (item.edge === null || item.edge === undefined ? "—" : Number(item.edge).toFixed(3)) +
        " pnl=" + fmtUsd(item.pnl) +
        " size=" + fmtCount(item.size) +
        (item.reason ? " reason=" + item.reason : "");
      grid.appendChild(block);
    });
    wrap.appendChild(grid);
    const fills = items.filter(function (i) { return i.kind === "fill"; }).length;
    const rejects = items.filter(function (i) { return i.kind === "reject"; }).length;
    const partials = items.filter(function (i) { return i.kind === "partial"; }).length;
    wrap.appendChild(el("div", "heat-caption",
      "last " + items.length + " events — fills " + fills + " · rejects " + rejects + " · partials " + partials +
      " — block size/shade = |edge|" + (data.source ? " — source: " + data.source : "")));
    body.appendChild(wrap);
  }

  /* ---------------- 03 pnl ---------------- */

  function drawPnl(series) {
    const canvas = document.getElementById("pnl-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 820;
    const height = canvas.clientHeight || 170;
    if (canvas.width !== width * dpr) { canvas.width = width * dpr; canvas.height = height * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const seriesWithTs = series.filter(function (point) {
      return point.locked !== null && point.locked !== undefined;
    });
    if (!seriesWithTs.length) return;

    const padL = 46, padR = 8, padT = 8, padB = 16;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;
    const values = [];
    seriesWithTs.forEach(function (p) {
      values.push(p.locked || 0, p.session || 0, p.residual || 0);
    });
    let minV = Math.min(0, Math.min.apply(null, values));
    let maxV = Math.max(0, Math.max.apply(null, values));
    if (maxV - minV < 0.0001) { maxV = minV + 1; }
    const x = function (i) {
      if (seriesWithTs.length === 1) return padL + plotW / 2;
      return padL + plotW * (i / (seriesWithTs.length - 1));
    };
    const y = function (v) { return padT + plotH * (1 - (v - minV) / (maxV - minV)); };

    ctx.strokeStyle = "#1a1a1a";
    ctx.fillStyle = "#666";
    ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";
    ctx.beginPath();
    ctx.moveTo(padL, y(0));
    ctx.lineTo(padL + plotW, y(0));
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(maxV.toFixed(2), padL - 4, padT + 8);
    ctx.fillText(minV.toFixed(2), padL - 4, padT + plotH);
    ctx.textAlign = "left";
    ctx.fillText(timeHMS(seriesWithTs[0].ts_utc), padL, height - 4);
    ctx.textAlign = "right";
    ctx.fillText(timeHMS(seriesWithTs[seriesWithTs.length - 1].ts_utc), padL + plotW, height - 4);

    const drawLine = function (key, color) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      seriesWithTs.forEach(function (point, i) {
        const v = point[key];
        if (v === null || v === undefined) return;
        if (i === 0) ctx.moveTo(x(i), y(v)); else ctx.lineTo(x(i), y(v));
      });
      ctx.stroke();
    };
    drawLine("locked", "#00ff41");
    drawLine("session", "#ffb000");
    drawLine("residual", "#ff4136");
    if (seriesWithTs.length === 1) {
      const p = seriesWithTs[0];
      [["locked", "#00ff41"], ["session", "#ffb000"], ["residual", "#ff4136"]].forEach(function (pair) {
        const v = p[pair[0]];
        if (v === null || v === undefined) return;
        ctx.fillStyle = pair[1];
        ctx.beginPath();
        ctx.arc(x(0), y(v), 2.4, 0, Math.PI * 2);
        ctx.fill();
      });
    }
  }

  function renderPnl(data) {
    const body = document.getElementById("body-pnl");
    const readout = document.getElementById("pnl-readout");
    clear(readout);
    if (!data) {
      drawPnl([]);
      readout.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE"));
      return;
    }
    if (data.error) {
      drawPnl([]);
      readout.appendChild(el("div", "empty", "READER ERROR: " + data.error));
      return;
    }
    const series = data.series || [];
    drawPnl(series);
    const latest = data.latest || {};
    const cells = [
      ["LOCKED", latest.locked, "pnl-cell"],
      ["SESSION", latest.session, "pnl-cell"],
      ["RESIDUAL", latest.residual, "pnl-cell"],
      ["WOULD-LOCK", latest.would_lock, "pnl-cell"],
    ];
    const hasAny = cells.some(function (c) { return c[1] !== null && c[1] !== undefined; });
    if (!hasAny) {
      readout.appendChild(el("div", "empty", "NO DATA — 0 METRIC POINTS (" + (data.notes || "") + ")"));
      return;
    }
    cells.forEach(function (c) {
      const value = c[1];
      const cls = value === null || value === undefined ? "" : (value >= 0 ? "pos" : "neg");
      const cell = el("div", c[2]);
      cell.appendChild(el("div", "lbl", c[0]));
      cell.appendChild(el("div", "val " + cls, fmtUsd(value)));
      readout.appendChild(cell);
    });
  }

  /* ---------------- 04 depth ---------------- */

  function renderDepth(data) {
    const body = document.getElementById("body-depth");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    if (data.error) { body.appendChild(el("div", "empty", "READER ERROR: " + data.error)); return; }
    const continuity = data.continuity || {};
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
    bar.appendChild(el("div", "ok", ""));
    bar.firstChild.style.width = Math.round(okFrac * 1000) / 10 + "%";
    bar.appendChild(el("div", "bad", ""));
    bar.lastChild.style.flex = "1";
    body.appendChild(bar);

    const lines = el("div", "depth-lines");
    const line = function (key, value, cls) {
      const wrap = el("div");
      wrap.appendChild(el("span", "k", key + " "));
      wrap.appendChild(el("span", cls || "", value));
      lines.appendChild(wrap);
    };
    line("CONTINUITY", fmtCount(continuity.complete) + "/" + fmtCount(continuity.windows) + " windows (" + String(Math.round(okFrac * 1000) / 10) + "%)", okFrac > 0 ? "pos" : "neg");
    if (data.last_30_windows) {
      line("LAST 30", fmtCount(data.last_30_windows.complete) + "/" + fmtCount(data.last_30_windows.windows) + " complete");
    }
    line("LAST CAPTURE", data.last_capture_age_s === null || data.last_capture_age_s === undefined ? "—" : String(Math.round(data.last_capture_age_s)) + "s ago", data.last_capture_age_s !== null && data.last_capture_age_s !== undefined && data.last_capture_age_s < 30 ? "pos" : "neg");
    const proc = data.process || {};
    line("CAPTURE PROC", (proc.alive ? "ALIVE" : "DEAD") + " pid=" + fmtCount(proc.pid) + " lock=" + (proc.lock_held ? "held" : "missing"), proc.alive ? "pos" : "neg");
    (data.shards || []).forEach(function (shard) {
      const rows = shard.rows === null || shard.rows === undefined ? "—" : fmtCount(shard.rows);
      line("SHARD " + shard.name, rows + " rows · " + (shard.age_s === null || shard.age_s === undefined ? "—" : String(Math.round(shard.age_s)) + "s ago"));
    });
    body.appendChild(lines);
  }

  /* ---------------- 05 alerts ---------------- */

  function renderAlerts(data) {
    const body = document.getElementById("body-alerts");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    if (data.error) { body.appendChild(el("div", "empty", "READER ERROR: " + data.error)); return; }
    const items = data.items || [];
    if (!items.length) { body.appendChild(el("div", "empty", "NO ALERTS RECORDED")); return; }
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

  /* ---------------- 06 models ---------------- */

  function renderModels(data) {
    const body = document.getElementById("body-models");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    if (data.error) { body.appendChild(el("div", "empty", "PARSER UNAVAILABLE: " + data.error)); return; }
    const models = data.models || [];
    if (!models.length) { body.appendChild(el("div", "empty", "NO MODEL ARTIFACTS FOUND")); return; }
    const table = el("table", "model-table");
    const head = el("tr");
    head.appendChild(el("th", "", "MODEL"));
    head.appendChild(el("th", "", "VERDICT"));
    head.appendChild(el("th", "", "KEY METRICS"));
    table.appendChild(head);
    models.forEach(function (model) {
      const tr = el("tr");
      const nameCell = el("td");
      nameCell.appendChild(el("span", "", model.name || model.id));
      nameCell.appendChild(el("div", "", ""));
      nameCell.lastChild.textContent = "";
      tr.appendChild(nameCell);
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

  /* ---------------- 07 latency ---------------- */

  function drawLatency(series) {
    const canvas = document.getElementById("lat-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 400;
    const height = canvas.clientHeight || 90;
    if (canvas.width !== width * dpr) { canvas.width = width * dpr; canvas.height = height * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const points = series.filter(function (p) {
      return p.p50_ms !== null && p.p50_ms !== undefined;
    });
    if (!points.length) return;
    const padL = 6, padR = 6, padT = 6, padB = 14;
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
        const h = (v / maxV) * plotH;
        ctx.fillStyle = colors[j];
        ctx.fillRect(padL + i * groupW + j * barW, padT + plotH - h, barW, h);
      });
    });
    ctx.fillStyle = "#666";
    ctx.font = "9px SFMono-Regular, Menlo, Monaco, monospace";
    ctx.textAlign = "left";
    ctx.fillText(timeHMS(points[0].ts_utc), padL, height - 3);
    ctx.textAlign = "right";
    ctx.fillText(timeHMS(points[points.length - 1].ts_utc), padL + plotW, height - 3);
    ctx.fillText(maxV.toFixed(0) + "ms", padL + plotW, padT + 8);
  }

  function renderLatency(data) {
    const body = document.getElementById("body-latency");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    if (data.error) { body.appendChild(el("div", "empty", "READER ERROR: " + data.error)); return; }
    const latest = data.latest || {};
    const hasAny = latest.p50_ms !== null && latest.p50_ms !== undefined ||
      latest.p95_ms !== null && latest.p95_ms !== undefined;
    if (!hasAny) {
      body.appendChild(el("div", "empty", "NO DATA — n=" + fmtCount(latest.n) + " FIRES"));
      return;
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
    wrap.appendChild(el("canvas", "", ""));
    wrap.firstChild.id = "lat-canvas";
    body.appendChild(wrap);
    drawLatency(data.series || []);
  }

  /* ---------------- 08 checklist ---------------- */

  function renderChecklist(data) {
    const body = document.getElementById("body-checklist");
    clear(body);
    if (!data) { body.appendChild(el("div", "empty", "NO DATA — BACKEND UNREACHABLE")); return; }
    if (data.error) { body.appendChild(el("div", "empty", "PARSER UNAVAILABLE: " + data.error)); return; }
    const gates = data.gates || [];
    if (!gates.length) { body.appendChild(el("div", "empty", "NO GATE ROWS PARSED")); return; }
    const summary = data.summary || {};
    const counts = el("div", "ck-summary");
    const countBox = function (label, key, cls) {
      const box = el("div", "ck-count " + cls);
      box.appendChild(el("span", "num", fmtCount(summary[key])));
      box.appendChild(el("span", "lbl", " " + label));
      counts.appendChild(box);
    };
    countBox("PASS", "verified_pass", "pass");
    countBox("FAIL", "verified_fail", "fail");
    countBox("BLOCKED", "blocked_external", "blocked");
    countBox("UNTESTED", "not_yet_tested", "untested");
    counts.appendChild(el("span", "", ""));
    counts.lastChild.textContent = "doc: " + (data.doc || "—") + (data.generated_utc ? " @ " + data.generated_utc : "");
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

  /* ---------------- ticker (tape + stream) ---------------- */

  function renderTicker(data) {
    const tape = document.getElementById("tape-track");
    clear(tape);
    if (!data || data.error || !(data.items || []).length) {
      tape.appendChild(el("span", "tape-item tape-empty", "◇ NO OPPORTUNITY DATA THIS SESSION — WAITING FOR FEED"));
      return;
    }
    const items = data.items;
    const buildSpan = function (item, sep) {
      const node = el("span", "tape-item");
      node.appendChild(el("span", "", sep + " "));
      node.appendChild(el("span", "", timeHMS(item.ts_utc) + " "));
      node.appendChild(el("span", "", (item.event || "?").toUpperCase() + " "));
      node.appendChild(el("span", "", (item.market_id || "").slice(-16) + " "));
      if (item.combined !== null && item.combined !== undefined) {
        node.appendChild(el("span", "", "Σ=" + Number(item.combined).toFixed(2) + " "));
      }
      if (item.edge !== null && item.edge !== undefined) {
        node.appendChild(el("span", "", "EDGE=" + (item.edge >= 0 ? "+" : "") + Number(item.edge).toFixed(3) + " "));
      }
      node.appendChild(el("span", "", (item.outcome || "").toUpperCase()));
      if (item.reason) node.appendChild(el("span", "", " (" + item.reason + ")"));
      if (item.pnl !== null && item.pnl !== undefined) node.appendChild(el("span", "", " PNL=" + fmtUsd(item.pnl)));
      return node;
    };
    items.forEach(function (item) { tape.appendChild(buildSpan(item, "◆")); });
    tape.appendChild(buildSpan(items[items.length - 1], "◆"));
    tape.appendChild(el("span", "tape-item", "— END OF FEED — REPEAT"));
    tape.appendChild(buildSpan(items[items.length - 1], "◆"));

    const stream = document.getElementById("body-ticker-stream");
    clear(stream);
    items.forEach(function (item) {
      const row = el("div", "tick-line");
      row.appendChild(el("span", "wire-time", timeHMS(item.ts_utc) + " "));
      const edge = item.edge;
      row.appendChild(el("span", edge >= 0 ? "edge-pos" : "edge-neg",
        (edge === null || edge === undefined ? "—" : Number(edge).toFixed(4)) + " "));
      row.appendChild(el("span", "", (item.event || "?").toUpperCase() + " "));
      row.appendChild(el("span", "", String(item.market_id || "").slice(-24) + " "));
      const outcomeCls = item.outcome === "filled" ? "out-filled" : (item.outcome === "partial" ? "out-partial" : "out-rejected");
      row.appendChild(el("span", outcomeCls, (item.outcome || "").toUpperCase()));
      if (item.reason) row.appendChild(el("span", "", " " + item.reason));
      if (item.pnl !== null && item.pnl !== undefined) row.appendChild(el("span", "", " pnl=" + fmtUsd(item.pnl)));
      row.title = item.ts_utc + " " + (item.market_id || "") + " combined=" + (item.combined || "—") + " edge=" + (item.edge || "—") + " " + (item.outcome || "") + (item.reason ? " " + item.reason : "");
      stream.appendChild(row);
    });
    stream.scrollTop = stream.scrollHeight;
  }

  /* ---------------- clock & command bar ---------------- */

  function tickClock() {
    const now = new Date();
    const utc = document.getElementById("cmd-utc");
    if (utc) utc.textContent = now.toISOString().slice(0, 19).replace("T", " ") + "Z";
    const footer = document.getElementById("footer-ts");
    if (footer) footer.textContent = "rendered " + now.toISOString().slice(0, 19).replace("T", " ") + "Z";
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
    const mode = document.getElementById("cmd-mode");
    mode.textContent = data.mode || "—";
    const kill = document.getElementById("cmd-kill");
    const killState = (data.kill_switch || {}).state || "unknown";
    kill.className = "cmd-badge " + (killState === "halted" ? "halted" : "");
    kill.textContent = killState.toUpperCase();
    const session = data.session || {};
    const sess = document.getElementById("cmd-sess");
    sess.textContent = session.elapsed_s === null || session.elapsed_s === undefined ? "—" : String(Math.round(session.elapsed_s)) + "s";
    const loops = document.getElementById("cmd-loops");
    loops.textContent = fmtCount(session.loops);
  }

  /* ---------------- boot ---------------- */

  function start() {
    const loop = function (endpoint, renderFn) {
      const run = function () { poll(endpoint, renderFn); };
      run();
      setInterval(run, POLL_MS);
    };
    loop("/api/ticker?limit=60", renderTicker);
    loop("/api/heatmap?limit=48", renderHeatmap);
    loop("/api/pnl", renderPnl);
    loop("/api/depth", renderDepth);
    loop("/api/alerts?limit=50", renderAlerts);
    loop("/api/models", renderModels);
    loop("/api/latency", renderLatency);
    loop("/api/checklist", renderChecklist);
    loop("/api/command", function (data) {
      renderCommand(data);
      renderStatus(data);
    });
    setInterval(refreshAgeBadges, 1000);
    setInterval(tickClock, 1000);
    tickClock();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
