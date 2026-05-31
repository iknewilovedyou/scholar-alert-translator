#!/usr/bin/env bash
# 每日学术快讯 — cron 用
# 用法: bash run.sh
# cron: 0 9 * * * cd /path/to/scholar-alert-translator && bash run.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="${OUTPUT_DIR:-$SCRIPT_DIR/output}"
LOG_FILE="$SCRIPT_DIR/cron.log"
MAX_LOG_LINES=1000

mkdir -p "$OUTPUT_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始抓取..." >> "$LOG_FILE"

cd "$SCRIPT_DIR"
python3 scripts/fetch-scholar-alerts.py \
  --since-days 1 \
  --output-dir "$OUTPUT_DIR" \
  >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

# 日志轮转: 只保留最近 N 行
tail -n "$MAX_LOG_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"

# 输出文件
echo "输出在: $OUTPUT_DIR/scholar_alert_output.pdf"
ls -lh "$OUTPUT_DIR/scholar_alert_output."* 2>/dev/null
