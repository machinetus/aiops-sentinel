# AIOps 智能运维 Agent（Sentinel）：技术设计（Rev 1）

> 仓库：`aiops-sentinel`（monorepo：agent 源码 + demo 应用 + 部署脚本 + 混沌脚本）。
> 事实源：`prd.md`、飞书开放平台文档（事件长连接 / 消息卡片 / 自定义机器人）、gh CLI 文档、目标环境（Win11 + VMware Ubuntu 22.04+）。
> 原则：白名单执行、人工确认红线、LLM 只建议不执行、fail-safe 转人工、可演示可复现。

## 0. 设计决策摘要

| # | 决策 | 状态 | 理由 |
|---|---|---|---|
| D1 | 四个 systemd 单元（router / monitor / worker / demo-app），Worker 进程内含三种角色协程 | 决定 | 组件可独立重启演示；不引入消息队列，部署面最小 |
| D2 | SQLite 单写者（仅 Router 写），WAL 模式 | 决定 | Worker / Monitor 全部经 Router 内部 API 读写，规避并发写 |
| D3 | 告警治理：指纹去重 + 计数 + 恢复自动关闭 | 决定 | 演示不轰炸群里；贴近真实 AIOps 的降噪诉求 |
| D4 | 规则优先、LLM 兜底的意图理解 | 决定 | 常用指令零延迟零成本；自由文本才调 LLM |
| D5 | 白名单执行器：`actions.yaml` 定义 + sudoers 精确匹配 + 运行时二次校验（三重门） | 决定 | 安全底线，越权直接拒绝并告警 |
| D6 | 副作用仅两条通道：sudoers 精确命令 + demo-app `127.0.0.1` admin API | 决定 | 副作用面收敛，审计集中 |
| D7 | 复检（post-verify）内建于 OpsWorker | 决定 | 自愈必须自证恢复才算 resolved |
| D8 | 补丁格式 = search/replace 块 + 新文件全文，执行器精确匹配 | 决定 | 比 LLM 直接产 unified diff 稳定得多；匹配失败即中止 |
| D9 | 修复流水线：worktree + venv 沙箱 + draft PR + 人工确认（双通道） | 决定 | 代码变更可审查、可回滚、演示不中断 |
| D10 | 确认双通道：卡片按钮 / @agent 指令 | 决定 | 卡片回调长连接可用性有不确定性，指令兜底 |
| D11 | LLM 输出强制 JSON schema + 低温度 + 失败转人工 | 决定 | 可控可测，不静默重试 |
| D12 | monorepo：agent + target-app 同库，三克隆分工（runtime / deploy / sandbox） | 决定 | PR 与部署链路最短；runtime 只读防自我修改 |
| D13 | 事件状态机 + 任务表持久化 | 决定 | 断电重启后状态可恢复、时间线报告可回放 |
| D14 | demo 缺陷可开关（`BUG_MODE` 环境变量） | 决定 | 同一代码既可演示又可回归 |
| D15 | 审计双落：SQLite（可查询）+ append-only JSONL（可归档） | 决定 | 演示与事后审计两用 |
| D16 | wheelhouse 预热，沙箱安装离线可用 | 决定 | 演示不依赖 PyPI；网络抖动不砸场 |

## 1. 总体架构

### 1.1 拓扑

```
┌───────────────────────── Win11 宿主机 ─────────────────────────┐
│   浏览器（飞书群 / GitHub PR）        SSH / 终端                  │
└───────────────┬────────────────────────────┬───────────────────┘
                │ VMware（NAT / 桥接）        │
┌───────────────┴────────────────────────────┴───────────────────┐
│ Ubuntu VM                                                      │
│                                                                │
│  ┌───────────────┐  告警 HTTP  ┌────────────────────┐          │
│  │ aiops-monitor │ ──────────▶ │ aiops-router       │◀── @ 指令 │
│  │  四类探针      │            │ 127.0.0.1:18081     │   长连接  │
│  └───────────────┘            │ 降噪/分诊/状态机/派发│           │
│  ┌───────────────┐  webhook   │ SQLite（单写者）    │──卡片──▶ 飞书群
│  │ 业务程序(可选) │ ──────────▶ └─────────┬──────────┘◀─按钮/指令─┘
│  └───────────────┘                      │ claim/result (HTTP)   │
│                         ┌───────────────┼───────────────┐       │
│                         ▼               ▼               ▼       │
│                  ┌───────────┐  ┌────────────┐  ┌────────────┐  │
│                  │ OpsWorker │  │ DiagWorker │  │ FixWorker  │  │
│                  │ 白名单自愈 │  │ LLM 根因    │  │ 补丁→PR→确认│  │
│                  └─────┬─────┘  └─────┬──────┘  └─────┬──────┘  │
│                        │              │               │         │
│             sudoers 精确命令      只读日志+代码    worktree+venv+gh│
│              + admin API(:9101)        │               │         │
│                        ▼              └───────┬───────┘         │
│                  ┌────────────────────────────▼─────┐           │
│                  │ demo-app（systemd, :18000/:9101）  │           │
│                  └──────────────────────────────────┘           │
│          deploy 克隆拉取 ◀──────── GitHub（monorepo, main 保护） │
└────────────────────────────────────────────────────────────────┘
```

