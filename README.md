# NoneBot2 QQ Bot

基于 NoneBot2、OneBot V11 和 Amrita 的 QQ 聊天机器人。支持白名单群聊和私聊、按会话管理上下文与记忆、网页搜索、QQ 消息和表情工具，以及用于配置和排查问题的 WebUI。

## 功能

### 群聊和私聊

- **白名单与管理员**：分别配置允许处理的群和私聊账号。管理员可用 `/静默` 开启静默，用 `/恢复` 关闭静默。静默期间仍记录白名单聊天，管理员可以继续与机器人对话。
- **聊天节奏**：支持被 @、叫名字、提问或接续话题时触发，可配置跟进窗口、观察期、触发概率和频率。冷场主动接话可以单独开启，设置时间段、冷却条件和每日次数。
- **上下文和回复**：支持等待对方说完、合并短时间内的多条输入、预览最近未读消息。回复可引用本群消息或 @ 本群成员，分条发送时可按字数停顿，并对每个会话限频。
- **人设与模型**：可设置群聊和私聊提示词，使用模型预设配置网关、模型 ID、上下文预算和输出预算。读图需要支持图像输入的模型。
- **出站保护**：检测并拦截疑似凭据或本机路径的回复，可对手机号、邮箱等个人信息打码，并在拦截时通知管理员。

### 会话摘要与长期记忆

| 功能 | 行为 |
| --- | --- |
| 会话摘要 | 示例配置在历史达到 20 条或达到 Token 阈值时触发，前提是有较早的消息可压缩。保留最近一轮原文，将更早的消息与同日已有摘要合并。摘要按来源时间保留 7 天。 |
| 会话管理 | 在权限和配置允许时，用 `/session` 查看、归档或恢复会话，手动压缩上下文，查看或清空摘要。 |
| 长期记忆 | 按会话保存话题、念头、印象和事实，可以搜索、编辑或删除。话题保留 48 小时，念头保留 7 天，印象和事实可持续保留。 |
| 晚间自动提炼 | 启用后默认在北京时间 23:00 处理白名单会话在前后两次计划时间之间的新聊天文本。每会话每天最多提炼一次，可设置素材条数、字符预算和产出上限。模型必须引用本批消息作为依据，提炼结果写入本地记忆，不会发到 QQ。 |

晚间提炼素材保留 8 天。`configure.py` 生成的配置默认关闭自动提炼，可在「配置管理 → qianwen → 晚间自动记忆」开启。提炼使用当前模型预设，会将所选聊天和已有记忆发送给配置的模型网关。

### 网页搜索和 QQ 工具

| 工具组 | 能做什么 |
| --- | --- |
| 联网 | `search_web` 搜索网页，返回标题、链接与摘要。`fetch_url` 读取网页文字，`fetch_video` 读取 B 站视频信息、公开热评及可用字幕。 |
| 消息与媒体 | `get_members` 查询群成员。`get_message_detail`、`get_message_images` 读取会话内指定消息或图片。`get_forward_msg` 展开合并转发。`get_self_image` 获取机器人头像。 |
| 表情和拍一拍 | `list_stickers`、`get_sticker_image` 浏览图库，`send_sticker`、`send_poke` 发出表情或拍一拍。`collect_sticker`、`sticker_note`、`set_sticker_remark`、`drop_sticker` 用于收藏、标注或移出图库。QQ 收藏及备注功能取决于 OneBot 实现和开关。 |
| 节奏与反馈 | `wait_for_messages` 等待新消息或等对方说完。`set_wake` 调节活跃、潜水或等待某人等状态。`report_feedback` 将反馈记录到本地面板。 |
| 记忆 | `memory_save`、`memory_search`、`memory_forget`、`memory_clear` 保存、检索、删除或按分类清理当前会话的记忆。 |

搜索支持**百度千帆**和**必应**。千帆启用且有可用 Key 时优先使用，失败或无结果时可切到已启用的必应。搜索和网页正文工具的调用及结果供当轮使用，后续请求会过滤对应的历史工具记录。

### WebUI

启动后访问本机 `http://127.0.0.1:8080/`，使用 `.env` 中的 `WEBUI_USER_NAME` 和 `WEBUI_PASSWORD` 登录。

| 面板入口 | 用途 |
| --- | --- |
| 配置管理 → qianwen | 管理白名单、管理员、聊天节奏、表情和图片限制、晚间提炼、追踪和出站设置。 |
| 聊天管理 → 提示词预设 | 分别新建或编辑群聊和私聊的人设模板。 |
| 聊天管理 → 网页搜索 | 设置百度千帆 Key，独立启停和测试两个搜索渠道。也可直接访问 `/qianwen-search`。 |
| 聊天管理 → 她的记忆 | 按会话和分类查看、搜索、新增、编辑或清理记忆。也可直接访问 `/qianwen-memory`。 |
| 系统信息 → 对话追踪 | 查看触发原因、模型请求、工具调用、发送结果、报错和分步耗时。需先启用追踪，生成配置默认关闭。 |
| 系统信息 → 她的反馈 | 查看机器人通过反馈工具提交的记录。也可直接访问 `/qianwen-feedback`。 |

