#!/bin/bash
# 双击运行：校验登录态 → 增量同步语音课件
# 想只看看计划而不真导出：在终端里跑  ./一键同步.command --dry-run
cd "$(dirname "$0")" || exit 1

# 选解释器：优先项目内 .venv，退回系统 python3
PY=""
for c in ".venv/bin/python" "$(command -v python3 2>/dev/null)" "/usr/bin/python3"; do
  if [ -n "$c" ] && [ -x "$c" ]; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "找不到 python3。先装命令行工具：xcode-select --install"
  read -n 1 -s -r -p "按任意键关闭…"; echo
  exit 1
fi

echo "icourse 语音课件同步"
echo "解释器：$PY"
echo

"$PY" icourse_sync.py "$@"
code=$?

echo
if [ "$code" -eq 0 ]; then
  echo "完成。产物在 $HOME/Documents/语音课件"
else
  echo "退出码 $code —— 上面有报错，按提示处理即可。"
fi
echo
read -n 1 -s -r -p "按任意键关闭窗口…"
echo
