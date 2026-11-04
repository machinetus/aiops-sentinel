#!/usr/bin/env bash
# ============================================================================
# 混沌注入一：假死/宕机（design §10.1 场景一）
# 用法：bash scripts/demo/chaos_kill.sh
#
# 关键：SIGKILL 之后必须 stop —— demo-app.service 配置了 Restart=on-failure，
# 若只 kill，systemd 会在 3 秒内自动拉起，探针（15s 周期）大概率错过宕机窗口，
# 且场景一中 Agent 的自愈重启动作将失去意义。stop 后服务保持 inactive，
# 宕机状态留给 Agent（阶段 3 OpsWorker 白名单 restart）处置。
#
# 预期（阶段 2）：≤15s 进程告警（critical, process_dead）+ HTTP 告警；
# 手动 systemctl start demo-app 后 ≤30s 收到恢复。
# 预期（阶段 3）：Agent 自动重启 + 复检 + 结果卡片，全程无人工。
# ============================================================================
set -euo pipefail
echo "[chaos] SIGKILL demo-app ..."
sudo systemctl kill --signal=SIGKILL demo-app.service 2>/dev/null || true
sudo systemctl stop demo-app.service
echo "[chaos] 已注入（服务保持 down）。观察："
echo "  sqlite3 /var/lib/aiops/aiops.db \"select id,source,severity,error_class,count,status from alert;\""
