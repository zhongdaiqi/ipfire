# chnroute-linux — 中国大陆 IP 段每日自动更新 + 「仅允许国内访问」落地

在 Linux 上每天自动拉取中国大陆（CN）IPv4/IPv6 地址段，清洗聚合后原子写入内核，
配合入站白名单实现「仅允许国内 IP 访问」；同时可导出 ipset / nftables / **RouterOS** 三种格式，
用于多 WAN 出口策略路由（PBR）与国内外分流。

> 本方案是 `chnroute` 思路的工程化落地版：`chnroute` 源自 Linux 社区，本质是
> 「中国大陆 CIDR 数据库」，用于在无 BGP 能力的边缘设备上实现近似 BGP 的按目标选路。

---

## 一、实测结果（2026-09-30，国内家宽）

```
APNIC 官方源         v4=8792 段  v6=2043 段      约 300 秒（国际出口慢）
chnroutes2 聚合源    v4=3898 段                  1.7 秒
china-operator-ip    v4=6207 段                  1.7 秒
china-operator-ip6   v6=3414 段                  1.7 秒
...
─────────────────────────────────────────────────────────
聚合结果             IPv4  5714 段 / 346,035,968 地址（约 3.46 亿，与 CNNIC 统计吻合）
                     IPv6  2247 段
ISP 分组             电信 2843 段 / 移动 1003 段 / 联通 1733 段 / 教育网 85 段
```

两个模式：

| 模式 | 命令 | 耗时 | 说明 |
|---|---|---|---|
| 全量 | `--sources all` | ~300s | APNIC 官方 + 聚合源，最权威 |
| 快速（默认用于定时任务） | `--sources aggregate` | ~10–80s | 只用 jsdelivr 镜像聚合源，国内网络推荐 |

**GitHub 源可达性实测**（这决定脚本必须做镜像回退）：

| 镜像 | 结果 |
|---|---|
| `cdn.jsdelivr.net` | ✅ 96 KB / 1.7 s |
| `ghproxy.net` | ✅ 96 KB / 2.0 s |
| `raw.githubusercontent.com` | ❌ 超时（国内直连不通） |
| `raw.gitmirror.com` | ❌ DNS 解析失败 |

---

## 二、文件清单

| 文件 | 作用 |
|---|---|
| `update_chnroute.py` | **主脚本**：抓取 → 清洗 → 聚合 → 多格式导出 → 原子写入内核 |
| `apply-whitelist.sh` | 启用「仅允许国内访问」入站白名单，**自带防自锁自动回滚** |
| `install.sh` | 一键部署（装文件、建目录、注册定时任务、首次运行） |
| `chnroute-update.service` / `.timer` | systemd 每日定时更新单元 |
| `README.md` | 本文档 |

**运行要求**：Linux（Debian / Ubuntu / CentOS 7+ / RHEL / 麒麟 / 统信均可），**Python 3.6+**。
主脚本只用标准库、零第三方依赖，并**专门适配了 CentOS 7 自带的 Python 3.6.8**——
不需要你额外编译新版本 Python。落地到内核需 `ipset` + `iptables`（纯导出格式不需要）。

### 装 Python 这件事，脚本会自己处理

`install.sh` 启动时会在全系统扫描可用的 python3（含 `/opt/rh/rh-python38/...` 这类 SCL 路径、
`/usr/local/bin/python3.x` 这类手工编译路径），挑版本最高的那个用。然后：

| 检测结果 | 行为 |
|---|---|
| 有 **3.8+** | 直接用，无提示 |
| 只有 **3.6 / 3.7** | 直接用（脚本兼容 3.6+），只提示一句「低于推荐的 3.8」 |
| 只有 **< 3.6** 或**完全没有 Python 3** | **弹出询问**：「是否现在自动安装 Python 3.8+？（自动联网安装）」 |

回答 `y`（或直接回车）后，脚本按当前发行版的包管理器**自动试装**，候选顺序为：

```
python3.12 → python3.11 → python3.10 → python3.9 → python3.8     # 具体小版本
   ↓ 都装不上（例如 CentOS 7 仓库里没有）
centos-release-scl → rh-python38 → rh-python39                    # RHEL/CentOS 7 走 SCL 源
   ↓ 还是不行／源不可达
python3                                                          # 发行版默认（CentOS 7 是 3.6.8）
```

