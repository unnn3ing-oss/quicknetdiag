#!/usr/bin/env python3
"""家用網路醫生（netdiag）

自動偵測造成連線卡頓、頻繁斷線的原因：區分問題是出在
WiFi 訊號／路由器、還是 ISP 上游、還是 DNS，並給出對應建議。

用法：
    python3 netdiag.py                    單次快篩
    python3 netdiag.py --monitor 3600     監控 1 小時（每 5 秒量測一次）
    python3 netdiag.py --monitor 0        監控到手動按 Ctrl+C 為止
    python3 netdiag.py --monitor 0 --csv log.csv   監控並把每筆量測寫進 CSV

不需要額外安裝套件，Python 3.8+ 內建函式庫即可執行。
"""

from __future__ import annotations

import argparse
import csv
import platform
import re
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

DEFAULT_EXTERNAL_TARGETS = [
    ("Cloudflare DNS", "1.1.1.1"),
    ("Google DNS", "8.8.8.8"),
]
DNS_TEST_DOMAINS = ["www.google.com", "www.cloudflare.com", "www.youtube.com"]

OS_NAME = platform.system()  # "Linux" / "Darwin" / "Windows"

COLOR = {
    "high": "\033[91m",
    "medium": "\033[93m",
    "low": "\033[96m",
    "ok": "\033[92m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}


def c(text: str, key: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{COLOR.get(key, '')}{text}{COLOR['reset']}"


# --------------------------------------------------------------------------
# 資料結構
# --------------------------------------------------------------------------

@dataclass
class PingResult:
    host: str
    sent: int = 0
    received: int = 0
    loss_pct: float = 100.0
    min_ms: Optional[float] = None
    avg_ms: Optional[float] = None
    max_ms: Optional[float] = None
    jitter_ms: Optional[float] = None
    error: Optional[str] = None

    @property
    def reachable(self) -> bool:
        return self.received > 0


@dataclass
class WifiInfo:
    ssid: Optional[str] = None
    signal_dbm: Optional[float] = None
    signal_pct: Optional[float] = None
    channel: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.ssid is not None or self.signal_dbm is not None or self.signal_pct is not None


@dataclass
class Snapshot:
    timestamp: datetime
    gateway_ip: Optional[str]
    gateway_ping: Optional[PingResult]
    external_pings: list = field(default_factory=list)
    dns_ms: dict = field(default_factory=dict)  # domain -> ms or None
    wifi: Optional[WifiInfo] = None


# --------------------------------------------------------------------------
# 偵測預設閘道器（路由器 IP）
# --------------------------------------------------------------------------

def get_default_gateway() -> Optional[str]:
    try:
        if OS_NAME == "Linux":
            out = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r"default via (\S+)", out)
            if m:
                return m.group(1)
        elif OS_NAME == "Darwin":
            out = subprocess.run(
                ["route", "-n", "get", "default"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r"gateway:\s*(\S+)", out)
            if m:
                return m.group(1)
        elif OS_NAME == "Windows":
            out = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, timeout=5,
            ).stdout
            # 逐行找「Default Gateway / 預設閘道」後面接著非空 IP 的那行
            lines = out.splitlines()
            for i, line in enumerate(lines):
                if re.search(r"(Default Gateway|預設閘道)", line):
                    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
                    if m:
                        return m.group(1)
                    # 有些情況 IP 會接在下一行
                    for j in range(i + 1, min(i + 3, len(lines))):
                        m2 = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", lines[j])
                        if m2:
                            return m2.group(1)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# 網路介面累積流量（用來算即時上傳/下載頻寬）
# --------------------------------------------------------------------------

def _default_interface() -> Optional[str]:
    try:
        if OS_NAME == "Darwin":
            out = subprocess.run(
                ["route", "-n", "get", "default"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r"interface:\s*(\S+)", out)
            if m:
                return m.group(1)
        elif OS_NAME == "Linux":
            out = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r"\bdev\s+(\S+)", out)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def get_net_io_counters() -> Optional[dict]:
    """回傳目前對外網路介面累積收送的位元組數（不是速率，速率要呼叫端自己算差值）。

    讀不到（平台不支援／介面找不到）就回傳 None，呼叫端要能容忍拿不到頻寬資料。
    """
    try:
        if OS_NAME == "Darwin":
            iface = _default_interface()
            if not iface:
                return None
            out = subprocess.run(["netstat", "-ib"], capture_output=True, text=True, timeout=5).stdout
            lines = out.splitlines()
            if not lines:
                return None
            header = lines[0].split()
            try:
                i_idx = header.index("Ibytes")
                o_idx = header.index("Obytes")
            except ValueError:
                return None
            for line in lines[1:]:
                parts = line.split()
                if len(parts) > max(i_idx, o_idx) and parts[0] == iface:
                    try:
                        return {
                            "interface": iface,
                            "bytes_recv": int(parts[i_idx]),
                            "bytes_sent": int(parts[o_idx]),
                        }
                    except ValueError:
                        continue
            return None

        if OS_NAME == "Linux":
            iface = _default_interface()
            if not iface:
                return None
            with open("/proc/net/dev", "r") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    name, rest = line.split(":", 1)
                    if name.strip() != iface:
                        continue
                    fields = rest.split()
                    return {
                        "interface": iface,
                        "bytes_recv": int(fields[0]),
                        "bytes_sent": int(fields[8]),
                    }
            return None

        if OS_NAME == "Windows":
            # 沒有簡單、不用額外套件又能拿到「單一介面」計數器的方法，
            # 退而求其次用 netstat -e 的全介面加總（比沒有好，但不是只算對外那張網卡）。
            out = subprocess.run(["netstat", "-e"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"Bytes\s+(\d+)\s+(\d+)", out)
            if m:
                return {
                    "interface": None,
                    "bytes_recv": int(m.group(1)),
                    "bytes_sent": int(m.group(2)),
                }
            return None
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# Ping（跨平台，解析盡量不依賴語系文字，靠數字位置判讀）
# --------------------------------------------------------------------------

def _build_ping_cmd(host: str, count: int, timeout_s: float) -> list:
    if OS_NAME == "Windows":
        return ["ping", "-n", str(count), "-w", str(int(timeout_s * 1000)), host]
    if OS_NAME == "Darwin":
        return ["ping", "-c", str(count), host]
    # Linux
    return ["ping", "-c", str(count), "-W", str(max(1, int(round(timeout_s)))), host]


def ping_host(host: str, count: int = 8, timeout_s: float = 1.5) -> PingResult:
    result = PingResult(host=host, sent=count)
    cmd = _build_ping_cmd(host, count, timeout_s)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=count * timeout_s + 10,
        )
        out = proc.stdout
    except FileNotFoundError:
        result.error = "系統缺少 ping 指令"
        return result
    except subprocess.TimeoutExpired:
        result.error = "ping 逾時未回應"
        return result
    except Exception as exc:  # noqa: BLE001
        result.error = f"執行 ping 失敗：{exc}"
        return result

    # 逐筆 RTT（三平台的 "time=" / "time<" 格式都一致，語系不影響）
    rtts = [float(x) for x in re.findall(r"time[=<]\s*([\d.]+)\s*ms", out, re.IGNORECASE)]
    result.received = len(rtts)

    if rtts:
        result.min_ms = min(rtts)
        result.max_ms = max(rtts)
        result.avg_ms = sum(rtts) / len(rtts)
        result.jitter_ms = statistics.pstdev(rtts) if len(rtts) > 1 else 0.0

    # 遺失率：優先找「Sent/Received/Lost」這類三個 "=" 的統計行
    # （逐行比對，跳過含 ttl 的逐筆回覆行，避免把 "bytes=32 time=5ms TTL=57"
    #  這種單行三個 "=" 誤判成統計數字；只認同時含 "%" 的統計行，
    #  避免跟 "Minimum = .. Maximum = .. Average = .." 那行搞混）
    triplet = None
    for line in out.splitlines():
        if re.search(r"ttl", line, re.IGNORECASE):
            continue
        if "%" not in line:
            continue
        m = re.search(r"=\s*(\d+)[^\d=]+=\s*(\d+)[^\d=]+=\s*(\d+)", line)
        if m:
            triplet = m
            break

    if triplet:
        sent, received, lost = (int(g) for g in triplet.groups())
        if sent > 0:
            result.sent = sent
            result.received = received if received else result.received
            result.loss_pct = round(lost / sent * 100, 1)
    else:
        # Unix 格式："N packets transmitted, M received / M packets received, L% packet loss"
        m = re.search(r"(\d+)\s+packets transmitted,\s*(\d+)\s+(?:packets\s+)?received", out)
        if m:
            sent, received = int(m.group(1)), int(m.group(2))
            result.sent = sent
            result.loss_pct = round((sent - received) / sent * 100, 1) if sent else 100.0
        elif result.received:
            result.loss_pct = round((count - result.received) / count * 100, 1)

    return result


# --------------------------------------------------------------------------
# DNS 解析時間
# --------------------------------------------------------------------------

def resolve_dns_ms(domain: str, timeout_s: float = 2.0) -> Optional[float]:
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout_s)
        start = time.time()
        socket.getaddrinfo(domain, 443)
        return round((time.time() - start) * 1000, 1)
    except Exception:
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)


