#!/usr/bin/env bash
# ============================================================================
# 混沌注入一：假死/宕机（design §10.1 场景一）
# 用法：bash scripts/demo/chaos_kill.sh
# 预期：≤15s 飞书告警卡（critical, process_dead）→ systemd 自动拉起 → 恢复关闭
# ============================================================================
set -euo pipefail
echo "[chaos] SIGKILL demo-app ..."
sudo systemctl kill --signal=SIGKILL demo-app.service
echo "[chaos] 已注入。观察： systemctl status demo-app | sqlite3 /var/lib/aiops/aiops.db 'select * from alert;'"
