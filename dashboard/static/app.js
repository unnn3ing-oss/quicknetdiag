(() => {
  "use strict";

  const POLL_MS = 3000;

  const css = getComputedStyle(document.documentElement);
  const COLOR = {
    gw: css.getPropertyValue("--series-gw").trim(),
    ext: css.getPropertyValue("--series-ext").trim(),
    wifi: css.getPropertyValue("--series-wifi").trim(),
    down: css.getPropertyValue("--series-down").trim(),
    up: css.getPropertyValue("--series-up").trim(),
    grid: "rgba(11,11,11,0.08)",
    axis: "rgba(11,11,11,0.35)",
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

  function escapeXml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;",
    }[c]));
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

    const bw = current.bandwidth;
    document.getElementById("downMbps").textContent = bw ? fmt(bw.down_mbps, 1) : "--";
    document.getElementById("upMbps").textContent = bw ? fmt(bw.up_mbps, 1) : "--";
    document.getElementById("bandwidthNote").textContent = bw ? "下載／上傳" : "此平台暫不支援量測";

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
    modem: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="8" width="18" height="9" rx="2"/><circle cx="8" cy="12.5" r="1"/><circle cx="12" cy="12.5" r="1"/></svg>',
    router: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="11" width="18" height="8" rx="2"/><path d="M8 11 6 4M16 11l2-7"/><circle cx="8" cy="15" r="1"/></svg>',
    switch: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="7" width="18" height="10" rx="2"/><path d="M6.5 17v2M10.5 17v2M14.5 17v2M18.5 17v2"/></svg>',
    poe_switch: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="7" width="18" height="10" rx="2"/><path d="M6.5 17v2M10.5 17v2M14.5 17v2M18.5 17v2"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/></svg>',
    nvr: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="6" width="14" height="12" rx="2"/><path d="M17 10l4-2.5v9L17 14"/></svg>',
    ipcam: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="2" y="9" width="13" height="9" rx="2"/><path d="M15 12.5l6-3.5v8l-6-3.5"/><circle cx="8.5" cy="13.5" r="2"/></svg>',
    ap: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 9a12 12 0 0 1 16 0M7 12.5a7.5 7.5 0 0 1 10 0"/><circle cx="12" cy="17" r="1.4" fill="currentColor" stroke="none"/></svg>',
    mesh: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 9a12 12 0 0 1 16 0M7 12.5a7.5 7.5 0 0 1 10 0"/><circle cx="12" cy="17" r="1.4" fill="currentColor" stroke="none"/></svg>',
    smart_home: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="10" r="6"/><path d="M9.5 20h5M10.5 16.5h3"/></svg>',
    unknown: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="7"/></svg>',
    default: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="7"/></svg>',
  };

  const FLOOR_LABELS = { Uncategorized: "未分類區" };
  const CARD_W = 172;
  const CARD_H = 62;
  const TOPO_SCALE = 0.5;
  const TOPO_PAD = 26;

  function floorSortKey(floor) {
    if (floor === "Uncategorized") return [2, floor];
    const m = /^(\d+)/.exec(floor);
    if (m) return [0, parseInt(m[1], 10), floor];
    return [1, floor];
  }

  function renderTopology(nodes) {
    const root = document.getElementById("topologyRoot");
    root.innerHTML = "";
    if (!nodes || !nodes.length) {
      root.appendChild(el("div", { class: "topo-empty" }, [
        document.createTextNode("尚未設定架構圖，編輯 dashboard/topology.json 加入你家的設備即可自動顯示（或指向現有掃描工具的 devices.json）。"),
      ]));
      return;
    }

    const byId = new Map(nodes.map((n) => [n.id, n]));
    const pos = (n) => ({ x: (n.x || 0) * TOPO_SCALE, y: (n.y || 0) * TOPO_SCALE });

    let maxX = 0, maxY = 0;
    for (const n of nodes) {
      const p = pos(n);
      maxX = Math.max(maxX, p.x + CARD_W);
      maxY = Math.max(maxY, p.y + CARD_H);
    }
    const canvas = el("div", { class: "topo-canvas" });
    canvas.style.width = `${maxX + TOPO_PAD}px`;
    canvas.style.height = `${maxY + TOPO_PAD}px`;

    // 樓層外框（同一層的設備框在一起，跟原圖一樣分區）
    const floors = new Map();
    for (const n of nodes) {
      const key = n.floor || "Uncategorized";
      if (!floors.has(key)) floors.set(key, []);
      floors.get(key).push(n);
    }
    const floorKeys = [...floors.keys()].sort((a, b) => {
      const ka = floorSortKey(a), kb = floorSortKey(b);
      return ka < kb ? -1 : ka > kb ? 1 : 0;
    });
    if (floorKeys.length > 1) {
      for (const key of floorKeys) {
        const members = floors.get(key);
        let fx0 = Infinity, fy0 = Infinity, fx1 = -Infinity, fy1 = -Infinity;
        for (const n of members) {
          const p = pos(n);
          fx0 = Math.min(fx0, p.x);
          fy0 = Math.min(fy0, p.y);
          fx1 = Math.max(fx1, p.x + CARD_W);
          fy1 = Math.max(fy1, p.y + CARD_H);
        }
        const pad = 20;
        const box = el("div", { class: "topo-floor-box" });
        box.style.left = `${fx0 - pad}px`;
        box.style.top = `${fy0 - pad - 22}px`;
        box.style.width = `${fx1 - fx0 + pad * 2}px`;
        box.style.height = `${fy1 - fy0 + pad * 2 + 22}px`;
        const label = el("div", { class: "topo-floor-label" }, [
          document.createTextNode(FLOOR_LABELS[key] || key),
        ]);
        box.appendChild(label);
        canvas.appendChild(box);
      }
    }

    // 連線（含 port 標籤），畫在節點卡片底下。同一個父節點底下的多個子節點，
    // 從父卡片底邊不同的 x 位置分別出發（fan-out），避免全部疊在正中央同一點、
    // 讓線條彼此糾纏；顏色/線型依連線類型區分（有線／PoE／WiFi），方便一眼分辨。
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "topo-svg");
    svg.setAttribute("width", maxX + TOPO_PAD);
    svg.setAttribute("height", maxY + TOPO_PAD);

    const childrenByParent = new Map();
    for (const n of nodes) {
      if (!n.parent || !byId.has(n.parent)) continue;
      if (!childrenByParent.has(n.parent)) childrenByParent.set(n.parent, []);
      childrenByParent.get(n.parent).push(n);
    }
    for (const kids of childrenByParent.values()) {
      kids.sort((a, b) => pos(a).x - pos(b).x);
    }

    function edgeStyle(connType) {
      const t = (connType || "").toUpperCase();
      if (t.includes("POE")) return { color: COLOR.ext, dash: "" };
      if (t.includes("WIFI") || t.includes("WI-FI")) return { color: COLOR.wifi, dash: "5 4" };
      if (t.includes("CAT") || t.includes("有線") || t.includes("ETHERNET")) return { color: COLOR.gw, dash: "" };
      return { color: COLOR.muted, dash: "" };
    }

    let svgInner = "";
    for (const n of nodes) {
      if (!n.parent || !byId.has(n.parent)) continue;
      const parent = byId.get(n.parent);
      const pp = pos(parent), cp = pos(n);

      const siblings = childrenByParent.get(n.parent);
      const idx = siblings.indexOf(n);
      const fanX = siblings.length > 1
        ? pp.x + 20 + ((CARD_W - 40) * idx) / (siblings.length - 1)
        : pp.x + CARD_W / 2;

      const x1 = fanX, y1 = pp.y + CARD_H;
      const x2 = cp.x + CARD_W / 2, y2 = cp.y;
      const midY = (y1 + y2) / 2;
      const style = edgeStyle(n.connection_type);
      const dashAttr = style.dash ? ` stroke-dasharray="${style.dash}"` : "";
      svgInner += `<path d="M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}" fill="none" stroke="${style.color}" stroke-opacity="0.55" stroke-width="1.4"${dashAttr}/>`;
      if (n.port_label) {
        const lx = (x1 + x2) / 2, ly = midY;
        svgInner += `<text x="${lx}" y="${ly}" text-anchor="middle" dominant-baseline="middle" class="topo-edge-label" paint-order="stroke" stroke="var(--surface)" stroke-width="4">${escapeXml(n.port_label)}</text>`;
      }
    }
    svg.innerHTML = svgInner;
    canvas.appendChild(svg);

    // 節點卡片
    for (const n of nodes) {
      const p = pos(n);
      const up = n.ip ? n.up : null;
      const card = el("div", { class: "topo-node", "data-up": String(up), "data-id": n.id });
      card.style.left = `${p.x}px`;
      card.style.top = `${p.y}px`;
      card.style.width = `${CARD_W}px`;
      card.appendChild(el("span", { class: "led" }));
      card.appendChild(el("span", { class: "topo-icon", html: TOPO_ICONS[n.type] || TOPO_ICONS.default }));
      const meta = el("div", { class: "topo-meta" });
      meta.appendChild(el("span", { class: "topo-name" }, [document.createTextNode(n.name || n.id)]));
      const ipBase = n.ip_display || n.ip;
      const ipText = ipBase ? (n.avg_ms != null ? `${ipBase} · ${fmt(n.avg_ms, 0)}ms` : ipBase) : (n.location || "—");
      meta.appendChild(el("span", { class: "topo-ip" }, [document.createTextNode(ipText)]));
      card.appendChild(meta);
      canvas.appendChild(card);
    }

    root.appendChild(canvas);
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

  const bandwidthChart = new LineChart(
    document.getElementById("bandwidthChart"),
    document.getElementById("bandwidthTooltip"),
    [
      { key: "down_mbps", color: COLOR.down, label: "下載" },
      { key: "up_mbps", color: COLOR.up, label: "上傳" },
    ],
    "Mbps"
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
      bandwidthChart.setData(data.history);
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
    bandwidthChart.render();
  });

  poll();
  setInterval(poll, POLL_MS);
})();