### 1.2 组件与端口

| 组件 | systemd 单元 | 端口 | 运行用户 | 读写面 |
|---|---|---|---|---|
| Router | `aiops-router.service` | `127.0.0.1:18081` | aiops | SQLite（唯一写者）、飞书、GitHub（只读） |
| Monitor | `aiops-monitor.service` | — | aiops | 只读探测；写告警走 Router API |
| Worker（Ops/Diag/Fix 三角色） | `aiops-worker.service` | — | aiops | 白名单命令、admin API、sandbox 克隆、gh、LLM |
| demo 应用 | `demo-app.service` | `:18000` 业务（VM 内可达）、`127.0.0.1:9101` admin | demoapp | `/var/log/demo-app/app.log`、`/run/demo-app/state.json` |

Router 内部 API（全部仅 127.0.0.1）：

> 端口说明（2026-11-04 部署确认）：目标 VM 上 8081 与 8000 已被既有服务占用，
> 故 Router 使用 **18081**、demo-app 业务口使用 **18000**；admin 9101 经查空闲沿用。

| Method | Path | 调用方 | 说明 |
|---|---|---|---|
| POST | `/api/v1/alerts` | Monitor / 业务 webhook | 告警入口（指纹降噪在此） |
| POST | `/api/v1/tasks/claim` | Worker | 认领下一个排队任务（带 5min 租约） |
| POST | `/api/v1/tasks/{id}/result` | Worker | 回传任务结果 |
| POST | `/api/v1/feishu/events` | 飞书接收协程 | 入站 @ 消息 / 卡片按钮（进程内转发亦可） |
| GET | `/healthz` | 探针 / 巡检 | 存活检查 |

### 1.3 两条主链数据流

- **告警链**：probe / webhook → `POST /alerts` → 指纹去重 → 分诊规则 → incident + task → Worker 认领 → 执行 + 复检 → 结果卡片。
- **指令链**：飞书 @ → 长连接事件 → 规则表 / LLM 意图 → 查询类直接应答；操作类走同一条 task 链。

### 1.4 目录布局与部署拓扑

```
/opt/aiops/
├── runtime/            # agent 运行克隆（root:aiops 0750，aiops 只读）；人工 git pull 更新
├── deploy/             # demo-app 部署克隆（FixWorker 合并后拉取 + 重启）
├── sandbox/
│   ├── repo/           # FixWorker 工作克隆（创建 worktree 用）
│   ├── wt-<incident>/  # 每个修复任务的 worktree + .sbx-venv
│   └── wheelhouse/     # 离线依赖缓存
├── venv/               # agent 运行 venv
└── logs -> /var/log/aiops
/var/lib/aiops/aiops.db
/var/log/demo-app/app.log
/etc/aiops/{config.yaml, probes.yaml, actions.yaml, secrets.env(0600)}
```

- runtime 只读防自我修改：systemd 单元设 `PYTHONDONTWRITEBYTECODE=1`（避免 `__pycache__` 写失败）。
- 三个克隆（runtime / deploy / sandbox/repo）相互独立：运行、部署、改码互不污染；这是「Agent 不能改自己」的物理隔离。

### 1.5 仓库结构

```
aiops-sentinel/
├── docs/                # prd / design / implement / 演示手册
├── src/
│   ├── common/          # config / logging+脱敏 / audit / db / fingerprint
│   ├── monitor/         # 四类探针
│   ├── router/          # API / 分诊规则 / 意图 / 状态机 / 派发
│   ├── workers/         # ops_worker / diag_worker / fix_worker
│   ├── feishu/          # sender(卡片) / receiver(长连接) / cards(模板)
│   ├── llm/             # client / prompts / schema 校验
│   └── sandbox/         # worktree / patcher / test_runner
├── target-app/          # demo 被监控应用（FastAPI）
│   ├── app/             # main.py / orders.py / limits.py ...
│   └── tests/
├── deploy/
│   ├── systemd/         # 四个 .service
│   ├── sudoers/         # aiops-sentinel 精确授权片段
│   └── bootstrap.sh
├── config/              # *.yaml.example
├── scripts/
│   ├── demo/            # chaos_kill.sh / chaos_dirtydata.sh / chaos_flood.sh
│   └── export_audit.py
└── tests/               # agent 单测 / 集成 / e2e(慢标)
```