# --------------------------------------------------------------------------
# WiFi 訊號（best-effort，讀不到就回傳空物件，不影響其他診斷）
# --------------------------------------------------------------------------

def get_wifi_info() -> WifiInfo:
    info = WifiInfo()
    try:
        if OS_NAME == "Linux":
            _wifi_linux(info)
        elif OS_NAME == "Darwin":
            _wifi_macos(info)
        elif OS_NAME == "Windows":
            _wifi_windows(info)
    except Exception:
        pass
    return info


def _wifi_linux(info: WifiInfo) -> None:
    # 優先用 nmcli（大多數桌面 Linux distro 都有 NetworkManager）
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid,signal,chan", "dev", "wifi"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and parts[0].lower() == "yes":
                info.ssid = parts[1] or None
                info.signal_pct = float(parts[2]) if parts[2] else None
                info.channel = parts[3] or None
                return
    except FileNotFoundError:
        pass

    # 退而求其次用 iwconfig
    try:
        out = subprocess.run(["iwconfig"], capture_output=True, text=True, timeout=5).stdout
        m_ssid = re.search(r'ESSID:"([^"]*)"', out)
        m_sig = re.search(r"Signal level[=:]\s*(-?\d+)\s*dBm", out)
        m_qual = re.search(r"Link Quality[=:]\s*(\d+)/(\d+)", out)
        if m_ssid:
            info.ssid = m_ssid.group(1)
        if m_sig:
            info.signal_dbm = float(m_sig.group(1))
        if m_qual:
            info.signal_pct = round(int(m_qual.group(1)) / int(m_qual.group(2)) * 100, 1)
    except FileNotFoundError:
        pass


