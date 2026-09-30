#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_chnroute.py — 中国大陆 IP 段（chnroute）每日自动更新 + 多平台落地

设计目标
--------
1. 每日从权威数据源拉取中国大陆 IPv4/IPv6 地址段，清洗、去重、CIDR 聚合；
2. 内置「完整性守卫」：数据源异常（条目骤减/骤增）时拒绝落地，避免白名单塌陷自锁；
3. 输出多种可直接落地的格式：
     - 纯 CIDR 文本        （通用）
     - ipset restore 文件  （Linux 首选，配合 swap 做到原子更新、不断流）
     - nftables set        （nft -f 事务加载）
     - RouterOS .rsc       （/ip firewall address-list 批量导入 + 可选 PBR 模板）
4. --apply 时以「影子集合 + swap」方式写入内核，更新过程中旧集合持续生效。

数据源
------
- APNIC 官方 delegated 统计文件：权威、只增不减、带 allocated/assigned 状态（必需源）
- china-operator-ip：BGP 聚合，附带电信/移动/联通/教育网归属（国内可选）
- misakaio/chnroutes2：已聚合的最小 CIDR 集，体积小、速度快
GitHub 系数据源在国内直连 raw.githubusercontent.com 常被墙，脚本内置 jsdelivr /
ghproxy.net / gh-proxy.com 多镜像自动回退（按序尝试，谁通且快用谁）。

用法
----
  # 仅生成数据，不碰内核（最安全，第一次先这么跑）
  python3 update_chnroute.py --outdir /var/lib/chnroute

  # 生成并原子写入内核 ipset
  sudo python3 update_chnroute.py --outdir /var/lib/chnroute --apply

  # 国内网络推荐：跳过慢速的 APNIC 全量文件，只用镜像上的聚合源（秒级完成）
  python3 update_chnroute.py --sources aggregate --outdir /var/lib/chnroute --apply

  # 追加电信/移动/联通/教育网分组 + RouterOS PBR 模板
  sudo python3 update_chnroute.py --isp --ros-pbr --apply

  # nftables 后端
  sudo python3 update_chnroute.py --backend nft --nft-table inet filter --apply

退出码
------
  0 成功 / 1 运行错误 / 2 完整性守卫拦截（数据未更新，保留旧数据） / 3 参数或环境错误
"""

import argparse
import concurrent.futures
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "1.1.0"
UA = "chnroute-updater/%s (https://github.com/gaoyifan/china-operator-ip)" % VERSION

# 可选日志文件句柄（--log-file），UTF-8 追加写，便于 cron/systemd 巡检
LOG_FH = None

APNIC_URLS = [
    "https://ftp.apnic.net/apnic/stats/apnic/delegated-apnic-latest",
    "https://ftp.apnic.net/stats/apnic/delegated-apnic-latest",
]

# ------------------------------------------------------------------ 数据结构

Net4 = ipaddress.IPv4Network
Net6 = ipaddress.IPv6Network


def gh_mirrors(repo: str, ref: str, path: str) -> List[str]:
    """
    给定 GitHub 仓库/分支/文件，返回按「国内可用性」排序的 URL 候选。
    实测（2026-09，国内家宽）：jsdelivr ≈1.7s，ghproxy.net ≈2s，
    raw.githubusercontent.com 直接超时。因此 raw 放最后兜底。
    """
    raw = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, ref, path)
    return [
        "https://cdn.jsdelivr.net/gh/%s@%s/%s" % (repo, ref, path),
        "https://ghproxy.net/%s" % raw,
        "https://gh-proxy.com/%s" % raw,
        raw,
    ]


class Feed(object):
    """
    一个数据源。kind: apnic=官方统计文件, cidr=纯 CIDR 列表
    刻意不用 dataclass —— CentOS 7 自带 Python 3.6，无 dataclasses 模块。
    """

    __slots__ = ("name", "urls", "kind", "required", "isp")

    def __init__(self, name, urls, kind, required=False, isp=None):
        # type: (str, List[str], str, bool, Optional[str]) -> None
        self.name = name
        self.urls = urls
        self.kind = kind
        self.required = required
        self.isp = isp

    def __repr__(self):
        return "Feed(%s, required=%s, isp=%s)" % (self.name, self.required, self.isp)


class Bundle(object):
    """一次抓取的合并结果"""

    __slots__ = ("v4", "v6", "isp_v4", "isp_v6")

    def __init__(self):
        self.v4 = []      # type: List[Net4]
        self.v6 = []      # type: List[Net6]
        self.isp_v4 = {}  # type: Dict[str, List[Net4]]
        self.isp_v6 = {}  # type: Dict[str, List[Net6]]

    def extend(self, other):
        # type: (Bundle) -> None
        self.v4.extend(other.v4)
        self.v6.extend(other.v6)
        for k, v in other.isp_v4.items():
            self.isp_v4.setdefault(k, []).extend(v)
        for k, v in other.isp_v6.items():
            self.isp_v6.setdefault(k, []).extend(v)


# ------------------------------------------------------------------ 工具函数

def _emit(stream, msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        stream.write(line + "\n")
        stream.flush()
    except Exception:  # noqa: BLE001 - 终端编码异常不应中断任务
        pass
    if LOG_FH is not None:
        try:
            LOG_FH.write(line + "\n")
            LOG_FH.flush()
        except Exception:  # noqa: BLE001
            pass


def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        _emit(sys.stdout, msg)


def warn(msg: str) -> None:
    _emit(sys.stderr, "WARN  " + msg)


def die(msg: str, code: int = 1) -> "None":
    _emit(sys.stderr, "ERROR " + msg)
    sys.exit(code)


def http_get(url: str, timeout: int = 30, retries: int = 3) -> str:
    """带重试的 GET，返回文本。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": UA, "Accept-Encoding": "identity", "Cache-Control": "no-cache"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return raw.decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - 网络层错误统一重试
            last_err = exc
            if attempt < retries:
                time.sleep(min(2 * attempt, 6))
    raise RuntimeError("下载失败 %s -> %s" % (url, last_err))