## 2. 数据模型（SQLite，Router 单写者）

### 2.1 `alert`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `fingerprint` | TEXT NOT NULL | `sha256(source|probe|subject|error_class)` |
| `source` | TEXT NOT NULL | `probe:process` / `probe:http` / `probe:log` / `probe:traffic` / `webhook` / `manual` |
| `severity` | TEXT NOT NULL | `critical` / `warning` / `info` |
| `subject` | TEXT NOT NULL | 服务名（`demo-app`） |
| `error_class` | TEXT | `process_dead` / `http_5xx` / `ValueError` / `qps_over` … |
| `payload_json` | TEXT NOT NULL | 原始载荷（含触发样本，供复测重放） |
| `count` | INTEGER DEFAULT 1 | 同指纹聚合计数 |
| `first_seen_at` / `last_seen_at` | TEXT | ISO8601 |
| `status` | TEXT DEFAULT 'firing' | `firing` / `resolved` |

索引：`(fingerprint, status)`、`(status, last_seen_at)`。

### 2.2 `incident`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 演示中的「Incident #n」 |
| `fingerprint` / `alert_id` | TEXT / INTEGER | 归因 |
| `title` | TEXT | 人类可读标题 |
| `classification` | TEXT | `process_down` / `app_error` / `traffic_anomaly` / `manual_*` |
| `state` | TEXT DEFAULT 'open' | 见 2.6 状态机 |
| `severity` | TEXT | |
| `escalation_reason` | TEXT NULL | 升级原因（失败 / 超时 / 人工拒绝） |
| `created_at` / `updated_at` / `resolved_at` | TEXT | |

索引：`(state)`、`(classification, created_at)`（flapping 统计用）。

### 2.3 `task`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `incident_id` | INTEGER NOT NULL | |
| `type` | TEXT NOT NULL | `ops` / `diag` / `fix` |
| `status` | TEXT DEFAULT 'queued' | `queued` / `running` / `succeeded` / `failed` / `cancelled` |
| `payload_json` | TEXT | 动作名与参数 / 诊断上下文 / 修复输入 |
| `result_json` | TEXT | 执行结果（退出码、复检、根因、PR 信息） |
| `worker_id` / `lease_expires_at` / `attempts` | TEXT / TEXT / INTEGER | 认领租约 5min；重派最多 1 次 |

### 2.4 `action_log`（审计）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `incident_id` / `task_id` | INTEGER | 可空（如人工直接操作） |
| `operator` | TEXT NOT NULL | `agent` / `human:<open_id 或备注>` |
| `action` | TEXT NOT NULL | `restart_demo_app` / `block_ip` / `pr_create` / `pr_merge` / `pr_reject` / `whitelist_deny` … |
| `detail_json` | TEXT | 命令、退出码、输出摘要（截断 200 字符、脱敏）、PR 链接 |
| `created_at` | TEXT | |

双落：写库同时 append `/var/log/aiops/audit.jsonl`（0600，logrotate 按周）。

### 2.5 `patch_proposal`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `incident_id` | INTEGER NOT NULL | |
| `branch` | TEXT | `aiops/fix-<incident>` |
| `pr_number` / `pr_url` | INTEGER / TEXT | draft PR |
| `root_cause` | TEXT | 来自 DiagWorker |
| `diff_stat` | TEXT | `+14 −2 (2 files)` |
| `test_summary` | TEXT | `8 passed in 3.2s` |
| `status` | TEXT | `generating` / `testing` / `awaiting_confirm` / `merged` / `rejected` / `failed` |
| `created_at` / `updated_at` | TEXT | |

约束：同 incident 最多一行 `awaiting_confirm`（应用层单飞校验）。

### 2.6 事件状态机

| from | 事件 | to | 副作用 |
|---|---|---|---|
| — | 分诊 `process_down` / `traffic_anomaly` | `open → acting` | 派发 ops task |
| — | 分诊 `app_error` | `open → diagnosing` | 派发 diag task |
| `diagnosing` | diag `need_code_fix=true` | `acting` | 派发 fix task |
| `diagnosing` | diag `need_code_fix=false` | `acting` | 派发 diag 建议的 ops task |
| `acting` | ops 成功 + 复检通过 | `verifying → resolved` | 绿色结果卡片 |
| `acting` | ops 失败 / 复检 3 次不过 / restart 配额尽 | `escalated` | 转人工卡片 |
| `acting` | fix 创建 draft PR | `awaiting_human` | PR 确认卡片 |
| `awaiting_human` | 确认 → 合并 → 部署 → 复测通过 | `resolved` | 上线卡片 |
| `awaiting_human` | 拒绝 | `escalated` | 关 PR + 转人工卡片 |
| 任意 | 任务租约过期重派仍失败 / `awaiting_human` 超 30min | `escalated` | 超时卡片 |

