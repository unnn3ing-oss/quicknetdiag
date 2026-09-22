#!/usr/bin/env python3
"""家用網路醫生 —— 即時儀表板伺服器

在背景持續量測（路由器／對外連線／WiFi 訊號／你自訂的設備拓樸），
本機開一個網頁伺服器把即時狀態、歷史趨勢圖、診斷結果用網頁呈現。

用法：
    python3 dashboard/server.py
    然後瀏覽器開 http://127.0.0.1:8765

    macOS 可用 Safari「檔案 → 加入 Dock」把這個網頁存成獨立視窗的 App。

不需要額外安裝套件，Python 3.8+ 內建函式庫即可執行。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import netdiag  # noqa: E402

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(DASHBOARD_DIR, "static")
DEFAULT_TOPOLOGY_PATH = os.path.join(DASHBOARD_DIR, "topology.json")

SEVERITY_TO_STATUS = {"ok": "good", "low": "warning", "medium": "serious", "high": "critical"}
STATUS_RANK = {"good": 0, "warning": 1, "serious": 2, "critical": 3}

STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


def overall_status(issues: list) -> str:
    worst = "good"
    for issue in issues:
        role = SEVERITY_TO_STATUS.get(issue.severity, "good")
        if STATUS_RANK[role] > STATUS_RANK[worst]:
            worst = role
    return worst


def _split_ip(raw) -> tuple:
    """把 "192.168.10.100:51630" 拆成 (拿去 ping 的 host, 顯示用的完整字串)。"""
    if not raw:
        return None, None
    raw = str(raw).strip()
    if not raw:
        return None, None
    host = raw.split(":")[0] if raw.count(":") == 1 else raw  # 避免誤切 IPv6，只切「一個冒號」的情況
    return host, raw


def _from_scanner_schema(data: dict) -> list:
    """轉換另一套掃描工具（network-monitor）的 devices.json 格式。"""
    devices = data.get("devices", {})
    edges = data.get("topology", {})
    nodes = []
    for dev_id, dev in devices.items():
        edge = edges.get(dev_id, {})
        host, ip_display = _split_ip(dev.get("ip_address"))
        name = dev.get("custom_name") or dev.get("detected_name") or dev.get("series") or dev_id
        nodes.append({
            "id": dev_id,
            "name": name,
            "type": dev.get("device_type") or "default",
            "ip": host,
            "ip_display": ip_display,
            "location": dev.get("location") or "",
            "parent": edge.get("parent_mac") or None,
            "port_label": edge.get("port_label_src") or "",
            "connection_type": edge.get("connection_type") or "",
            "floor": dev.get("floor") or "Uncategorized",
            "x": dev.get("custom_x"),
            "y": dev.get("custom_y"),
        })
    return nodes


def _auto_layout(nodes: list) -> None:
    """替沒有座標的節點（例如手寫的簡易 topology.json）自動排版：depth 當列，同列依序排開。"""
    by_id = {n["id"]: n for n in nodes}
    depth_cache = {}

    def depth_of(node_id, guard=0):
        if node_id in depth_cache:
            return depth_cache[node_id]
        if guard > 20:
            return 0
        node = by_id.get(node_id)
        if not node or not node.get("parent") or node["parent"] not in by_id:
            depth_cache[node_id] = 0
            return 0
        d = depth_of(node["parent"], guard + 1) + 1
        depth_cache[node_id] = d
        return d

    rows: dict = {}
    for n in nodes:
        if n.get("x") is not None and n.get("y") is not None:
            continue
        d = depth_of(n["id"])
        rows.setdefault(d, []).append(n)

    for depth, row_nodes in rows.items():
        for i, n in enumerate(row_nodes):
            n["x"] = 80 + i * 220
            n["y"] = 80 + depth * 165


def load_topology(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "devices" in data and "topology" in data:
            nodes = _from_scanner_schema(data)
        else:
            nodes = data.get("nodes", [])
            for n in nodes:
                n.setdefault("ip_display", n.get("ip"))
                n.setdefault("floor", "住家")
                n.setdefault("port_label", "")
                n.setdefault("connection_type", "")
                n.setdefault("location", "")

        valid_ids = {n["id"] for n in nodes}
        for n in nodes:
            if n.get("parent") is not None and n["parent"] not in valid_ids:
                n["parent"] = None  # 設定檔打錯字時退成根節點，不讓整份拓樸圖噴掉

        _auto_layout(nodes)
        return nodes
    except (json.JSONDecodeError, OSError, KeyError) as exc:
        print(f"[警告] 讀取拓樸設定檔失敗（{path}）：{exc}，本輪拓樸圖略過。")
        return []


def ping_topology(nodes: list) -> list:
    """平行 ping 拓樸圖裡每個有 IP 的節點，回傳附上狀態的節點清單。"""
    results = []

    def check(node: dict) -> dict:
        out = dict(node)
        ip = node.get("ip")
        if not ip:
            out["up"] = None  # 沒有 IP（例如數據機）：不判斷線上/離線，只當結構節點
            out["avg_ms"] = None
            return out
        pr = netdiag.ping_host(ip, count=1, timeout_s=0.8)
        out["up"] = pr.received > 0
        out["avg_ms"] = pr.avg_ms
        return out

    if not nodes:
        return results
    with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
        results = list(pool.map(check, nodes))
    return results


class AppState:
    def __init__(self, history_cap: int, topology_path: str):
        self.lock = threading.Lock()
        self.history = deque(maxlen=history_cap)
        self.current = None
        self.topology = []
        self.topology_path = topology_path
        self.started_at = datetime.now()
        self.rounds = 0
        self.dropout_count = 0
        self.longest_dropout_s = 0.0
        self._in_dropout = False
        self._dropout_start = None
        self._up_rounds = 0

    def record_round(self, snap: "netdiag.Snapshot", issues: list, topology: list,
                      down_mbps: float = None, up_mbps: float = None) -> None:
        with self.lock:
            self.rounds += 1
            gp = snap.gateway_ping
            up = (gp is None) or (gp.received > 0)
            if up:
                self._up_rounds += 1
            now = time.time()
            if not up and not self._in_dropout:
                self._in_dropout = True
                self._dropout_start = now
            elif up and self._in_dropout:
                self._in_dropout = False
                dur = now - self._dropout_start
                self.dropout_count += 1
                self.longest_dropout_s = max(self.longest_dropout_s, dur)

            ext_name, ext_pr = snap.external_pings[0] if snap.external_pings else (None, None)
            wifi = snap.wifi
            sig = None
            if wifi and wifi.available:
                sig = wifi.signal_dbm if wifi.signal_dbm is not None else wifi.signal_pct

            point = {
                "t": snap.timestamp.isoformat(timespec="seconds"),
                "gw_loss": gp.loss_pct if gp else None,
                "gw_ms": round(gp.avg_ms, 1) if gp and gp.avg_ms is not None else None,
                "ext_loss": ext_pr.loss_pct if ext_pr else None,
                "ext_ms": round(ext_pr.avg_ms, 1) if ext_pr and ext_pr.avg_ms is not None else None,
                "wifi_signal": sig,
                "down_mbps": round(down_mbps, 2) if down_mbps is not None else None,
                "up_mbps": round(up_mbps, 2) if up_mbps is not None else None,
            }
            self.history.append(point)

            self.current = {
                "timestamp": snap.timestamp.isoformat(timespec="seconds"),
                "gateway": (
                    {
                        "ip": snap.gateway_ip,
                        "up": up,
                        "loss_pct": gp.loss_pct,
                        "avg_ms": gp.avg_ms,
                        "jitter_ms": gp.jitter_ms,
                    } if gp else None
                ),
                "external": (
                    {
                        "name": ext_name,
                        "host": ext_pr.host,
                        "loss_pct": ext_pr.loss_pct,
                        "avg_ms": ext_pr.avg_ms,
                    } if ext_pr else None
                ),
                "wifi": (
                    {
                        "ssid": wifi.ssid,
                        "signal_dbm": wifi.signal_dbm,
                        "signal_pct": wifi.signal_pct,
                        "channel": wifi.channel,
                    } if wifi and wifi.available else None
                ),
                "status": overall_status(issues),
                "issues": [
                    {"severity": i.severity, "title": i.title, "detail": i.detail, "suggestion": i.suggestion}
                    for i in issues
                ],
                "bandwidth": (
                    {"down_mbps": round(down_mbps, 2), "up_mbps": round(up_mbps, 2)}
                    if down_mbps is not None and up_mbps is not None else None
                ),
            }
            if topology:
                self.topology = topology

    def snapshot_json(self) -> dict:
        with self.lock:
            elapsed = max((datetime.now() - self.started_at).total_seconds(), 1e-9)
            uptime_pct = round(self._up_rounds / self.rounds * 100, 2) if self.rounds else 100.0
            return {
                "server_time": datetime.now().isoformat(timespec="seconds"),
                "since": self.started_at.isoformat(timespec="seconds"),
                "current": self.current,
                "history": list(self.history),
                "topology": self.topology,
                "stats": {
                    "rounds": self.rounds,
                    "elapsed_s": round(elapsed, 0),
                    "dropout_count": self.dropout_count,
                    "longest_dropout_s": round(self.longest_dropout_s, 0),
                    "uptime_pct": uptime_pct,
                },
            }


def monitor_loop(state: AppState, gateway_ip, ext_target, interval_s: float,
                  dns_every: int, topology_every: int, csv_path: str | None) -> None:
    csv_writer = None
    csv_file = None
    if csv_path:
        import csv
        is_new = not os.path.exists(csv_path)
        csv_file = open(csv_path, "a", newline="", encoding="utf-8-sig")
        csv_writer = csv.writer(csv_file)
        if is_new:
            csv_writer.writerow(["timestamp", "gateway_loss_pct", "gateway_avg_ms",
                                  "ext_loss_pct", "ext_avg_ms", "wifi_signal", "gateway_up",
                                  "down_mbps", "up_mbps"])

    round_no = 0
    prev_counters = None
    prev_time = None
    while True:
        start = time.time()
        round_no += 1
        check_dns = dns_every > 0 and (round_no - 1) % dns_every == 0
        snap = netdiag.take_snapshot(gateway_ip, [], ping_count=3, check_dns=check_dns,
                                      targets=[ext_target])
        issues = netdiag.diagnose(snap)

        topology = []
        if topology_every > 0 and (round_no - 1) % topology_every == 0:
            nodes = load_topology(state.topology_path)
            topology = ping_topology(nodes)

        down_mbps = up_mbps = None
        counters = netdiag.get_net_io_counters()
        now = time.time()
        if counters and prev_counters and prev_time:
            dt = now - prev_time
            d_recv = counters["bytes_recv"] - prev_counters["bytes_recv"]
            d_sent = counters["bytes_sent"] - prev_counters["bytes_sent"]
            if dt > 0 and d_recv >= 0 and d_sent >= 0:  # 負值代表計數器重置（介面重連等），這輪跳過不算
                down_mbps = d_recv * 8 / 1e6 / dt
                up_mbps = d_sent * 8 / 1e6 / dt
        prev_counters = counters
        prev_time = now

        state.record_round(snap, issues, topology, down_mbps, up_mbps)

        if csv_writer:
            gp = snap.gateway_ping
            ext_pr = snap.external_pings[0][1] if snap.external_pings else None
            sig = None
            if snap.wifi and snap.wifi.available:
                sig = snap.wifi.signal_dbm if snap.wifi.signal_dbm is not None else snap.wifi.signal_pct
            csv_writer.writerow([
                snap.timestamp.isoformat(timespec="seconds"),
                gp.loss_pct if gp else "",
                f"{gp.avg_ms:.1f}" if gp and gp.avg_ms is not None else "",
                ext_pr.loss_pct if ext_pr else "",
                f"{ext_pr.avg_ms:.1f}" if ext_pr and ext_pr.avg_ms is not None else "",
                sig if sig is not None else "",
                int((gp is None) or (gp.received > 0)),
                f"{down_mbps:.2f}" if down_mbps is not None else "",
                f"{up_mbps:.2f}" if up_mbps is not None else "",
            ])
            csv_file.flush()

        elapsed = time.time() - start
        time.sleep(max(0.2, interval_s - elapsed))


class Handler(BaseHTTPRequestHandler):
    state: AppState = None  # 由 main() 注入

    def log_message(self, fmt, *args):
        pass  # 安靜一點，不要把每個請求都印到終端機

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str) -> None:
        if not os.path.isfile(path):
            self.send_error(404, "Not Found")
            return
        ext = os.path.splitext(path)[1]
        ctype = STATIC_CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send_json(self.state.snapshot_json())
            return
        if path == "/":
            self._send_file(os.path.join(STATIC_DIR, "index.html"))
            return
        # 只允許 static/ 底下的白名單檔案，避免任意檔案讀取
        safe_name = os.path.basename(path)
        if path.startswith("/") and safe_name in ("style.css", "app.js"):
            self._send_file(os.path.join(STATIC_DIR, safe_name))
            return
        self.send_error(404, "Not Found")


def main() -> None:
    parser = argparse.ArgumentParser(description="家用網路醫生 —— 即時儀表板伺服器")
    parser.add_argument("--port", type=int, default=8765, help="伺服器監聽埠號，預設 8765")
    parser.add_argument("--host", type=str, default="127.0.0.1",
                         help="監聽位址，預設只給本機瀏覽存取；要讓手機/其他裝置在家用網路內連進來看，"
                              "改成 0.0.0.0（僅建議在信任的家用網路使用）")
    parser.add_argument("--interval", type=float, default=5.0, help="量測間隔秒數，預設 5 秒")
    parser.add_argument("--gateway", type=str, default=None, help="手動指定路由器 IP（預設自動偵測）")
    parser.add_argument("--history", type=int, default=720,
                         help="網頁圖表保留的資料點數，預設 720（5 秒一次約等於 1 小時）")
    parser.add_argument("--dns-every", type=int, default=12,
                         help="每幾輪做一次完整 DNS 檢測，預設 12（約每分鐘一次），設 0 關閉")
    parser.add_argument("--topology-every", type=int, default=1,
                         help="每幾輪重新量測一次拓樸圖節點狀態，預設每輪都測")
    parser.add_argument("--topology-file", type=str, default=DEFAULT_TOPOLOGY_PATH,
                         help="拓樸圖設定檔路徑，預設 dashboard/topology.json")
    parser.add_argument("--csv", type=str, default=None, help="同時把每輪量測附加寫入這個 CSV 檔")
    args = parser.parse_args()

    gateway_ip = args.gateway or netdiag.get_default_gateway()
    ext_name, ext_host = netdiag.DEFAULT_EXTERNAL_TARGETS[0]

    state = AppState(history_cap=args.history, topology_path=args.topology_file)
    Handler.state = state

    t = threading.Thread(
        target=monitor_loop,
        args=(state, gateway_ip, (ext_name, ext_host), args.interval,
              args.dns_every, args.topology_every, args.csv),
        daemon=True,
    )
    t.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}"
    print(f"家用網路醫生儀表板啟動了，瀏覽器開：{url}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中...")
        server.shutdown()


if __name__ == "__main__":
    main()
