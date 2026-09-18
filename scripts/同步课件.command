#!/bin/bash
# 双击运行：按 config.json 同步语音课件
cd "$(dirname "$0")/.." || exit 1

echo "===================================="
echo " icourse 语音课件同步"
echo " 目录：$(pwd)"
echo "===================================="
echo

# Python 定位：优先挑「装了 browser-cookie3」的解释器 —— 能直接读浏览器 Cookie
# 走纯 HTTP 同步（快、不用开浏览器）；一个都没有则退回 python3 走 bsk 兜底路线。
PY=""
for CAND in \
  "$HOME/.workbuddy/binaries/python/envs/default/bin/python" \
  "/opt/homebrew/bin/python3" \
  "/usr/local/bin/python3" \
  "$HOME/.pyenv/shims/python3" \
  "$(command -v python3 2>/dev/null)"; do
  if [ -n "$CAND" ] && [ -x "$CAND" ] && "$CAND" -c "import browser_cookie3" >/dev/null 2>&1; then
    PY="$CAND"; break
  fi
done
[ -z "$PY" ] && PY="$(command -v python3 2>/dev/null || echo /usr/bin/python3)"
echo "Python：$PY"
echo

"$PY" src/icc_sync.py "$@"
code=$?

echo
if [ $code -eq 0 ]; then
  echo "完成。"
else
  echo "退出码 $code（出错了）。"
  echo "提示：若提示登录态无效，先跑一次现场登录："
  echo "  \"$PY\" src/icc_sync.py --login"
fi
echo
echo "按回车关闭窗口..."
read -r _