- 升级（escalated）后同指纹新告警仍记录但不再自动处置（冷却 10min）。
- 所有状态迁移写 `action_log`（operator=`agent`，action=`state_transition`）。

## 3. 感知面设计（aiops-monitor）

### 3.1 探针（`probes.yaml` 驱动）

```yaml
probes:
  - type: process          # systemd is-active + 主 PID 存活
    subject: demo-app
    interval: 15
  - type: http             # GET /health；连续 3 次失败才告警（防抖）
    subject: demo-app
    url: http://127.0.0.1:18000/health
    expect: 200
    consecutive_failures: 3
    interval: 15
  - type: log              # 错误行速率 + 错误类归类
    subject: demo-app
    path: /var/log/demo-app/app.log
    error_pattern: 'ERROR|Traceback'
    rate_per_min: 5
    interval: 30
  - type: traffic          # QPS 滑窗计数
    subject: demo-app
    metrics_url: http://127.0.0.1:18000/metrics
    qps_threshold: 50
    interval: 10
```

- 探针循环为 asyncio task，互不阻塞；探测失败按类型映射 `error_class`（`process_dead` / `http_5xx` / 日志首行异常名 / `qps_over`）。
- 日志探针记录触发样本（含脏数据请求原文），存入 `alert.payload_json`——**复测重放的数据源**。

### 3.2 防抖与指纹

- HTTP 探针需连续 N 次失败（默认 3）才上报；恢复需连续 2 次成功才发恢复。
- 指纹 = `sha256(source|probe|subject|error_class)`；同指纹 5min 窗口内：不新增告警、`count += 1`、刷新 `last_seen_at`，且只对**首条**触发分诊（防风暴重复建 incident）。

### 3.3 恢复判定

- 告警侧：探针恢复 → `alert.status=resolved`，群内不发噪音（可在 config 开启恢复通知）。
- 事件侧：只有任务复检通过或人工操作才 resolve 事件，告警恢复不自动关事件。

### 3.4 Webhook 入口

- 业务程序 `POST /api/v1/alerts`，body：`{subject, severity, error_class, message, sample?}`；demo 用 `curl` 模拟「业务主动上报」触发源，与探针共用同一条降噪 / 分诊链。

## 4. Router 设计

### 4.1 分诊规则表（可配置）

| 特征（source + error_class + severity） | classification | playbook |
|---|---|---|
| `probe:process` + `process_dead` + critical | `process_down` | ops：`restart_demo_app` + 复检 |
| `probe:http` + `http_5xx` + critical | `process_down` | 同上 |
| `probe:log` 错误率超阈 + warning | `app_error` | diag：根因分析 |
| `probe:traffic` + `qps_over` + warning | `traffic_anomaly` | ops：`set_rate_limit`（自动到期） |
| `webhook` 任意 | 按载荷映射 | 同上映射 |
| `manual`（@ 指令） | `manual_*` / `query` | 见 4.2 |

### 4.2 意图理解（规则优先，LLM 兜底）

内置指令规则（正则，大小写不敏感）：

| 模式 | 意图 |
|---|---|
| `状态\|status` | `query.status` |
| `重启.*demo-app\|restart` | `manual_ops(restart_demo_app)` |
| `诊断\|diag` | `manual_diag` |
| `修复\|fix` | `manual_fix` |
| `报告 (\d+)\|report` | `query.timeline(incident)` |
| `确认.*PR#?(\d+)\|confirm` | `pr_confirm` |
| `拒绝.*PR#?(\d+)\|reject` | `pr_reject` |
| `帮助\|help` | `help` |

- 未命中 → LLM 意图分类；输出 schema：

```json
{"intent": "ops|diag|fix|query|confirm|reject|help|unknown",
 "params": {"service": "...", "pr_number": 12},
 "confidence": 0.0}
```

- `confidence < 0.6` 或 `unknown` → 反问澄清卡片（“你是想让我重启 demo-app，还是查看状态？”）。

### 4.3 任务派发协议

