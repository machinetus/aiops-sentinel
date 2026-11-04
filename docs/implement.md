# 实施计划：AIOps 智能运维 Agent（Sentinel）

> 前置阅读：`docs/prd.md`、`docs/design.md`。
> 原则：环境先行、每阶段可验证可回滚、安全测试随阶段落地不后补、演示剧本随功能即时固化。
> 环境：Win11 宿主机 + VMware Ubuntu 22.04+；VM 内路径以 `/opt/aiops` 为根。

## 阶段 0：环境、凭据与通道基线

- [ ] VM 初始化：apt 更新、python3.10+ / venv、git、gh CLI、sqlite3、chrony 校时。
- [ ] GitHub：创建 monorepo `aiops-sentinel`；main 分支保护（禁 push、仅 PR squash）；创建 fine-grained PAT（repo: PR 读写）；VM 内 `gh auth login`。
- [ ] 三克隆布局：`/opt/aiops/runtime`（root:aiops 0750）、`/opt/aiops/deploy`（deploy key 只读）、`/opt/aiops/sandbox/repo`。
- [ ] 飞书：建演示群 + 自定义机器人（webhook + 加签）；创建企业自建应用（机器人能力、`im:message` / `im:message:send_as_bot`、事件长连接订阅 `im.message.receive_v1`）；机器人入群。
- [ ] LLM：确定供应商与模型，申请 key（OpenAI 兼容）。
- [ ] `/etc/aiops/secrets.env`（0600）：飞书 webhook/secret/app_id/app_secret、GH_TOKEN、LLM_KEY。
- [ ] 通道自检脚本 `scripts/demo/selftest_*.sh`。

**验证**：
```bash
# 出站：群内可见 "[Sentinel] 通道自检"
curl -s -X POST "$FEISHU_WEBHOOK" -H 'Content-Type: application/json' \
  -d '{"msg_type":"text","content":{"text":"[Sentinel] 通道自检"}}'
# 入站：群内 @bot 说 ping，终端打印事件
python -m src.feishu.selftest_listen
# 分支保护生效（应被拒绝）
cd /opt/aiops/runtime && git push --dry-run origin main
gh auth status
```

**评审门 G0**：通道清单（webhook / 长连接 / 卡片回调可用性结论）、凭据落位、分支保护、目录与权限核对。

## 阶段 1：骨架、公共库与部署单元

- [ ] 仓库结构按 design §1.5 落位；`pyproject.toml` + `requirements.txt`。
- [ ] `src/common`：config 加载（YAML + 环境变量覆盖）、logging + 脱敏、audit 双落（SQLite + JSONL）、db（WAL、busy_timeout）、fingerprint。
- [ ] SQLite schema（design §2 五表）+ 建表脚本 + 简单迁移约定（`schema.sql` 版本号）。
- [ ] Router API 骨架：`/healthz`、`/api/v1/alerts`、`/tasks/claim`、`/tasks/{id}/result`（仅 127.0.0.1）。
- [ ] 四个 systemd 单元 + `deploy/sudoers/aiops-sentinel` + `deploy/bootstrap.sh`（用户、目录、venv、wheelhouse 预热、单元安装）。
- [ ] config / probes / actions 三个 `*.yaml.example`。

**验证**：
```bash
sudo bash deploy/bootstrap.sh
systemctl status aiops-router aiops-monitor aiops-worker demo-app --no-pager
curl -s http://127.0.0.1:18081/healthz
sqlite3 /var/lib/aiops/aiops.db '.tables'   # 五表齐全
```

**评审门 G1**：schema、API 面、systemd / sudoers 最小化、目录权限（runtime 只读）确认。

## 阶段 2：demo-app + 感知面 + 告警出飞书

- [ ] `target-app`：FastAPI `/health`、`/metrics`（QPS 滑窗）、`/api/orders`（`BUG_MODE=type_bug` 缺陷）、`/api/search`；`127.0.0.1:9101` admin API（block / unblock / rate_limit + 到期解除）；日志 JSON 行输出。
- [ ] `src/monitor` 四类探针 + 防抖 + 指纹降噪 + 恢复关闭（design §3）。
- [ ] `src/feishu/sender`：加签 webhook、卡片模板（告警 / 结果 / 升级 / 根因 / PR / 时间线）。
- [ ] 本阶段 Router 仅做降噪落库 + 告警卡片直发（尚无自动处置）。
- [ ] `scripts/demo/chaos_kill.sh` / `chaos_dirtydata.sh` / `chaos_flood.sh`。