装完立刻重新探测并继续安装流程；若最终只拿到 3.6 也会给出明确提示（脚本兼容，可正常用）。
所有安装命令都带 `< /dev/null`，**绝不会卡在 GPG key 导入 / debconf 之类的交互提问上**。

相关开关：

```bash
sudo ./install.sh -y            # 全程免交互：直接自动安装，不弹询问
sudo ./install.sh --no-install  # 禁止自动装 Python（缺依赖立刻报错退出，适合审计/离线环境）
ASSUME_YES=1 sudo ./install.sh  # 等价于 -y，便于 ansible / 云初始化脚本调用

# 你把 Python 3.8 装在了非标准路径（源码编译到 /opt/python38 之类）？直接指定即可，
# 指定后跳过探测与自动安装：
sudo ./install.sh --python /opt/python38/bin/python3.8
sudo ./install.sh --python /opt/rh/rh-python38/root/usr/bin/python3.8   # SCL 装的
```

非交互环境（没有终端，例如被 ansible 拉起）下不会静默乱装，而是提示你「加 `-y` 重跑」或手动安装。
`--python` 指定的解释器会做版本与可用性校验，低于 3.6 或跑不起来会直接报错退出，不会带着问题往下走。

---

## 三、快速开始

### 3.1 Debian / Ubuntu

```bash
sudo apt update && sudo apt install -y python3 ipset iptables
sudo ./install.sh --with-timer
```

### 3.2 CentOS 7 / RHEL 7 / 麒麟 / 统信（重点看这里）

CentOS 7 自带 Python 3.6.8，本工具**已专门适配到 Python 3.6**（刻意不用 `dataclasses`、
不用 `from __future__ import annotations`、不用海象运算符），**你不需要额外编译新 Python**。

```bash
# 1) 依赖（ipset 默认没装）
#    python3 这一项其实可以省：install.sh 检测到缺失/过低会主动询问并自动安装
sudo yum install -y ipset iptables

# 2) ★ 关键：CentOS 7 默认跑 firewalld，它会接管 netfilter 并在重载时
#    清空你写入的 iptables 规则（症状：策略过一会儿自己失效）。
#    两种处理方式，任选其一：
sudo systemctl disable --now firewalld
sudo yum install -y iptables-services
sudo systemctl enable --now iptables
#    —— 或者改用 nftables 后端（--backend nft），但 firewalld 同样会干扰，仍需停用

# 3) 确认 ipset 内核模块可用（hash:net 是名单的核心数据结构）
sudo modprobe ip_set_hash_net
sudo ipset list          # 无报错即可

# 4) 安装
sudo ./install.sh --with-timer
```

**CentOS 7 已顺带处理掉的坑：**

| 坑 | 处理方式 |
|---|---|
| Python 仅 3.6 | 主脚本下沉兼容 3.6；`install.sh` 自动挑最高版本，**低于 3.6 时询问并自动安装 3.8+**（见上一节） |
| 自己装过 Python 3.8（SCL 或源码编译） | 候选列表里已包含 `/opt/rh/rh-python38/root/usr/bin/python3.8`、`/usr/local/bin/python3.x`，**会被自动认到，无需改 PATH** |
| 包管理器卡在交互提问（GPG key、debconf） | 所有安装命令带 `< /dev/null`，问不到就失败跳下一个候选，不会挂住脚本 |
| systemd 219 不支持 `StandardOutput=append:`（240+ 才有） | 日志改由脚本 `--log-file` 自己追加，systemd 输出进 journal |
| systemd 219 不支持 `systemd-run --collect`（236+ 才有） | 失败自动退回不带 `--collect` 的形式，再退回 `at`，最后退回后台进程 |
| iproute2 版本老，`ss -H` / `state established` 可能不支持 | 改用 `ss -tn` + awk 过滤 `ESTAB`，任何版本都能取到活跃 SSH 对端 |
| ipset 内核模块未加载 | `install.sh` 自动 `modprobe ip_set_hash_net` |
| `ca-certificates` 过旧导致 HTTPS 证书校验失败 | 脚本错误信息里直接给出处置提示，见下方 FAQ |

### 3.3 通用三步（装完就做）