def is_root() -> bool:
    """非 POSIX 平台（如 Windows 调试环境）无 geteuid，直接视为有权，避免崩溃。"""
    if not hasattr(os, "geteuid"):
        return True
    try:
        return os.geteuid() == 0
    except Exception:  # noqa: BLE001
        return False


def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def run(cmd: Sequence[str], stdin_text: Optional[str] = None, check: bool = True) -> Tuple[int, str]:
    proc = subprocess.run(
        list(cmd),
        input=stdin_text.encode() if stdin_text else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out = proc.stdout.decode("utf-8", "replace").strip()
    if check and proc.returncode != 0:
        raise RuntimeError("命令失败(%d): %s\n%s" % (proc.returncode, " ".join(cmd), out))
    return proc.returncode, out


# ------------------------------------------------------------------ 解析层

def parse_apnic(text: str, country: str) -> Tuple[List[Net4], List[Net6]]:
    """
    APNIC 官方 delegated 文件格式：
        apnic|CN|ipv4|1.0.1.0|256|20110414|allocated
        apnic|CN|ipv6|2400:3200::|32|20110414|allocated
    IPv4 的单位是「地址个数」而非掩码，需要用 summarize_address_range 转 CIDR。
    """
    v4: List[Net4] = []
    v6: List[Net6] = []
    cc = country.upper()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split("|")
        if len(f) < 7:
            continue
        _registry, f_cc, rtype, start, value, _date, status = f[0], f[1], f[2], f[3], f[4], f[5], f[6]
        if f_cc.upper() != cc:
            continue
        if status not in ("allocated", "assigned"):
            continue
        if rtype == "ipv4":
            try:
                count = int(value)
                first = ipaddress.IPv4Address(start)
            except ValueError:
                continue
            if count <= 0:
                continue
            last = ipaddress.IPv4Address(int(first) + count - 1)
            try:
                v4.extend(ipaddress.summarize_address_range(first, last))
            except ValueError:
                continue
        elif rtype == "ipv6":
            try:
                v6.append(ipaddress.IPv6Network("%s/%s" % (start, value), strict=False))
            except ValueError:
                continue
    return v4, v6


def parse_cidr_text(text: str) -> Tuple[List[Net4], List[Net6]]:
    """纯 CIDR 文本（每行一条，支持 # 注释、逗号/空白分隔、行内多值）。"""
    v4: List[Net4] = []
    v6: List[Net6] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        for tok in line.replace(",", " ").replace("\t", " ").split():
            try:
                net = ipaddress.ip_network(tok, strict=False)
            except ValueError:
                continue
            if net.version == 4:
                v4.append(net)  # type: ignore[arg-type]
            else:
                v6.append(net)  # type: ignore[arg-type]
    return v4, v6


def collapse_v4(nets: Iterable[Net4]) -> List[Net4]:
    uniq = set(nets)
    if not uniq:
        return []
    return sorted(ipaddress.collapse_addresses(uniq), key=lambda n: int(n.network_address))


def collapse_v6(nets: Iterable[Net6]) -> List[Net6]:
    uniq = set(nets)
    if not uniq:
        return []
    return sorted(ipaddress.collapse_addresses(uniq), key=lambda n: int(n.network_address))


def count_ips(nets: Iterable) -> int:
    return sum(int(n.num_addresses) for n in nets)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ 抓取层

def build_feeds(use_isp: bool, sources: str) -> List[Feed]:
    """
    sources:
      all       = APNIC 权威全量 + 聚合源（默认，最稳）
      apnic     = 只用 APNIC 官方文件（最权威，但国内直连较慢，约 1.7MB）
      aggregate = 只用镜像上的聚合源（快，秒级完成，适合国内网络/每日定时任务）
    """
    feeds: List[Feed] = []
    if sources in ("all", "apnic"):
        feeds.append(Feed("apnic", APNIC_URLS, "apnic", required=True))
    if sources in ("all", "aggregate"):
        feeds.append(Feed(
            "chnroutes2",
            gh_mirrors("misakaio/chnroutes2", "master", "chnroutes.txt"),
            "cidr"))
        feeds.append(Feed(
            "china-operator-ip",
            gh_mirrors("gaoyifan/china-operator-ip", "ip-lists", "china.txt"),
            "cidr"))
        feeds.append(Feed(
            "china-operator-ip6",
            gh_mirrors("gaoyifan/china-operator-ip", "ip-lists", "china6.txt"),
            "cidr"))
    if use_isp:
        # (前缀, 有 v4 文件, 有 v6 文件) —— 依 china-operator-ip/ip-lists 实际文件清单
        for isp, has4, has6 in (
            ("chinanet", True, True),   # 中国电信 ChinaNet
            ("cmcc", True, True),       # 中国移动 CMNET
            ("unicom", True, True),     # 中国联通 CHINA169
            ("cernet", True, True),     # 中国教育网
            ("cstnet", True, True),     # 中国科技网
            ("drpeng", True, False),    # 鹏博士
            ("googlecn", True, True),   # Google 中国
        ):
            if has4:
                feeds.append(Feed(
                    "isp-%s" % isp,
                    gh_mirrors("gaoyifan/china-operator-ip", "ip-lists", "%s.txt" % isp),
                    "cidr", isp=isp))
            if has6:
                feeds.append(Feed(
                    "isp6-%s" % isp,
                    gh_mirrors("gaoyifan/china-operator-ip", "ip-lists", "%s6.txt" % isp),
                    "cidr", isp=isp))
    return feeds


def _fetch_one(feed: Feed, country: str, timeout: int, retries: int) -> Tuple[Feed, List, List, Optional[str], Optional[str]]:
    """按候选 URL 顺序尝试，返回 (feed, v4, v6, 成功URL, 错误信息)。"""
    errors: List[str] = []
    for url in feed.urls:
        host = url.split("/")[2] if "//" in url else url
        try:
            text = http_get(url, timeout=timeout, retries=retries)
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)[:70]
            # 老系统（如 CentOS 7）常见：ca-certificates 过旧 -> 证书校验失败
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "SSLError" in type(exc).__name__:
                detail += "（证书校验失败，试试：yum update -y ca-certificates）"
            errors.append("%s -> %s" % (host, detail))
            continue
        if not text.strip():
            errors.append("%s -> 内容为空" % host)
            continue
        if feed.kind == "apnic":
            v4, v6 = parse_apnic(text, country)
        else:
            v4, v6 = parse_cidr_text(text)
        if not v4 and not v6:
            errors.append("%s -> 未解析出任何网段（可能是错误页面）" % host)
            continue
        return feed, v4, v6, url, None
    return feed, [], [], None, "; ".join(errors)


