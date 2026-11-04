# AIOps 智能运维 Agent（Sentinel）：感知 → 调度 → 自愈 → 人工确认闭环

Req: 2026-11-04-aiops-sentinel-mvp

## Goal

在 Win11 宿主机的 VMware Ubuntu 虚拟机内，构建一套 Router-Worker 架构的 AIOps 智能运维 Agent 原型（代号 **Sentinel**）：持续巡检 demo 业务应用的运行状态；异常告警经指纹降噪后推送飞书群；@agent 支持自然语言运维指令；Agent 按白名单 SOP 自动执行轻量自愈（重启 / 限流 / 封禁）并**自证恢复**；定位到代码缺陷时自动生成补丁、沙箱测试、提交 PR，经飞书群**人工一键确认**后合并、部署、复测、回传。全程审计留痕，三个故障场景可一键复现演示。

## 背景与现状（2026-11-04 环境核对）

- 运行环境：Win11 宿主机 + VMware Ubuntu 22.04+；Agent 与被监控 demo 应用同机部署，systemd 托管，四个单元相互独立、可单独重启。
- 工程托管：GitHub monorepo（agent 源码 + demo 应用 + 部署脚本 + 混沌脚本）；main 分支保护，仅 PR（squash）可合。
- 消息面：飞书群已具备自定义机器人 webhook（出站告警）；企业自建应用 + 事件长连接可接收 @ 消息（入站指令）；卡片按钮回调可用性在阶段 0 验证，不可用则以指令确认兜底。
- 可复用能力：psutil（进程探测）、FastAPI / uvicorn（demo 应用与内部 API）、lark-oapi（飞书官方 SDK）、gh CLI（PR 流）、OpenAI 兼容 LLM API（GLM / DeepSeek 等可插拔）。
- 缺口：无探针与告警治理、无调度与事件状态机、无白名单执行器、无诊断与修复流水线、无审计。

## 范围（四个面 + 一个演示面）

- **F0 环境与部署基线**：一键 bootstrap、systemd 单元、密钥与配置分离、wheelhouse 离线缓存。
- **F1 感知面**：进程 / HTTP / 日志 / 流量四类探针 + Webhook 告警入口 + 指纹降噪聚合。
- **F2 调度面（Router）**：规则优先、LLM 兜底的分诊与意图理解；任务派发协议；事件状态机。
- **F3 执行面（Worker）**：OpsWorker 白名单自愈 + 复检；DiagWorker LLM 根因分析（只读）；FixWorker 补丁 → 沙箱 → PR → 人工确认 → 部署 → 复测。
- **F4 演示面**：三个一键混沌场景 + @ 指令交互 + 事故时间线报告。

## Requirements

### R0 环境与部署基线

- R0.1 monorepo 单仓库；main 分支保护（禁直接 push、仅 PR squash 合并）；`runtime` / `deploy` / `sandbox` 三个独立克隆各司其职。
- R0.2 `bootstrap.sh` 一键初始化：aiops / demoapp 用户、目录布局（`/opt/aiops`）、venv、四个 systemd 单元、sudoers 白名单片段、secrets 模板、wheelhouse 预热。
- R0.3 密钥（飞书 app secret、GitHub token、LLM key）仅存 `/etc/aiops/secrets.env`（0600），不入库、不入日志。
- R0.4 四个 systemd 单元：`aiops-router` / `aiops-monitor` / `aiops-worker` / `demo-app`；`Restart=on-failure`，崩溃自动拉起。
- R0.5 行为全部配置驱动（`config.yaml` / `probes.yaml` / `actions.yaml`），改阈值与开关不动代码。

### R1 感知面

- R1.1 四类探针，周期与阈值配置化（默认 15s）：进程（systemd active + PID）、HTTP（`/health` 连续 3 次失败才告警，防抖）、日志（错误行速率阈值 + 错误类归类）、流量（`/metrics` QPS 阈值）。
- R1.2 Webhook 入口：Router 暴露仅绑定 `127.0.0.1` 的 API，业务程序可主动 POST 告警（demo 用 curl 模拟第二触发源）。
- R1.3 告警降噪：指纹 = `source + probe + subject + error_class` 的 SHA-256；5 分钟窗口内同指纹只发一条卡片、计数递增；探针恢复后自动关闭告警。
- R1.4 分级：critical（进程宕 / HTTP 不通）、warning（错误率 / 限流触发）、info（流量峰值）。

### R2 调度面（Router）