- Worker 每 2s `POST /tasks/claim {worker_type, worker_id}` → Router 取该类型最早 queued 任务，置 `running`、写 5min 租约。
- Worker 完成后 `POST /tasks/{id}/result {status, result_json}`。
- 租约到期未回传 → 重派一次（`attempts=2`）；再失败 → incident `escalated`。
- 串并行：同 `type` 任务按 id 顺序派发（FIFO 串行）；不同 type 可并行。
- 单飞：派发 `fix` 前检查同 incident 是否已有非终态 fix task / `awaiting_confirm` 的 patch_proposal。

### 4.4 超时与 flapping

- 任务执行硬超时：ops 60s、diag 120s、fix 600s（含 LLM 与沙箱）。
- flapping：`process_down` 分诊时查询「同 subject 近 15min 同 classification 的 incident 数」≥ 2（即将创建第 3 个）→ 不派发 restart，直接 `escalated`，卡片说明“疑似抖动，已停止自动重启”。

## 5. Worker 设计

### 5.1 OpsWorker：白名单执行器 + 复检

`actions.yaml`（示例）：

```yaml
actions:
  restart_demo_app:
    type: exec
    command: ["/usr/bin/sudo", "/usr/bin/systemctl", "restart", "demo-app.service"]
    verify: {type: http, url: "http://127.0.0.1:18000/health", expect: 200}
    verify_attempts: 3
    verify_interval: 10
    max_per_incident: 3
  block_ip:
    type: http
    method: POST
    url: "http://127.0.0.1:9101/admin/limits"
    body: {action: block, ip: "{ip}", duration_min: 10}
  set_rate_limit:
    type: http
    method: POST
    url: "http://127.0.0.1:9101/admin/limits"
    body: {action: rate_limit, qps: 50, duration_min: 5}
```

- 执行器只认 action 名，不做字符串拼接执行；`exec` 型命令数组与 sudoers 授权**逐字精确匹配**，运行前二次校验（三重门的第三重）。
- 未定义动作 / 参数不匹配 → 拒绝 + `whitelist_deny` 审计 + 安全告警卡片（负测试用 `rm -rf` 注入验证）。
- 复检循环：动作后按 `verify` 探测，全部通过才上报 succeeded；否则按 `verify_attempts` 重试，仍失败上报 failed → Router 升级。
- `restart` 配额：每 incident 最多 3 次（`max_per_incident`），超限直接升级。

### 5.2 DiagWorker：根因分析（只读）

输入装配（全部确定性代码完成）：

- incident 摘要 + 告警 `payload_json`（含触发样本）；
- 日志尾部 200 行（经脱敏：token / Authorization / 手机号掩码）；
- `target-app/` 文件树 + 栈帧提到的文件内容（每文件 ≤ 400 行，最多 3 个文件）。

LLM 输出 schema：

```json
{"root_cause": "一句话根因",
 "error_class": "ValueError",
 "suspect_files": [{"path": "target-app/app/orders.py", "line": 42, "reason": "..."}],
 "evidence": ["app.log:... invalid literal for int() with base 10: 'abc'"],
 "fix_suggestion": "...",
 "need_code_fix": true,
 "confidence": 0.9}
```

- 校验：`suspect_files[].path` 必须命中路径白名单（`^target-app/`），否则丢弃该条并降置信度。
- 输出渲染为蓝色根因卡片（根因 / 证据 / 文件:行号 / 置信度）；`need_code_fix=true` → Router 派发 fix task。

### 5.3 FixWorker：修复流水线（八步）

1. **建 worktree**：`git -C /opt/aiops/sandbox/repo fetch`；`git worktree add wt-<incident> -b aiops/fix-<incident> origin/main`。
2. **上下文装配**：diag 结果 + suspect 文件内容 + 触发样本，交 LLM 生成补丁。
3. **补丁格式**（D8，search/replace 块）：

```json
{"changes": [
  {"file": "target-app/app/orders.py",
   "replacements": [{"old_text": "    total = int(amount) * price",
                     "new_text": "    if not isinstance(amount, int):\n        raise HTTPException(400, 'amount must be int')\n    total = amount * price"}]},
  {"file": "target-app/tests/test_orders.py",
   "new_file": "<完整测试文件内容>"}],
 "test_command": "pytest -q"}
```

4. **确定性应用**：路径白名单（仅 `^target-app/`，含其 tests/；根级 tests/ 属 agent 自身，不可改）→ 每段 `old_text` 在文件中**唯一匹配**才替换 → 变更总行数 ≤ 200 → 必须触及测试文件。任一不满足 → task failed，不建 PR。
5. **沙箱测试**：worktree 内 `python -m venv .sbx-venv && pip install --no-index --find-links /opt/aiops/sandbox/wheelhouse -e target-app && .sbx-venv/bin/pytest target-app/tests -q --timeout=60`；整体超时 180s；退出码非 0 → failed。
6. **提交 draft PR**：`git commit` + `git push origin aiops/fix-<incident>` + `gh pr create --draft`（标题 = `[aiops] fix: <root_cause 摘要>`；正文 = incident、根因、变更清单、测试证据）。
7. **飞书确认卡片**（双通道）：

