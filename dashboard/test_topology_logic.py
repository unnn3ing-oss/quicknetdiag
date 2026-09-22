import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

ok_count = 0
fail_count = 0


def check(name, cond):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"OK   {name}")
    else:
        fail_count += 1
        print(f"FAIL {name}")


# ---------------------------------------------------------------------
# _grid_layout_all：卡片對齊虛擬格線、彼此不重疊
# ---------------------------------------------------------------------


def is_on_grid(n):
    dx = (n["x"] - server.GRID_ORIGIN) / server.GRID_STEP_X
    return abs(dx - round(dx)) < 1e-6


def any_overlap(nodes):
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if server._rects_overlap(nodes[i]["x"], nodes[i]["y"], nodes[j]["x"], nodes[j]["y"], 0):
                return True
    return False


# 疏密不一、亂七八糟的原始座標，模擬另一套工具匯出的資料
messy = [
    {"id": "W1", "x": 10.0, "y": 5.0, "parent": None, "floor": "1F"},
    {"id": "SW", "x": 999.0, "y": 812.0, "parent": "W1", "floor": "1F"},
    {"id": "C1", "x": 3.0, "y": 3.0, "parent": "SW", "floor": "1F"},   # 跟 W1 疊在一起
    {"id": "C2", "x": 4.0, "y": 4.0, "parent": "SW", "floor": "1F"},   # 也跟 W1、C1 疊在一起
    {"id": "P1", "x": 500.0, "y": 500.0, "parent": None, "floor": "2F"},
]
server._grid_layout_all(messy)
check("格線排版後彼此都不重疊", not any_overlap(messy))
check("格線排版後每張卡片的 x 都對齊虛擬格線", all(is_on_grid(n) for n in messy))
by_id_messy = {n["id"]: n for n in messy}
check("同一層裡，父節點在子節點正上方那一列（y 較小）", by_id_messy["W1"]["y"] < by_id_messy["SW"]["y"] < by_id_messy["C1"]["y"])
check("不同樓層的卡片 y 範圍不會疊在一起", by_id_messy["P1"]["y"] > by_id_messy["C1"]["y"])


# ---------------------------------------------------------------------
# assign_floor_by_position
# ---------------------------------------------------------------------

nodes = [
    {"id": "r1", "x": 100.0, "y": 100.0, "floor": "1F"},
    {"id": "r2", "x": 300.0, "y": 100.0, "floor": "1F"},
    {"id": "r3", "x": 100.0, "y": 500.0, "floor": "2F"},
    {"id": "moved", "x": 105.0, "y": 105.0, "floor": "2F"},  # 資料上還是 2F，但座標其實在 1F 那一叢
]
new_floor = server.assign_floor_by_position(nodes, "moved")
check("拖到 1F 區域附近會被判定為 1F", new_floor == "1F")


# ---------------------------------------------------------------------
# upsert_node：新增／改名／防循環
# ---------------------------------------------------------------------

base_nodes = [
    {"id": "router", "name": "路由器", "type": "router", "ip": "192.168.1.1", "ip_display": "192.168.1.1",
     "mac": "", "location": "", "parent": None, "port_label": "", "connection_type": "", "floor": "1F",
     "x": 100.0, "y": 100.0},
    {"id": "switch", "name": "交換器", "type": "switch", "ip": None, "ip_display": None,
     "mac": "", "location": "", "parent": "router", "port_label": "", "connection_type": "有線Cat6",
     "floor": "1F", "x": 100.0, "y": 250.0},
]

nodes2 = [dict(n) for n in base_nodes]
nodes2 = server.upsert_node(nodes2, {"id": "cam1", "name": "新攝影機", "ip": "192.168.1.50",
                                      "type": "ipcam", "parent": "switch", "floor": "1F"})
