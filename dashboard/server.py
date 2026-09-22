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
import copy
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

CARD_W = 172
CARD_H = 62
CARD_GAP = 16
NODE_FIELDS = ("name", "ip", "ip_display", "mac", "location", "type",
               "connection_type", "port_label", "floor")


def overall_status(issues: list) -> str:
    worst = "good"
    for issue in issues:
        role = SEVERITY_TO_STATUS.get(issue.severity, "good")
        if STATUS_RANK[role] > STATUS_RANK[worst]:
            worst = role
    return worst


# --------------------------------------------------------------------------
# 拓樸圖：讀取／格式轉換
# --------------------------------------------------------------------------

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
            "mac": dev.get("mac_address") or "",
            "location": dev.get("location") or "",
            "parent": edge.get("parent_mac") or None,
            "port_label": edge.get("port_label_src") or "",
            "connection_type": edge.get("connection_type") or "",
            "floor": dev.get("floor") or "Uncategorized",
            "x": dev.get("custom_x"),
            "y": dev.get("custom_y"),
        })
    return nodes


def _to_scanner_schema(nodes: list, base: dict) -> dict:
    """把節點清單寫回另一套工具的 devices.json 格式，保留每台設備原本我們不管理的欄位。"""
    data = copy.deepcopy(base)
    old_devices = data.get("devices", {})
    devices = {}
    topology_map = {}
    for n in nodes:
        dev = dict(old_devices.get(n["id"], {}))
        dev["id"] = n["id"]
        dev["custom_name"] = n.get("name") or n["id"]
        dev["ip_address"] = n.get("ip_display") or n.get("ip") or ""
        dev["mac_address"] = n.get("mac") or ""
        dev["location"] = n.get("location") or ""
        dev["device_type"] = n.get("type") or "unknown"
        dev["floor"] = n.get("floor") or "Uncategorized"
        dev["custom_x"] = n.get("x")
        dev["custom_y"] = n.get("y")
        devices[n["id"]] = dev
        if n.get("parent"):
            label = n.get("port_label") or f"{n['parent']} - {n['id']}"
            topology_map[n["id"]] = {
                "parent_mac": n["parent"],
                "child_mac": n["id"],
                "connection_type": n.get("connection_type") or "",
                "port_label_src": label,
                "port_label_tgt": label,
            }
    data["devices"] = devices
    data["topology"] = topology_map
    return data


def _to_simple_schema(nodes: list, base: dict) -> dict:
    data = copy.deepcopy(base)
    clean = []
    for n in nodes:
        clean.append({
            "id": n["id"],
            "name": n.get("name") or n["id"],
            "type": n.get("type") or "default",
            "ip": n.get("ip"),
            "mac": n.get("mac") or "",
            "location": n.get("location") or "",
            "parent": n.get("parent"),
            "port_label": n.get("port_label") or "",
            "connection_type": n.get("connection_type") or "",
            "floor": n.get("floor") or "住家",
            "x": n.get("x"),
            "y": n.get("y"),
        })
    data["nodes"] = clean
    return data


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


def load_topology_full(path: str) -> tuple:
    """回傳 (nodes, meta)。meta 保留原始檔案結構與格式，供回寫使用。"""
    if not os.path.exists(path):
        return [], {"schema": "simple", "raw": {"nodes": []}, "path": path}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "devices" in data and "topology" in data:
            nodes = _from_scanner_schema(data)
            meta = {"schema": "scanner", "raw": data, "path": path}
        else:
            nodes = data.get("nodes", [])
            for n in nodes:
                n.setdefault("ip_display", n.get("ip"))
                n.setdefault("mac", "")
                n.setdefault("floor", "住家")
                n.setdefault("port_label", "")
                n.setdefault("connection_type", "")
                n.setdefault("location", "")
            meta = {"schema": "simple", "raw": data, "path": path}

        valid_ids = {n["id"] for n in nodes}
        for n in nodes:
            if n.get("parent") is not None and n["parent"] not in valid_ids:
                n["parent"] = None  # 設定檔打錯字時退成根節點，不讓整份拓樸圖噴掉

        _auto_layout(nodes)
        return nodes, meta
    except (json.JSONDecodeError, OSError, KeyError) as exc:
        print(f"[警告] 讀取拓樸設定檔失敗（{path}）：{exc}，本輪拓樸圖略過。")
        return [], {"schema": "simple", "raw": {"nodes": []}, "path": path}


def load_topology(path: str) -> list:
    nodes, _ = load_topology_full(path)
    return nodes


