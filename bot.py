import nonebot

nonebot.init()


nonebot.load_plugin("nonebot_plugin_localstore")

import amrita  # noqa: E402

amrita.init()
amrita.load_plugins()



if nonebot.get_plugin("whitelist") is None:
    raise SystemExit("白名单插件 guards.whitelist 没有加载，机器人不启动；请检查 pyproject.toml 与 guards/whitelist.py")

if __name__ == "__main__":
    nonebot.run()