check("新增設備成功、清單多一筆", len(nodes2) == 3 and any(n["id"] == "cam1" for n in nodes2))
new_node = next(n for n in nodes2 if n["id"] == "cam1")
check("新設備跟既有設備沒有重疊", not any(
    server._rects_overlap(new_node["x"], new_node["y"], n["x"], n["y"], 0)
    for n in nodes2 if n["id"] != "cam1"
))
check("新設備對齊虛擬格線", is_on_grid(new_node))

# 重複 id 應該擋掉
try:
    server.upsert_node([dict(n) for n in base_nodes], {"id": "switch", "old_id": "cam-not-exist",
                                                          "name": "x", "parent": None})
    check("新增重複 id 應該報錯", False)
except server.TopologyError:
    check("新增重複 id 應該報錯", True)

# 改名：router -> gateway，switch 的 parent 要跟著更新
nodes3 = [dict(n) for n in base_nodes]
nodes3 = server.upsert_node(nodes3, {"id": "gateway", "old_id": "router", "name": "路由器",
                                      "ip": "192.168.1.1", "parent": None})
ids3 = {n["id"] for n in nodes3}
switch3 = next(n for n in nodes3 if n["id"] == "switch")
check("改名後舊 id 消失、新 id 存在", "router" not in ids3 and "gateway" in ids3)
check("子節點的 parent 參照跟著更新成新 id", switch3["parent"] == "gateway")

# 防循環：想把 router 的 parent 設成 switch（switch 的上層本來就是 router）
try:
    server.upsert_node([dict(n) for n in base_nodes], {"id": "router", "old_id": "router",
                                                          "name": "路由器", "parent": "switch"})
    check("設定會造成循環的上層應該報錯", False)
except server.TopologyError:
    check("設定會造成循環的上層應該報錯", True)


# ---------------------------------------------------------------------
# delete_node：子節點過繼
# ---------------------------------------------------------------------

nodes4 = [
    {"id": "a", "parent": None},
    {"id": "b", "parent": "a"},
    {"id": "c", "parent": "b"},  # c 的上層是 b，刪掉 b 後 c 應該過繼給 a
]
nodes4 = server.delete_node(nodes4, "b")
ids4 = {n["id"]: n for n in nodes4}
check("刪除節點後清單少一筆", "b" not in ids4)
check("子節點過繼給被刪除節點的上層", ids4["c"]["parent"] == "a")


# ---------------------------------------------------------------------
# save_topology 往返（scanner schema）：不該動到我們沒管理的欄位
# ---------------------------------------------------------------------

raw = {
    "devices": {
        "cam1": {
            "id": "cam1", "mac_address": "aa:bb:cc", "ip_address": "192.168.1.50",
            "custom_name": "舊名字", "detected_name": "", "brand": "海康", "series": "X1",
            "device_type": "ipcam", "vlan": "10", "floor": "1F", "location": "客廳",
            "camera_view_location": "", "speed": "100Mbps", "notes": "特殊備註不能不見",
            "is_online": True, "latency": 3, "last_seen": "x", "updated_at": "y",
            "custom_x": 100, "custom_y": 100,
        },
    },
    "topology": {},
    "theme": "neon",
    "version": 17,
}
nodes5, meta5 = server._from_scanner_schema(raw), {"schema": "scanner", "raw": raw, "path": "/tmp/_test_topo.json"}
nodes5[0]["name"] = "新名字"  # 只改名稱
server.save_topology(nodes5, meta5)

with open("/tmp/_test_topo.json", encoding="utf-8") as f:
    saved = json.load(f)
dev = saved["devices"]["cam1"]
check("save_topology 保留原本沒管理的欄位（brand）", dev["brand"] == "海康")
check("save_topology 保留原本沒管理的欄位（notes）", dev["notes"] == "特殊備註不能不見")
check("save_topology 保留頂層 theme/version", saved["theme"] == "neon" and saved["version"] == 17)
check("save_topology 有更新我們管理的欄位（custom_name）", dev["custom_name"] == "新名字")
os.remove("/tmp/_test_topo.json")

print()
print(f"{ok_count} passed, {fail_count} failed")
sys.exit(1 if fail_count else 0)