**验证**：
```bash
bash scripts/demo/chaos_kill.sh && sleep 20   # 群内收到红色告警卡片
bash scripts/demo/chaos_dirtydata.sh          # 风暴 10 次只发 1 条卡片且计数=10
sqlite3 /var/lib/aiops/aiops.db 'select fingerprint,count,status from alert;'
```

**评审门 G2**：探针阈值与防抖、降噪语义、卡片文案、chaos 脚本可复现性确认。

## 阶段 3：Router 分诊 + OpsWorker 自愈闭环（场景一完成）

- [ ] 分诊规则表 + incident / task 落库 + 状态机（design §2.6 / §4.1）。
- [ ] 任务协议：claim / result / 租约重派一次 / 同类型串行 / 超时升级。
- [ ] OpsWorker：白名单执行器（三重门）+ 复检循环 + restart 配额 + flapping 保护。
- [ ] 结果 / 升级卡片；`whitelist_deny` 安全告警。
- [ ] 单测：规则表、状态机、执行器负测试（`rm -rf` 注入）、flapping。

**验证**：
```bash
bash scripts/demo/chaos_kill.sh    # 群内：告警→(自动重启)→绿色结果卡片，全程无人工
for i in 1 2 3; do bash scripts/demo/chaos_kill.sh; sleep 25; done
# 第 3 次应触发 flapping：不再自动重启，收到转人工卡片
pytest tests/unit -q
```

**评审门 G3**：白名单三重门、复检语义、flapping、审计行齐全确认。

## 阶段 4：DiagWorker 根因分析

- [ ] `src/llm` 统一客户端（OpenAI 兼容、temperature 0.1、超时重试、pydantic schema 校验）。
- [ ] 诊断上下文装配：脱敏日志尾部 200 行 + `target-app` 文件树 + suspect 文件内容（只读）。
- [ ] 根因 prompt + 输出 schema（design §5.2）；路径白名单过滤 `suspect_files`。
- [ ] 根因卡片渲染；`need_code_fix` → 派发 fix task（本阶段先只出卡片，不接 fix）。
- [ ] 单测：schema 非法输入、脱敏、上下文装配；LLM 失败 → escalated。

**验证**：
```bash
bash scripts/demo/chaos_dirtydata.sh
# 群内：降噪告警 → 蓝色根因卡片（含 orders.py:行号 + 证据 + 置信度）
pytest tests/unit -q -k diag
```

**评审门 G4**：根因卡片可解释性（文件:行号 + 证据）、LLM 失败语义确认。

## 阶段 5：FixWorker 修复闭环（场景二完成）

- [ ] worktree 管理 + sandbox/repo 克隆 + wheelhouse 离线安装。
- [ ] 补丁生成与应用：search/replace 块 + 新文件、唯一匹配、路径白名单、行数上限、必须含测试（design §5.3）。
- [ ] 沙箱：venv + `pytest --timeout=60`、整体 180s、退出码门禁。
- [ ] draft PR：commit / push / `gh pr create --draft`；PR 卡片（双通道确认文案）。
- [ ] ConfirmService：按钮 + 指令统一入口、幂等、`awaiting_confirm` 校验。
- [ ] 确认后闭环：squash merge → deploy 拉取 + 白名单重启 + 复检 + 重放样本 → resolved 卡片；拒绝路径关 PR + escalated。
- [ ] 单飞校验；单 incident 补丁生成 ≤ 2 次。
- [ ] 单测：补丁应用器全负路径、沙箱门禁、幂等；集成：StubFeishu 确认 / 拒绝双分支。

**验证**：
```bash
bash scripts/demo/chaos_dirtydata.sh
# 群内：告警→根因→PR 卡片；点[确认合并]→合并+部署+复测 200→resolved 卡片
# 拒绝分支：cd /opt/aiops/deploy && sudo git reset --hard v0.1-base && sudo systemctl restart demo-app
# 然后重跑 chaos_dirtydata.sh，这次点[拒绝]（或 @agent 拒绝 PR#n）：PR 关闭、事件 escalated
pytest tests/unit tests/integration -q
```

**评审门 G5**：沙箱硬门、人工确认红线（未确认零合并）、部署 + 复测闭环、单飞确认。

## 阶段 6：流量自愈 + 人工指令（场景三 / 四完成）

- [ ] `set_rate_limit` / `block_ip` 白名单动作接 admin API + 到期自动解除。
- [ ] 入站长连接接收 @ 消息 + 幂等去重 + 意图链（规则表优先、LLM 兜底、低置信度反问）。
- [ ] 内置指令：状态 / 重启 / 诊断 / 修复 / 报告 / 确认 PR / 拒绝 PR / 帮助。
- [ ] 时间线报告卡片（按 incident 聚合 alert / task / action_log / patch_proposal）。
- [ ] 单测：意图正则表、LLM 意图 schema、报告聚合。

