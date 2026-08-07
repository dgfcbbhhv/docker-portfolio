#!/usr/bin/env sh
# 数据/笔记/配置备份（Linux / Docker 主机）
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
TARGET="$ROOT/backups/$STAMP"
mkdir -p "$TARGET"
for item in data notes config.json requirements.txt requirements.lock.txt; do
  [ -e "$ROOT/$item" ] && cp -a "$ROOT/$item" "$TARGET/"
done
# 只保留最近 14 份
ls -1dt "$ROOT"/backups/*/ 2>/dev/null | tail -n +15 | xargs -r rm -rf
echo "Backup created: $TARGET"
