#!/usr/bin/env bash
# 每日学术快讯 — cron 用
# 用法: bash run.sh
# cron: 0 9 * * * cd /path/to/scholar-alert-translator && bash run.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load local delivery settings for cron. Keep secrets in .env, not in crontab.
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  . "$SCRIPT_DIR/.env"
  set +a
fi

OUTPUT_DIR="${OUTPUT_DIR:-$SCRIPT_DIR/output}"
LOG_FILE="${LOG_FILE:-$SCRIPT_DIR/cron.log}"
MAX_LOG_LINES="${MAX_LOG_LINES:-1000}"

JSON_FILE="$OUTPUT_DIR/papers_translated.json"
MARKDOWN_FILE="$OUTPUT_DIR/scholar_alert_output.md"
PDF_FILE="$OUTPUT_DIR/scholar_alert_output.pdf"

mkdir -p "$OUTPUT_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始抓取..." >> "$LOG_FILE"

cd "$SCRIPT_DIR"
set +e
python3 scripts/fetch-scholar-alerts.py \
  --since-days "${SINCE_DAYS:-1}" \
  --output-dir "$OUTPUT_DIR" \
  >> "$LOG_FILE" 2>&1
RUN_STATUS=$?
set -e

if [ "$RUN_STATUS" -eq 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成" >> "$LOG_FILE"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 失败: exit=$RUN_STATUS" >> "$LOG_FILE"
fi

DELIVER_FILE=""
if [ -s "$PDF_FILE" ]; then
  DELIVER_FILE="$PDF_FILE"
elif [ -s "$MARKDOWN_FILE" ]; then
  DELIVER_FILE="$MARKDOWN_FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] PDF 不存在，改为发送 Markdown: $MARKDOWN_FILE" >> "$LOG_FILE"
fi

export OUTPUT_DIR LOG_FILE JSON_FILE MARKDOWN_FILE PDF_FILE DELIVER_FILE RUN_STATUS

if [ -n "${NOTIFY_COPY_TO:-}" ] && [ -n "$DELIVER_FILE" ]; then
  mkdir -p "$NOTIFY_COPY_TO"
  cp "$DELIVER_FILE" "$NOTIFY_COPY_TO/"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已复制到 NOTIFY_COPY_TO: $NOTIFY_COPY_TO" >> "$LOG_FILE"
fi

# Optional file delivery hook. Example in .env:
# SEND_FILE_CMD='openclaw send-file "$DELIVER_FILE"'
if [ -n "${SEND_FILE_CMD:-}" ] && [ -n "$DELIVER_FILE" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 执行发送命令: $SEND_FILE_CMD" >> "$LOG_FILE"
  set +e
  sh -c "$SEND_FILE_CMD" >> "$LOG_FILE" 2>&1
  SEND_STATUS=$?
  set -e
  if [ "$SEND_STATUS" -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 发送失败: exit=$SEND_STATUS" >> "$LOG_FILE"
    RUN_STATUS="$SEND_STATUS"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 发送完成: $DELIVER_FILE" >> "$LOG_FILE"
  fi
elif [ -z "$DELIVER_FILE" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 没有可发送文件（PDF/Markdown 均不存在）" >> "$LOG_FILE"
fi

echo "" >> "$LOG_FILE"

# 日志轮转: 只保留最近 N 行
tail -n "$MAX_LOG_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"

# 输出文件
echo "输出目录: $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"/scholar_alert_output.* "$OUTPUT_DIR"/papers_translated.json 2>/dev/null

exit "$RUN_STATUS"