```json
{"msg_type": "interactive", "card": {
  "header": {"title": {"tag": "plain_text", "content": "🔧 修复待确认 · Incident #12"}, "template": "orange"},
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md",
      "content": "**根因**：orders.py:42 未校验 amount 类型，`int('abc')` 抛 ValueError\n**触发样本**：POST /api/orders {\"amount\": \"abc\"}"}},
    {"tag": "div", "text": {"tag": "lark_md",
      "content": "**PR**：[#13](https://github.com/.../pull/13) `+14 −2 (2 files)` · 沙箱测试 `8 passed`"}},
    {"tag": "action", "actions": [
      {"tag": "button", "text": {"content": "✅ 确认合并", "tag": "plain_text"},
       "type": "primary", "value": {"act": "pr_confirm", "patch_id": 13}},
      {"tag": "button", "text": {"content": "❌ 拒绝", "tag": "plain_text"},
       "type": "danger", "value": {"act": "pr_reject", "patch_id": 13}}]},
    {"tag": "note", "elements": [{"tag": "plain_text",
      "content": "按钮不可用时可回复：@agent 确认 PR#13 / @agent 拒绝 PR#13"}]}]}}
```

8. **确认后闭环**：`gh pr merge 13 --squash` → `/opt/aiops/deploy` 内 `git fetch && git reset --hard origin/main` → 白名单动作 `restart_demo_app` → 复检 → **重放触发样本**（`alert.payload_json.sample`）期望 2xx → incident `resolved` + 上线卡片。拒绝路径：`gh pr close` → `escalated`。worktree 用后清理。

## 6. 飞书集成

### 6.1 出站（自定义机器人 webhook）

- 群机器人 webhook + 加签（HMAC-SHA256）；发送器带 10s 超时、失败重试 1 次、仍失败落 `action_log`（`feishu_send_failed`）。
- 卡片族与颜色：告警（红 critical / 橙 warning）、处置结果（绿）、升级转人工（红）、根因报告（蓝）、PR 确认（橙）、时间线（蓝）。

### 6.2 入站（自建应用 + 事件长连接）

- lark-oapi `ws` 长连接接收 `im.message.receive_v1`；仅处理群内 @ 本机器人的消息，去 @ 前缀后走 4.2 意图链。
- 幂等：`event_id` / `message_id` 本地缓存去重（飞书可能重投）。
- 权限清单（阶段 0 申请核对）：机器人能力开启、`im:message`（接收）、`im:message:send_as_bot`（发送）；机器人拉入演示群。
- 卡片按钮回调：优先长连接回调；**不可用则降级为纯指令确认**（卡片文案自带指令提示，见 5.3 第 7 步 note）。

### 6.3 双通道确认语义

- `pr_confirm` / `pr_reject` 统一入口：按钮 value 与指令解析最终落到同一 `ConfirmService`；校验 patch 处于 `awaiting_confirm`，幂等（二次点击提示已处理）。
- demo 不校验点击者身份（Non-goal）；`operator` 记录 open_id 以备审计。

## 7. LLM 集成

| 用途 | 输入 | 输出 | 失败语义 |
|---|---|---|---|
| 意图理解（兜底） | 指令文本 + 指令表 | intent JSON | 置信度低 → 反问澄清 |
| 根因分析 | 脱敏日志 + 代码上下文 | 根因 JSON | 超时 / 解析失败 → incident `escalated` |
| 补丁生成 | diag 结果 + 文件内容 | 补丁 JSON | 生成失败 / 应用失败 → failed，不建 PR |

- 统一客户端：OpenAI 兼容 `base_url + model + api_key`（环境变量注入，可换 GLM / DeepSeek / 本地模型）。
- 参数：temperature 0.1、超时 60s、重试 1 次；输出先过 JSON 解析与 schema 校验（pydantic），字段非法视为失败。
- **注入缓解**：日志与消息是不可信输入，但 LLM 输出只产生「建议」——补丁必须过路径白名单、精确匹配、沙箱三道确定性关卡；任何指令不得让 LLM 直接产出可执行命令。
- 成本控制：只有规则未命中的自由文本、diag、fix 三处调用；单 incident 的补丁生成最多 2 次。

## 8. 安全设计

