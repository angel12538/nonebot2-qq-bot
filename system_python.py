import os
from pathlib import Path
import runpy
import sys


ROOT = Path(__file__).resolve().parent
ENTRIES = {"bot": ROOT / "bot.py", "qq-tools": ROOT / "qq_tools" / "server.py"}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ENTRIES:
        raise SystemExit("用法：python system_python.py {bot|qq-tools}")
    entry = ENTRIES[sys.argv[1]]
    os.chdir(ROOT)
    os.environ["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", "")))
    sys.path.insert(0, str(ROOT))
    sys.argv = [str(entry), *sys.argv[2:]]
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