```bash
# 1) 确认数据合理（应为 4000~8000 段、覆盖 3.4 亿+ 地址）
head -5 /var/lib/chnroute/chnroute-v4.txt
cat /var/lib/chnroute/state.json

# 2) 原子写入内核 ipset（可反复执行，更新过程不断流）
sudo /usr/local/bin/update_chnroute.py --outdir /var/lib/chnroute --apply

# 3) 启用「仅允许国内访问」
echo "你的公网出口IP" | sudo tee -a /etc/chnroute/whitelist.txt
sudo /usr/local/sbin/apply-whitelist.sh          # 应用，300 秒后自动回滚
#   ↑ 立刻另开一个终端验证 SSH 是否还通
sudo /usr/local/sbin/apply-whitelist.sh --commit # 确认无误，取消回滚，永久生效
```

---

## 四、三个真正重要的工程细节

### 1. 白名单不会把你锁在门外（防自锁三重保险）

白名单一旦生效，境外 IP（包括你此刻的 SSH 跳板）会**立刻被拒**。所以 `apply-whitelist.sh`：

1. **自动豁免**：回环、内网（10/8、172.16/12、192.168/16）、本机全部公网 IP、
   **当前所有已建立 TCP 会话的对端**（也就是你正在用的那条 SSH 连接）、
   `/etc/chnroute/whitelist.txt` 自定义列表 —— 全部自动进豁免集合；
2. **自动回滚**：生效后立刻调度一个 300 秒的定时回滚（优先 `systemd-run`，退化到 `at`，再退化到后台进程）。
   规则就算写错，最多断 300 秒，机器会自己恢复；
3. **显式提交**：你在窗口期内确认 SSH 正常，执行 `--commit` 取消回滚，策略才永久生效。

### 2. 数据更新是原子的，不丢包不乱序

用「影子集合 + swap」，而不是「删除重建」：

```
ipset create cn4-tmp ...   ←  灌入新数据到影子集合（线上集合仍在正常工作）
ipset restore -exist < chnroute-ipset.v4   （集合名改写为 cn4-tmp）
ipset swap cn4-tmp cn4     ←  内核里一次指针交换，瞬时生效，零空窗
ipset destroy cn4-tmp
```

如果新数据抓取失败或校验不过，`cn4` 保持原样 —— 白名单永远不会变成空集合。

### 3. 完整性守卫：防止数据源异常导致白名单塌陷

白名单塌陷 = 灾难（所有国内用户被拒之门外）。所以每次落地前强制校验：

- 绝对下限：IPv4 ≥ 2000 段、≥ 1 亿地址（实测约 5714 段 / 3.46 亿）
- 绝对上限：IPv4 ≤ 15 亿地址（超过说明把非 CN 网段并进来了）
- 相对变化：较上次条目数/IP 数**骤降超 20%** 直接拒绝；**骤增 3 倍以上**也拒绝
- 命中即退出码 `2`，**文件与内核都不改动**，保留上一次的可用数据
- 确认数据源无误时用 `--force` 跳过

---

## 五、脚本能力速查

```bash
# 常用
python3 update_chnroute.py --outdir /var/lib/chnroute            # 只生成数据
python3 update_chnroute.py --sources aggregate --apply           # 快速模式 + 写内核
python3 update_chnroute.py --isp --ros-pbr --format all --apply  # 全格式 + ISP 分组 + ROS PBR 模板
python3 update_chnroute.py --backend nft --nft-table 'inet filter' --apply
python3 update_chnroute.py --dry-run --apply                     # 只演示不落地
python3 update_chnroute.py --log-file /var/log/chnroute/update.log
```

| 参数 | 说明 |
|---|---|
| `--sources all\|apnic\|aggregate` | 数据源组合，默认 `all` |
| `--isp` | 追加电信/移动/联通/教育网/科技网/鹏博士分组 |
| `--format txt,ipset,nft,ros` | 输出格式，`all` 表示全部 |
| `--set-prefix cn` | ipset 集合前缀 → `cn4` / `cn6` |
| `--ros-list CN` / `--ros-pbr` | RouterOS list 名 / 生成 PBR 模板 |
| `--apply` / `--backend` | 原子写入内核 / 选 ipset 或 nft |
| `--force` | 跳过完整性守卫（危险） |
| `--quiet` / `--log-file` | 静默 / UTF-8 日志落盘 |

**输出文件**（`--format all`）：

