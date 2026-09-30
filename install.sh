#!/usr/bin/env bash
# =============================================================================
# install.sh — 一键部署 chnroute 每日自动更新（Debian/Ubuntu/CentOS/RHEL/麒麟/统信 等）
#
#   sudo ./install.sh                 # 安装 + 立即跑一次（只生成数据，不动内核）
#   sudo ./install.sh --with-timer    # 额外注册 systemd 每日定时更新
#   sudo ./install.sh --with-cron     # 无 systemd 时用 crontab 每日定时更新
#   sudo ./install.sh --uninstall     # 卸载（保留 /var/lib/chnroute 数据）
#   sudo ./install.sh -y              # 全程免交互：Python 版本过低时直接自动安装 3.8+
#   sudo ./install.sh --no-install    # 禁止自动装 Python（缺依赖就报错退出，适合审计环境）
#
# 安装后的目录布局：
#   /opt/chnroute/                  源码与文档
#   /usr/local/bin/update_chnroute.py     主更新脚本
#   /usr/local/sbin/apply-whitelist.sh    白名单落地脚本（含防自锁回滚）
#   /var/lib/chnroute/              生成的数据（chnroute-v4.txt / ipset / nft / ros / state.json）
#   /etc/chnroute/whitelist.txt     自定义豁免 IP（改这里加白名单，不要改脚本）
#   /var/log/chnroute/update.log    更新日志
# =============================================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-/opt/chnroute}"
DATA_DIR="${DATA_DIR:-/var/lib/chnroute}"
CONF_DIR="${CONF_DIR:-/etc/chnroute}"
LOG_DIR="${LOG_DIR:-/var/log/chnroute}"
BIN_MAIN="${BIN_MAIN:-/usr/local/bin/update_chnroute.py}"
BIN_APPLY="${BIN_APPLY:-/usr/local/sbin/apply-whitelist.sh}"

WITH_TIMER=0
WITH_CRON=0
DO_UNINSTALL=0

# Python 版本策略：
#   PY_MIN       脚本能运行的最低版本（刻意兼容 CentOS 7 自带的 3.6.8）
#   PY_RECOMMEND 低于 PY_MIN 时自动安装的目标版本
PY_MIN="3.6"
PY_RECOMMEND="3.8"

msg()  { printf '\033[32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[install]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
install.sh — chnroute 每日自动更新 / 「仅允许国内访问」一键部署

用法：
  sudo ./install.sh [选项]

选项：
  --with-timer      注册 systemd 每日定时更新（04:30 + 随机延迟）
  --with-cron       无 systemd 时改用 crontab 定时更新
  -y, --yes         全程免交互：Python 版本过低时直接自动安装 3.8+
  --no-install      禁止自动安装 Python（依赖缺失时直接报错退出）
  --python PATH     显式指定解释器（例如 /opt/rh/rh-python38/root/usr/bin/python3.8），
                    跳过自动探测与自动安装
  --uninstall       卸载（保留 /var/lib/chnroute 数据与 /etc/chnroute 配置）
  -h, --help        显示本帮助

环境变量：
  ASSUME_YES=1      等价于 --yes（便于 ansible / 云初始化脚本调用）
  PREFIX / DATA_DIR / CONF_DIR / LOG_DIR   自定义安装路径

安装后目录：
  /usr/local/bin/update_chnroute.py     主更新脚本
  /usr/local/sbin/apply-whitelist.sh    白名单落地脚本（含防自锁回滚）
  /var/lib/chnroute/                    生成的数据
  /etc/chnroute/whitelist.txt           自定义豁免 IP
  /var/log/chnroute/update.log          更新日志
USAGE
}

ASSUME_YES="${ASSUME_YES:-0}"
AUTO_INSTALL=1
FORCE_PY=""
case "${ASSUME_YES}" in 1|y|Y|yes|YES|true|True) ASSUME_YES=1 ;; *) ASSUME_YES=0 ;; esac

