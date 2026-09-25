#!/bin/bash
# 雙擊本檔案即可啟動 netdiag 儀表板並自動開瀏覽器。
#
# 第一次雙擊如果被 macOS 擋下（顯示「無法驗證開發者」），
# 改成「右鍵點此檔案 → 打開」，跳出的視窗選「打開」就能執行，之後雙擊就正常了。
#
# 這個視窗可以直接關掉，伺服器會繼續留在背景執行；
# 要真正停止，改雙擊 stop_dashboard.command。

cd "$(dirname "$0")" || exit 1

PORT=8765

# 如果你的網路架構圖要接另一套工具（例如 network-monitor）產生的 devices.json，
# 把下面這行開頭的 # 拿掉，並改成你實際的檔案絕對路徑：
# TOPOLOGY_FILE="/Users/你的帳號/path/to/devices.json"

URL="http://127.0.0.1:${PORT}"
LOGFILE="/tmp/netdiag_dashboard.log"
PIDFILE="/tmp/netdiag_dashboard.pid"

if curl -s -o /dev/null "$URL" 2>/dev/null; then
  echo "儀表板已經在背景跑了，直接開瀏覽器。"
  open "$URL"
  sleep 2
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "找不到 python3，請先到 https://www.python.org/ 安裝。"
  read -r -p "按 Enter 關閉這個視窗..." _
  exit 1
fi

ARGS=(--port "$PORT")
if [ -n "$TOPOLOGY_FILE" ]; then
  ARGS+=(--topology-file "$TOPOLOGY_FILE")
fi

echo "啟動 netdiag 儀表板（背景執行）..."
nohup python3 dashboard/server.py "${ARGS[@]}" > "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"

ready=0
for _ in $(seq 1 30); do
  if curl -s -o /dev/null "$URL" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 0.3
done

if [ "$ready" -eq 1 ]; then
  open "$URL"
  echo "完成，瀏覽器應該已經開好了。"
  echo "這個視窗可以直接關閉，儀表板會繼續在背景跑（要停止請雙擊 stop_dashboard.command）。"
else
  echo "伺服器好像沒有正常啟動，錯誤訊息如下（也存在 $LOGFILE）："
  cat "$LOGFILE"
fi

echo
read -r -p "按 Enter 關閉這個視窗..." _
