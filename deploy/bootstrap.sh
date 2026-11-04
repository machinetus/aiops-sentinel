#!/usr/bin/env bash
# ============================================================================
# Sentinel AIOps 一键初始化脚本（implement.md 阶段 0/1；design §1.4 / §8 / §9）
#
# 用法（在 Ubuntu VM 内、仓库任意克隆目录下执行）：
#   sudo bash deploy/bootstrap.sh
#
# 可选环境变量：
#   REPO_URL=git@github.com:you/aiops-sentinel.git
#     缺省用「本脚本所在仓库」做本地克隆源（git clone 只带已提交内容，
#     有未提交改动请先 commit，或用 REPO_URL 指向远端仓库）。
#
# 幂等：可重复执行；已存在的用户/目录/配置不会被覆盖或破坏。
# 目标布局（design §1.4）：
#   /opt/aiops/runtime        agent 运行克隆（root:aiops 0750，aiops 只读，防自我修改）
#   /opt/aiops/deploy         demo-app 部署克隆（aiops 可写，FixWorker 合并后拉取）
#   /opt/aiops/sandbox/repo   FixWorker 工作克隆 + wheelhouse 离线依赖
#   /opt/aiops/venv           agent 运行环境        /opt/aiops/venv-demo  demo-app 环境
#   /var/lib/aiops            SQLite 数据库          /var/log/aiops        审计 JSONL
#   /var/log/demo-app         demo-app 日志（aiops 组可读，供日志探针采集）
#   /etc/aiops                config/probes/actions.yaml + secrets.env(0600)
# ============================================================================
set -euo pipefail

# ---------- 0. 前置检查 ----------
if [[ $EUID -ne 0 ]]; then
  echo "[bootstrap] 错误：请用 sudo / root 运行（需要创建用户与写 /etc）" >&2
  exit 1
fi
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_URL="${REPO_URL:-$SRC_DIR}"
echo "[bootstrap] 源仓库：$REPO_URL"

# 依赖自检：缺什么装什么（阶段 0 清单；gh/chrony 等后续阶段再补）
MISSING=()
for cmd in git python3 curl sqlite3; do
  command -v "$cmd" >/dev/null 2>&1 || MISSING+=("$cmd")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "[bootstrap] 安装缺失依赖：${MISSING[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git python3 python3-venv python3-pip curl sqlite3
fi

# ---------- 1. 系统用户（低权限运行，design §8） ----------
# aiops  ：运行 router/monitor/worker 三大组件
# demoapp：运行被监控业务应用 demo-app；追加进 aiops 组，
#          使 /var/log/demo-app 以 demoapp:aiops 0750 授权 aiops 读日志
id -u aiops   >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin aiops
id -u demoapp >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin demoapp
id -nG demoapp | grep -qw aiops || usermod -aG aiops demoapp

# ---------- 2. 目录布局（design §1.4） ----------
install -d -o root   -g aiops -m 0750 /opt/aiops
install -d -o root   -g aiops -m 0750 /opt/aiops/runtime
install -d -o aiops  -g aiops -m 0750 /opt/aiops/deploy
install -d -o aiops  -g aiops -m 0750 /opt/aiops/sandbox /opt/aiops/sandbox/repo /opt/aiops/sandbox/wheelhouse
install -d -o aiops  -g aiops -m 0750 /var/lib/aiops /var/log/aiops
install -d -o demoapp -g aiops -m 0750 /var/log/demo-app
# 注意：/etc/aiops 必须组可读（root:aiops 0750）——aiops 服务用户需要 traverse 目录
# 读取 config/probes/actions.yaml；secrets.env 保持 0600 由 systemd 以 root 注入环境。
# （2026-11-04 真机踩坑：root:root 0750 导致服务一直读不到配置、静默用默认值）
install -d -o root -g aiops -m 0750 /etc/aiops

# ---------- 3. 三克隆分工（design D12：运行 / 部署 / 改码物理隔离） ----------
clone_to() {
  local dst="$1"
  if [[ -d "$dst/.git" ]]; then
    echo "[bootstrap] 已存在克隆，跳过：$dst"
  else
    # safe.directory：本脚本常以 root 运行，而源克隆可能属普通用户，
    # 不加会报 "dubious ownership" 拒绝克隆
    git -c safe.directory='*' clone "$REPO_URL" "$dst"
  fi
}
clone_to /opt/aiops/runtime
clone_to /opt/aiops/deploy
clone_to /opt/aiops/sandbox/repo

# runtime 锁为 root:aiops 只读（aiops 进程能读能执行、不能改 → 禁自我修改）
chown -R root:aiops /opt/aiops/runtime
chmod -R u=rwX,g=rX,o= /opt/aiops/runtime
# deploy / sandbox 交给 aiops（deploy 拉取、FixWorker 建 worktree 都以 aiops 身份）
chown -R aiops:aiops /opt/aiops/deploy /opt/aiops/sandbox