**验证**：
```bash
bash scripts/demo/chaos_flood.sh        # 限流卡片；停止后到期自动解除 + resolved
# 群内依次发送：@agent 状态 / @agent 报告 <n> / @agent 帮助 / @agent 确认 PR#<n>
pytest tests/unit -k intent
```

**评审门 G6**：指令矩阵、限流到期解除、时间线完整性确认。

## 阶段 7：审计、报告与安全收口

- [ ] `scripts/export_audit.py`（JSON / CSV 导出，脱敏复核）。
- [ ] 安全清单逐项核验：sudoers 仅两条无通配、runtime 只读、内部 API 仅 127.0.0.1、secrets 不入库（`.gitignore` + 历史扫描）、日志脱敏抽查。
- [ ] 负测试归档：越权命令、路径逃逸补丁（`../`）、匹配歧义补丁、二次点击幂等。
- [ ] action_log 覆盖率核对：三场景全链路动作（agent + human）逐行可对账。

**验证**：
```bash
sudo grep -R . /etc/sudoers.d/aiops-sentinel        # 仅两条精确命令
python scripts/export_audit.py --last 1h --out /tmp/audit.csv
pytest tests/unit -k "whitelist or patcher or redact"
```

**评审门 G7**：安全清单签字、审计对账通过。

## 阶段 8：演示工程化

- [ ] `docs/demo-guide.md`：三场景时序表、台词、预期卡片截图、故障预判（flapping 是特性、LLM 断网降级）。
- [ ] chaos 脚本加 `--reset`（deploy 克隆 reset 回 `v0.1-base` 标签 + 重启 demo-app 还原被修复的缺陷、清限流状态），保证连演；首次演示前仓库打 `v0.1-base` 标签。
- [ ] `README.md`：架构图、快速开始（bootstrap → 三场景）、安全模型。
- [ ] 录屏：三场景完整一遍（断网兜底素材）。

**验证**：
```bash
bash scripts/demo/chaos_kill.sh --reset && bash scripts/demo/chaos_dirtydata.sh --reset \
  && bash scripts/demo/chaos_flood.sh --reset   # 连续 3 轮不重启 aiops 服务
```

**评审门 G8**：演示手册可用性（他人照做可复现）、连演稳定性确认。

## 阶段 9：全量验证与彩排

- [ ] `pytest tests -q`（含 `@slow` e2e）全绿；`python -m compileall src target-app`。
- [ ] 计时彩排：三场景 8–10min + 指令互动 2min；记录实际耗时进演示手册。
- [ ] 降级彩排：屏蔽 LLM 出站 → 场景二在 diag 处转人工卡片（预期行为）；恢复后复跑。
- [ ] 演示前检查清单：`systemctl status` 四单元、`gh auth status`、飞书双通道、磁盘 / 时钟。
- [ ] 收尾：文档定稿入库（docs/ 三份 + demo-guide + README）、tag `v0.1-demo`。

**验证**：
```bash
pytest tests -q
systemctl is-active aiops-router aiops-monitor aiops-worker demo-app
date && timedatectl status   # 时钟正常
```

**评审门 G9**：彩排通过、检查清单执行、录屏归档。

## 回滚点

| 面 | 回滚动作 |
|---|---|
| 全局 | `sudo systemctl stop aiops-monitor aiops-worker`（保留 router 可看历史）；demo-app 独立存活不受影响 |
| 单场景 | `actions.yaml` 删除对应动作 → 该类告警只通知不处置 |
| 修复面 | `config.yaml: fix_worker.enabled=false` → diag 后直接转人工，不出 PR |
| LLM | `config.yaml: llm.enabled=false` → 规则与指令关键字仍可用，diag/fix 转人工 |
| 部署 | `/opt/aiops/deploy` 内 `git reset --hard <tag>` + 重启 demo-app |
| 飞书 | 停自建应用长连接 → 只出卡片不能 @；webhook 出站保留 |
| 数据 | SQLite 单文件可备份可清空重建（`cp aiops.db aiops.db.bak`） |

## 实施顺序约束

- 阶段 0→1→2→3 强顺序（通道 / 骨架 / 感知 / 自愈逐层依赖）。
- 阶段 4→5 强顺序（fix 依赖 diag 输出）；阶段 6 可与 4/5 并行（不同代码面）。
- 阶段 7 依赖 5（审计对账需全链路）；阶段 8 依赖 3/5/6 全过门；阶段 9 最后。
- 每阶段过评审门再进入下一阶段；发现 PRD/design 矛盾回文档修订，不在代码里临时改语义。
- 安全测试（白名单负测试、越权、脱敏、幂等）随对应阶段落地，不后补。