def save_topology(nodes: list, meta: dict) -> None:
    """依原始格式把節點清單寫回檔案（覆寫），只動我們管理的欄位，其餘保留。原子寫入避免寫壞檔案。"""
    if meta["schema"] == "scanner":
        data = _to_scanner_schema(nodes, meta["raw"])
    else:
        data = _to_simple_schema(nodes, meta["raw"])
    path = meta["path"]
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    meta["raw"] = data


# --------------------------------------------------------------------------
# 拓樸圖：防重疊排版 ＋ 依拖曳位置判斷樓層
# --------------------------------------------------------------------------

def _rects_overlap(ax, ay, bx, by, gap) -> bool:
    return (ax < bx + CARD_W + gap and ax + CARD_W + gap > bx and
            ay < by + CARD_H + gap and ay + CARD_H + gap > by)


def resolve_single(others: list, target: dict, gap: int = CARD_GAP, max_steps: int = 400) -> None:
    """把 target 往外推，直到不再跟 others 裡任何一張卡片重疊（含固定間距）；只動 target，不動其他卡片。"""
    import math
    for _ in range(max_steps):
        collided = None
        for n in others:
            if n is target or n["id"] == target["id"]:
                continue
            if _rects_overlap(target["x"], target["y"], n["x"], n["y"], gap):
                collided = n
                break
        if not collided:
            return
        tx = target["x"] + CARD_W / 2
        ty = target["y"] + CARD_H / 2
        ox = collided["x"] + CARD_W / 2
        oy = collided["y"] + CARD_H / 2
        dx, dy = tx - ox, ty - oy
        dist = math.hypot(dx, dy) or 1.0
        dx, dy = dx / dist, dy / dist
        target["x"] += dx * 14
        target["y"] += dy * 14
    # 推了 max_steps 次還沒喬開（極端密集），退而求其次找一個空位網格
    _place_in_free_grid(others, target, gap)


def _place_in_free_grid(others: list, target: dict, gap: int) -> None:
    step_x, step_y = CARD_W + gap, CARD_H + gap
    for row in range(60):
        for col in range(20):
            x, y = 40 + col * step_x, 40 + row * step_y
            if not any(_rects_overlap(x, y, n["x"], n["y"], gap) for n in others if n["id"] != target["id"]):
                target["x"], target["y"] = x, y
                return


def compute_floor_boxes(nodes: list, exclude_id: str = None) -> dict:
    boxes: dict = {}
    for n in nodes:
        if n["id"] == exclude_id:
            continue
        f = n.get("floor") or "Uncategorized"
        x0, y0, x1, y1 = boxes.get(f, (float("inf"), float("inf"), float("-inf"), float("-inf")))
        boxes[f] = (
            min(x0, n["x"]), min(y0, n["y"]),
            max(x1, n["x"] + CARD_W), max(y1, n["y"] + CARD_H),
        )
    return boxes


def assign_floor_by_position(nodes: list, moved_id: str) -> str:
    """依卡片目前座標，判斷離哪個樓層區塊最近（落在區塊內距離算 0），回傳該樓層名稱。"""
    target = next((n for n in nodes if n["id"] == moved_id), None)
    if target is None:
        return "Uncategorized"
    boxes = compute_floor_boxes(nodes, exclude_id=moved_id)
    if not boxes:
        return target.get("floor") or "Uncategorized"
    cx, cy = target["x"] + CARD_W / 2, target["y"] + CARD_H / 2
    best_floor, best_dist = target.get("floor"), float("inf")
    for floor, (x0, y0, x1, y1) in boxes.items():
        dx = max(x0 - cx, 0, cx - x1)
        dy = max(y0 - cy, 0, cy - y1)
        dist = dx * dx + dy * dy
        if dist < best_dist:
            best_dist, best_floor = dist, floor
    return best_floor


# --------------------------------------------------------------------------
# 拓樸圖：新增／編輯／刪除
# --------------------------------------------------------------------------

class TopologyError(Exception):
    pass


def _has_cycle(nodes: list, node_id: str, new_parent: str) -> bool:
    by_id = {n["id"]: n for n in nodes}
    cur = new_parent
    guard = 0
    while cur is not None and guard < 100:
        if cur == node_id:
            return True
        cur = by_id.get(cur, {}).get("parent")
        guard += 1
    return False


