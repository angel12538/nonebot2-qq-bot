import json
from pathlib import Path
import secrets
import sys
import tomllib

from dotenv import dotenv_values
import tomli_w


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"


def write_missing(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(text)
    except FileExistsError:
        pass


def main():
    env_path = ROOT / ".env"
    defaults = (ROOT / ".env.example").read_text(encoding="utf-8")
    defaults = defaults.replace("ONEBOT_ACCESS_TOKEN=\n", f"ONEBOT_ACCESS_TOKEN={secrets.token_urlsafe(24)}\n")
    defaults = defaults.replace("WEBUI_PASSWORD=\n", f"WEBUI_PASSWORD={secrets.token_urlsafe(24)}\n")
    write_missing(env_path, defaults)

    tools = json.loads((EXAMPLES / "qq-tools.json").read_text(encoding="utf-8"))
    tools["token"] = dotenv_values(env_path).get("ONEBOT_ACCESS_TOKEN") or ""
    write_missing(ROOT / "qq_tools" / "config.json", json.dumps(tools, indent=2) + "\n")

    chat = tomllib.loads((EXAMPLES / "chat.toml").read_text(encoding="utf-8"))
    command = [sys.executable, "-I", "-X", "utf8", "-B", str(ROOT / "system_python.py"), "qq-tools"]
    chat["core"]["function_config"]["agent_mcp_server_scripts"] = ["stdio://" + json.dumps(command)]
    write_missing(ROOT / "config" / "chat" / "config.toml", tomli_w.dumps(chat))
    write_missing(ROOT / "config" / "qianwen" / "config.toml", (EXAMPLES / "qianwen.toml").read_text(encoding="utf-8"))
    write_missing(ROOT / "config" / "chat" / "models" / "default.json", (EXAMPLES / "model.json").read_text(encoding="utf-8"))
    print("本地运行配置已就绪。请填写模型和 QQ 白名单，并配置 OneBot 接入。")


if __name__ == "__main__":
    main()
