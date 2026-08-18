#!/bin/sh
# One-shot retention provisioning for the Langfuse stack (compose service
# langfuse-retention-init). Idempotent: safe to re-run at any time via
#   docker compose -f docker-compose.langfuse.yml up -d langfuse-retention-init
#
# Applies:
#   - ClickHouse TTL (180d default, RETENTION_DAYS) on the 6 trace tables
#   - MinIO ILM expiry rule on the events/ blob prefix (same window)
#
# Runs in the minio image: only sh builtins + curl + mc are available
# (no grep/sed/awk) - string checks use shell `case` patterns.
set -eu

RETENTION_DAYS="${RETENTION_DAYS:-180}"
CH_USER="${CLICKHOUSE_USER:-clickhouse}"
CH_PASS="${CLICKHOUSE_PASSWORD:-clickhouse}"
CH_URL="http://clickhouse:8123"
MC_ALIAS="retention-init"

log() { echo "[retention-init] $*"; }

# Per-poll curl cap (seconds). wait_until narrows it to the remaining
# wall-clock budget before each poll so the last poll cannot overshoot its
# deadline; outside waits it stays at the full 10s.
poll_curl_cap=10

ch_query() {
  curl -sf --connect-timeout 5 --max-time "$poll_curl_cap" -u "$CH_USER:$CH_PASS" "$CH_URL/" --data-binary "$1"
}

# events_* tables carry TYPE text(...) indices that re-validate on ALTER
# (not a wait predicate: the full 10s cap always applies)
ch_alter() {
  curl -sf --connect-timeout 5 --max-time 10 -u "$CH_USER:$CH_PASS" "$CH_URL/?enable_full_text_index=1" --data-binary "$1"
}

web_healthy() {
  curl -sf --connect-timeout 5 --max-time "$poll_curl_cap" http://langfuse-web:3000/api/public/health >/dev/null 2>&1
}

table_exists() {
  [ "$(ch_query "EXISTS default.$1")" = "1" ]
}

# Wall-clock bounded wait. The deadline is checked BEFORE each poll and the
# poll's curl cap is narrowed to the remaining budget (predicate functions
# read $poll_curl_cap), so the window is a hard upper bound: a predicate
# starting at deadline-epsilon gets a ~0s cap, not the full 10s. The
# post-poll sleep is likewise capped at the remaining budget.
wait_until() {
  what="$1"
  timeout_s="$2"
  shift 2
  deadline=$(( $(date +%s) + timeout_s ))
  while :; do
    remaining=$(( deadline - $(date +%s) ))
    if [ "$remaining" -le 0 ]; then
      log "FATAL: $what not ready after ${timeout_s}s"
      exit 1
    fi
    if [ "$remaining" -gt 10 ]; then
      poll_curl_cap=10
    else
      poll_curl_cap=$remaining
    fi
    if "$@"; then
      poll_curl_cap=10
      return 0
    fi
    remaining=$(( deadline - $(date +%s) ))
    if [ "$remaining" -gt 2 ]; then
      sleep 2
    elif [ "$remaining" -gt 0 ]; then
      sleep "$remaining"
    fi
  done
}

# langfuse-web runs the ClickHouse migrations on boot; TTL columns need the
# tables to exist first (fresh volume), so poll web health, then each table.
log "waiting for langfuse-web health"
wait_until "langfuse-web health" 240 web_healthy

for table in traces observations scores events_core events_full blob_storage_file_log; do
  wait_until "default.$table" 300 table_exists "$table"
done
log "all 6 trace tables present"

apply_ttl() {
  table="$1"
  column="$2"
  ddl="$(ch_query "SHOW CREATE TABLE default.$table")"
  case "$ddl" in
    *"TTL $column + toIntervalDay($RETENTION_DAYS)"*)
      log "ttl ok: default.$table ($column, ${RETENTION_DAYS}d)"
      ;;
    *)
      if ! ch_alter "ALTER TABLE default.$table MODIFY TTL $column + INTERVAL $RETENTION_DAYS DAY" >/dev/null; then
        log "FATAL: ALTER on default.$table exceeded the 10s curl cap - the"
        log "mutation keeps running server-side; re-run to converge:"
        log "  docker compose -f docker-compose.langfuse.yml up -d langfuse-retention-init"
        exit 1
      fi
      log "ttl applied: default.$table ($column, ${RETENTION_DAYS}d)"
      ;;
  esac
}

apply_ttl traces                timestamp
apply_ttl observations          start_time
apply_ttl scores                timestamp
apply_ttl events_core           start_time
apply_ttl events_full           start_time
apply_ttl blob_storage_file_log created_at

# mc ilm rule add duplicates same-prefix rules, so only add when absent.
mc alias set "$MC_ALIAS" http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
ilm="$(mc ilm ls "$MC_ALIAS/langfuse" 2>/dev/null || true)"
case "$ilm" in
  *" events/ "*)
    case "$ilm" in
      *" $RETENTION_DAYS "*)
        log "ilm ok: events/ rule already ${RETENTION_DAYS}d"
        ;;
      *)
        log "WARN: events/ rule exists with different days; align manually:"
        log "  docker exec modex-langfuse-minio mc ilm ls local/langfuse"
        log "  docker exec modex-langfuse-minio mc ilm rule edit local/langfuse --id <id> --expire-days $RETENTION_DAYS"
        ;;
    esac
    ;;
  *)
    mc ilm rule add "$MC_ALIAS/langfuse" --prefix "events/" --expire-days "$RETENTION_DAYS" >/dev/null
    log "ilm applied: events/ -> ${RETENTION_DAYS}d"
    ;;
esac

log "done: ${RETENTION_DAYS}d retention provisioned (ClickHouse TTL + MinIO events/ ILM)"