def _wifi_macos(info: WifiInfo) -> None:
    airport = (
        "/System/Library/PrivateFrameworks/Apple80211.framework"
        "/Versions/Current/Resources/airport"
    )
    try:
        out = subprocess.run([airport, "-I"], capture_output=True, text=True, timeout=5).stdout
        m_ssid = re.search(r"\bSSID:\s*(.+)", out)
        m_rssi = re.search(r"agrCtlRSSI:\s*(-?\d+)", out)
        m_chan = re.search(r"\bchannel:\s*(\S+)", out)
        if m_ssid:
            info.ssid = m_ssid.group(1).strip()
        if m_rssi:
            info.signal_dbm = float(m_rssi.group(1))
        if m_chan:
            info.channel = m_chan.group(1)
        if info.available:
            return
    except FileNotFoundError:
        pass

    # 新版 macOS（Sonoma 之後）拿掉了 airport 指令，改用 system_profiler
    try:
        out = subprocess.run(
            ["system_profiler", "SPAirPortDataType"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        m_sig = re.search(r"(?:Signal / Noise|訊號 ?/ ?雜訊比)\s*:\s*(-?\d+)\s*dBm", out)
        if m_sig:
            info.signal_dbm = float(m_sig.group(1))
        m_chan = re.search(r"(?:Channel|頻道)\s*:\s*(\S+)", out)
        if m_chan:
            info.channel = m_chan.group(1)
    except FileNotFoundError:
        pass


def _wifi_windows(info: WifiInfo) -> None:
    out = subprocess.run(
        ["netsh", "wlan", "show", "interfaces"],
        capture_output=True, text=True, timeout=5,
    ).stdout
    m_ssid = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", out, re.MULTILINE)
    m_sig = re.search(r"(?:Signal|訊號)\s*:\s*(\d+)\s*%", out)
    m_chan = re.search(r"(?:Channel|頻道)\s*:\s*(\S+)", out)
    if m_ssid:
        info.ssid = m_ssid.group(1)
    if m_sig:
        info.signal_pct = float(m_sig.group(1))
    if m_chan:
        info.channel = m_chan.group(1)


# --------------------------------------------------------------------------
# 快照與診斷
# --------------------------------------------------------------------------

def take_snapshot(gateway_ip: Optional[str], extra_targets: list, ping_count: int,
                   check_dns: bool = True, targets: Optional[list] = None) -> Snapshot:
    """組一份完整快照。

    targets 給定時取代預設的對外目標清單（用於儀表板背景輪詢時只測一個目標，
    避免每輪都把 DEFAULT_EXTERNAL_TARGETS 全測一次拖慢輪詢間隔）；
    check_dns=False 時跳過 DNS 測試（同樣是為了讓高頻率輪詢更快）。
    """
    snap = Snapshot(timestamp=datetime.now(), gateway_ip=gateway_ip, gateway_ping=None)
    if gateway_ip:
        snap.gateway_ping = ping_host(gateway_ip, count=ping_count)

    ext_targets = targets if targets is not None else DEFAULT_EXTERNAL_TARGETS + extra_targets
    for name, host in ext_targets:
        snap.external_pings.append((name, ping_host(host, count=ping_count)))

    if check_dns:
        for domain in DNS_TEST_DOMAINS:
            snap.dns_ms[domain] = resolve_dns_ms(domain)

    snap.wifi = get_wifi_info()
    return snap


@dataclass
class Issue:
    severity: str  # high / medium / low / ok
    title: str
    detail: str
    suggestion: str


def diagnose(snap: Snapshot) -> list:
    issues: list = []

    if snap.gateway_ip is None:
        issues.append(Issue(
            "low", "無法自動偵測路由器 IP",
            "略過區網（路由器）連線測試。",
            "可用 --gateway 手動指定路由器 IP（通常是 192.168.1.1 或 192.168.0.1）。",
        ))
    elif snap.gateway_ping is not None:
        gp = snap.gateway_ping
        if gp.loss_pct >= 100:
            issues.append(Issue(
                "high", "路由器完全無回應",
                f"對 {gp.host} 送出 {gp.sent} 個封包，全部沒有回應。",
                "檢查路由器/AP 是否斷電、當機，或裝置是否確實連上 WiFi／網路線；"
                "若是 Mesh 系統，檢查該節點的回程（backhaul）是否斷線。",
            ))
        elif gp.loss_pct > 5:
            issues.append(Issue(
                "medium", "區網連線不穩（封包遺失）",
                f"對路由器 {gp.host} 的封包遺失率 {gp.loss_pct}%。",
                "多半是 WiFi 訊號問題：嘗試靠近路由器／AP、更換頻道，或檢查是否有微波爐、"
                "藍牙裝置、鄰居 WiFi 造成干擾；若走網路線，檢查線材與接頭。",
            ))
        elif (gp.avg_ms is not None and gp.avg_ms > 30) or (gp.jitter_ms is not None and gp.jitter_ms > 15):
            issues.append(Issue(
                "medium", "區網延遲或抖動偏高",
                f"到路由器平均延遲 {gp.avg_ms:.1f} ms、抖動 {gp.jitter_ms:.1f} ms"
                "（區網延遲正常應在數毫秒內）。",
                "建議檢查是否為 2.4GHz 頻段壅塞，切到 5GHz／6GHz 頻段；"
                "Mesh 系統可檢查節點間回程品質，或改用有線回程。",
            ))
        else:
            issues.append(Issue("ok", "區網（到路由器）連線正常", "", ""))

    # 對外連線
    ext_ok = []
    for name, pr in snap.external_pings:
        if pr.loss_pct >= 50:
            ext_ok.append(False)
        else:
            ext_ok.append(True)

    gateway_healthy = (
        snap.gateway_ping is not None
        and snap.gateway_ping.loss_pct <= 5
        and (snap.gateway_ping.avg_ms or 0) <= 30
    )

    if ext_ok and not any(ext_ok):
        detail = "、".join(f"{name}({pr.loss_pct}% 遺失)" for name, pr in snap.external_pings)
        if gateway_healthy:
            issues.append(Issue(
                "high", "對外連線異常，但區網正常",
                f"{detail}。路由器本身回應正常，問題出在路由器之外。",
                "問題可能在 ISP 或上游線路：建議重開數據機（modem），"
                "若持續發生請聯絡電信業者回報線路品質。",
            ))
        else:
            issues.append(Issue(
                "medium", "對外連線異常，且區網本身也不穩",
                f"{detail}。",
                "先排除區網／WiFi 問題（參考上方建議），穩定後再確認對外連線是否恢復。",
            ))
    elif ext_ok and not all(ext_ok):
        bad = [name for (name, _), ok in zip(snap.external_pings, ext_ok) if not ok]
        issues.append(Issue(
            "low", "部分對外目標連線不穩",
            f"{'、'.join(bad)} 封包遺失偏高，其餘正常，可能是暫時性壅塞。",
            "可再觀察一段時間，若持續發生建議用 --monitor 長時間監控確認是否為固定模式。",
        ))
    elif ext_ok:
        issues.append(Issue("ok", "對外連線正常", "", ""))

    # DNS（dns_ms 是空字典代表這輪沒測 DNS，直接略過，不當成全部失敗）
    dns_ok = {d: (ms is not None) for d, ms in snap.dns_ms.items()}
    if not snap.dns_ms:
        pass
    elif not any(dns_ok.values()) and any(ext_ok):
        issues.append(Issue(
            "medium", "DNS 解析全部失敗，但對外連線正常",
            "能連上外部 IP，但網域名稱都解析不到。",
            "建議把裝置或路由器的 DNS 伺服器改成 1.1.1.1（Cloudflare）或 8.8.8.8（Google）試試。",
        ))
    elif any(v is False for v in dns_ok.values()):
        slow_or_fail = [d for d, ok in dns_ok.items() if not ok]
        issues.append(Issue(
            "low", "部分網域解析失敗",
            f"{'、'.join(slow_or_fail)} 解析失敗或逾時。",
            "若只是偶發可忽略；經常發生的話同樣建議更換 DNS 伺服器。",
        ))
    else:
        slow = [d for d, ms in snap.dns_ms.items() if ms and ms > 200]
        if slow:
            issues.append(Issue(
                "low", "DNS 解析偏慢",
                f"{'、'.join(f'{d}({snap.dns_ms[d]:.0f}ms)' for d in slow)}。",
                "可考慮更換較快的 DNS 伺服器（如 1.1.1.1）。",
            ))

    # WiFi 訊號
    if snap.wifi and snap.wifi.available:
        weak = False
        if snap.wifi.signal_dbm is not None and snap.wifi.signal_dbm <= -70:
            weak = True
        if snap.wifi.signal_pct is not None and snap.wifi.signal_pct < 40:
            weak = True
        if weak:
            sig_desc = (
                f"{snap.wifi.signal_dbm:.0f} dBm" if snap.wifi.signal_dbm is not None
                else f"{snap.wifi.signal_pct:.0f}%"
            )
            issues.append(Issue(
                "medium", "WiFi 訊號偏弱",
                f"目前訊號強度 {sig_desc}（SSID: {snap.wifi.ssid or '未知'}）。",
                "建議靠近 AP、增加 Mesh 節點，或確認該處是否被牆體/樓層阻隔；"
                "訊號弱是造成卡頓與斷線最常見的原因之一。",
            ))

    if not issues:
        issues.append(Issue("ok", "目前偵測未發現異常", "", "網路品質看起來正常。"))

    order = {"high": 0, "medium": 1, "low": 2, "ok": 3}
    issues.sort(key=lambda i: order.get(i.severity, 9))
    return issues


# --------------------------------------------------------------------------
# 輸出
# --------------------------------------------------------------------------

SEVERITY_LABEL = {"high": "[嚴重]", "medium": "[注意]", "low": "[留意]", "ok": "[正常]"}


def print_snapshot_report(snap: Snapshot, issues: list, use_color: bool) -> None:
    ts = snap.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    print(c(f"\n==== 家用網路診斷報告 {ts} ====", "bold", use_color))

    if snap.gateway_ping:
        gp = snap.gateway_ping
        avg = f"{gp.avg_ms:.1f} ms" if gp.avg_ms is not None else "N/A"
        jit = f"{gp.jitter_ms:.1f} ms" if gp.jitter_ms is not None else "N/A"
        print(f"路由器 {gp.host}：遺失 {gp.loss_pct}%，平均延遲 {avg}，抖動 {jit}")
    else:
        print("路由器：未偵測到 / 未測試")

    for name, pr in snap.external_pings:
        avg = f"{pr.avg_ms:.1f} ms" if pr.avg_ms is not None else "N/A"
        print(f"對外 {name}（{pr.host}）：遺失 {pr.loss_pct}%，平均延遲 {avg}")

    for domain, ms in snap.dns_ms.items():
        print(f"DNS {domain}：{f'{ms:.0f} ms' if ms is not None else '解析失敗'}")

    if snap.wifi and snap.wifi.available:
        sig = (
            f"{snap.wifi.signal_dbm:.0f} dBm" if snap.wifi.signal_dbm is not None
            else (f"{snap.wifi.signal_pct:.0f}%" if snap.wifi.signal_pct is not None else "N/A")
        )
        print(f"WiFi：SSID={snap.wifi.ssid or '未知'}，訊號={sig}，頻道={snap.wifi.channel or '未知'}")
    else:
        print("WiFi：未偵測到（可能走有線，或此平台無法讀取）")

    print(c("\n診斷結果：", "bold", use_color))
    for issue in issues:
        label = c(SEVERITY_LABEL[issue.severity], issue.severity, use_color)
        print(f"{label} {issue.title}")
        if issue.detail:
            print(f"       {issue.detail}")
        if issue.suggestion:
            print(c(f"       建議：{issue.suggestion}", "dim", use_color))
    print()


# --------------------------------------------------------------------------
# 監控模式
# --------------------------------------------------------------------------

def run_monitor(gateway_ip: Optional[str], extra_targets: list, duration_s: int,
                 interval_s: float, csv_path: Optional[str], use_color: bool) -> None:
    print(c(
        f"開始監控（每 {interval_s} 秒量測一次，"
        + (f"共 {duration_s} 秒" if duration_s > 0 else "按 Ctrl+C 停止") + "）...",
        "bold", use_color,
    ))

    csv_file = None
    csv_writer = None
    if csv_path:
        csv_file = open(csv_path, "w", newline="", encoding="utf-8-sig")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow([
            "timestamp", "gateway_loss_pct", "gateway_avg_ms", "gateway_jitter_ms",
            "ext_loss_pct", "ext_avg_ms", "wifi_ssid", "wifi_signal", "gateway_up",
        ])

    start = time.time()
    dropout_events = []  # (start_time, end_time)
    in_dropout = False
    dropout_start = None

    gateway_loss_history = []
    gateway_avg_history = []
    ext_loss_history = []
    signal_history = []

    round_no = 0
    try:
        while True:
            now = time.time()
            if duration_s > 0 and now - start >= duration_s:
                break
            round_no += 1

            gp = ping_host(gateway_ip, count=3, timeout_s=1.0) if gateway_ip else None
            name0, host0 = DEFAULT_EXTERNAL_TARGETS[0]
            ep = ping_host(host0, count=3, timeout_s=1.0)
            wifi = get_wifi_info()

            gateway_up = (gp is None) or (gp.loss_pct < 100)
            if gp:
                gateway_loss_history.append(gp.loss_pct)
                if gp.avg_ms is not None:
                    gateway_avg_history.append(gp.avg_ms)
            ext_loss_history.append(ep.loss_pct)
            sig_val = wifi.signal_dbm if wifi.signal_dbm is not None else wifi.signal_pct
            if sig_val is not None:
                signal_history.append(sig_val)

            if gp is not None:
                if not gateway_up and not in_dropout:
                    in_dropout = True
                    dropout_start = now
                elif gateway_up and in_dropout:
                    in_dropout = False
                    dropout_events.append((dropout_start, now))

            ts = datetime.now().strftime("%H:%M:%S")
            gw_desc = (
                f"路由器 遺失{gp.loss_pct:>3.0f}% 延遲{gp.avg_ms:.0f}ms" if gp and gp.avg_ms is not None
                else (f"路由器 遺失{gp.loss_pct:>3.0f}% 無回應" if gp else "路由器 未測試")
            )
            ext_desc = (
                f"對外 遺失{ep.loss_pct:>3.0f}% 延遲{ep.avg_ms:.0f}ms" if ep.avg_ms is not None
                else f"對外 遺失{ep.loss_pct:>3.0f}% 無回應"
            )
            sig_desc = f" WiFi訊號{sig_val:.0f}" if sig_val is not None else ""
            status_color = "high" if (gp and gp.loss_pct >= 100) else (
                "medium" if (gp and gp.loss_pct > 5) or ep.loss_pct > 5 else "ok"
            )
            print(c(f"[{ts}] {gw_desc} | {ext_desc}{sig_desc}", status_color, use_color))

            if csv_writer:
                csv_writer.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    gp.loss_pct if gp else "",
                    f"{gp.avg_ms:.1f}" if gp and gp.avg_ms is not None else "",
                    f"{gp.jitter_ms:.1f}" if gp and gp.jitter_ms is not None else "",
                    ep.loss_pct,
                    f"{ep.avg_ms:.1f}" if ep.avg_ms is not None else "",
                    wifi.ssid or "",
                    sig_val if sig_val is not None else "",
                    int(gateway_up),
                ])
                csv_file.flush()

            sleep_left = interval_s - (time.time() - now)
            if sleep_left > 0:
                time.sleep(sleep_left)
    except KeyboardInterrupt:
        print(c("\n收到中斷，停止監控並產出總結...", "bold", use_color))
    finally:
        if in_dropout:
            dropout_events.append((dropout_start, time.time()))
        if csv_file:
            csv_file.close()

    print_monitor_summary(
        round_no, time.time() - start, dropout_events,
        gateway_loss_history, gateway_avg_history, ext_loss_history,
        signal_history, use_color,
    )