def fetch_all(feeds: Sequence[Feed], country: str, timeout: int, retries: int,
              quiet: bool) -> Tuple[Bundle, Dict[str, str]]:
    """
    并发抓取所有源（APNIC 全量文件在国内可能耗时数分钟，并发避免被它拖住）。
    必需源失败 => 终止；可选源失败 => 警告后跳过。
    """
    bundle = Bundle()
    status: Dict[str, str] = {}
    results: Dict[str, Tuple[Feed, List, List, Optional[str], Optional[str]]] = {}

    workers = max(1, min(6, len(feeds)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch_one, f, country, timeout, retries) for f in feeds]
        for fut in concurrent.futures.as_completed(futures):
            feed, v4, v6, ok_url, err = fut.result()
            results[feed.name] = (feed, v4, v6, ok_url, err)

    for feed in feeds:  # 按定义顺序输出，日志稳定可读
        _, v4, v6, ok_url, err = results[feed.name]
        if err:
            status[feed.name] = "fail: %s" % err[:160]
            if feed.required:
                die("必需数据源全部不可用：%s\n%s" % (feed.name, err), 1)
            warn("可选源不可用，已跳过：%s" % feed.name)
            if not quiet:
                for e in err.split("; ")[:4]:
                    sys.stderr.write("             %s\n" % e)
            continue
        status[feed.name] = "ok v4=%d v6=%d via %s" % (
            len(v4), len(v6), (ok_url or "").split("/")[2])
        if feed.isp:
            if v4:
                bundle.isp_v4.setdefault(feed.isp, []).extend(v4)
            if v6:
                bundle.isp_v6.setdefault(feed.isp, []).extend(v6)
        else:
            bundle.v4.extend(v4)
            bundle.v6.extend(v6)
        log("  源 %-24s v4=%-6d v6=%-6d [%s]" % (
            feed.name, len(v4), len(v6), (ok_url or "").split("/")[2]), quiet)
    return bundle, status



