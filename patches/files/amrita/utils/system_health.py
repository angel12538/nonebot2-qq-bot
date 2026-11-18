import os
import platform
import shutil
import sys
import time

import psutil


_NET_SKIP = (
    "loopback", "meta", "clash", "mihomo", "sing-box", "tun", "tap", "vmware", "vethernet", "virtualbox",
    "hyper-v", "wsl", "docker", "veth", "br-", "tailscale", "zerotier", "wireguard", "bluetooth", "蓝牙",
    "本地连接*", "isatap", "teredo",
)
_net_last: tuple[float, int, int] | None = None


psutil.cpu_percent(interval=None)


def _physical_net_io() -> tuple[int, int]:
    """只累计在用的真实网卡。代理的虚拟网卡（Meta/TUN）、虚拟机网卡、回环上的流量
    要么是同一份流量再算一遍，要么根本没出本机，加进去会让数字虚高。"""
    stats = psutil.net_if_stats()
    sent = recv = 0
    matched = False
    for name, counters in psutil.net_io_counters(pernic=True).items():
        info = stats.get(name)
        low = name.lower()
        if info is None or not info.isup or low == "lo" or any(key in low for key in _NET_SKIP):
            continue
        matched = True
        sent += counters.bytes_sent
        recv += counters.bytes_recv
    if not matched:
        total = psutil.net_io_counters()
        return total.bytes_sent, total.bytes_recv
    return sent, recv


def _net_rates(sent: int, recv: int) -> tuple[float, float]:
    """和上一次采样比，算出每秒上行/下行字节数；第一次没有可比的，返回 0。"""
    global _net_last
    now = time.monotonic()
    rates = (0.0, 0.0)
    if _net_last is not None and now > _net_last[0]:
        span = now - _net_last[0]
        rates = (max(0, sent - _net_last[1]) / span, max(0, recv - _net_last[2]) / span)
    _net_last = (now, sent, recv)
    return rates


def _disks() -> list[dict]:
    """列出每个可写的本地分区（跳过光驱和只读卷），并标出程序所在的那个（current）。
    面板「Bot 状态」页只显示 current 那一个。"""
    here = os.path.normcase(os.path.abspath(os.getcwd()))
    out: list[dict] = []
    seen: set[str] = set()
    for part in psutil.disk_partitions(all=False):
        opts = part.opts.lower().split(",")
        if not part.fstype or "cdrom" in opts or "ro" in opts or "readonly" in opts or part.device in seen:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        if not usage.total:
            continue
        seen.add(part.device)
        out.append(
            {
                "mount": part.mountpoint.rstrip("\\") or part.mountpoint,
                "total": usage.total,
                "used": usage.used,
                "percent": round(usage.used / usage.total * 100, 1),
                "current": False,
                "_root": os.path.normcase(part.mountpoint),
            }
        )

    owners = [d for d in out if here.startswith(d["_root"])]
    if owners:
        max(owners, key=lambda d: len(d["_root"]))["current"] = True
    for d in out:
        del d["_root"]
    return out


def calculate_system_usage() -> dict:
    system_name = platform.system()
    system_version = platform.version()
    python_version = sys.version
    memory = psutil.virtual_memory()
    cpu_usage = psutil.cpu_percent(interval=None)
    logical_cores = psutil.cpu_count(logical=True)
    disk_usage_origin = shutil.disk_usage(os.getcwd())
    disk_usage = (disk_usage_origin.used / disk_usage_origin.total) * 100
    net_sent, net_recv = _physical_net_io()
    sent_rate, recv_rate = _net_rates(net_sent, net_recv)

    return {
        "cpu_usage": cpu_usage,
        "memory_usage": memory.percent,
        "disk_usage": float(f"{disk_usage:.2f}"),
        "system_name": system_name,
        "system_version": system_version,
        "python_version": python_version,
        "logical_cores": logical_cores,
        "network_io": {
            "sent": net_sent,
            "received": net_recv,
            "sent_rate": round(sent_rate, 1),
            "recv_rate": round(recv_rate, 1),
        },
        "disks": _disks(),
    }


def calculate_system_health() -> dict:
    """
    计算系统健康值

    Returns:
        dict: 包含各项指标健康度和总体健康值的字典
    """

    cpu_percent = psutil.cpu_percent(interval=None)
    cpu_health = _calculate_cpu_health(cpu_percent)


    memory_info = psutil.virtual_memory()
    memory_health = _calculate_memory_health(memory_info.percent)


    disk_info = psutil.disk_usage("/")
    disk_percent = (disk_info.used / disk_info.total) * 100
    disk_health = _calculate_disk_health(disk_percent)


    process_health = _calculate_process_health()


    load_health = _calculate_load_health()


    overall_health = (
        cpu_health * 0.25
        + memory_health * 0.25
        + disk_health * 0.25
        + process_health * 0.20
        + load_health * 0.05
    )


    health_level = _get_health_level(overall_health)

    return {
        "overall_health": round(overall_health, 2),
        "health_level": health_level,
        "details": {
            "cpu": {"usage": round(cpu_percent, 2), "health": cpu_health},
            "memory": {"usage": round(memory_info.percent, 2), "health": memory_health},
            "disk": {"usage": round(disk_percent, 2), "health": disk_health},
            "process": {"health": process_health},
            "load": {"health": load_health},
        },
    }


def _calculate_cpu_health(cpu_percent: float) -> float:
    """计算CPU健康度"""
    if cpu_percent <= 70:
        return 100.0
    elif cpu_percent <= 85:
        return 100.0 - (cpu_percent - 70) * 2
    else:
        health = 70.0 - (cpu_percent - 85) * 3
        return max(0.0, health)


def _calculate_memory_health(memory_percent: float) -> float:
    """计算内存健康度"""
    if memory_percent <= 80:
        return 100.0
    elif memory_percent <= 90:
        return 100.0 - (memory_percent - 80) * 2
    else:
        health = 80.0 - (memory_percent - 90) * 4
        return max(0.0, health)


def _calculate_disk_health(disk_percent: float) -> float:
    """计算磁盘健康度"""
    if disk_percent <= 85:
        return 100.0
    elif disk_percent <= 95:
        return 100.0 - (disk_percent - 85) * 1.5
    else:
        health = 85.0 - (disk_percent - 95) * 5
        return max(0.0, health)


def _calculate_process_health() -> float:
    """计算进程健康度（简化实现）"""
    try:

        process_count = len(list(psutil.process_iter()))


        if process_count < 500:
            return 100.0
        elif process_count < 1000:
            return 90.0
        else:
            return 80.0
    except Exception:
        return 85.0


def _calculate_load_health() -> float:
    """计算系统负载健康度"""
    try:

        load_avg = os.getloadavg()
        load = load_avg[0]


        cpu_count = psutil.cpu_count()


        if cpu_count is None:
            cpu_count = 1

        if load <= cpu_count * 0.7:
            return 100.0
        elif load <= cpu_count:
            return 100.0 - (load - cpu_count * 0.7) * 20
        else:
            health = 85.0 - (load - cpu_count) * 15
            return max(0.0, health)
    except Exception:
        return 85.0


def _get_health_level(health: float) -> str:
    """根据健康值确定健康等级"""
    if 90 <= health <= 100:
        return "Excellent"
    elif 75 <= health < 90:
        return "Good"
    elif 60 <= health < 75:
        return "Fair"
    elif 40 <= health < 60:
        return "Poor"
    else:
        return "Critical"
