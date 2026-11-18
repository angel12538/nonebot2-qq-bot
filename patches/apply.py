from importlib.metadata import distribution
from pathlib import Path
from shutil import copyfile
import sys


FILES = Path(__file__).resolve().parent / "files"
VERSIONS = {
    "amrita": "1.16.0",
    "amrita_core": "1.4.0",
    "nonebot_plugin_amrita": "0.5.0",
    "nonebot_plugin_uniconf": "0.1.3",
}


def main():
    if sys.prefix == sys.base_prefix:
        raise SystemExit("请在项目的 Python 虚拟环境中应用补丁。")
    replacements = {}
    for package, version in VERSIONS.items():
        installed = distribution(package)
        if installed.version != version:
            raise SystemExit(f"{package} 需要 {version}，当前安装的是 {installed.version}。")
        root = Path(installed.locate_file("")).resolve()
        if not root.is_relative_to(Path(sys.prefix).resolve()):
            raise SystemExit(f"{package} 必须安装在当前虚拟环境中。")
        for source in sorted((FILES / package).rglob("*.py")):
            relative = source.relative_to(FILES)
            destination = (root / relative).resolve()
            if not destination.is_relative_to(root / package) or not destination.parent.is_dir():
                raise SystemExit(f"无法应用补丁 {relative.as_posix()}。")
            compile(source.read_bytes(), relative.as_posix(), "exec")
            replacements[source] = destination
    for source, destination in replacements.items():
        copyfile(source, destination)
    print(f"已应用 {len(replacements)} 个源码补丁。")


if __name__ == "__main__":
    main()
