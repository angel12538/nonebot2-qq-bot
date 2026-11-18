# NoneBot2 QQ Bot

基于 NoneBot2、OneBot V11 和 Amrita 的 QQ 聊天机器人，包含群聊和私聊白名单、百度千帆与必应网页搜索、会话摘要、记忆和对话追踪。达到 20 条历史消息时触发压缩，保留最近一轮原文，超过 7 天的摘要会过期。网页搜索工具结果供当轮使用。

## 安装

使用 Windows 和 Python 3.13。在项目目录运行以下 PowerShell 命令。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe patches\apply.py
.\.venv\Scripts\python.exe configure.py
```

## 配置和启动

1. 在 `config/chat/models/default.json` 填写模型 ID、OpenAI 兼容网关地址和 API Key。
2. 在 `config/qianwen/config.toml` 的 `whitelist.groups` 或 `whitelist.private` 填写允许交互的群号或 QQ 号。管理员 QQ 号可填入 `admin.superusers` 和 `.env` 的 `SUPERUSERS`。
3. 在 OneBot V11 实现中配置反向 WebSocket `ws://127.0.0.1:8080/onebot/v11/ws`，令牌使用 `.env` 中的 `ONEBOT_ACCESS_TOKEN`。在 `qq_tools/config.json` 填写 OneBot HTTP API 地址及相应令牌。
4. 运行 `.\.venv\Scripts\python.exe system_python.py bot`。使用 `.env` 生成的账号密码登录本机 Amrita WebUI。网页搜索设置位于 `http://127.0.0.1:8080/qianwen-search`，可测试百度千帆和必应。千帆 Key 可通过面板保存，或在本地 `.env` 设置 `BAIDU_API_KEY`。

本地配置文件、聊天数据、日志和凭据由 `.gitignore` 排除。`configure.py` 只创建缺失的文件。第三方源码出处和对应许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