def print_monitor_summary(rounds: int, elapsed_s: float, dropout_events: list,
                           gw_loss_hist: list, gw_avg_hist: list, ext_loss_hist: list,
                           signal_hist: list, use_color: bool) -> None:
    print(c("\n==== 監控總結 ====", "bold", use_color))
    mins = elapsed_s / 60
    print(f"監控時長：約 {mins:.1f} 分鐘（{rounds} 回合）")

    if dropout_events:
        longest = max(e - s for s, e in dropout_events)
        total_down = sum(e - s for s, e in dropout_events)
        print(c(f"路由器完全斷線次數：{len(dropout_events)} 次，"
                f"最長 {longest:.0f} 秒，總計斷線 {total_down:.0f} 秒", "high", use_color))
    else:
        print(c("路由器沒有偵測到完全斷線。", "ok", use_color))

    if gw_loss_hist:
        avg_loss = statistics.mean(gw_loss_hist)
        print(f"區網（路由器）平均封包遺失率：{avg_loss:.1f}%")
    if gw_avg_hist:
        print(f"區網平均延遲：{statistics.mean(gw_avg_hist):.1f} ms"
              + (f"，延遲標準差：{statistics.pstdev(gw_avg_hist):.1f} ms" if len(gw_avg_hist) > 1 else ""))
    if ext_loss_hist:
        avg_ext_loss = statistics.mean(ext_loss_hist)
        print(f"對外連線平均封包遺失率：{avg_ext_loss:.1f}%")
    if signal_hist:
        print(f"WiFi 訊號：平均 {statistics.mean(signal_hist):.0f}，"
              f"最弱 {min(signal_hist):.0f}，最強 {max(signal_hist):.0f}")

    # 簡單整合診斷
    print(c("\n整合判斷：", "bold", use_color))
    said_something = False
    if dropout_events and signal_hist:
        said_something = True
        print("- 若斷線時間點常與訊號變弱重疊，優先懷疑 WiFi 訊號/干擾問題；"
              "若訊號一直穩定卻仍斷線，較可能是路由器硬體或 ISP 線路問題。")
    if gw_loss_hist and ext_loss_hist:
        avg_gw = statistics.mean(gw_loss_hist)
        avg_ext = statistics.mean(ext_loss_hist)
        if avg_gw <= 2 and avg_ext > 5:
            said_something = True
            print(c("- 區網穩定但對外遺失率偏高，問題大機率在 ISP／數據機，"
                    "建議重開數據機並回報電信業者。", "high", use_color))
        elif avg_gw > 5:
            said_something = True
            print(c("- 區網本身遺失率偏高，先排查 WiFi 訊號、頻道壅塞或 Mesh 回程再看對外品質。",
                    "medium", use_color))
    if not said_something:
        print("- 監控期間數據不足以下明確結論，建議延長監控時間或觀察使用高峰時段。")
    print()


