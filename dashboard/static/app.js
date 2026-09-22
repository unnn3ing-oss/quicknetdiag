(() => {
  "use strict";

  const POLL_MS = 3000;

  const css = getComputedStyle(document.documentElement);
  const COLOR = {
    gw: css.getPropertyValue("--series-gw").trim(),
    ext: css.getPropertyValue("--series-ext").trim(),
    wifi: css.getPropertyValue("--series-wifi").trim(),
    grid: "rgba(255,255,255,0.08)",
    axis: "rgba(255,255,255,0.35)",
    muted: css.getPropertyValue("--text-muted").trim(),
  };

  const STATUS_LABEL = { good: "正常", warning: "留意", serious: "注意", critical: "嚴重" };

  // ---------------------------------------------------------------------
  // 小工具
  // ---------------------------------------------------------------------

  function fmt(n, digits = 0) {
    if (n === null || n === undefined || Number.isNaN(n)) return "--";
    return Number(n).toFixed(digits);
  }

  function timeLabel(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toTimeString().slice(0, 8);
  }

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else node.setAttribute(k, v);
    }
    for (const c of children) node.appendChild(c);
    return node;
  }

  // ---------------------------------------------------------------------
  // 狀態列與數值卡
  // ---------------------------------------------------------------------

  function updateStatusPill(status) {
    const pill = document.getElementById("statusPill");
    const text = document.getElementById("statusText");
    pill.dataset.status = status || "good";
    text.textContent = STATUS_LABEL[status] || "正常";
  }

  function updateStatCards(current, stats) {
    if (!current) return;

    const gw = current.gateway;
    document.getElementById("gwMs").textContent = gw && gw.avg_ms != null ? fmt(gw.avg_ms, 1) : (gw ? "逾時" : "--");
    document.getElementById("gwLoss").textContent = gw ? `${fmt(gw.loss_pct, 0)}%` : "--";
    document.getElementById("gwJitter").textContent = gw && gw.jitter_ms != null ? fmt(gw.jitter_ms, 1) : "--";

    const ext = current.external;
    document.getElementById("extHost").textContent = ext ? `(${ext.name || ext.host})` : "";
    document.getElementById("extMs").textContent = ext && ext.avg_ms != null ? fmt(ext.avg_ms, 1) : (ext ? "逾時" : "--");
    document.getElementById("extLoss").textContent = ext ? `${fmt(ext.loss_pct, 0)}%` : "--";

    const wifi = current.wifi;
    const wifiSignalEl = document.getElementById("wifiSignal");
    const wifiBar = document.getElementById("wifiBar");
    if (wifi) {
      document.getElementById("wifiSsid").textContent = `(${wifi.ssid || "未知"})`;
      document.getElementById("wifiChannel").textContent = wifi.channel || "--";
      if (wifi.signal_pct != null) {
        wifiSignalEl.textContent = `${fmt(wifi.signal_pct, 0)}%`;
        wifiBar.style.width = `${Math.max(0, Math.min(100, wifi.signal_pct))}%`;
      } else if (wifi.signal_dbm != null) {
        wifiSignalEl.textContent = `${fmt(wifi.signal_dbm, 0)}dBm`;
        const pct = Math.max(0, Math.min(100, (wifi.signal_dbm + 90) / 60 * 100));
        wifiBar.style.width = `${pct}%`;
      }
    } else {
      document.getElementById("wifiSsid").textContent = "";
      wifiSignalEl.textContent = "無資料";
      wifiBar.style.width = "0%";
      document.getElementById("wifiChannel").textContent = "--";
    }

    if (stats) {
      document.getElementById("uptimePct").textContent = fmt(stats.uptime_pct, 1);
      document.getElementById("dropoutCount").textContent = stats.dropout_count;
      document.getElementById("longestDropout").textContent = fmt(stats.longest_dropout_s, 0);
    }

    document.getElementById("lastUpdate").textContent = timeLabel(current.timestamp);
  }

  function updateIssues(issues) {
    const list = document.getElementById("issuesList");
    list.innerHTML = "";
    if (!issues || !issues.length) {
      list.appendChild(el("li", { class: "issue-item", "data-severity": "ok" }, [
        el("div", {}, [document.createTextNode("尚無資料")]),
      ]));
      return;
    }
    for (const issue of issues) {
      const item = el("li", { class: "issue-item", "data-severity": issue.severity });
      const body = el("div", {});
      body.appendChild(el("div", { class: "issue-title" }, [document.createTextNode(issue.title)]));
      if (issue.detail) {
        body.appendChild(el("div", { class: "issue-detail" }, [document.createTextNode(issue.detail)]));
      }
      if (issue.suggestion) {
        body.appendChild(el("div", { class: "issue-suggestion" }, [document.createTextNode(`建議：${issue.suggestion}`)]));
      }
      item.appendChild(body);
      list.appendChild(item);
    }
  }

  // ---------------------------------------------------------------------
  // 折線圖（手繪 canvas，不依賴外部圖表庫，離線也能跑）
  // ---------------------------------------------------------------------

  function resizeCanvasToDisplaySize(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height));
    if (canvas._cssW !== w || canvas._cssH !== h) {
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas._cssW = w;
      canvas._cssH = h;
    }
    return { w, h, dpr };
  }

  class LineChart {
    constructor(canvas, tooltipEl, seriesDefs, unit) {
      this.canvas = canvas;
      this.tooltipEl = tooltipEl;
      this.seriesDefs = seriesDefs; // [{key, color, label}]
      this.unit = unit;
      this.data = [];
      this.ctx = canvas.getContext("2d");
      canvas.addEventListener("mousemove", (e) => this._onHover(e));
      canvas.addEventListener("mouseleave", () => this._hideTooltip());
    }

    setData(data) {
      this.data = data || [];
      this.render();
    }

    _scales(w, h) {
      const pad = { top: 10, right: 10, bottom: 20, left: 34 };
      let min = Infinity, max = -Infinity;
      for (const p of this.data) {
        for (const s of this.seriesDefs) {
          const v = p[s.key];
          if (v === null || v === undefined) continue;
          if (v < min) min = v;
          if (v > max) max = v;
        }
      }
      if (!Number.isFinite(min)) { min = 0; max = 1; }
      if (min === max) { min -= 1; max += 1; }
      const span = max - min;
      min -= span * 0.12;
      max += span * 0.12;
      if (this.unit === "%") { min = Math.max(0, min); max = Math.max(max, 5); }
      if (min < 0 && this.unit !== "%") min = Math.min(min, 0);

      const n = Math.max(this.data.length - 1, 1);
      const x = (i) => pad.left + (i / n) * (w - pad.left - pad.right);
      const y = (v) => {
        const t = (v - min) / (max - min || 1);
        return h - pad.bottom - t * (h - pad.top - pad.bottom);
      };
      return { pad, min, max, x, y };
    }

    render(hoverIndex = null) {
      const { w, h, dpr } = resizeCanvasToDisplaySize(this.canvas);
      const ctx = this.ctx;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      if (this.data.length < 2) {
        ctx.fillStyle = COLOR.muted;
        ctx.font = "12px var(--sans)";
        ctx.textAlign = "center";
        ctx.fillText("尚無資料，等待第一輪量測…", w / 2, h / 2);
        return;
      }

      const { pad, min, max, x, y } = this._scales(w, h);

      // 網格與 y 軸標籤
      ctx.strokeStyle = COLOR.grid;
      ctx.lineWidth = 1;
      ctx.fillStyle = COLOR.muted;
      ctx.font = "10px var(--mono)";
      ctx.textAlign = "right";
      const ticks = 4;
      for (let i = 0; i <= ticks; i++) {
        const v = min + ((max - min) * i) / ticks;
        const yy = y(v);
        ctx.beginPath();
        ctx.moveTo(pad.left, yy + 0.5);
        ctx.lineTo(w - pad.right, yy + 0.5);
        ctx.stroke();
        ctx.fillText(v.toFixed(this.unit === "%" ? 0 : 0), pad.left - 6, yy + 3);
      }

      // x 軸時間標籤（頭尾與中間；頭尾貼齊邊界對齊，避免文字被裁掉）
      const lastIdx = this.data.length - 1;
      const midIdx = Math.floor(lastIdx / 2);
      ctx.textAlign = "left";
      ctx.fillText(timeLabel(this.data[0].t).slice(0, 5), x(0), h - 5);
      ctx.textAlign = "center";
      ctx.fillText(timeLabel(this.data[midIdx].t).slice(0, 5), x(midIdx), h - 5);
      ctx.textAlign = "right";
      ctx.fillText(timeLabel(this.data[lastIdx].t).slice(0, 5), x(lastIdx), h - 5);

      // 各系列折線
      for (const s of this.seriesDefs) {
        ctx.beginPath();
        let started = false;
        for (let i = 0; i < this.data.length; i++) {
          const v = this.data[i][s.key];
          if (v === null || v === undefined) { started = false; continue; }
          const px = x(i), py = y(v);
          if (!started) { ctx.moveTo(px, py); started = true; }
          else ctx.lineTo(px, py);
        }
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.stroke();
      }

      if (hoverIndex !== null && this.data[hoverIndex]) {
        const hx = x(hoverIndex);
        ctx.strokeStyle = COLOR.axis;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(hx, pad.top);
        ctx.lineTo(hx, h - pad.bottom);
        ctx.stroke();
        for (const s of this.seriesDefs) {
          const v = this.data[hoverIndex][s.key];
          if (v === null || v === undefined) continue;
          ctx.beginPath();
          ctx.fillStyle = s.color;
          ctx.arc(hx, y(v), 3.5, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }

    _onHover(e) {
      if (this.data.length < 2) return;
      const rect = this.canvas.getBoundingClientRect();
      const relX = e.clientX - rect.left;
      const n = this.data.length - 1;
      const pad = 34;
      const usable = rect.width - pad - 10;
      let ratio = (relX - pad) / usable;
      ratio = Math.max(0, Math.min(1, ratio));
      const idx = Math.round(ratio * n);
      this.render(idx);
      this._showTooltip(e, idx, rect);
    }

    _showTooltip(e, idx, rect) {
      const point = this.data[idx];
      if (!point) return;
      const rows = this.seriesDefs
        .map((s) => {
          const v = point[s.key];
          const vs = v === null || v === undefined ? "--" : `${fmt(v, 1)}${this.unit}`;
          return `<div class="tt-row"><i class="dot" style="background:${s.color}"></i>${s.label} ${vs}</div>`;
        })
        .join("");
      this.tooltipEl.innerHTML = `<div class="tt-row">${timeLabel(point.t)}</div>${rows}`;
      this.tooltipEl.hidden = false;
      const relX = e.clientX - rect.left;
      this.tooltipEl.style.left = `${Math.max(50, Math.min(rect.width - 50, relX))}px`;
    }

    _hideTooltip() {
      this.tooltipEl.hidden = true;
      this.render(null);
    }
  }

  // ---------------------------------------------------------------------
  // 網路架構拓樸圖
  // ---------------------------------------------------------------------

  const TOPO_ICONS = {
    modem: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="8" width="18" height="9" rx="2"/><circle cx="8" cy="12.5" r="1"/><circle cx="12" cy="12.5" r="1"/></svg>',
    router: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="11" width="18" height="8" rx="2"/><path d="M8 11 6 4M16 11l2-7"/><circle cx="8" cy="15" r="1"/></svg>',
    switch: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="7" width="18" height="10" rx="2"/><path d="M6.5 17v2M10.5 17v2M14.5 17v2M18.5 17v2"/></svg>',
    nvr: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="6" width="14" height="12" rx="2"/><path d="M17 10l4-2.5v9L17 14"/></svg>',
    mesh: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 9a12 12 0 0 1 16 0M7 12.5a7.5 7.5 0 0 1 10 0"/><circle cx="12" cy="17" r="1.4" fill="currentColor" stroke="none"/></svg>',
    default: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="7"/></svg>',
  };

  function computeDepths(nodes) {
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const depth = new Map();
    function depthOf(id, guard = 0) {
      if (depth.has(id)) return depth.get(id);
      if (guard > 20) return 0;
      const node = byId.get(id);
      if (!node || node.parent == null || !byId.has(node.parent)) {
        depth.set(id, 0);
        return 0;
      }
      const d = depthOf(node.parent, guard + 1) + 1;
      depth.set(id, d);
      return d;
    }
    for (const n of nodes) depthOf(n.id);
    return depth;
  }

  function renderTopology(nodes) {
    const root = document.getElementById("topologyRoot");
    root.innerHTML = "";
    if (!nodes || !nodes.length) {
      root.appendChild(el("div", { class: "topo-empty" }, [
        document.createTextNode("尚未設定架構圖，編輯 dashboard/topology.json 加入你家的設備即可自動顯示。"),
      ]));
      return;
    }

    const depths = computeDepths(nodes);
    const maxDepth = Math.max(...nodes.map((n) => depths.get(n.id) || 0));
    const rows = [];
    for (let d = 0; d <= maxDepth; d++) rows.push([]);
    for (const n of nodes) rows[depths.get(n.id) || 0].push(n);

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "topo-svg");
    root.appendChild(svg);

    const nodeEls = new Map();
    for (const row of rows) {
      const rowEl = el("div", { class: "topo-row" });
      for (const n of row) {
        const up = n.ip ? n.up : null;
        const card = el("div", { class: "topo-node", "data-up": String(up), "data-id": n.id });
        card.appendChild(el("span", { class: "led" }));
        card.appendChild(el("span", { class: "topo-icon", html: TOPO_ICONS[n.type] || TOPO_ICONS.default }));
        const meta = el("div", { class: "topo-meta" });
        meta.appendChild(el("span", { class: "topo-name" }, [document.createTextNode(n.name || n.id)]));
        const ipText = n.ip ? (n.avg_ms != null ? `${n.ip} · ${fmt(n.avg_ms, 0)}ms` : n.ip) : "—";
        meta.appendChild(el("span", { class: "topo-ip" }, [document.createTextNode(ipText)]));
        card.appendChild(meta);
        rowEl.appendChild(card);
        nodeEls.set(n.id, card);
      }
      root.appendChild(rowEl);
    }

    requestAnimationFrame(() => {
      const rootRect = root.getBoundingClientRect();
      svg.setAttribute("width", root.scrollWidth);
      svg.setAttribute("height", root.scrollHeight);
      svg.setAttribute("viewBox", `0 0 ${root.scrollWidth} ${root.scrollHeight}`);
      let paths = "";
      for (const n of nodes) {
        if (n.parent == null) continue;
        const childEl = nodeEls.get(n.id);
        const parentEl = nodeEls.get(n.parent);
        if (!childEl || !parentEl) continue;
        const c = childEl.getBoundingClientRect();
        const p = parentEl.getBoundingClientRect();
        const x1 = p.left - rootRect.left + p.width / 2 + root.scrollLeft;
        const y1 = p.bottom - rootRect.top + root.scrollTop;
        const x2 = c.left - rootRect.left + c.width / 2 + root.scrollLeft;
        const y2 = c.top - rootRect.top + root.scrollTop;
        const midY = (y1 + y2) / 2;
        paths += `<path d="M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}" fill="none" stroke="rgba(255,255,255,0.22)" stroke-width="1.4"/>`;
      }
      svg.innerHTML = paths;
    });
  }

  // ---------------------------------------------------------------------
  // 輪詢主迴圈
  // ---------------------------------------------------------------------

  const latencyChart = new LineChart(
    document.getElementById("latencyChart"),
    document.getElementById("latencyTooltip"),
    [
      { key: "gw_ms", color: COLOR.gw, label: "路由器" },
      { key: "ext_ms", color: COLOR.ext, label: "對外" },
    ],
    "ms"
  );

  const lossChart = new LineChart(
    document.getElementById("lossChart"),
    document.getElementById("lossTooltip"),
    [
      { key: "gw_loss", color: COLOR.gw, label: "路由器" },
      { key: "ext_loss", color: COLOR.ext, label: "對外" },
    ],
    "%"
  );

  let consecutiveErrors = 0;

  async function poll() {
    try {
      const res = await fetch("/api/status", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      consecutiveErrors = 0;
      document.getElementById("connError").hidden = true;

      if (data.current) {
        updateStatusPill(data.current.status);
        updateStatCards(data.current, data.stats);
        updateIssues(data.current.issues);
      }
      latencyChart.setData(data.history);
      lossChart.setData(data.history);
      if (data.topology) renderTopology(data.topology);
    } catch (err) {
      consecutiveErrors += 1;
      if (consecutiveErrors >= 2) {
        document.getElementById("connError").hidden = false;
      }
    }
  }

  window.addEventListener("resize", () => {
    latencyChart.render();
    lossChart.render();
  });

  poll();
  setInterval(poll, POLL_MS);
})();