| 面 | 措施 |
|---|---|
| 命令执行 | 三重门：`actions.yaml` 白名单 + sudoers 逐字精确授权 + 执行器运行时校验 |
| sudoers 片段 | 仅 `/usr/bin/systemctl restart demo-app.service` 与 `/usr/bin/systemctl is-active demo-app.service` 两条，无通配符 |
| 文件写入 | FixWorker 路径白名单仅 `^target-app/`（含其 tests/）；runtime 目录 root:aiops 0750 只读 |
| 网络 | Router / admin API 仅绑 `127.0.0.1`；LLM 出站域名固定为配置值 |
| Git | deploy 克隆用只读 deploy key；gh token 最小 scope（repo: PR 读写）；main 分支保护 |
| 密钥 | `/etc/aiops/secrets.env` 0600；日志脱敏正则（token / secret / Authorization / Bearer） |
| 审计 | agent 与 human 动作全量双落；`whitelist_deny` 安全事件单独告警 |
| 红线 | 不 push main、不自动合并、不改 agent 自身、沙箱不过不建 PR |

sudoers 片段（`/etc/sudoers.d/aiops-sentinel`）：

```
# 仅授权这一条写操作；is-active / status 等只读查询本就无需 sudo，不入白名单
aiops ALL=(root) NOPASSWD: /usr/bin/systemctl restart demo-app.service
```

## 9. 部署与配置

### 9.1 systemd 单元要点

- 四单元均 `Restart=on-failure`、`After=network-online.target`；aiops 三单元 `User=aiops`、`Environment=PYTHONDONTWRITEBYTECODE=1`、`EnvironmentFile=/etc/aiops/secrets.env`。
- demo-app：`User=demoapp`、`StandardOutput=append:/var/log/demo-app/app.log`（目录 demoapp:aiops 0750、文件 0640，aiops 可读日志）、`Environment=BUG_MODE=type_bug`。

### 9.2 配置文件分层

| 文件 | 内容 | 变更方式 |
|---|---|---|
| `config.yaml` | 组件开关（`fix_worker.enabled` 等）、飞书凭据引用、LLM、租约与超时 | 改后 restart 对应单元 |
| `probes.yaml` | 探针类型 / 周期 / 阈值 | 改后 restart monitor |
| `actions.yaml` | 白名单动作与复检参数 | 改后 restart worker |
| `secrets.env` | 三类密钥 | 0600，不入库 |

### 9.3 demo-app 缺陷矩阵（`BUG_MODE`）

| 模式 | 缺陷 | 触发 | 演示用途 |
|---|---|---|---|
| `type_bug`（默认） | `orders.py:42` 对 `amount` 直接 `int()`，字符串输入抛 ValueError → 500 | `POST /api/orders {"amount":"abc"}` | 场景二：诊断 + 修复闭环 |
| `off` | 无缺陷 | — | 回归基线 / 复测 |
| —（内置，不受开关控制） | 进程可被 kill；`/api/search` 可被压测打满 | `chaos_kill.sh` / `chaos_flood.sh` | 场景一 / 三 |

- 演示可重演：场景二合并修复后缺陷即消除；需重演时把 `/opt/aiops/deploy` reset 回基线标签 `v0.1-base` 并重启（`chaos_dirtydata.sh --reset` 封装该操作），仓库在首次演示前打 `v0.1-base` 标签。

### 9.4 wheelhouse 离线缓存

- bootstrap 时 `pip download -r requirements.txt -d /opt/aiops/sandbox/wheelhouse`；沙箱安装 `--no-index --find-links`，演示不依赖 PyPI。

## 10. 演示剧本

### 10.1 场景一：假死自愈（约 60s，无人工）

> 实现备注（2026-11-04 部署实测）：demo-app 配置了 `Restart=on-failure`，
> 纯 SIGKILL 会被 systemd 在 3s 内拉起，探针（15s 周期）大概率错过宕机窗口。
> 故 `chaos_kill.sh` = SIGKILL **+ stop**，把宕机状态留给 Agent 处置——
> 场景一里 Worker 的白名单 restart 动作才有意义。

| t | 动作 | 群内可见 |
|---|---|---|
| t0 | `bash scripts/demo/chaos_kill.sh`（SIGKILL + stop） | — |
| ~t+15s | 探针告警（critical） | 🔴 告警卡片：demo-app 进程退出（PID …） |
| ~t+20s | OpsWorker 白名单重启 + 复检 | — |
| ~t+45s | 复检 3/3 通过 | 🟢 结果卡片：已自动重启并恢复，耗时 Xs，Incident #n resolved |

### 10.2 场景二：代码缺陷修复闭环（约 3–5min，含一次人工确认）