# --------------------------------------------------------------------------
# 進入點
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="家用網路醫生：自動偵測 WiFi / 路由器 / DNS / ISP 造成的卡頓與斷線問題",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--monitor", type=int, default=None,
                         help="進入監控模式，持續 N 秒（0 表示持續到 Ctrl+C），不指定則只做單次快篩")
    parser.add_argument("--interval", type=float, default=5.0,
                         help="監控模式的量測間隔秒數，預設 5 秒")
    parser.add_argument("--csv", type=str, default=None,
                         help="監控模式時，把每筆量測寫進這個 CSV 檔")
    parser.add_argument("--count", type=int, default=8,
                         help="單次快篩時每個目標的 ping 次數，預設 8")
    parser.add_argument("--gateway", type=str, default=None,
                         help="手動指定路由器 IP（預設自動偵測）")
    parser.add_argument("--target", action="append", default=[], metavar="NAME=HOST",
                         help="額外要 ping 的目標，格式 NAME=HOST，可重複使用")
    parser.add_argument("--no-color", action="store_true", help="關閉彩色輸出")
    args = parser.parse_args()

    use_color = (not args.no_color) and sys.stdout.isatty()

    extra_targets = []
    for t in args.target:
        if "=" in t:
            name, host = t.split("=", 1)
        else:
            name, host = t, t
        extra_targets.append((name, host))

    gateway_ip = args.gateway or get_default_gateway()

    if args.monitor is None:
        snap = take_snapshot(gateway_ip, extra_targets, args.count)
        issues = diagnose(snap)
        print_snapshot_report(snap, issues, use_color)
    else:
        run_monitor(gateway_ip, extra_targets, args.monitor, args.interval, args.csv, use_color)


if __name__ == "__main__":
    main()