# ------------------------------------------------------------------ 完整性守卫

def sanity_check(v4: List[Net4], v6: List[Net6], prev: Optional[dict],
                 force: bool, quiet: bool) -> Tuple[bool, List[str]]:
    """
    白名单塌陷 = 灾难（会把国内用户挡在门外）。因此：
      - 首次运行只做绝对阈值检查
      - 之后与上次比较：条目数/IP 数骤降超阈值即拒绝
      - 骤增过高（可能把全世界包进来）也拒绝
    """
    reasons: List[str] = []
    v4_ips = count_ips(v4)
    v6_ips = count_ips(v6)

    # 绝对下限：中国大陆公网 IPv4 至少 1 亿地址、2000 个段
    if len(v4) < 2000:
        reasons.append("IPv4 条目数异常偏少：%d < 2000" % len(v4))
    if v4_ips < 100_000_000:
        reasons.append("IPv4 覆盖地址数异常偏少：%d < 1亿" % v4_ips)
    # 绝对上限：IPv4 全空间 42.9 亿，国内占比远超 20% 说明抓错数据
    if v4_ips > 1_500_000_000:
        reasons.append("IPv4 覆盖地址数异常偏多：%d > 15亿（疑似把非 CN 网段并入）" % v4_ips)

    if prev:
        p_v4 = prev.get("v4", {}).get("entries")
        p_ips = prev.get("v4", {}).get("ips")
        if p_v4 and len(v4) < p_v4 * 0.8:
            reasons.append("IPv4 条目数较上次骤降：%d -> %d（>20%%）" % (p_v4, len(v4)))
        if p_ips and v4_ips < p_ips * 0.8:
            reasons.append("IPv4 覆盖地址数较上次骤降：%d -> %d（>20%%）" % (p_ips, v4_ips))
        if p_ips and v4_ips > p_ips * 3:
            reasons.append("IPv4 覆盖地址数较上次骤增 3 倍以上：%d -> %d" % (p_ips, v4_ips))

    ok = not reasons
    if not ok and force:
        warn("完整性守卫拦截以下问题，但已指定 --force，强行继续：")
        for r in reasons:
            warn("  · %s" % r)
        ok = True
    elif not ok:
        for r in reasons:
            warn("  · %s" % r)
    else:
        log("完整性守卫通过：v4 %d 段 / %d 地址，v6 %d 段" % (len(v4), v4_ips, len(v6)), quiet)
    return ok, reasons


# ------------------------------------------------------------------ 输出层