- R2.1 规则引擎优先：特征 → 分类 → playbook；仅人工自由文本在规则不命中时走 LLM 意图理解。
- R2.2 分类：`process_down` / `app_error` / `traffic_anomaly` / `manual_ops` / `manual_diag` / `manual_fix` / `query`。
- R2.3 事件状态机：`open → diagnosing → acting → verifying → awaiting_human → resolved / escalated`；任一环节失败或超时即升级转人工，不静默重试放大故障。
- R2.4 任务派发：Worker 轮询认领（2s）+ 结果回传；同类型任务串行、跨类型并行；同 incident 在途 fix 任务仅一个（**单飞**）。
- R2.5 人工指令：@agent + 自然语言；内置指令：状态 / 重启 / 诊断 / 修复 / 报告 / 确认 PR / 拒绝 PR / 帮助；LLM 置信度 < 0.6 时反问澄清。
- R2.6 flapping 保护：15 分钟内同服务第 3 次 `process_down` → 停止自动重启、直接转人工。

### R3 执行面（Worker）

- R3.1 OpsWorker：仅执行 `actions.yaml` 白名单动作；动作后**自动复检**（最多 3 次 × 10s），复检通过才允许关事件；失败升级。restart 动作每 incident 最多 3 次。
- R3.2 白名单动作集：restart demo-app（sudoers 精确授权）、block / unblock IP、限流开关与配额（demo-app `127.0.0.1` admin API）、限流到期自动解除。
- R3.3 DiagWorker：聚合日志片段（脱敏）+ 仓库代码上下文，LLM 输出结构化根因（`root_cause` / `suspect_files` / `evidence` / `confidence` / `need_code_fix`）；只读不写。
- R3.4 FixWorker 流水线：建分支 → LLM 生成补丁（search/replace 块，路径白名单仅 `target-app/`（含其 tests/），必须包含测试）→ 精确匹配应用校验 → 沙箱 venv + pytest（超时 180s）→ 通过才建 draft PR → 飞书确认卡片 → 人工确认 → squash 合并 → deploy 拉取 + 重启 → 复测（重放触发请求）→ 关事件。
- R3.5 红线：不 push main、不自动合并、不修改 agent 自身代码；沙箱或测试失败不建 PR；LLM 网络 / 解析失败转人工。

### R4 飞书集成

- R4.1 出站卡片族：告警（红 / 橙）、处置结果（绿）、升级转人工（红）、根因报告（蓝）、PR 确认（diff 统计 + 测试结果 + 确认 / 拒绝按钮）、时间线报告。
- R4.2 入站：企业自建应用长连接接收 @ 消息；确认**双通道** = 卡片按钮 + 自然语言指令（`@agent 确认 PR#n` / `@agent 拒绝 PR#n`），按钮通道不可用时演示不中断。
- R4.3 卡片与消息携带 incident 编号与时间戳；事件按 message_id / event_id 幂等去重。

### R5 审计与安全

- R5.1 所有动作（含人工确认）写 `action_log`：操作者（agent / human）、动作、命令、退出码、输出摘要（截断脱敏）、时间；SQLite + append-only JSONL 双落。
- R5.2 白名单**三重门**：`actions.yaml` 定义 + sudoers 精确匹配 + 执行器运行时二次校验；越权命令拒绝并产生安全告警。
- R5.3 LLM 只产出建议（意图 / 根因 / 补丁），一律不直接执行；执行与校验全部走确定性代码。
- R5.4 沙箱隔离：独立 venv + git worktree、pytest 超时、测试不依赖网络（wheelhouse 预热）、低权限 aiops 用户运行。
- R5.5 内部 API 全部只绑 `127.0.0.1`；agent runtime 目录对 aiops 只读（防自我修改）。

### R6 演示面

- R6.1 场景一 假死自愈：`chaos_kill.sh` kill -9 → 15s 内告警 → 自动重启 → 复检 → 结果卡片；全程无人工。
- R6.2 场景二 代码缺陷闭环：`chaos_dirtydata.sh` 脏数据触发 500 → 降噪告警 → 根因卡片（文件:行号 + 证据）→ PR 卡片 → 人工点确认 → 合并部署 → 复测 200 → 关事件。
- R6.3 场景三 异常流量：`chaos_flood.sh` 压测 → 自动限流 + 卡片 → 停止后自动解除 + 关事件。
- R6.4 人工指令：@agent 状态 / 报告 / 帮助；@agent 确认 PR#n / 拒绝 PR#n。
- R6.5 演示可复现：三个混沌脚本 + 演示手册（时序 + 台词 + 预期卡片文案）+ 录屏兜底。

