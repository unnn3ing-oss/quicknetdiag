import sys
import types
import unittest.mock as mock

sys.path.insert(0, ".")
import netdiag  # noqa: E402

LINUX_OUT = """PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=12.3 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=57 time=11.8 ms
64 bytes from 1.1.1.1: icmp_seq=3 ttl=57 time=13.1 ms

--- 1.1.1.1 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2003ms
rtt min/avg/max/mdev = 11.800/12.400/13.100/0.543 ms
"""

LINUX_LOSS_OUT = """PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=12.3 ms

--- 1.1.1.1 ping statistics ---
3 packets transmitted, 1 received, 66% packet loss, time 2003ms
rtt min/avg/max/mdev = 12.300/12.300/12.300/0.000 ms
"""

MAC_OUT = """PING 1.1.1.1 (1.1.1.1): 56 data bytes
64 bytes from 1.1.1.1: icmp_seq=0 ttl=57 time=10.123 ms
64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=9.876 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=57 time=11.001 ms

--- 1.1.1.1 ping statistics ---
3 packets transmitted, 3 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 9.876/10.333/11.001/0.467 ms
"""

WIN_EN_OUT = """Pinging 1.1.1.1 with 32 bytes of data:
Reply from 1.1.1.1: bytes=32 time=5ms TTL=57
Reply from 1.1.1.1: bytes=32 time=6ms TTL=57
Reply from 1.1.1.1: bytes=32 time=4ms TTL=57

Ping statistics for 1.1.1.1:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 4ms, Maximum = 6ms, Average = 5ms
"""

WIN_ZH_OUT = """Pinging 1.1.1.1 (使用 32 位元組的資料):
回覆自 1.1.1.1: 位元組=32 time=5ms TTL=57
回覆自 1.1.1.1: 位元組=32 time=6ms TTL=57
要求等候逾時。

1.1.1.1 的 Ping 統計資料:
    封包: 已傳送 = 3，已收到 = 2，已遺失 = 1 (33% 遺失),
黃金時間 (以毫秒計) 的近似值:
    最小值 = 5ms，最大值 = 6ms，平均 = 5ms
"""

WIN_TIMEOUT_OUT = """Pinging 192.168.1.1 with 32 bytes of data:
Request timed out.
Request timed out.
Request timed out.

Ping statistics for 192.168.1.1:
    Packets: Sent = 3, Received = 0, Lost = 3 (100% loss),
"""


def run_case(name, out, expected_sent, expected_received, expected_loss):
    with mock.patch("subprocess.run") as m:
        m.return_value = types.SimpleNamespace(stdout=out, returncode=0)
        r = netdiag.ping_host("1.1.1.1", count=expected_sent)
    ok = (r.sent == expected_sent and r.received == expected_received and r.loss_pct == expected_loss)
    print(f"{'OK  ' if ok else 'FAIL'} {name}: sent={r.sent} received={r.received} loss={r.loss_pct}% "
          f"avg={r.avg_ms} jitter={r.jitter_ms}")
    return ok


results = []
results.append(run_case("Linux 正常", LINUX_OUT, 3, 3, 0.0))
results.append(run_case("Linux 部分遺失", LINUX_LOSS_OUT, 3, 1, 66.7))
results.append(run_case("macOS 正常", MAC_OUT, 3, 3, 0.0))
results.append(run_case("Windows 英文 正常", WIN_EN_OUT, 3, 3, 0.0))
results.append(run_case("Windows 中文 部分遺失", WIN_ZH_OUT, 3, 2, 33.3))
results.append(run_case("Windows 英文 全部逾時", WIN_TIMEOUT_OUT, 3, 0, 100.0))

print()
print("ALL PASS" if all(results) else "SOME FAILED")