```
chnroute-v4.txt / chnroute-v6.txt     纯 CIDR（带元信息头），通用格式
chnroute-ipset.v4 / .v6               ipset restore 格式（原子 swap 用）
chnroute.nft                          nftables set 定义（nft -f 单事务加载）
chnroute-ros.rsc                      RouterOS address-list 批量导入脚本
chnroute-ros-pbr.rsc                  RouterOS 策略路由模板（国内直连 / 境外分流）
chnroute-diff.txt                     与上次更新的差异（新增/移除各 200 条）
state.json                            本次摘要：段数、地址数、各源状态
cn-chinanet.txt / cn-cmcc.txt / ...   ISP 分组（--isp 时生成）
```

---

## 六、三种落地方式

### A) ipset + iptables（推荐，兼容性最好）

```bash
sudo update_chnroute.py --outdir /var/lib/chnroute --format all --apply
sudo apply-whitelist.sh --commit
sudo apply-whitelist.sh --status     # 查看链与集合
sudo apply-whitelist.sh --rollback   # 随时撤销
```

生成的 IPv4 链结构（IPv6 同理走 `ip6tables`）：

```
CHNROUTE-IN:
  1. -m conntrack --ctstate ESTABLISHED,RELATED  -j ACCEPT   ← 放行已建立连接（响应包）
  2. -i lo                                        -j ACCEPT   ← 回环
  3. -m set --match-set chnroute-wl4 src          -j ACCEPT   ← 豁免集合（内网/本机/活跃会话/自定义）
  4. -m set --match-set cn4 src                   -j ACCEPT   ← 中国大陆
  5. -j DROP                                                  ← 其余拒绝（MODE=loose 时改为 RETURN）
```

挂到 `INPUT` 首位（`-I INPUT 1`），优先于既有规则。可用环境变量覆盖：

```bash
sudo SET4=cn4 MODE=strict ROLLBACK_DELAY=600 WHITELIST_FILE=/etc/chnroute/whitelist.txt ./apply-whitelist.sh
```

### B) nftables

```bash
sudo update_chnroute.py --backend nft --nft-table 'inet filter' --format nft --apply
```

`nft -f chnroute.nft` 整文件是**一个事务**，同名 set 被原子替换。在你自己的规则里引用：

```
nft add rule inet filter input ip saddr @cn4 accept
```

> 注意：nftables 的 set 与引用它的规则必须在**同一张表**内，所以用 `--nft-table` 指定你的表名（默认 `inet filter`）。

### C) RouterOS（多 WAN 策略路由）

```bash
python3 update_chnroute.py --outdir ./out --format ros --isp --ros-pbr
# 把 chnroute-ros.rsc 上传到设备 /file，然后：
#   /import file-name=chnroute-ros.rsc
```

生成的 `chnroute-ros-pbr.rsc` 是可直接改用的模板（国内直连、境外走代理线路）：

```
/ip firewall mangle
add chain=prerouting src-address-list=LAN dst-address-list=CN  action=mark-routing new-routing-mark=to-cn       passthrough=yes
add chain=prerouting src-address-list=LAN dst-address-list=!CN action=mark-routing new-routing-mark=to-overseas passthrough=yes

/ip route
add dst-address=0.0.0.0/0 gateway=<国内WAN>   routing-mark=to-cn       distance=1 check-gateway=ping
add dst-address=0.0.0.0/0 gateway=<海外WAN>   routing-mark=to-overseas distance=1 check-gateway=ping
```

配合 `--isp` 生成的 `CN-CHINANET` / `CN-CMCC` / `CN-UNICOM` 列表，还能做运营商级精细分流
（三网互联互通瓶颈是实打实的，电信用户访问移动 IDC 绕行会显著抬高 RTT 与丢包）。
`check-gateway=ping` + `distance` 分级实现链路健康感知与自动备份。

---

## 七、定时更新

```bash
sudo ./install.sh --with-timer    # systemd：每天 04:30 + 随机 30 分钟，自动 --apply
sudo ./install.sh --with-cron     # 无 systemd 时用 crontab
```

systemd 单元要点：

- `RandomizedDelaySec=1800` 打散触发时间，避免与其他定时任务叠加
- `Persistent=true` 关机/休眠错过时间点后开机补跑
- `TimeoutStartSec=900` 超时视为失败，**旧 ipset 不受影响**
- 日志落 `/var/log/chnroute/update.log`

检查：

```bash
systemctl list-timers chnroute-update.timer
journalctl -u chnroute-update.service -n 50
tail -f /var/log/chnroute/update.log
```

---

## 八、常见问题