## 安装与启动

准备 **Windows、Python 3.13**，以及提供 OneBot V11 反向 WebSocket 和 HTTP API 的 QQ 客户端。在下载的项目目录运行 PowerShell 命令。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe patches\apply.py
.\.venv\Scripts\python.exe configure.py
```

`patches/apply.py` 会将仓库中的补丁应用到虚拟环境中与版本匹配的依赖。`configure.py` 只创建缺失的配置文件，首次运行会生成 WebUI 密码和 OneBot 接入令牌。

1. **模型**：在 `config/chat/models/default.json` 填写模型 ID、OpenAI 兼容网关地址和 API Key。调用 QQ 工具需要所选模型支持工具调用。
2. **白名单**：在 `config/qianwen/config.toml` 的 `whitelist.groups` 和 `whitelist.private` 填写允许交互的群号和 QQ 号。需要聊天管理权限时填写 `admin.superusers`。生成后的白名单与管理员列表均为空。
3. **OneBot**：将客户端的反向 WebSocket 指向 `ws://127.0.0.1:8080/onebot/v11/ws`，使用 `.env` 的 `ONEBOT_ACCESS_TOKEN`。再在 `qq_tools/config.json` 中配置客户端的 HTTP API 地址和令牌，供 MCP 工具读取消息、发送表情等。
4. **启动**：运行下面的命令。登录面板后，可按需开启主动接话、晚间提炼和对话追踪。必应默认开启，百度千帆需要先提供 Key 再启用。千帆 Key 可在面板保存，也可在本地 `.env` 设置 `BAIDU_API_KEY`。

```powershell
.\.venv\Scripts\python.exe system_python.py bot
```

已有部署更新时，将仓库更新到最新提交后重启机器人，即可加载 `guards/nightly_memory.py` 中的晚间提炼修复。

## 修改人设提示词

登录 WebUI，进入「聊天管理 → 提示词预设」，选择群聊或私聊，编辑当前使用的模板并保存。两种场景默认都选中 `default`，可以分别设置角色身份、语气、称呼和回复示例。

也可以直接用文本编辑器修改文件。下列路径均相对于项目根目录，提示词以 UTF-8 编码保存。

| 场景 | 默认文件 | `config/chat/config.toml` 中的模板选项 |
| --- | --- | --- |
| 群聊 | `config/chat/group_prompts/default.txt` | `group_prompt_character` |
| 私聊 | `config/chat/private_prompts/default.txt` | `private_prompt_character` |

首次启动时，机器人会在空的提示词目录中创建 `default.txt`。修改当前模板后，等日志出现「提示词文件已重载」，后续对话就会使用新内容。需要切换模板时，在对应目录新建 `角色名.txt`，把上表对应的配置项改为文件名去掉 `.txt` 后的部分，然后重启机器人。

例如，群聊使用 `config/chat/group_prompts/friendly.txt`，私聊继续用默认模板，可将 `config/chat/config.toml` 的顶层选项设置如下。

```toml
group_prompt_character = "friendly"
private_prompt_character = "default"
```

模板中可使用 `{self_id}`、`{user_id}`、`{user_name}`，机器人会在聊天时填入自己的 QQ 号、当前发言人的 QQ 号和昵称。篇幅较长的示例可以放在带 `〔完整〕` 标记的一级或二级标题下，由 `config/qianwen/config.toml` 中的 `persona.full_every_n` 控制完整人设的发送频率。

需要拆分规则时，可在项目根目录下创建 `persona/` 并按需添加以下 UTF-8 文件。规则加载由 `config/qianwen/config.toml` 中的 `persona.rules_enabled` 控制，默认开启。保存后，下一次构建提示词时会读取新内容。

| 文件 | 用途 |
| --- | --- |
| `persona/rules.md` | 聊天行为规则，例如如何接话、何时保持静默。 |
| `persona/tools.md` | 工具判断规则，例如使用条件、参数和操作限制。 |
| `persona/safety.md` | 聊天与工具判断共用的安全边界。 |

## 项目文件

| 路径 | 内容 |
| --- | --- |
| [guards/](guards/) | 白名单、会话节奏、晚间提炼、记忆与搜索面板、对话追踪。 |
| [qq_tools/](qq_tools/) | QQ 动作、联网搜索、记忆文件管理和 MCP 服务。 |
| [patches/](patches/) | Amrita 等依赖的源码补丁和应用脚本。 |
| [examples/](examples/) | 安装脚本使用的配置示例。 |

实际 Key、QQ 白名单、聊天记录、日志和记忆保存在运行环境中，运行文件由 `.gitignore` 排除。第三方源码出处和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