def write_text_file(path: Path, lines: Iterable[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for ln in lines:
            fh.write(ln)
            fh.write("\n")
    os.replace(str(tmp), str(path))


def out_txt(outdir: Path, stamp: str, v4: List[Net4], v6: List[Net6],
            sources: Dict[str, str]) -> None:
    head = [
        "# chnroute (China mainland) — generated %s" % stamp,
        "# generator: update_chnroute.py v%s" % VERSION,
        "# ipv4 entries: %d  addresses: %d" % (len(v4), count_ips(v4)),
        "# ipv6 entries: %d" % len(v6),
        "# sources: %s" % ", ".join(sorted(sources.keys())),
        "# 用途：Linux 白名单（仅允许国内访问）/ 代理分流 / 策略路由",
    ]
    write_text_file(outdir / "chnroute-v4.txt", list(head) + [str(n) for n in v4])
    write_text_file(outdir / "chnroute-v6.txt", list(head) + [str(n) for n in v6])


def write_ipset_file(path: Path, set_name: str, family: str,
                     nets: Iterable, maxelem: int = 1_048_576) -> None:
    """
    ipset restore 格式。文件内使用「基础集合名」，应用时会改写成影子名。
    首行 create + flush，保证 restore 到已存在/已污染的集合也得到干净结果。
    """
    lines = [
        "create %s hash:net family %s hashsize 8192 maxelem %d" % (set_name, family, maxelem),
        "flush %s" % set_name,
    ]
    lines.extend("add %s %s" % (set_name, n) for n in nets)
    write_text_file(path, lines)


def write_nft_file(path: Path, stamp: str, table: str, set4: str, v4: List[Net4],
                   set6: str, v6: List[Net6]) -> None:
    """
    nftables set 定义。用 nft -f 加载是单个事务，替换同名 set 是原子的。
    注意：set 需与引用它的规则位于同一张表；默认表名 inet filter，可用 --nft-table 改。
    """
    def block(name: str, typ: str, nets: Iterable) -> List[str]:
        items = [str(n) for n in nets]
        out = [
            "    set %s {" % name,
            "        type %s" % typ,
            "        flags interval",
            "        auto-merge",
        ]
        if items:
            out.append("        elements = {")
            for i, it in enumerate(items):
                out.append("            %s%s" % (it, "," if i < len(items) - 1 else ""))
            out.append("        }")
        out.append("    }")
        return out

    lines: List[str] = [
        "#!/usr/sbin/nft -f",
        "# chnroute for nftables — %s" % stamp,
        "# 加载: nft -f %s   （整文件为一个事务，原子生效）" % path.name,
        "# 引用示例: nft add rule inet filter input ip saddr @%s accept" % set4,
        "",
        "table %s {" % table,
    ]
    lines += block(set4, "ipv4_addr", v4)
    if v6:
        lines += block(set6, "ipv6_addr", v6)
    lines.append("}")
    write_text_file(path, lines)


def write_ros_rsc(path: Path, stamp: str, list_name: str, v4: List[Net4], v6: List[Net6],
                  isp_v4: Optional[Dict[str, List[Net4]]] = None,
                  isp_v6: Optional[Dict[str, List[Net6]]] = None) -> None:
    """
    RouterOS 导入脚本。先清空同名 list 再批量写入，避免陈旧条目堆积。
    导入方式：把文件上传到 /file，然后 /import file-name=chnroute-ros.rsc
    """
    lines: List[str] = [
        "# chnroute for RouterOS — %s" % stamp,
        "# 用法: 上传到设备 /file 后执行  /import file-name=%s" % path.name,
        "# 提示: 条目较多时导入需要几十秒，属正常现象",
        "",
        "# ---- IPv4 ----",
        ':if ([:len [/ip firewall address-list find where list="%s"]] > 0) do={/ip firewall address-list remove [find where list="%s"]}' % (list_name, list_name),
        "/ip firewall address-list",
    ]
    for n in v4:
        lines.append('add list=%s address=%s comment="chnroute-%s"' % (list_name, n, stamp[:10]))
    if v6:
        lines += [
            "",
            "# ---- IPv6 ----",
            ':if ([:len [/ipv6 firewall address-list find where list="%s"]] > 0) do={/ipv6 firewall address-list remove [find where list="%s"]}' % (list_name, list_name),
            "/ipv6 firewall address-list",
        ]
        for n in v6:
            lines.append('add list=%s address=%s comment="chnroute-%s"' % (list_name, n, stamp[:10]))

    if isp_v4:
        for isp in sorted(isp_v4.keys()):
            nets = isp_v4[isp]
            if not nets:
                continue
            upper = isp.upper()
            lines += [
                "",
                "# ---- IPv4 / %s ----" % upper,
                ':if ([:len [/ip firewall address-list find where list="CN-%s"]] > 0) do={/ip firewall address-list remove [find where list="CN-%s"]}' % (upper, upper),
                "/ip firewall address-list",
            ]
            for n in nets:
                lines.append('add list=CN-%s address=%s comment="%s-%s"' % (upper, n, isp, stamp[:10]))
    if isp_v6:
        for isp in sorted(isp_v6.keys()):
            nets = isp_v6[isp]
            if not nets:
                continue
            upper = isp.upper()
            lines += [
                "",
                "# ---- IPv6 / %s ----" % upper,
                ':if ([:len [/ipv6 firewall address-list find where list="CN-%s"]] > 0) do={/ipv6 firewall address-list remove [find where list="CN-%s"]}' % (upper, upper),
                "/ipv6 firewall address-list",
            ]
            for n in nets:
                lines.append('add list=CN-%s address=%s comment="%s-%s"' % (upper, n, isp, stamp[:10]))
    write_text_file(path, lines)


def write_ros_pbr(path: Path, stamp: str, list_name: str) -> None:
    """RouterOS 策略路由（PBR）模板 —— 国内直连、其余走海外/代理线路。"""
    lines = [
        "# chnroute PBR 模板 for RouterOS — %s" % stamp,
        "# 本文件是【模板】，网关/接口/路由标记请按自己的拓扑改好再导。",
        "# 前提：ros 已导入 %s（list=%s），并已配置至少两条 WAN 出口。" % ("chnroute-ros.rsc", list_name),
        "",
        "# 1) 内网地址列表（按需改成自己的网段/VLAN）",
        "/ip firewall address-list",
        'add list=LAN address=192.168.88.0/24 comment="示例：改成本地 LAN"',
        "",
        "# 2) 标记国内目标流量 -> 走国内线路（in-wan）",
        "/ip firewall mangle",
        'add chain=prerouting src-address-list=LAN dst-address-list=%s action=mark-routing new-routing-mark=to-cn passthrough=yes comment="chnroute: 国内直连"' % list_name,
        "",
        "# 3) 其余流量 -> 走海外/代理线路（out-wan）",
        "/ip firewall mangle",
        'add chain=prerouting src-address-list=LAN dst-address-list=!%s dst-address-type=!local action=mark-routing new-routing-mark=to-overseas passthrough=yes comment="chnroute: 境外分流"' % list_name,
        "",
        "# 4) 路由表（check-gateway=ping + distance 做健康度感知与备份）",
        "/ip route",
        'add dst-address=0.0.0.0/0 gateway=192.168.1.1 routing-mark=to-cn distance=1 check-gateway=ping comment="示例：国内 WAN"',
        'add dst-address=0.0.0.0/0 gateway=192.168.2.1 routing-mark=to-cn distance=2 check-gateway=ping comment="示例：国内 WAN 备份"',
        'add dst-address=0.0.0.0/0 gateway=10.0.0.1 routing-mark=to-overseas distance=1 check-gateway=ping comment="示例：海外 WAN"',
        "",
        "# 5) 按运营商精细分流（需 --isp 生成 CN-CHINANET / CN-CMCC / CN-UNICOM 列表）",
        "# /ip firewall mangle",
        '# add chain=prerouting src-address-list=LAN dst-address-list=CN-CHINANET action=mark-routing new-routing-mark=to-telecom passthrough=yes',
        '# add chain=prerouting src-address-list=LAN dst-address-list=CN-CMCC action=mark-routing new-routing-mark=to-mobile passthrough=yes',
        '# add chain=prerouting src-address-list=LAN dst-address-list=CN-UNICOM action=mark-routing new-routing-mark=to-unicom passthrough=yes',
        "",
        "# 6) 热更新：上传新的 chnroute-ros.rsc 后执行",
        '# /import file-name=chnroute-ros.rsc',
    ]
    write_text_file(path, lines)


def write_diff_file(outdir: Path, v4: List[Net4]) -> Optional[str]:
    """与上一次的 chnroute-v4.txt 比较，输出差异摘要。"""
    cur = outdir / "chnroute-v4.txt"
    prev = outdir / "chnroute-v4.prev.txt"
    if not prev.exists() or not cur.exists():
        return None

    def load(p: Path) -> set:
        s = set()
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            try:
                s.add(ipaddress.ip_network(ln, strict=False))
            except ValueError:
                continue
        return s

    old = load(prev)
    new = load(cur)
    added = sorted(new - old, key=lambda n: int(n.network_address))
    removed = sorted(old - new, key=lambda n: int(n.network_address))
    lines = [
        "# chnroute diff — %s" % now_iso(),
        "# 上次: %d 段 / 本次: %d 段" % (len(old), len(new)),
        "# 新增: %d 段 (%d 地址)  移除: %d 段 (%d 地址)" % (
            len(added), count_ips(added), len(removed), count_ips(removed)),
        "",
        "## 新增（前 200 条）",
    ]
    lines += ["+ %s" % n for n in added[:200]]
    lines += ["", "## 移除（前 200 条）"]
    lines += ["- %s" % n for n in removed[:200]]
    write_text_file(outdir / "chnroute-diff.txt", lines)
    return "新增 %d 段 / 移除 %d 段" % (len(added), len(removed))


# ------------------------------------------------------------------ 应用层

def _rewrite_restore(text: str, old_name: str, new_name: str) -> str:
    """把 restore 文件里的集合名从基础名改写成影子名。"""
    out = []
    for ln in text.splitlines():
        parts = ln.split(None, 2)
        if len(parts) >= 2 and parts[0] in ("create", "flush", "add", "del", "test") and parts[1] == old_name:
            parts[1] = new_name
            ln = " ".join(parts)
        out.append(ln)
    return "\n".join(out) + "\n"


def apply_ipset(base4: str, base6: str, outdir: Path, apply_v6: bool,
                quiet: bool, dry_run: bool) -> None:
    """
    影子集合 + swap：新数据先灌进 <name>-tmp，再与线上集合原子交换。
    整个过程线上集合始终是完整的可用集合，不会出现「空窗期放行/拦截错乱」。
    """
    if not which("ipset"):
        die("未找到 ipset 命令，请先安装：apt install ipset / yum install ipset", 3)
    if not is_root() and not dry_run:
        die("--apply 需要 root 权限", 3)

    jobs = [(base4, "inet", outdir / "chnroute-ipset.v4", 4)]
    if apply_v6:
        jobs.append((base6, "inet6", outdir / "chnroute-ipset.v6", 6))

    for base, family, path, ver in jobs:
        if not path.exists():
            warn("缺少 %s，跳过 IPv%d" % (path.name, ver))
            continue
        tmp = "%s-tmp" % base
        content = path.read_text(encoding="utf-8")
        rewritten = _rewrite_restore(content, base, tmp)
        n_lines = rewritten.count("\nadd ")

        if dry_run:
            log("[dry-run] 将以 %d 条记录替换 ipset %s（family %s）" % (n_lines, base, family), quiet)
            continue

        # 1) 确保线上集合存在（首次运行时创建）
        run(["ipset", "create", base, "hash:net", "family", family,
             "hashsize", "8192", "maxelem", "1048576"], check=False)
        # 2) 清掉可能残留的影子集合，重新灌入
        run(["ipset", "destroy", tmp], check=False)
        run(["ipset", "create", tmp, "hash:net", "family", family,
             "hashsize", "8192", "maxelem", "1048576"], check=False)
        run(["ipset", "restore", "-exist"], stdin_text=rewritten)
        cnt = run(["ipset", "list", tmp], check=False)[1].count("\n")
        # 3) 原子交换 + 清理
        run(["ipset", "swap", tmp, base])
        run(["ipset", "destroy", tmp], check=False)
        log("已应用 ipset %s：%d 条（family %s，原子 swap 完成）" % (base, n_lines, family), quiet)


def apply_nft(path: Path, quiet: bool, dry_run: bool) -> None:
    """nft -f 整文件是一个事务，加载即原子替换同名 set。"""
    if not which("nft"):
        die("未找到 nft 命令，请先安装 nftables", 3)
    if not path.exists():
        die("缺少 %s" % path.name, 3)
    if dry_run:
        log("[dry-run] 将执行 nft -f %s" % path, quiet)
        return
    if not is_root():
        die("--apply 需要 root 权限", 3)
    code, out = run(["nft", "-f", str(path)], check=False)
    if code != 0:
        die("nft 加载失败：\n%s" % out, 1)
    log("已应用 nftables set：%s（原子事务）" % path.name, quiet)


# ------------------------------------------------------------------ 状态

def load_state(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_state(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


# ------------------------------------------------------------------ 主流程

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="update_chnroute.py",
        description="中国大陆 IP 段（chnroute）每日自动更新 + 多平台落地",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--outdir", default="/var/lib/chnroute", help="输出目录（默认 /var/lib/chnroute）")
    p.add_argument("--country", default="CN", help="国家代码，默认 CN")
    p.add_argument("--sources", choices=["all", "apnic", "aggregate"], default="all",
                   help="数据源组合：all=APNIC+聚合源(默认)；apnic=仅 APNIC 官方(最权威但慢)；"
                        "aggregate=仅镜像聚合源(快，秒级，国内网络推荐)")
    p.add_argument("--no-ipv6", action="store_true", help="不处理 IPv6")
    p.add_argument("--isp", action="store_true",
                   help="额外拉取并生成电信/移动/联通/教育网等 ISP 分组（BGP 数据源）")
    p.add_argument("--format", default="txt,ipset",
                   help="输出格式，逗号分隔：txt,ipset,nft,ros（默认 txt,ipset）")
    p.add_argument("--set-prefix", default="cn",
                   help="ipset 集合前缀，默认 cn -> cn4 / cn6")
    p.add_argument("--nft-table", default="inet filter", help="nftables 表名，默认 'inet filter'")
    p.add_argument("--nft-set4", default="cn4", help="nftables IPv4 set 名，默认 cn4")
    p.add_argument("--nft-set6", default="cn6", help="nftables IPv6 set 名，默认 cn6")
    p.add_argument("--ros-list", default="CN", help="RouterOS address-list 名，默认 CN")
    p.add_argument("--ros-pbr", action="store_true", help="额外生成 RouterOS 策略路由模板")
    p.add_argument("--apply", action="store_true",
                   help="生成后原子写入内核（ipset swap 或 nft -f），需要 root")
    p.add_argument("--backend", choices=["ipset", "nft"], default="ipset",
                   help="--apply 的落地后端，默认 ipset")
    p.add_argument("--force", action="store_true", help="跳过完整性守卫（危险，慎用）")
    p.add_argument("--timeout", type=int, default=60, help="单次 HTTP 超时秒数，默认 60")
    p.add_argument("--retries", type=int, default=2, help="单 URL 重试次数，默认 2（多镜像本身即兜底）")
    p.add_argument("--quiet", action="store_true", help="减少输出（适合 cron/systemd）")
    p.add_argument("--log-file", default=None,
                   help="同时把日志以 UTF-8 追加写入该文件（如 /var/log/chnroute/update.log）")
    p.add_argument("--dry-run", action="store_true", help="只演示，不真正写内核")
    p.add_argument("--version", action="version", version="update_chnroute.py %s" % VERSION)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    global LOG_FH
    args = build_parser().parse_args(argv)
    quiet = args.quiet
    if args.log_file:
        try:
            Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
            LOG_FH = open(args.log_file, "a", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("无法打开日志文件 %s: %s\n" % (args.log_file, exc))
    formats = {x.strip().lower() for x in args.format.split(",") if x.strip()}
    if "all" in formats:
        formats = {"txt", "ipset", "nft", "ros"}

    outdir = Path(args.outdir).expanduser()
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        die("无法创建输出目录 %s（权限不足，试试 sudo）" % outdir, 3)

    stamp = now_iso()
    log("chnroute 更新开始 —— %s（目录 %s）" % (stamp, outdir), quiet)

    # 1) 抓取
    feeds = build_feeds(args.isp, args.sources)
    log("数据源 %d 个（模式 %s），开始并发抓取…" % (len(feeds), args.sources), quiet)
    raw, source_status = fetch_all(feeds, args.country, args.timeout, args.retries, quiet)
    if not raw.v4:
        die("未能解析出任何 IPv4 网段，放弃本次更新（保留旧数据）", 2)

    # 2) 聚合成最小 CIDR 集合
    v4 = collapse_v4(raw.v4)
    v6 = collapse_v6(raw.v6) if not args.no_ipv6 else []
    log("聚合完成：IPv4 %d 段（原始 %d）→ %d 地址；IPv6 %d 段" % (
        len(v4), len(raw.v4), count_ips(v4), len(v6)), quiet)

    isp_v4: Dict[str, List[Net4]] = {}
    isp_v6: Dict[str, List[Net6]] = {}
    if args.isp:
        for isp, nets in raw.isp_v4.items():
            isp_v4[isp] = collapse_v4(nets)
        for isp, nets in raw.isp_v6.items():
            if not args.no_ipv6:
                isp_v6[isp] = collapse_v6(nets)
        log("ISP 分组：%s" % ", ".join("%s=%d" % (k, len(v)) for k, v in sorted(isp_v4.items())), quiet)

    # 3) 完整性守卫
    state_path = outdir / "state.json"
    prev_state = load_state(state_path)
    ok, reasons = sanity_check(v4, v6, prev_state, args.force, quiet)
    if not ok:
        warn("完整性守卫已拦截本次更新，内核与文件均未改动。确认数据源无误可用 --force 强制更新。")
        return 2

    # 4) 备份旧文件用于 diff
    for name in ("chnroute-v4.txt", "chnroute-v6.txt"):
        cur = outdir / name
        if cur.exists():
            shutil.copy2(str(cur), str(outdir / (name.replace(".txt", ".prev.txt"))))

    # 5) 输出
    out_txt(outdir, stamp, v4, v6, source_status)
    if "ipset" in formats:
        write_ipset_file(outdir / "chnroute-ipset.v4", "%s4" % args.set_prefix, "inet", v4)
        if v6:
            write_ipset_file(outdir / "chnroute-ipset.v6", "%s6" % args.set_prefix, "inet6", v6)
    if "nft" in formats:
        write_nft_file(outdir / "chnroute.nft", stamp, args.nft_table,
                       args.nft_set4, v4, args.nft_set6, v6)
    if "ros" in formats:
        write_ros_rsc(outdir / "chnroute-ros.rsc", stamp, args.ros_list, v4, v6, isp_v4, isp_v6)
        if args.ros_pbr:
            write_ros_pbr(outdir / "chnroute-ros-pbr.rsc", stamp, args.ros_list)
    if args.isp:
        for isp, nets in isp_v4.items():
            write_text_file(outdir / ("cn-%s.txt" % isp), [str(n) for n in nets])

    diff_summary = write_diff_file(outdir, v4)

    # 6) 落地内核
    if args.apply:
        if args.backend == "nft":
            apply_nft(outdir / "chnroute.nft", quiet, args.dry_run)
        else:
            apply_ipset("%s4" % args.set_prefix, "%s6" % args.set_prefix,
                        outdir, bool(v6), quiet, args.dry_run)
    else:
        log("未指定 --apply，仅生成数据文件（内核未改动）", quiet)

    # 7) 状态
    save_state(state_path, {
        "version": VERSION,
        "updated_at": stamp,
        "country": args.country,
        "v4": {"entries": len(v4), "ips": count_ips(v4)},
        "v6": {"entries": len(v6), "ips": count_ips(v6)},
        "sources": source_status,
        "formats": sorted(formats),
        "diff": diff_summary,
        "applied": bool(args.apply and not args.dry_run),
    })

    log("完成。v4=%d 段/%d 地址，v6=%d 段%s%s" % (
        len(v4), count_ips(v4), len(v6),
        "，差异：" + diff_summary if diff_summary else "",
        "，已写入内核" if (args.apply and not args.dry_run) else ""), quiet)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        die("被用户中断", 3)
    except RuntimeError as exc:
        die(str(exc), 1)