1. `bash scripts/demo/chaos_dirtydata.sh`：10 次非法请求 + 正常流量 → 错误率告警（**风暴只发 1 条卡片，计数 10**）。
2. DiagWorker 根因卡片（蓝）：`orders.py:42`，证据日志 3 行，置信度 0.9。
3. FixWorker：PR 确认卡片（橙）：diff `+14 −2`、沙箱 `8 passed`、[确认合并] [拒绝]。
4. **人工点「确认合并」** → squash 合并 → deploy 拉取 + 重启 + 复检 + 重放样本得 200 → 🟢 上线卡片，Incident resolved。
5. 备选分支：点「拒绝」或 `@agent 拒绝 PR#n` → PR 关闭 + 转人工卡片。

### 10.3 场景三：异常流量自愈（约 90s + 5min 自动解除）

- `bash scripts/demo/chaos_flood.sh`（50 并发持续 60s）→ QPS 告警 → `set_rate_limit(qps=50, 5min)` → 卡片；停止压测后到期自动解除 + resolved 卡片。

### 10.4 人工指令演示

- `@agent 状态` → 四单元 / 应用健康摘要卡片。
- `@agent 报告 12` → Incident #12 时间线（告警 → 诊断 → 动作 → PR → 确认，含操作者与时间戳）。
- `@agent 帮助` → 指令表。

## 11. 测试

### 11.1 单元测试（`tests/unit`，无外部依赖）

- 指纹 / 降噪窗口 / 计数聚合 / 自动恢复。
- 分诊规则表全分类覆盖；意图正则表；LLM 输出 schema 校验（含非法 JSON）。
- 白名单执行器：正常动作、未定义动作、参数越界、`rm -rf` 负测试（应 `whitelist_deny`）。
- 补丁应用器：唯一匹配、多处替换、匹配 0 次 / 2 次拒绝、路径白名单拒绝、行数上限。
- 状态机全部迁移 + 超时升级 + flapping 判定。
- 脱敏函数：token / Authorization / 手机号。

### 11.2 集成测试（`tests/integration`，StubFeishu 捕获卡片 + 临时 SQLite + 假动作）

- 告警 → 分诊 → 派发 → 认领 → 复检 → resolved 全链（Stub 动作）。
- 告警风暴只建 1 个 incident；租约过期重派一次。
- `awaiting_human` 确认 / 拒绝双通道幂等。

### 11.3 e2e 混沌回归（`tests/e2e`，`@slow`，VM 内跑）

- 三场景脚本化断言：DB 终态、捕获卡片序列、demo-app 健康、复测样本返回 2xx、审计行齐全。
- 场景二含拒绝分支；kill ×3 触发 flapping。

### 11.4 彩排清单（阶段 9）

- 三场景计时排练（目标 8–10min 讲解节奏）；LLM 断网降级演练（屏蔽 LLM 出站 → diag 转人工卡片）；录屏兜底。

## 12. 风险

| 风险 | 缓解 |
|---|---|
| LLM 补丁质量不稳定 | search/replace 精确匹配 + 沙箱硬门 + 人工确认；单 incident 生成 ≤ 2 次；彩排校准 prompt |
| 卡片按钮回调不可用 | 双通道确认（指令兜底），卡片文案自带指令提示；阶段 0 即验证 |
| 告警误报 | HTTP 防抖 3 次 + 阈值配置化 + 演示前预校准 |
| 演示依赖外网（飞书 / GitHub / LLM） | 规则类场景（一 / 三）不依赖 LLM；wheelhouse 离线；录屏兜底 |
| Agent 自身故障 | systemd `Restart=on-failure`；演示手册前置 `systemctl status` 检查项 |
| SQLite 并发 | 单写者 + WAL + busy_timeout=5s |
| flapping 演示「砸场」 | flapping 是设计特性，演示手册明确第 3 次 kill 的预期表现 |
| gh token / 机器人凭据过期 | fine-grained PAT；演示前检查清单（阶段 9） |
| VM 时钟漂移影响窗口判定 | bootstrap 安装 chrony；阶段 0 校时 |

## 13. 待确认（少量）

1. LLM 供应商与模型（建议云端 GLM / DeepSeek + 一个备用模型；本地模型作兜底演示项）。
2. 卡片回调长连接在当前 lark-oapi 版本的可用性（阶段 0 实测；降级路径已备）。
3. PR 合并账号：个人 PAT 即可，还是需要 org 下的 bot 账号（影响审计 `operator` 展示）。
4. 是否加 nginx 反代（当前 app 级限流已满足演示；预留 actions.yaml 扩展位）。
5. 演示时长目标（本设计按 8–10 分钟三场景 + 2 分钟指令互动编排）。
