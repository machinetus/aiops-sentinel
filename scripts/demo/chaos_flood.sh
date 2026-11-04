#!/usr/bin/env bash
# ============================================================================
# 混沌注入三：异常流量（design §10.3 场景三）
# 用法：bash scripts/demo/chaos_flood.sh [秒数，默认 60]
# 预期：QPS 突破 probes.yaml 阈值（默认 50）→ 流量告警（qps_over）
# 注意：本阶段仅告警；限流动作在阶段 3/6 接入
# ============================================================================
set -euo pipefail
DURATION="${1:-60}"
BASE="http://127.0.0.1:18000"
echo "[chaos] ${DURATION}s 高频压测 /api/search ..."
timeout "$DURATION" bash -c "seq 1 100000 | xargs -P 50 -I{} curl -s -o /dev/null -m 2 '$BASE/api/search?q=flood' || true"
echo "[chaos] 压测结束。观察：sqlite3 /var/lib/aiops/aiops.db \"select id,source,severity,error_class,count,status from alert;\""
