#!/usr/bin/env bash
# =============================================================================
# apply-whitelist.sh — 在 Linux 上启用「仅允许中国大陆 IP 访问」的入站白名单
#
# 依赖：ipset + iptables/ip6tables（或 nftables），以及由 update_chnroute.py
#       --apply 维护的 ipset 集合（默认 cn4 / cn6）。
#
# 安全设计（重点）：白名单一旦生效，境外 IP（含你此刻的 SSH 跳板）会立刻被拒。
#   因此本脚本：
#     1) 自动把「回环 / 内网 / 本机公网 IP / 当前活跃 SSH 会话对端 / 自定义白名单」
#        全部写入豁免集合，天然放行；
#     2) 生效后自动调度一个 N 秒（默认 300s）的【自动回滚】定时任务；
#     3) 你在窗口期内确认 SSH 仍然可用，执行 --commit 取消回滚，策略才永久生效。
#   换句话说：就算规则写错了，最多断 N 秒，机器会自己恢复。
#
# 用法：
#   sudo ./apply-whitelist.sh                 # 应用 + 5 分钟后自动回滚
#   sudo ./apply-whitelist.sh --commit        # 确认无误，取消回滚（策略永久生效）
#   sudo ./apply-whitelist.sh --rollback      # 立刻撤销白名单
#   sudo ./apply-whitelist.sh --status        # 查看当前状态
#   sudo ./apply-whitelist.sh --delay 600     # 自定义回滚窗口
#
#   环境变量：
#     SET4=cn4 SET6=cn6        ipset 集合名
#     WHITELIST_FILE=/etc/chnroute/whitelist.txt   额外豁免（每行一个 IP/CIDR）
#     MODE=strict|loose        strict=非国内一律 DROP；loose=只叠加放行不改判定
#     CHAIN_PREFIX=CHNROUTE    链名前缀
# =============================================================================
set -euo pipefail

SET4="${SET4:-cn4}"
SET6="${SET6:-cn6}"
WL4="chnroute-wl4"
WL6="chnroute-wl6"
WHITELIST_FILE="${WHITELIST_FILE:-/etc/chnroute/whitelist.txt}"
MODE="${MODE:-strict}"
CHAIN_PREFIX="${CHAIN_PREFIX:-CHNROUTE}"
CHAIN4="${CHAIN_PREFIX}-IN"
CHAIN6="${CHAIN_PREFIX}-IN6"
DELAY="${ROLLBACK_DELAY:-300}"
ROLLBACK_BIN="/usr/local/sbin/chnroute-rollback.sh"
AUTOROLLBACK_UNIT="chnroute-autorollback"

msg()  { printf '\033[32m[chnroute]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[chnroute]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[chnroute]\033[0m %s\n' "$*" >&2; exit 1; }

need_root() { [ "$(id -u)" -eq 0 ] || die "需要 root：sudo $0 $*"; }