def upsert_node(nodes: list, body: dict) -> list:
    new_id = (body.get("id") or "").strip()
    if not new_id:
        raise TopologyError("設備編號不能空白")
    old_id = (body.get("old_id") or new_id).strip()

    by_id = {n["id"]: n for n in nodes}
    existing = by_id.get(old_id)
    is_new = existing is None

    if is_new:
        if new_id in by_id:
            raise TopologyError(f"設備編號「{new_id}」已經存在")
        node = {"id": new_id, "x": body.get("x"), "y": body.get("y"),
                "parent": body.get("parent") or None}
        for k in NODE_FIELDS:
            node[k] = body.get(k) or ""
        node["ip"] = body.get("ip") or None
        node["ip_display"] = node["ip"]
        node["floor"] = body.get("floor") or "Uncategorized"
        if node["x"] is None or node["y"] is None:
            node["x"] = 80.0
            node["y"] = 80.0
        nodes.append(node)
        others = [n for n in nodes if n["id"] != new_id]
        resolve_single(others, node)
        if not body.get("floor"):
            node["floor"] = assign_floor_by_position(nodes, new_id)
        return nodes

    if new_id != old_id and new_id in by_id:
        raise TopologyError(f"設備編號「{new_id}」已經存在")

    parent_given = "parent" in body
    new_parent = (body.get("parent") or None) if parent_given else existing.get("parent")
    check_id = new_id if new_id != old_id else old_id
    if new_parent and (new_parent == check_id or _has_cycle(nodes, old_id, new_parent)):
        raise TopologyError("上層設備不能是自己或自己的子節點（會形成循環）")

    if new_id != old_id:
        existing["id"] = new_id
        for n in nodes:
            if n.get("parent") == old_id:
                n["parent"] = new_id

    for k in NODE_FIELDS:
        if k in body:
            existing[k] = body[k]
    if parent_given:  # 只有請求真的帶了 parent 欄位才更新，拖曳只帶 x/y 時不能把既有的上層關係洗掉
        existing["parent"] = new_parent

    moved = "x" in body and "y" in body and body["x"] is not None and body["y"] is not None
    if moved:
        existing["x"], existing["y"] = float(body["x"]), float(body["y"])
        others = [n for n in nodes if n["id"] != existing["id"]]
        resolve_single(others, existing)
        existing["floor"] = assign_floor_by_position(nodes, existing["id"])
    elif "floor" in body and body["floor"]:
        existing["floor"] = body["floor"]

    return nodes


def delete_node(nodes: list, node_id: str) -> list:
    target = next((n for n in nodes if n["id"] == node_id), None)
    if target is None:
        raise TopologyError("找不到這個設備")
    parent = target.get("parent")
    for n in nodes:
        if n.get("parent") == node_id:
            n["parent"] = parent  # 子節點過繼給被刪除節點的上層
    return [n for n in nodes if n["id"] != node_id]


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


# --------------------------------------------------------------------------
# 應用狀態
# --------------------------------------------------------------------------

class AppState:
    def __init__(self, history_cap: int, topology_path: str):
        self.lock = threading.Lock()
        self.topology_lock = threading.Lock()  # 保護拓樸檔案的讀取/寫入，避免背景輪詢跟編輯 API 互相干擾
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

    def set_topology(self, topology: list) -> None:
        with self.lock:
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


def refresh_topology(state: AppState) -> list:
    """重新讀檔＋ping，更新 state.topology，回傳最新節點清單（給編輯 API 當回應用）。"""
    with state.topology_lock:
        nodes, _meta = load_topology_full(state.topology_path)
        topology = ping_topology(nodes)
    state.set_topology(topology)
    return topology


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
            with state.topology_lock:
                nodes, _meta = load_topology_full(state.topology_path)
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

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

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

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "請求格式錯誤"}, status=400)
            return

        if path == "/api/topology/node":
            self._handle_upsert(body)
            return
        if path == "/api/topology/node/delete":
            self._handle_delete(body)
            return
        self.send_error(404, "Not Found")

    def _handle_upsert(self, body: dict) -> None:
        state = self.state
        try:
            with state.topology_lock:
                nodes, meta = load_topology_full(state.topology_path)
                nodes = upsert_node(nodes, body)
                save_topology(nodes, meta)
        except TopologyError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except OSError as exc:
            self._send_json({"error": f"寫入檔案失敗：{exc}"}, status=500)
            return
        topology = refresh_topology(state)
        self._send_json({"topology": topology})

    def _handle_delete(self, body: dict) -> None:
        state = self.state
        node_id = (body.get("id") or "").strip()
        try:
            with state.topology_lock:
                nodes, meta = load_topology_full(state.topology_path)
                nodes = delete_node(nodes, node_id)
                save_topology(nodes, meta)
        except TopologyError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except OSError as exc:
            self._send_json({"error": f"寫入檔案失敗：{exc}"}, status=500)
            return
        topology = refresh_topology(state)
        self._send_json({"topology": topology})


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
                         help="拓樸圖設定檔路徑，預設 dashboard/topology.json；"
                              "在網頁上新增/編輯/刪除/拖曳設備都會直接寫回這個檔案")
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
