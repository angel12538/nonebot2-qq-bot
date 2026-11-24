import os
from pathlib import Path
import runpy
import sys

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent
ENTRIES = {"bot": ROOT / "bot.py", "qq-tools": ROOT / "qq_tools" / "server.py"}


def _configure_proxy_bypass() -> None:
    """把 .env 中的 NO_PROXY 合并进进程环境，供 HTTP 客户端使用。"""
    configured = dotenv_values(ROOT / ".env", encoding="utf-8-sig").get("NO_PROXY")
    proxy_hosts = [
        host.strip()
        for name in ("NO_PROXY", "no_proxy")
        for host in os.environ.get(name, "").split(",")
        if host.strip()
    ]
    if configured:
        proxy_hosts.extend(
            host.strip() for host in configured.split(",") if host.strip()
        )
    if not proxy_hosts:
        return
    bypass = ",".join(dict.fromkeys(proxy_hosts))
    os.environ["NO_PROXY"] = bypass
    os.environ["no_proxy"] = bypass


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ENTRIES:
        raise SystemExit("用法：python system_python.py {bot|qq-tools}")
    entry = ENTRIES[sys.argv[1]]
    os.chdir(ROOT)
    _configure_proxy_bypass()
    os.environ["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", "")))
    sys.path.insert(0, str(ROOT))
    sys.argv = [str(entry), *sys.argv[2:]]
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