# 探测能否与用户交互（支持 curl | bash 这类 stdin 被占用的场景）
HAS_TTY=0
if (exec </dev/tty) 2>/dev/null; then HAS_TTY=1; fi

# ask_yes_no "提示语" [默认y|n]：返回 0=是，1=否，2=无法询问（非交互环境）
ask_yes_no() {
  local prompt="$1" def="${2:-y}" ans=""
  if [ "$ASSUME_YES" -eq 1 ]; then
    msg "$prompt → 自动应答：是（--yes / ASSUME_YES=1）"
    return 0
  fi
  if [ "$HAS_TTY" -eq 0 ]; then return 2; fi
  while :; do
    if [ "$def" = "y" ]; then
      printf '\033[36m[install]\033[0m %s [Y/n] ' "$prompt" > /dev/tty
    else
      printf '\033[36m[install]\033[0m %s [y/N] ' "$prompt" > /dev/tty
    fi
    read -r ans < /dev/tty || ans=""
    case "$ans" in
      "")   if [ "$def" = "y" ]; then return 0; else return 1; fi ;;
      y|Y|yes|YES|Yes|是) return 0 ;;
      n|N|no|NO|No|否)    return 1 ;;
      *)    printf '\033[33m[install]\033[0m 请输入 y 或 n。\n' > /dev/tty ;;
    esac
  done
}

while [ $# -gt 0 ]; do
  a="$1"
  case "$a" in
    --with-timer) WITH_TIMER=1 ;;
    --with-cron)  WITH_CRON=1 ;;
    -y|--yes)     ASSUME_YES=1 ;;
    --no-install|--no-auto-install) AUTO_INSTALL=0 ;;
    --python)     FORCE_PY="${2:-}"; [ -n "$FORCE_PY" ] || die "--python 需要跟一个解释器路径"; shift ;;
    --uninstall)  DO_UNINSTALL=1 ;;
    -h|--help)    usage; exit 0 ;;
    *) die "未知参数：$a（用 --help 查看用法）" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "需要 root 权限：sudo $0 $*"

# ------------------------------------------------------------------ 卸载
if [ "$DO_UNINSTALL" -eq 1 ]; then
  msg "卸载 chnroute…"
  systemctl disable --now chnroute-update.timer 2>/dev/null || true
  rm -f /etc/systemd/system/chnroute-update.service /etc/systemd/system/chnroute-update.timer
  systemctl daemon-reload 2>/dev/null || true
  crontab -l 2>/dev/null | grep -v 'update_chnroute.py' | crontab - 2>/dev/null || true
  "$BIN_APPLY" --rollback 2>/dev/null || true
  rm -f "$BIN_MAIN" "$BIN_APPLY"
  rm -rf "$PREFIX"
  warn "数据目录 $DATA_DIR 与配置 $CONF_DIR 已保留（如需彻底清理请手动删除）"
  msg "卸载完成。"
  exit 0
fi

# ------------------------------------------------------------------ 依赖
msg "检查依赖…"
version_ge() {  # 判断 $1 >= $2（用 sort -V 做版本比较）
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" = "$2" ]
}

# ------------------------------------------------------- Python 解释器挑选
# 主脚本只依赖标准库，且刻意兼容到 3.6（CentOS 7 自带就是 3.6.8），
# 因此在多个 python3 候选里挑版本最高的那个。
# 若一台机器上连 3.6 都找不到，则询问用户是否自动安装 3.8+。
PYBIN=""
PYVER=""

# 候选解释器：常见命令名 + SCL / 手工编译的常见落点（Kylin、UOS 也覆盖）
PY_CANDIDATES="python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3.7 python3.6
python3 /usr/bin/python3 /usr/local/bin/python3 /usr/local/bin/python3.8 /usr/local/bin/python3.9
/usr/local/bin/python3.10 /usr/local/bin/python3.11 /usr/local/bin/python3.12
/opt/rh/rh-python38/root/usr/bin/python3.8 /opt/rh/rh-python39/root/usr/bin/python3.9
/opt/python/cp38-cp38/bin/python3"

