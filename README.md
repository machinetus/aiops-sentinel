# Sentinel AIOps Agent

基于 **Router-Worker** 架构的智能运维（AIOps）Agent 原型：告警降噪 → 自动分诊 → 白名单自愈 → LLM 根因分析 → 代码修复 PR → 人工合并确认 → 自动部署复测。全程审计留痕，可在单台 Ubuntu 虚拟机上完整演示。

> 设计文档：[PRD](docs/prd.md) · [技术设计](docs/design.md) · [实施计划](docs/implement.md) · [演示手册](docs/demo-guide.md)

## 核心能力

| 能力 | 实现 |
|---|---|
| 感知 | 四类探针（进程 / HTTP / 日志 / 流量），防抖 + 指纹降噪（5min 窗口） |
| 调度 | 规则优先分诊 + 事件状态机（open→diagnosing→acting→awaiting_human→resolved/escalated） |
| 自愈 | OpsWorker 白名单执行（三重门）+ 复检自证恢复（3×10s）+ flapping 保护 |
| 诊断 | DiagWorker：脱敏日志 + 栈帧文件 → LLM 结构化根因（文件:行号 + 证据 + 置信度） |
| 修复 | FixWorker：worktree + search/replace 补丁 + 沙箱 pytest + 建 PR（失败带反馈重试 1 次） |
| 确认 | 人工在 GitHub Merge PR（红线：Agent 永不自动合并） |
| 闭环 | 合并轮询 → 白名单部署（git pull + restart）→ 复测重放 → resolved |
| 审计 | SQLite action_log + append-only JSONL 双落；`scripts/export_audit.py` 导出 |

## 目录结构

```
aiops-sentinel/
├── docs/                  prd / design / implement / demo-guide
├── src/
│   ├── common/            config / db(WAL) / fingerprint / log 脱敏 / audit
│   ├── router/            FastAPI：降噪 / 分诊(triage) / 状态机 / PR 轮询 / 派发
│   ├── monitor/           四类探针 + 状态机(去抖/恢复)
│   ├── workers/           ops(白名单执行) / diag(LLM 根因) / fix(补丁→PR) / patcher
│   ├── feishu/            出站卡片(关键词/签名双模式) + 指令解析(intent)
│   └── llm/               OpenAI 兼容客户端 + 诊断/修复 prompt
├── target-app/            demo 业务应用（故意内置 BUG_MODE 缺陷 + admin API）
├── deploy/                bootstrap.sh / sudoers / systemd 四单元
├── config/                config / probes / actions 模板
├── scripts/
│   ├── demo/              chaos_kill / chaos_dirtydata / chaos_flood
│   ├── dev/               smoke_router（骨架冒烟）
│   └── export_audit.py    审计导出（CSV/JSON）
└── tests/unit/            单元测试（pytest）
```

## 快速开始（Ubuntu VM）

```bash
git clone https://github.com/machinetus/aiops-sentinel.git && cd aiops-sentinel
sudo bash deploy/bootstrap.sh          # 用户/目录/venv/wheelhouse/sudoers/systemd 一次到位

# 按 /etc/aiops/secrets.env 模板填入飞书 webhook（与 LLM/gh token）
sudo vi /etc/aiops/secrets.env
sudo systemctl restart aiops-router aiops-monitor aiops-worker

# 查看状态
systemctl status aiops-router aiops-monitor aiops-worker demo-app --no-pager
curl -s http://127.0.0.1:18081/healthz
```

三个演示场景见 [docs/demo-guide.md](docs/demo-guide.md)：假死自愈 / 代码缺陷 LLM 修复 / 异常流量限流。

## 安全模型

- **三重门**：`actions.yaml` 白名单 + sudoers 精确命令授权（无通配符）+ 执行器运行时校验
- **两条副作用通道**：`sudo systemctl restart demo-app` 与 demo-app 的 `127.0.0.1:9101` admin API
- **人工红线**：PR 只由人工 Merge；Agent 不 push main、不自动合并、不改自身代码
- **隔离**：FixWorker 只能改 `target-app/`；runtime 目录对 aiops 只读；沙箱为独立 venv + worktree
- **凭据**：`/etc/aiops/secrets.env`（0600），不入库、日志脱敏

## 环境约定

- 目标：Win11 宿主机 + VMware Ubuntu 22.04+（Python 3.10+），systemd 四单元
- 内网环境：VM 外网需走宿主机代理（`HTTP(S)_PROXY` 已注入 systemd 单元；`NO_PROXY=127.0.0.1`）
- 端口：Router `127.0.0.1:18081`、demo-app `:18000`、admin `127.0.0.1:9101`（8081/8000 与宿主既有服务冲突已规避）
- 飞书：自定义机器人**关键词模式**（关键词 `Sentinel`），卡片标题统一前缀 `[Sentinel]`
- LLM：OpenAI 兼容（当前 DeepSeek `deepseek-flash`），凭据在 secrets.env

## 测试

```bash
python -m pytest tests -q          # 单元测试（pipeline/分诊/状态机/白名单/补丁器/意图…）
python scripts/dev/smoke_router.py # 骨架端到端冒烟（需 Router 运行）
```