has() { command -v "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------------ 回滚脚本
install_rollback_script() {
  cat > "$ROLLBACK_BIN" <<EOF
#!/usr/bin/env bash
# 由 apply-whitelist.sh 自动生成 —— 撤销 chnroute 入站白名单
set -uo pipefail
CHAIN4="$CHAIN4"; CHAIN6="$CHAIN6"
while iptables -C INPUT -j "\$CHAIN4" 2>/dev/null; do iptables -D INPUT -j "\$CHAIN4" || break; done
iptables -F "\$CHAIN4" 2>/dev/null; iptables -X "\$CHAIN4" 2>/dev/null
if command -v ip6tables >/dev/null 2>&1; then
  while ip6tables -C INPUT -j "\$CHAIN6" 2>/dev/null; do ip6tables -D INPUT -j "\$CHAIN6" || break; done
  ip6tables -F "\$CHAIN6" 2>/dev/null; ip6tables -X "\$CHAIN6" 2>/dev/null
fi
ipset destroy $WL4 2>/dev/null; ipset destroy $WL6 2>/dev/null
echo "\$(date '+%F %T') [chnroute] 白名单已回滚，入站策略恢复原状" >> /var/log/chnroute-rollback.log
EOF
  chmod 0755 "$ROLLBACK_BIN"
}

# ------------------------------------------------------ 收集豁免地址（防自锁）
collect_exempt() {
  local tmp="$1"
  {
    echo "127.0.0.0/8"
    echo "10.0.0.0/8"
    echo "172.16.0.0/12"
    echo "192.168.0.0/16"
    echo "169.254.0.0/16"
    echo "100.64.0.0/10"
    echo "::1/128"
    echo "fc00::/7"
    echo "fe80::/10"
    # 本机全部公网 IP（多网卡/别名 IP 一并放行）
    ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' || true
    ip -6 -o addr show scope global 2>/dev/null | awk '{print $4}' || true
    # 当前所有已建立 TCP 会话的对端（含你正在用的 SSH 连接）
    # 不用 ss -H / state 过滤：CentOS 7 的 iproute2 较老，改用 awk 过滤 ESTAB
    if has ss; then
      ss -tn 2>/dev/null | awk 'NR>1 && /ESTAB/ {print $4; print $5}' \
        | sed -E 's/:[0-9]+$//' | sed -E 's/^\[|\]$//g' || true
    fi
    # 自定义白名单
    if [ -f "$WHITELIST_FILE" ]; then
      sed -E 's/#.*//' "$WHITELIST_FILE" || true
    fi
  } | tr -d ' \r\t' | grep -v '^$' | sort -u > "$tmp"
}

build_ipset() {
  local tmp="$1"
  has ipset || die "未安装 ipset：apt install ipset / yum install ipset"

  # 国内集合必须已由 update_chnroute.py --apply 建好
  ipset list "$SET4" >/dev/null 2>&1 || die "ipset 集合 $SET4 不存在，请先执行：sudo update_chnroute.py --apply"
  msg "国内集合 $SET4：$(ipset list "$SET4" | sed -n 's/^Number of entries: //p') 条"

  ipset create "$WL4" hash:net family inet -exist
  ipset flush "$WL4"
  local n4=0
  while read -r addr; do
    case "$addr" in
      *:*) continue ;;
      "") continue ;;
    esac
    ipset add "$WL4" "$addr" -exist 2>/dev/null || warn "豁免地址无效已忽略：$addr"
    n4=$((n4 + 1))
  done < "$tmp"

  if ipset list "$SET6" >/dev/null 2>&1; then
    ipset create "$WL6" hash:net family inet6 -exist
    ipset flush "$WL6"
    while read -r addr; do
      case "$addr" in
        *:*) ipset add "$WL6" "$addr" -exist 2>/dev/null || true ;;
      esac
    done < "$tmp"
  fi
  msg "豁免集合 $WL4 已装载 $n4 条（回环/内网/本机公网IP/活跃会话/自定义白名单）"
}

# ------------------------------------------------------------------ 应用规则
apply_rules() {
  local target="$1"
  iptables -N "$CHAIN4" 2>/dev/null || iptables -F "$CHAIN4"

  iptables -A "$CHAIN4" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -A "$CHAIN4" -i lo -j ACCEPT
  iptables -A "$CHAIN4" -m set --match-set "$WL4" src -j ACCEPT
  iptables -A "$CHAIN4" -m set --match-set "$SET4" src -j ACCEPT
  if [ "$MODE" = "strict" ]; then
    iptables -A "$CHAIN4" -j DROP
  else
    iptables -A "$CHAIN4" -j RETURN
  fi

  if has ip6tables && ipset list "$SET6" >/dev/null 2>&1; then
    ip6tables -N "$CHAIN6" 2>/dev/null || ip6tables -F "$CHAIN6"
    ip6tables -A "$CHAIN6" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    ip6tables -A "$CHAIN6" -i lo -j ACCEPT
    ip6tables -A "$CHAIN6" -m set --match-set "$WL6" src -j ACCEPT
    ip6tables -A "$CHAIN6" -m set --match-set "$SET6" src -j ACCEPT
    if [ "$MODE" = "strict" ]; then
      ip6tables -A "$CHAIN6" -j DROP
    else
      ip6tables -A "$CHAIN6" -j RETURN
    fi
  fi

  # 挂到 INPUT 首位，保证优先于用户既有规则
  iptables -C INPUT -j "$CHAIN4" 2>/dev/null || iptables -I INPUT 1 -j "$CHAIN4"
  if has ip6tables && ip6tables -nL "$CHAIN6" >/dev/null 2>&1; then
    ip6tables -C INPUT -j "$CHAIN6" 2>/dev/null || ip6tables -I INPUT 1 -j "$CHAIN6"
  fi
  msg "规则已挂载到 INPUT（模式 $MODE，目标 $target）"
}

