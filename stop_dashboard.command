#!/bin/bash
# 雙擊本檔案即可停止背景執行中的 netdiag 儀表板。

PIDFILE="/tmp/netdiag_dashboard.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")"
  rm -f "$PIDFILE"
  echo "已停止儀表板。"
else
  # 找不到 pid 檔或程序已死，退而求其次用連接埠找出來關掉
  PID=$(lsof -ti tcp:8765 2>/dev/null)
  if [ -n "$PID" ]; then
    kill "$PID"
    echo "已停止儀表板（連接埠 8765）。"
  else
    echo "目前沒有偵測到在跑的儀表板。"
  fi
fi

echo
read -r -p "按 Enter 關閉這個視窗..." _