**Q：`install.sh` 问我「是否自动安装 Python 3.8+」，我选了 n / 没终端答不了，怎么办？**
三条路，任选：
```bash
sudo ./install.sh -y             # 让它自动装（非交互环境最省事）
sudo ./install.sh --no-install   # 明确禁止它装，缺 Python 时直接报错，你自己装
# 或者手动装好再跑：
sudo yum install -y centos-release-scl && sudo yum install -y rh-python38   # CentOS 7
# 装到 /opt/rh/rh-python38/root/usr/bin/python3.8，install.sh 会自动找到，不用配 PATH
```
脚本**不会背着你装东西**：非交互环境下它只提示、不安装；只有你回答了 `y` 或显式加了 `-y` 才动手。

**Q：自动安装跑完发现只装到 3.6，能接受吗？**
可以。主脚本刻意兼容 3.6（不用 `dataclasses` 等 3.7+ 特性），3.6 与 3.8+ 行为一致。
想要 3.8+ 就用 SCL 装 `rh-python38`，或 `apt install python3.11` 之类，再重跑 `install.sh` 即可。

**Q：CentOS 7 上报 `SSL: CERTIFICATE_VERIFY_FAILED` 怎么办？**
老系统的 `ca-certificates` 太旧，不认识 jsdelivr 等站点现在用的根证书。执行：
```bash
sudo yum update -y ca-certificates
# 若 yum 源已失效（CentOS 7 已 EOL），把源指向 vault：
sudo sed -i 's|^mirrorlist=|#mirrorlist=|; s|^#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|' /etc/yum.repos.d/CentOS-*.repo
sudo yum clean all && sudo yum makecache && sudo yum update -y ca-certificates
```
脚本在遇到该错误时也会直接把这个提示打出来。

**Q：白名单生效后过一会儿自己失效了？**
RHEL/CentOS 系基本可以断定是 **firewalld 重载把 iptables 规则冲掉了**。见 3.2 节：
停用 firewalld 并启用 iptables-services，或改走 nftables 后端。

**Q：APNIC 源太慢（约 5 分钟），能快吗？**
用 `--sources aggregate`（约 10 秒）。它走 jsdelivr 镜像上的 BGP 聚合数据，段数更少、覆盖一致。
systemd 单元默认就是这个模式。

**Q：用户访问国内 CDN 但 CDN 回源在境外，会不会被误封？**
白名单只判**源 IP**。国内 CDN 边缘节点在国内，源 IP 就是国内的，不受影响。

**Q：白名单生效后，出站请求的响应包会被丢吗？**
不会。链首放行 `ESTABLISHED,RELATED`，你主动发起的连接，响应包照常进来。

**Q：要不要同时放行 UDP / DNS？**
白名单基于 address-list，协议无关。但如果**只**给国外用户开 DNS，那么他们连域名都解析不了 ——
若确有境外用户，请把他们加进 `/etc/chnroute/whitelist.txt` 而不是开放端口。

**Q：误封了自己怎么救？**
1. 等 300 秒自动回滚（如果还没 `--commit`）；
2. 云控制台 VNC/串口执行 `sudo /usr/local/sbin/chnroute-rollback.sh`；
3. 事先把出口 IP 写进 `/etc/chnroute/whitelist.txt` 是唯一可靠的预防手段。

**Q：IPv6 要不要一起管？**
要。只做 v4 白名单时，境外用户仍可经 IPv6 进来。脚本默认 v4/v6 一起生成；
若服务器没有 IPv6，加 `--no-ipv6`。

**Q：想撤掉全部策略？**
`sudo /usr/local/sbin/apply-whitelist.sh --rollback`；完全卸载 `sudo ./install.sh --uninstall`。

---

## 九、数据源与合规说明

- **APNIC 官方统计文件**：`delegated-apnic-latest`，含 CN 的 `allocated`/`assigned` 记录，
  权威、只增不减。IPv4 记录以「地址个数」表示，脚本用 `summarize_address_range` 正确转 CIDR
  （例如 300 个地址 → `/24 + /27 + /29 + /30`，已单测验证）。
- **china-operator-ip**：BGP 路由表聚合，附带 ISP（电信/移动/联通/教育网等）归属。
- **chnroutes2**：已聚合的最小 CIDR 集，体积小、速度快。

以上均为公开的 RIR/BGP 数据派生品，用于网络运维与访问控制；请遵守当地法律法规及所在单位的网络管理规定。
