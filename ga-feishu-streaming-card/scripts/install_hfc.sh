#!/usr/bin/env bash
# 安装 hfc GA 插件桥（等价于 `hfc install`，供无 uv 环境使用）。
# 用法: scripts/install_hfc.sh [GA_ROOT]   （不传 GA_ROOT 时交给 hfc 探测链：--ga-root → HFC_GA_ROOT
#                                        → GA_ROOT/GA_HOME（需 AGENTS.md 或 plugins/ 标志）→ cwd 标志
#                                        → cwd 上级链首个标志目录；全部未命中则报错并以非零码退出）
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DIR")"
GA_ROOT="${1:-${HFC_GA_ROOT:-}}"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
ARGS=()
if [ -n "$GA_ROOT" ]; then
  ARGS+=(--ga-root "$GA_ROOT")
fi
if command -v uv >/dev/null 2>&1; then
  uv run python -m ga_feishu_streaming_card.cli install "${ARGS[@]}"
else
  python3 -m ga_feishu_streaming_card.cli install "${ARGS[@]}"
fi