schedule_autorollback() {
  local target="$1"
  if has systemd-run; then
    systemctl stop "${AUTOROLLBACK_UNIT}.timer" 2>/dev/null || true
    systemctl reset-failed "${AUTOROLLBACK_UNIT}.service" 2>/dev/null || true
    # --collect 需要 systemd 236+（CentOS 7 只有 219），失败则退回不带 --collect 的形式
    if systemd-run --on-active="${DELAY}s" --unit="$AUTOROLLBACK_UNIT" --collect "$ROLLBACK_BIN" >/dev/null 2>&1 \
       || systemd-run --on-active="${DELAY}s" --unit="$AUTOROLLBACK_UNIT" "$ROLLBACK_BIN" >/dev/null 2>&1; then
      msg "已调度 ${DELAY}s 后自动回滚（systemd-run，取消：systemctl stop ${AUTOROLLBACK_UNIT}.timer）"
      return 0
    fi
  fi
  if has at; then
    echo "$ROLLBACK_BIN" | at now + "$(( (DELAY + 59) / 60 ))" minutes >/dev/null 2>&1 \
      && { msg "已调度 ${DELAY}s 后自动回滚（at）"; return 0; }
  fi
  nohup bash -c "sleep $DELAY; '$ROLLBACK_BIN'" >/dev/null 2>&1 &
  disown || true
  msg "已调度 ${DELAY}s 后自动回滚（后台进程）"
}

cancel_autorollback() {
  if has systemctl; then systemctl stop "${AUTOROLLBACK_UNIT}.timer" 2>/dev/null || true; fi
  jobs -p >/dev/null 2>&1 || true
  pkill -f "$ROLLBACK_BIN" 2>/dev/null || true
  if has at && has atq; then
    atq 2>/dev/null | awk '{print $1}' | while read -r jid; do atrm "$jid" 2>/dev/null || true; done
  fi
}

status() {
  echo "== 链 =="
  iptables -S "$CHAIN4" 2>/dev/null || echo "  $CHAIN4 不存在"
  echo "== ipset =="
  for s in "$SET4" "$WL4" "$SET6" "$WL6"; do
    if ipset list "$s" >/dev/null 2>&1; then
      printf '  %-12s %s 条\n' "$s" "$(ipset list "$s" | sed -n 's/^Number of entries: //p')"
    fi
  done
  echo "== 自动回滚定时器 =="
  systemctl list-timers "$AUTOROLLBACK_UNIT*" --no-pager 2>/dev/null || echo "  未启用或非 systemd"
}

# ------------------------------------------------------------------ 入口
ACTION="apply"
while [ $# -gt 0 ]; do
  case "$1" in
    --commit)   ACTION="commit" ;;
    --rollback) ACTION="rollback" ;;
    --status)   ACTION="status" ;;
    --delay)    DELAY="${2:?--delay 需要秒数}"; shift ;;
    *) die "未知参数：$1" ;;
  esac
  shift
done

case "$ACTION" in
  status)  status ;;
  rollback)
    need_root --rollback
    install_rollback_script
    "$ROLLBACK_BIN"
    cancel_autorollback
    msg "已撤销白名单。"
    ;;
  commit)
    need_root --commit
    cancel_autorollback
    msg "已取消自动回滚，策略永久生效。若要撤销：sudo $0 --rollback"
    ;;
  apply)
    need_root
    has iptables || die "未安装 iptables"
    TMP="$(mktemp)"
    trap 'rm -f "$TMP"' EXIT
    collect_exempt "$TMP"
    build_ipset "$TMP"
    install_rollback_script
    apply_rules "INPUT($CHAIN4/$CHAIN6)"
    schedule_autorollback "INPUT"
    echo
    warn "白名单已生效！【${DELAY} 秒后会自动回滚】，请立刻新开一个终端验证 SSH 是否正常。"
    warn "确认无误后执行：sudo $0 --commit    取消回滚"
    warn "若已失联：等待 ${DELAY} 秒自动恢复，或从控制台 VNC/串口执行：sudo $ROLLBACK_BIN"
    ;;
esac
