# Sentinel 演示手册（三场景 + 操作清单）

> 状态（2026-11-04）：阶段 0-5 已完成并真机验收；场景一（全自动自愈）、场景二（LLM 修复，PR 已建等人工合并）已跑通；场景三（流量）已验证告警链路。
> 本手册按"明天（2026-11-05）继续"编排。

## 0. 演示前检查（5 分钟）

```bash
# 1) 四单元状态（应全 active，worker 已启用 ops+diag+fix）
systemctl status aiops-router aiops-monitor aiops-worker demo-app --no-pager | grep Active

# 2) Router 健康
curl -s http://127.0.0.1:18081/healthz

# 3) 飞书通道（TEST002 群，关键词 Sentinel）
#    发一条测试告警，群里应出现红色卡片：
curl -s -X POST http://127.0.0.1:18081/api/v1/alerts -H 'Content-Type: application/json' \
  -d '{"source":"manual","severity":"info","subject":"demo-app","error_class":"selftest","message":"[Sentinel] 演示前自检"}'

# 4) 清理上一次演示残留的 firing 告警（避免降噪窗口吞掉新告警）
sudo -u aiops sqlite3 /var/lib/aiops/aiops.db "update alert set status='resolved' where status='firing';"
```

如果 demo-app 是 active 但行为不对（比如 BUG_MODE 已修复），先执行重置：
`bash scripts/demo/chaos_dirtydata.sh --reset`

## 1. 场景一：假死自愈（全自动，约 60s，无人工）

| t | 动作 | 说得出口的台词 |
|---|---|---|
| t0 | `bash scripts/demo/chaos_kill.sh` | "现在直接把业务进程 SIGKILL 掉并停止服务，模拟宕机。" |
| t+~15s | Monitor 进程探针告警（critical / process_dead） | "15 秒内探针发现进程消失，飞书收到红色告警卡。" |
| t+~16s | Router 分诊：incident(acting) + ops task | "调度中心自动建事件、派发自愈任务。" |
| t+~18s | OpsWorker 白名单执行 `systemctl restart demo-app` | "执行单元走白名单安全通道重启，命令不在白名单一律拒绝。" |
| t+~30s | 复检 3×10s 通过 → resolved → 绿色结果卡 | "复检自证恢复后事件关闭，群里收到绿色处置卡。" |

**验证命令**：
```bash
sudo -u aiops sqlite3 /var/lib/aiops/aiops.db "select id,title,state from incident order by id desc limit 1;"
sudo -u aiops sqlite3 /var/lib/aiops/aiops.db "select id,type,status,substr(result_json,1,200) from task order by id desc limit 1;"
```

## 2. 场景二：代码缺陷 → LLM 修复 → 人工合并 → 自动部署（约 5-8 分钟，含一次人工确认）

**当前状态：PR #19 已由 Agent 创建（incident #9 awaiting_human），明天从这里继续。**

### 2a. 复现前半段（可选，若想重新演示一遍）

```bash
bash scripts/demo/chaos_dirtydata.sh          # 10 次脏数据 → 500 风暴（降噪后 1 条卡）
# 观察：橙色告警卡 → 蓝色根因卡（orders.py:36, 置信度≈0.96）→ 橙色 PR 待合并卡
```

### 2b. 人工确认（红线动作，只有你能做）

1. 打开飞书群里的 PR 卡，点链接进 GitHub：`https://github.com/machinetus/aiops-sentinel/pull/19`
2. 审阅变更（`orders.py +3 −7` 修复缺陷、`test_orders.py +12 −14` 同步断言、沙箱 11 测试全过）
3. 点 **Merge pull request**（squash）
4. 30 秒内 Router 轮询检测到 merged → 自动派发 deploy 任务（git pull + restart + 复检）
5. 复测重放触发样本（期望 400 而非 500）→ incident resolved → **绿色上线卡进群**

**验证命令**：
```bash
sudo -u aiops sqlite3 /var/lib/aiops/aiops.db "select id,incident_id,pr_number,status from patch_proposal;"
sudo -u aiops sqlite3 /var/lib/aiops/aiops.db "select id,title,state from incident order by id desc limit 1;"
systemctl status demo-app --no-pager | grep Active     # 部署后被重启过
journalctl -u aiops-router --since '-5 min' | grep -E 'PR #19|deploy'
```

> 演示后重置（还原被修复的缺陷，便于下次重演）：
> `bash scripts/demo/chaos_dirtydata.sh --reset`

## 3. 场景三：异常流量 → 限流自愈（当前告警链路已验证；限流动作在阶段 6 接入）

```bash
bash scripts/demo/chaos_flood.sh 25          # 25s 高频压测
# 观察：QPS 告警卡（qps_over）；阶段 6 将自动 set_rate_limit 并在到期后自动解除
```

## 4. 故障预案

| 情况 | 处理 |
|---|---|
| GitHub 502/503（今天出现多次） | 系统已内置 push/建 PR 3 次重试；若仍失败，incident 会 escalated 并出红色转人工卡 |
| LLM 不可用（超时/额度） | diag task failed → 升级转人工卡（fail-safe，不会静默降级） |
| 连续第 3 次 kill | flapping 保护触发：停止自动重启、转人工卡（这是特性，不是 bug） |
| 飞书无响应 | 检查 `/etc/aiops/secrets.env` 的 webhook 与机器人关键词 Sentinel |

## 5. 明天（2026-11-05）需要你操作的三件事

1. **Merge PR #19**（场景二后半段自动跑完）→ 群里会收到绿色上线卡
2. （可选）配置**飞书自建应用**以启用 @agent 指令通道（按钮确认/自然语言指令）：
   开发者后台创建自建应用 → 开权限 `im:message`、`im:message:send_as_bot` →
   事件订阅选「长连接」加 `im.message.receive_v1` → 机器人拉进 TEST002 → 把 App ID/Secret 发我
3. **彩排一遍**三场景（8-10 分钟），确认飞书卡片顺序与话术

## 6. 演示话术要点（答辩）

- "感知-调度-执行三层解耦，任何一层可独立重启不影响其他层。"
- "告警不是裸转发：指纹降噪把 10 次风暴收敛成 1 张卡 + 计数。"
- "自愈不是盲重启：复检自证恢复才算完成；20 分钟内第 3 次宕机会触发 flapping 保护转人工。"
- "代码修复的每一步都有确定性校验：路径白名单、old_text 唯一匹配、沙箱测试硬门。"
- "Agent 永远不能自己合并 PR——人工确认是唯一的发版入口，这是硬红线。"
- "所有动作双落审计（SQLite + JSONL），`export_audit.py` 一条命令导出。"