## Non-goals

- 多机 / 多应用 / 分布式编排（单 VM 单应用原型）。
- 修改 Agent 自身代码（self-modification 明确禁止，路径白名单之外）。
- PR 自动合并（人工确认是唯一合并入口，硬红线）。
- 真实 WAF / DDoS 清洗（仅 app 级限流与 IP 封禁演示）。
- 基础设施级故障自愈（宿主机 / 断网 / 断电）。
- 数据库脏数据清洗（只拦截新增，不回溯修复存量）。
- 多群 / 多租户 / 值班排班与操作者身份校验（demo 群内任何人可点确认；生产需校验 open_id）。
- 审批流 / SLA 管理 / 值班升级策略。

## Acceptance Criteria

- [ ] `bootstrap.sh` 一键拉起；`systemctl status` 四单元全绿。
- [ ] kill -9 demo-app → 15s 内飞书告警卡片，60s 内自动重启 + 复检通过 + 绿色结果卡片，全程无人工。
- [ ] 连续 3 次 kill → 第 3 次触发 flapping 保护，不再自动重启，转人工卡片。
- [ ] 脏数据请求 ×10（风暴）→ 群内仅 1 条告警卡片（计数 10）→ 根因卡片含 `orders.py:行号` 与日志证据 → PR 卡片含 diff 统计与测试结果。
- [ ] 点「确认合并」→ PR squash 合并 → demo-app 自动部署重启 → 复测请求 200 → 事件 resolved 卡片。
- [ ] 点「拒绝」（或 @agent 拒绝 PR#n）→ PR 关闭 → 事件 escalated；未确认前无任何合并发生。
- [ ] 压测触发限流卡片；停止压测后限流自动解除并关事件。
- [ ] @agent 状态 / 报告 n → 状态摘要 / 事故时间线卡片（告警-动作-分析-PR 全链路）。
- [ ] 伪造越权命令（`rm -rf`）被白名单拒绝并产生安全告警事件。
- [ ] action_log 覆盖 agent 与人工全部动作，`scripts/export_audit.py` 可导出。
- [ ] 三个场景连续演示 3 次不重启任何 aiops 服务；pytest 全绿；README + 演示手册 + 三份技术文档入库。

## 约束与安全边界

- 人工确认是 PR 合并唯一入口；Agent 永远不能自批。
- 副作用仅两条通道：sudoers 精确命令 + demo-app `127.0.0.1` admin API；其余一律只读。
- 白名单三重门 + 低权限运行 + runtime 只读，三层防线。
- LLM 输出全部结构化 JSON；解析失败 / 超时 fail-safe 转人工，不静默重试。
- 密钥不入库不入日志；日志与审计输出脱敏（token / secret / Authorization 掩码）。
- 沙箱测试必须通过且补丁必须包含回归测试，否则不建 PR。

## 依赖与关联

- 环境：Win11 + VMware Ubuntu 22.04+、Python 3.10+、systemd、git / gh CLI。
- 三方：飞书开放平台（自定义机器人 webhook + 自建应用长连接 + 消息卡片）、GitHub（分支保护 + PR）、OpenAI 兼容 LLM API（可插拔）。
- 库：psutil、httpx、FastAPI / uvicorn、lark-oapi、PyYAML、pytest / pytest-timeout。
- 文档：本 PRD + `design.md` + `implement.md`（同库 `docs/`）。

## 已拍板（会话结论）

- Router-Worker 架构，四个 systemd 单元；SQLite 单写者（Router），不引入外部消息队列。
- 告警降噪（指纹去重 + 计数 + 自动恢复）为一等公民，不做裸告警轰炸。
- 规则优先、LLM 兜底；LLM 只建议不执行。
- 白名单执行器 + localhost admin API 是仅有的两个副作用通道。
- 复检（post-verify）内建于自愈动作：必须自证恢复才算 resolved。
- 修复 = 补丁 + 沙箱 + draft PR + 飞书人工确认（双通道）+ 部署 + 复测，缺一不可。
- FixWorker 路径白名单仅 `target-app/`（含其 tests/）；agent 自身代码与仓库根级 tests/ 均不可改。
- 单飞：同 incident 同时最多一个在途 PR。
- demo 应用故意内置可开关缺陷（`BUG_MODE`），保证演示可复现、回归可自动化。
- wheelhouse 预热保证沙箱安装离线可用，演示不依赖 PyPI。
