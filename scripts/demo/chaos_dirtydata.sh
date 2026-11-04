#!/usr/bin/env bash
# ============================================================================
# 混沌注入二：脏数据触发代码缺陷（design §10.2 场景二）
#
# 用法：
#   bash scripts/demo/chaos_dirtydata.sh           # 注入 10 次非法请求
#   bash scripts/demo/chaos_dirtydata.sh --reset   # 还原被修复的缺陷（回到 v0.1-base 基线）
#
# 预期：10 次非法请求 → 500 × 10 → 日志探针告警（ValueError，风暴降噪 1 条卡片）
#       → （阶段 4）LLM 根因卡 → （阶段 5）修复 PR → 人工 GitHub Merge → 自动部署复测
#
# --reset 说明：场景二修复合并后缺陷即消失。重演前执行 --reset 把部署克隆
# 重置回基线标签 v0.1-base（含 type_bug 缺陷的 target-app）并重启。
# ============================================================================
set -euo pipefail
BASE="http://127.0.0.1:18000"

if [[ "${1:-}" == "--reset" ]]; then
  echo "[chaos] 重置 /opt/aiops/deploy 到基线 v0.1-base 并重启 demo-app ..."
  sudo -u aiops git -C /opt/aiops/deploy fetch --tags -q
  sudo -u aiops git -C /opt/aiops/deploy reset --hard v0.1-base -q
  sudo systemctl restart demo-app
  sleep 3
  curl -s -m 5 "$BASE/health"
  echo
  echo "[chaos] 已重置（bug_mode 应为 type_bug）"
  exit 0
fi

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
