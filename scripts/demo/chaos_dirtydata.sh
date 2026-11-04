#!/usr/bin/env bash
# ============================================================================
# 混沌注入二：脏数据触发代码缺陷（design §10.2 场景二）
# 前置：demo-app 以 BUG_MODE=type_bug 运行（demo-app.service 默认）
# 用法：bash scripts/demo/chaos_dirtydata.sh
# 预期：10 次非法请求 → 500 × 10 → 日志探针告警（ValueError，风暴降噪 1 条卡片）
# ============================================================================
set -euo pipefail
BASE="http://127.0.0.1:18000"
echo "[chaos] 注入脏数据 ×10（amount=\"abc\"）..."
for i in $(seq 1 10); do
  curl -s -o /dev/null -X POST "$BASE/api/orders" -H 'Content-Type: application/json' \
    -d "{\"item\":\"sku-$i\",\"amount\":\"abc\",\"price\":2}" || true
done
echo "[chaos] 混入正常请求 ×3 ..."
for i in 1 2 3; do
  curl -s -o /dev/null -X POST "$BASE/api/orders" -H 'Content-Type: application/json' \
    -d "{\"item\":\"ok-$i\",\"amount\":3,\"price\":2}" || true
done
echo "[chaos] 已注入。观察：sqlite3 /var/lib/aiops/aiops.db \"select id,source,severity,error_class,count from alert;\""