# detect_python：扫描候选并挑最高版本。成功返回 0，并设置全局 PYBIN / PYVER。
# 注意这里的探测会顺手 import ssl / urllib，能筛掉「装了但没编 ssl」的残缺 Python。
detect_python() {
  local c v
  PYBIN=""; PYVER=""
  for c in $PY_CANDIDATES; do
    case "$c" in
      /*) [ -x "$c" ] || continue ;;
      *)  command -v "$c" >/dev/null 2>&1 || continue ;;
    esac
    v="$("$c" -c 'import sys, ssl, ipaddress, json, argparse, urllib.request, concurrent.futures, pathlib, datetime; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || continue
    case "$v" in ''|2.*) continue ;; esac
    if [ -z "$PYVER" ] || version_ge "$v" "$PYVER"; then
      if [ -x "$c" ]; then PYBIN="$c"; else PYBIN="$(command -v "$c")"; fi
      PYVER="$v"
    fi
  done
  [ -n "$PYBIN" ]
}

# --------------------------------------------------- 自动安装 Python 3.8+
# 按包管理器逐个尝试候选包名，装完立刻重探测；只要拿到 >=3.8 就算成功，
# 拿不到但 >=3.6 也接受（脚本本身兼容 3.6）。
# 统一 `< /dev/null`：绝不让包管理器卡在交互式提问上（GPG key 导入、debconf 确认等），
# 问到问题时直接失败 → 自动跳到下一个候选方案，不会把安装脚本挂住。
_try_pkgs() {
  local mgr="$1"; shift
  local p
  for p in "$@"; do
    case "$mgr" in
      apt)    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$p" </dev/null >/dev/null 2>&1 && return 0 ;;
      dnf)    dnf install -y "$p"                      </dev/null >/dev/null 2>&1 && return 0 ;;
      yum)    yum install -y "$p"                      </dev/null >/dev/null 2>&1 && return 0 ;;
      zypper) zypper --non-interactive install -y "$p" </dev/null >/dev/null 2>&1 && return 0 ;;
      apk)    apk add --no-cache "$p"                  </dev/null >/dev/null 2>&1 && return 0 ;;
    esac
  done
  return 1
}

_ok_python() {  # 已探测到解释器且 >= PY_RECOMMEND
  detect_python && version_ge "$PYVER" "$PY_RECOMMEND"
}

install_python38() {
  local mgr=""
  if   command -v apt-get >/dev/null 2>&1; then mgr=apt
  elif command -v dnf     >/dev/null 2>&1; then mgr=dnf
  elif command -v yum     >/dev/null 2>&1; then mgr=yum
  elif command -v zypper  >/dev/null 2>&1; then mgr=zypper
  elif command -v apk     >/dev/null 2>&1; then mgr=apk
  else
    warn "未识别包管理器（apt/dnf/yum/zypper/apk 都没有），无法自动安装。"
    return 1
  fi

  msg "使用 $mgr 安装 Python $PY_RECOMMEND+ …（需要联网，可能要 1~3 分钟）"
  if [ "$mgr" = "apt" ]; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq </dev/null >/dev/null 2>&1 || true
  fi

  # 1) 直接指定小版本（Debian/Ubuntu 新版、openSUSE、Alpine 常见）
  msg "  → 尝试具体小版本 python3.12 / 3.11 / 3.10 / 3.9 / 3.8"
  _try_pkgs "$mgr" python3.12 python3.11 python3.10 python3.9 python3.8 || true
  _ok_python && return 0

  # 2) RHEL/CentOS 7：走 SCL 源（rh-python38 装到 /opt/rh/rh-python38/root/usr/bin）
  if [ "$mgr" = "yum" ]; then
    msg "  → RHEL/CentOS 7 走 SCL 源（centos-release-scl + rh-python38）"
    if ! rpm -q centos-release-scl >/dev/null 2>&1; then
      yum install -y centos-release-scl </dev/null >/dev/null 2>&1 \
        || warn "    安装 centos-release-scl 失败（无外网或源不可用），继续尝试其它方式"
    fi
    _try_pkgs yum rh-python38 rh-python39 || true
    _ok_python && return 0
  fi

  # 3) 发行版默认的 python3（Ubuntu 20.04+/Debian 11+/麒麟V10 等已自带 3.8+）
  msg "  → 尝试发行版默认 python3"
  _try_pkgs "$mgr" python3 python38 python39 || true
  _ok_python && return 0

  # 4) 退一步：只要 >= PY_MIN 也能跑（例如 Ubuntu 18.04 只给到 3.6）
  detect_python && version_ge "$PYVER" "$PY_MIN" && {
    warn "只装到 Python $PYVER（< $PY_RECOMMEND，但脚本兼容 3.6+，可正常使用）"
    return 0
  }
  return 1
}

manual_hint() {
  warn "请手动安装 Python 3.6+（建议 3.8+）后重新运行本脚本。参考命令："
  if   command -v apt-get >/dev/null 2>&1; then
    warn "  Debian/Ubuntu/统信UOS： apt-get update && apt-get install -y python3"
    warn "  （需要 3.8+ 时）      apt-get install -y python3.8   # 或 3.9 / 3.10 / 3.11"
  elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
    warn "  RHEL8+/CentOS8+/麒麟V10： dnf install -y python3"
    warn "  CentOS 7： yum install -y centos-release-scl && yum install -y rh-python38"
    warn "            （装完解释器在 /opt/rh/rh-python38/root/usr/bin/python3.8，本脚本能自动找到）"
  fi
  warn "  开源方案：源码编译 https://www.python.org/downloads/ （需 gcc make openssl-devel）"
  warn "  装好后重新执行： sudo $0 $*"
}

# ---- 主流程：检测 → 低于 3.6 则询问是否自动安装 3.8+ -----------------------
# 先处理 --python 显式指定的情况（优先级最高，跳过探测与自动安装）
if [ -n "$FORCE_PY" ]; then
  PYBIN=""
  if [ -x "$FORCE_PY" ]; then
    PYBIN="$FORCE_PY"
  else
    PYBIN="$(command -v "$FORCE_PY" 2>/dev/null || true)"
  fi
  [ -n "$PYBIN" ] || die "--python 指定的解释器不存在或不可执行：$FORCE_PY"
  PYVER="$("$PYBIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  [ -n "$PYVER" ] || die "--python 指定的解释器无法运行：$FORCE_PY"
  version_ge "$PYVER" "$PY_MIN" || die "--python 指定的解释器版本过低：$PYVER（需要 $PY_MIN+）"
  msg "--python 指定解释器：$PYBIN（Python $PYVER）"
fi

if [ -z "$FORCE_PY" ] && { ! detect_python || ! version_ge "$PYVER" "$PY_MIN"; }; then
  if [ -n "$PYBIN" ]; then
    warn "检测到的 Python 版本过低：$PYBIN = $PYVER（低于脚本要求的 $PY_MIN）"
  else
    warn "未检测到任何可用的 Python 3 解释器。"
  fi
  msg "脚本自身零第三方依赖，但需要 Python $PY_MIN+ 运行（推荐 $PY_RECOMMEND+）。"

  if [ "$AUTO_INSTALL" -eq 0 ]; then
    warn "已指定 --no-install，跳过自动安装。"
    manual_hint
    exit 3
  fi

  # 注意：这里必须用 `|| rc=$?` 承接返回值。
  # 因为脚本开头是 set -e，函数以非 0 返回时会被 shell 直接判定为失败并退出，
  # 后面的 case 分支就永远走不到了（表现为「用户选 n 却直接静默退出」）。
  rc=0
  ask_yes_no "是否现在自动安装 Python ${PY_RECOMMEND}+？（自动联网安装）" y || rc=$?
  case $rc in
    0)
      install_python38 || true
      if detect_python && version_ge "$PYVER" "$PY_MIN"; then
        msg "Python 已就绪：$PYBIN（$PYVER）"
      else
        warn "自动安装未能得到可用的 Python $PY_MIN+。"
        manual_hint
        exit 3
      fi
      ;;
    1)
      warn "已取消自动安装。"
      manual_hint
      exit 3
      ;;
    2)
      warn "当前是非交互环境（无终端），无法询问。"
      warn "如需自动安装，请加 -y 重跑： sudo $0 -y $*"
      manual_hint
      exit 3
      ;;
  esac
elif [ -n "$PYBIN" ] && ! version_ge "$PYVER" "$PY_RECOMMEND"; then
  msg "使用解释器 $PYBIN（Python $PYVER）——低于推荐的 $PY_RECOMMEND，但脚本兼容 $PY_MIN+，可正常使用"
fi

msg "使用解释器 $PYBIN（Python $PYVER，仅标准库，无第三方依赖）"

# --------------------------------------------------------------- 依赖检查
MISSING=()
for c in ipset iptables; do
  command -v "$c" >/dev/null 2>&1 || MISSING+=("$c")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  warn "缺少命令：${MISSING[*]}"
  if command -v dnf >/dev/null 2>&1; then
    warn "可执行：dnf install -y ipset iptables"
  elif command -v yum >/dev/null 2>&1; then
    warn "可执行：yum install -y ipset iptables    # CentOS/RHEL/麒麟/统信"
    warn "（CentOS 7 还需：yum install -y iptables-services，见下方 firewalld 提示）"
  elif command -v apt-get >/dev/null 2>&1; then
    warn "可执行：apt-get update && apt-get install -y ipset iptables"
  fi
  warn "依赖缺失不阻断安装，但 --apply / 白名单落地会失败。"
fi

# -------------------------------------------- firewalld 冲突检测（RHEL 系）
if command -v firewall-cmd >/dev/null 2>&1; then
  if systemctl is-active --quiet firewalld 2>/dev/null; then
    warn "=============================================================="
    warn "检测到 firewalld 正在运行，它会接管 netfilter 并在重载时清空"
    warn "脚本写入的 iptables 规则（表现为「策略过一会儿自己失效」）。"
    warn ""
    warn "推荐先执行："
    warn "  systemctl disable --now firewalld"
    warn "  yum install -y iptables-services"
    warn "  systemctl enable --now iptables"
    warn "=============================================================="
  fi
fi

# ------------------------------------------- CentOS 7 的 ipset 内核模块
if [ -r /etc/redhat-release ] && command -v ipset >/dev/null 2>&1; then
  if ! ipset list >/dev/null 2>&1; then
    warn "ipset 命令存在但内核模块未加载，尝试 modprobe ip_set / ip_set_hash_net …"
    modprobe ip_set 2>/dev/null || true
    modprobe ip_set_hash_net 2>/dev/null || true
    ipset list >/dev/null 2>&1 || warn "模块加载失败，请检查内核：lsmod | grep ip_set"
  fi
fi

# ------------------------------------------------------------------ 安装文件
msg "创建目录…"
mkdir -p "$PREFIX" "$DATA_DIR" "$LOG_DIR" "$CONF_DIR"
# 允许用 BIN_MAIN / BIN_APPLY 指到自定义位置（例如 ~/.local/bin），父目录一并创建
mkdir -p "$(dirname "$BIN_MAIN")" "$(dirname "$BIN_APPLY")"
touch "$LOG_DIR/update.log"

msg "安装脚本…"
install -m 0755 "$SRC_DIR/update_chnroute.py" "$BIN_MAIN"
install -m 0755 "$SRC_DIR/apply-whitelist.sh" "$BIN_APPLY"
# 把 shebang 固定为上面挑选出的解释器，避免 /usr/bin/env python3 指向意外版本
sed -i "1s|^#!.*|#!$PYBIN|" "$BIN_MAIN"
for f in README.md chnroute-update.service chnroute-update.timer; do
  [ -f "$SRC_DIR/$f" ] && install -m 0644 "$SRC_DIR/$f" "$PREFIX/$f"
done

if [ ! -f "$CONF_DIR/whitelist.txt" ]; then
  cat > "$CONF_DIR/whitelist.txt" <<'EOF'
# chnroute 自定义豁免白名单 —— 每行一个 IP 或 CIDR，支持 # 注释
# 这里列出的地址在「仅允许国内访问」策略下始终放行。
# 典型用途：公司出口固定 IP、异地办公点、监控平台、你自己的家宽公网 IP。
#
# 例如：
# 203.0.113.10
# 198.51.100.0/24
EOF
  msg "已创建白名单模板 $CONF_DIR/whitelist.txt"
fi

# ------------------------------------------------------------------ 定时任务
if [ "$WITH_TIMER" -eq 1 ]; then
  if command -v systemctl >/dev/null 2>&1; then
    msg "注册 systemd 定时器（每日 04:30 + 随机延迟，自动 --apply）…"
    install -m 0644 "$SRC_DIR/chnroute-update.service" /etc/systemd/system/chnroute-update.service
    install -m 0644 "$SRC_DIR/chnroute-update.timer"   /etc/systemd/system/chnroute-update.timer
    systemctl daemon-reload
    systemctl enable --now chnroute-update.timer
    systemctl list-timers chnroute-update.timer --no-pager || true
  else
    warn "未检测到 systemd，改用 cron。"
    WITH_CRON=1
  fi
fi

if [ "$WITH_CRON" -eq 1 ]; then
  msg "写入 crontab（每日 04:30 + 随机 0-30 分钟延迟，自动 --apply）…"
  CRON_LINE="30 4 * * * sleep \$((RANDOM % 1800)); $PYBIN $BIN_MAIN --outdir $DATA_DIR --sources aggregate --format all --apply --quiet --log-file $LOG_DIR/update.log >> /dev/null 2>&1"
  ( crontab -l 2>/dev/null | grep -v 'update_chnroute.py' || true; echo "$CRON_LINE" ) | crontab -
  crontab -l | grep update_chnroute.py || true
fi

# ------------------------------------------------------------------ 首次运行
msg "首次运行（只生成数据，不修改内核）…"
"$PYBIN" "$BIN_MAIN" --outdir "$DATA_DIR" --format all --isp --log-file "$LOG_DIR/update.log" \
  || warn "首次运行失败，请检查网络后手动重试"

cat <<EOF

=============================================================================
安装完成。接下来三步：

1) 看一眼数据是否合理（应为 4000~8000 段、覆盖 3.4 亿+ 地址）：
     head -5 $DATA_DIR/chnroute-v4.txt
     cat  $DATA_DIR/state.json

2) 把数据原子写入内核 ipset（可随时重复执行，更新不断流）：
     sudo $BIN_MAIN --outdir $DATA_DIR --format all --isp --apply

3) 启用「仅允许国内访问」（★关键：会在 N 秒后自动回滚，务必先测 SSH）：
     # 先确认白名单里有你的出口 IP
     echo "你的公网IP" >> $CONF_DIR/whitelist.txt
     sudo $BIN_APPLY                # 应用，默认 300 秒后自动回滚
     # 立刻另开终端验证 SSH，通了再执行：
     sudo $BIN_APPLY --commit       # 取消回滚，策略永久生效
     sudo $BIN_APPLY --status       # 随时查看状态
     sudo $BIN_APPLY --rollback     # 需要时立即撤销

定时更新：$( [ "$WITH_TIMER" -eq 1 ] && echo "已启用 systemd timer（每日 04:30）" || ([ "$WITH_CRON" -eq 1 ] && echo "已启用 cron（每日 04:30）" || echo "未启用，可加 --with-timer 重新安装" ) )
日志位置：$LOG_DIR/update.log

⚠️ 注意：改为 nftables 后端请加 --backend nft；详见 $PREFIX/README.md
=============================================================================
EOF