# ---------- 4. venv 与依赖（design §9.4：wheelhouse 离线缓存） ----------
if [[ ! -x /opt/aiops/venv/bin/python ]]; then
  python3 -m venv /opt/aiops/venv
fi
/opt/aiops/venv/bin/pip install --upgrade pip -q
/opt/aiops/venv/bin/pip install -r "$SRC_DIR/requirements.txt" -q

# demo-app 独立解释器（阶段 2 会在此 venv 内 pip install -e target-app）
if [[ ! -x /opt/aiops/venv-demo/bin/python ]]; then
  python3 -m venv /opt/aiops/venv-demo
fi
chown -R aiops:aiops /opt/aiops/venv /opt/aiops/venv-demo

# wheelhouse：把依赖提前下载到本地，沙箱安装用 --no-index 离线完成（演示不依赖 PyPI）
if pip download -r "$SRC_DIR/requirements.txt" -d /opt/aiops/sandbox/wheelhouse -q 2>/dev/null; then
  echo "[bootstrap] wheelhouse 预热完成"
else
  echo "[bootstrap] 警告：wheelhouse 下载失败（无外网？），阶段 5 沙箱需联网或手动补"
fi
# 沙箱 editable 安装（PEP517）需要构建件，一并离线缓存
pip download setuptools wheel -d /opt/aiops/sandbox/wheelhouse -q 2>/dev/null \
  && echo "[bootstrap] wheelhouse 构建件就绪" \
  || echo "[bootstrap] 警告：setuptools/wheel 下载失败，阶段 5 沙箱可能需要手动补"
chown -R aiops:aiops /opt/aiops/sandbox

# ---------- 5. 配置与密钥（design §9.2；密钥 0600 不入库） ----------
for f in config.yaml probes.yaml actions.yaml; do
  if [[ ! -f "/etc/aiops/$f" ]]; then
    install -o root -g aiops -m 0640 "$SRC_DIR/config/$f.example" "/etc/aiops/$f"
    echo "[bootstrap] 已生成 /etc/aiops/$f"
  fi
  # 幂等修正历史权限（root:root 0644 → root:aiops 0640，保证 aiops 可读）
  chown root:aiops "/etc/aiops/$f" 2>/dev/null || true
  chmod 0640 "/etc/aiops/$f" 2>/dev/null || true
done
if [[ ! -f /etc/aiops/secrets.env ]]; then
  cat > /etc/aiops/secrets.env <<'EOF'
# ============================================================================
# Sentinel 密钥文件（root:root 0600；systemd 以 root 读取注入进程环境）
# 铁律（PRD 约束与安全边界）：不入库、不打日志；填完真实值后 restart 对应服务
# ============================================================================
FEISHU_WEBHOOK_URL=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
GH_TOKEN=
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
EOF
  chmod 0600 /etc/aiops/secrets.env
  echo "[bootstrap] 已生成 /etc/aiops/secrets.env（请 sudo vi 填入真实密钥）"
fi

# ---------- 6. sudoers 白名单（design §8；务必先校验再安装！） ----------
# 语法错误会让全系统 sudo 不可用，所以必须 visudo -cf 校验通过才落盘。
SUDOERS_SRC="$SRC_DIR/deploy/sudoers/aiops-sentinel"
if visudo -cf "$SUDOERS_SRC" >/dev/null 2>&1; then
  install -o root -g root -m 0440 "$SUDOERS_SRC" /etc/sudoers.d/aiops-sentinel
  echo "[bootstrap] sudoers 白名单已安装（仅授权 restart demo-app 一条写操作）"
else
  echo "[bootstrap] 错误：sudoers 语法校验失败，已跳过安装，请人工检查 $SUDOERS_SRC" >&2
fi

# ---------- 7. systemd 单元（design §9.1） ----------
install -m 0644 "$SRC_DIR"/deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
# 阶段 0/1 只启动 Router（数据模型 + API 骨架已可用）；
# monitor（阶段 2）/ worker（阶段 3）/ demo-app（阶段 2）代码落地后再逐个启用：
systemctl enable --now aiops-router
# systemctl enable --now aiops-monitor   # 阶段 2 完成后取消注释执行
# systemctl enable --now demo-app        # 阶段 2 完成后取消注释执行
# systemctl enable --now aiops-worker    # 阶段 3 完成后取消注释执行

# ---------- 8. 完成摘要 ----------
echo
echo "[bootstrap] 完成。验证步骤："
echo "  1) systemctl status aiops-router --no-pager    # 应 active (running)"
echo "  2) curl -s http://127.0.0.1:8081/healthz       # 应返回 JSON"
echo "  3) sqlite3 /var/lib/aiops/aiops.db '.tables'   # 应列出五张表"
echo "  4) sudo visudo -cf /etc/sudoers.d/aiops-sentinel  # 语法应为 parsed OK"
