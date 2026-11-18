"""机器人扩展设置：在 AmritaBot 面板「配置管理 → qianwen」里修改，保存后自动生效。

文件位置 config/qianwen/config.toml（由 nonebot_plugin_uniconf 管理，面板读写同一个文件）。

本模块同时被两个进程使用：
- 机器人进程：guards/whitelist.py 在启动时把 QianwenSettings 注册进面板；
  site-packages 里的补丁（上下文、防抖、转发、出站守卫）通过 value() 读取；
- qq_tools 工具进程：直接读同一个文件。
读取按文件修改时间缓存，改完下一次调用就是新值。文件读不了时一律用默认值，
只有白名单例外：读不了就当名单为空（全部拒绝）。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent
OWNER = "qianwen"
CONFIG_PATH = ROOT / "config" / OWNER / "config.toml"


def _qq_numbers(value):
    """群号/QQ 号存成文本列表：面板的列表编辑器只认文本（保存时会对每项调用 trim）。
    旧文件里的数字写法照样接受；空项忽略，不是纯数字就报错，面板会提示保存失败。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        value = [value]
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        if not text.isdigit():
            raise ValueError(f"「{text}」不是纯数字的群号/QQ 号")
        if text not in out:
            out.append(text)
    return out


class WhitelistSettings(BaseModel):
    groups: list[str] = Field(default=[], description="允许读取和回复的群号；不在这里的群，消息不记录、不回复")
    private: list[str] = Field(default=[], description="允许私聊的 QQ 号；不在这里的人私聊机器人不会被处理")

    @field_validator("groups", "private", mode="before")
    @classmethod
    def _check_numbers(cls, value):
        return _qq_numbers(value)


class AdminSettings(BaseModel):
    superusers: list[str] = Field(
        default=[],
        description=(
            "机器人管理员的 QQ 号：能用 /静默、/session、/prompt 等管理命令，被拦截的消息会私聊通知他们。"
            "保存后立即生效，不用重启。留空就没有机器人管理员（面板不受影响）。"
            "权限系统里有 lp.admin 权限的人也算管理员"
        ),
    )

    @field_validator("superusers", mode="before")
    @classmethod
    def _check_numbers(cls, value):
        return _qq_numbers(value)


class StickerSettings(BaseModel):
    send_per_minute: int = Field(default=2, ge=0, le=60, description="每个会话每分钟最多发几张表情（0=不限）")
    send_per_hour: int = Field(default=20, ge=0, le=1000, description="每个会话每小时最多发几张表情（0=不限）")
    collect_per_minute: int = Field(default=2, ge=0, le=60, description="全账号每分钟最多收藏几张表情（0=不限）")
    collect_per_hour: int = Field(default=10, ge=0, le=1000, description="全账号每小时最多收藏几张表情（0=不限）")
    view_per_minute: int = Field(default=6, ge=0, le=60, description="每个会话每分钟最多看几次表情图（看图会消耗图片 token，0=不限）")
    view_per_hour: int = Field(default=60, ge=0, le=1000, description="每个会话每小时最多看几次表情图（0=不限）")
    qq_sync_seconds: int = Field(default=60, ge=10, le=3600, description="QQ 收藏表情的同步间隔（秒）")
    allow_private: bool = Field(default=False, description="私聊里是否允许发表情和拍一拍")
    poke_cooldown_seconds: int = Field(default=600, ge=0, le=86400, description="拍一拍同一个人的冷却时间（秒）")
    collect_to_qq: bool = Field(
        default=True, description="她收藏表情时存进机器人 QQ 账号的收藏表情（存不进去时退回本地图库）；关掉就只存本地"
    )
    allow_remark: bool = Field(
        default=False, description="允许她修改 QQ 收藏表情的官方备注（set_sticker_remark，会改到 QQ 账号里，默认关）"
    )
    include_in_prompt: bool = Field(
        default=True, description="每次叫醒时在提示里随机列几张有描述的表情，她不用先查图库就知道手上有什么"
    )
    prompt_max: int = Field(default=8, ge=0, le=30, description="提示里最多列几张表情（0=不列）")


class PersonaSettings(BaseModel):
    full_every_n: int = Field(
        default=4, ge=1, le=50,
        description="完整人设卡每几次发一次，其余只发核心部分（1=每次都发完整版；标题带〔完整〕的章节属于完整部分）",
    )
    rules_enabled: bool = Field(default=True, description="每次附带行为规则（persona/rules.md）")


class TimeSenseSettings(BaseModel):
    enabled: bool = Field(default=True, description="在提示里告诉她现在几点、触发消息有多旧")
    stale_chat_minutes: int = Field(default=10, ge=1, le=600, description="超过多少分钟的消息不算实时对话")
    stale_silent_minutes: int = Field(default=120, ge=5, le=2880, description="超过多少分钟的消息算很旧，默认不展开接话")


class WaitSettings(BaseModel):
    unfinished_quiet_seconds: float = Field(
        default=6.0, ge=0, le=60, description="对方最后一句像没说完（「然后」「但是」、逗号结尾）时，安静多少秒再回"
    )
    unfinished_max_wait_seconds: float = Field(default=10.0, ge=0, le=120, description="等对方说完最长等多少秒")
    unread_hint: bool = Field(default=True, description="在提示里告诉她上次看群以来来了多少条消息")
    unread_preview: bool = Field(
        default=True, description="群聊叫醒时，把她上次说话之后没叫她的群消息附在这次消息开头给她看（不进系统提示）"
    )
    unread_preview_count: int = Field(default=10, ge=1, le=30, description="未读预览最多附几条（取最近的）")
    unread_preview_chars: int = Field(default=120, ge=20, le=500, description="未读预览里每条最多多少字，超出截短")
    preview_recent: int = Field(
        default=4, ge=0, le=20, description="未读预览前面再附几条她上次说话前已看过的消息，方便接前文（0=不附）"
    )
    tool_enabled: bool = Field(
        default=True, description="允许她自己等一会儿（wait_for_messages 工具）：回复前等对方说完，或回复后等人接话"
    )
    tool_max_seconds: int = Field(
        default=120, ge=10, le=600, description="她自己等最长多少秒（等待期间这一轮一直占着，这个群的新消息要等她等完才处理）"
    )
    tool_quiet_seconds: int = Field(default=10, ge=3, le=60, description="对方安静多少秒算说完（她没指定时用这个）")


class RhythmSettings(BaseModel):
    allow_silence: bool = Field(
        default=True, description="允许她看了不回（只回「〔不回〕」时这条不发出去）；被 @、叫名字和私聊时仍要求回复"
    )
    follow_up_seconds: int = Field(
        default=60, ge=0, le=600, description="她说完话后多少秒内，刚才和她聊的人再说话不用 @ 也会叫醒她（0=关闭跟进）"
    )
    follow_up_max_rounds: int = Field(default=6, ge=1, le=50, description="不 @ 的连续跟进最多几轮，之后要重新 @ 或叫名字")
    observe_seconds: int = Field(
        default=300, ge=0, le=3600, description="她说完话后的观察期（秒）：期间群里任何人说话，都按下面的概率叫醒她（0=关闭）"
    )
    observe_probability: float = Field(default=0.3, ge=0, le=1, description="观察期里每条消息叫醒她的概率（0.3=30%）")
    ai_wake_control: bool = Field(default=True, description="允许她自己调节奏：潜水、活跃、等某个人（set_wake 工具）")
    max_dive_minutes: int = Field(default=240, ge=1, le=1440, description="她自己设潜水最长多少分钟")
    allow_endless_dive: bool = Field(
        default=True, description="允许她一直潜水（不设结束时间，直到她自己改回正常）；被 @ 和叫名字照样会叫醒她"
    )
    max_active_minutes: int = Field(default=30, ge=1, le=240, description="她自己设活跃模式（任何消息都叫醒）最长多少分钟")
    max_wait_minutes: int = Field(default=30, ge=1, le=240, description="她等某个人说话最长等多少分钟")
    silent_limit: int = Field(
        default=3, ge=1, le=20, description="活跃模式或观察期里，连续几次叫醒都选择不回，就退回正常节奏"
    )
    max_wakes_per_minute: int = Field(default=2, ge=0, le=30, description="非 @、非叫名字的叫醒，每个群每分钟最多几次（0=不限）")
    max_wakes_per_hour: int = Field(default=30, ge=0, le=600, description="非 @、非叫名字的叫醒，每个群每小时最多几次（0=不限）")
    question_wake: bool = Field(
        default=True, description="群里有人提问（问号、吗/呢结尾、「有没有」「怎么办」等）时有机会叫醒她；引用或 @ 别人的提问不算，潜水时不算"
    )
    question_probability: float = Field(default=0.5, ge=0, le=1, description="提问叫醒她的概率（0.5=50%）；叫醒后她可以选择不回")
    proactive_enable: bool = Field(default=True, description="冷场时给她主动开话题的机会；她在潜水、或最后说话的是她自己时不会触发")
    proactive_check_seconds: int = Field(
        default=60, ge=30, le=3600, description="多久检查一次各群是否冷场（秒）；间隔越长越省，但开口时间会比设定的冷场时长晚最多一个间隔"
    )
    proactive_idle_minutes: int = Field(default=40, ge=5, le=720, description="群里安静多少分钟算冷场")
    proactive_max_idle_minutes: int = Field(
        default=180, ge=10, le=1440, description="冷场时长超过这个值就不开始抽签，已经抽过但未命中时按半小时重试"
    )
    proactive_probability: float = Field(default=0.5, ge=0, le=1, description="每次抽签的命中概率。未命中每隔半小时重试，命中或有群友发言后结束本段抽签")
    proactive_daily_limit: int = Field(default=3, ge=0, le=50, description="每个群每天最多抽中几次开话题机会（0=不限）")
    proactive_start_hour: int = Field(default=9, ge=0, le=23, description="几点以后才主动开话题（含）")
    proactive_end_hour: int = Field(default=23, ge=0, le=24, description="几点以前才主动开话题（不含；和开始时间相同表示全天）")


class SendSettings(BaseModel):
    enabled: bool = Field(default=True, description="按群和私聊分别限制她发消息的条数，防止刷屏（表情包和拍一拍另有限制）")
    per_minute: int = Field(default=8, ge=0, le=60, description="每个群或私聊每分钟最多发几条（自动分句的每一条都算；0=不限）")
    per_hour: int = Field(default=60, ge=0, le=1000, description="每个群或私聊每小时最多发几条（0=不限）")
    typing_pace: bool = Field(
        default=True, description="分条之间按打字速度停顿：下一条越长停得越久（关掉恢复原来的 1~3 秒随机）"
    )
    gap_base_ms: int = Field(default=800, ge=0, le=5000, description="每条之间的基础停顿（毫秒）")
    gap_per_char_ms: int = Field(default=100, ge=0, le=1000, description="下一条每个字多停多少毫秒（100=每秒约打 10 个字）")
    gap_max_ms: int = Field(default=8000, ge=500, le=30000, description="按字数算出的停顿最长多少毫秒（不含下面的偶尔多停）")
    long_pause_probability: float = Field(
        default=0.1, ge=0, le=1, description="偶尔多停 2~5 秒的概率，像中途走神了一下（0=不多停）"
    )


class MentionSettings(BaseModel):
    allow_quote: bool = Field(default=True, description="允许她引用某条群消息回复（她写〔引用:消息id〕，只认本群的消息）")
    allow_at: bool = Field(default=True, description="允许她 @ 群友（她写〔@QQ号〕，只认本群成员，一条最多 3 个）")


class ImageSettings(BaseModel):
    enabled: bool = Field(default=True, description="允许她按消息 id 看某条消息里的图片和表情（get_message_images 工具）")
    max_per_call: int = Field(default=4, ge=1, le=9, description="一次最多看几张（看图会消耗图片 token）")
    max_edge: int = Field(default=768, ge=256, le=1536, description="图片缩到长边多少像素再给她看（越大越清楚，也越费 token）")
    view_per_minute: int = Field(default=6, ge=0, le=60, description="每个会话每分钟最多看几次（0=不限）")
    view_per_hour: int = Field(default=60, ge=0, le=1000, description="每个会话每小时最多看几次（0=不限）")
    gif_frames: bool = Field(
        default=True, description="聊天里收到的动图（含商城表情）均匀取 4 帧拼成一张给她看；关掉就只看得到第一帧"
    )


class ForwardSettings(BaseModel):
    max_depth: int = Field(default=3, ge=1, le=5, description="合并转发最多展开几层，含最外层（3=最外层加两层嵌套；1=不展开嵌套）")
    max_total: int = Field(default=60, ge=10, le=300, description="一个合并转发（含嵌套）最多读多少条消息")
    max_images: int = Field(default=4, ge=0, le=10, description="打开合并转发时最多附带几张图给她看（0=不看图）")


class SilentSettings(BaseModel):
    enabled: bool = Field(
        default=False,
        description="静默模式：群友和私聊消息照常记录但不叫醒她，只有上面「管理员」里的机器人管理员能和她对话；冷场也不主动开话题。管理员也可以发 /静默、/恢复 开关",
    )


class TraceSettings(BaseModel):
    enabled: bool = Field(
        default=True, description="记录每一轮的完整过程（收到的消息、发给网关的请求和返回、思考、工具调用、发送）和每步耗时，在面板「系统信息 → 对话追踪」查看"
    )
    keep_turns: int = Field(default=300, ge=20, le=5000, description="只保留最近多少轮（存在 data/trace.db）")
    max_chars: int = Field(default=20000, ge=1000, le=200000, description="每段文字最多记多少字，超出截断（图片数据一律换成占位符）")


class MemoryAutoSettings(BaseModel):
    enabled: bool = Field(default=True, title="启用晚间自动记忆", description="每天北京时间晚间为有新聊天的白名单会话各提炼一次，不向 QQ 发送消息；只收集启用期间的新文本，素材保留 8 天")
    hour: int = Field(default=23, ge=0, le=23, title="执行时间（小时）", description="北京时间；同一会话同一天最多请求一次，重启不重复")
    minute: int = Field(default=0, ge=0, le=59, title="执行时间（分钟）")
    max_messages: int = Field(default=200, ge=1, le=1000, title="每会话最多读取消息数", description="读取前一日执行时刻到本日执行时刻的新聊天，超过限制取最近的记录")
    max_input_chars: int = Field(default=12000, ge=1000, le=50000, title="新聊天字符预算", description="包括消息 ID、时间和发言人字段；已有记忆另有 6000 字符预算")
    max_items: int = Field(default=8, ge=1, le=20, title="每会话每次最多提炼条数", description="无值得记录的信息时允许零条；失败当晚不重试，结果记录在对话追踪和运行日志")


class OutboundSettings(BaseModel):
    mask_personal_info: bool = Field(default=True, description="发出前把手机号、身份证、银行卡、邮箱、住址打码")
    notify_admin: bool = Field(
        default=True, description="她的消息被整条拦下时私聊通知机器人管理员（只说在哪、什么类型、几点，不转发原文；同一会话 5 分钟内最多一次）"
    )
    block_notice: str = Field(
        default="（这条消息疑似包含本机路径或凭据，已被安全策略拦截）",
        description="含本机路径或凭据的消息会整条拦截（始终开启），这里是换上去的提示文字",
    )



_TITLES = {
    "whitelist": "白名单",
    "whitelist.groups": "允许的群号",
    "whitelist.private": "允许私聊的 QQ 号",
    "admin": "管理员",
    "admin.superusers": "机器人管理员 QQ 号",
    "sticker": "表情包",
    "sticker.send_per_minute": "每分钟发表情上限",
    "sticker.send_per_hour": "每小时发表情上限",
    "sticker.collect_per_minute": "每分钟收藏上限",
    "sticker.collect_per_hour": "每小时收藏上限",
    "sticker.view_per_minute": "每分钟看图上限",
    "sticker.view_per_hour": "每小时看图上限",
    "sticker.qq_sync_seconds": "QQ 收藏同步间隔（秒）",
    "sticker.allow_private": "私聊里发表情和拍一拍",
    "sticker.poke_cooldown_seconds": "拍一拍冷却（秒）",
    "sticker.collect_to_qq": "收藏进 QQ 收藏",
    "sticker.allow_remark": "允许改 QQ 收藏备注",
    "sticker.include_in_prompt": "提示里附带常用表情",
    "sticker.prompt_max": "附带几张表情",
    "persona": "人设注入",
    "persona.full_every_n": "完整人设卡每几次发一次",
    "persona.rules_enabled": "附带行为规则",
    "time_sense": "消息新旧判断",
    "time_sense.enabled": "启用",
    "time_sense.stale_chat_minutes": "不算实时的分钟数",
    "time_sense.stale_silent_minutes": "算很旧的分钟数",
    "wait": "等对方说完与看群",
    "wait.unfinished_quiet_seconds": "没说完时安静几秒再回",
    "wait.unfinished_max_wait_seconds": "最长等几秒",
    "wait.unread_hint": "提示未读条数",
    "wait.unread_preview": "附带未读消息预览",
    "wait.unread_preview_count": "未读预览最多几条",
    "wait.unread_preview_chars": "未读预览每条最多几个字",
    "wait.preview_recent": "附带几条已看过的消息",
    "wait.tool_enabled": "允许她自己等一会儿",
    "wait.tool_max_seconds": "自己等最长（秒）",
    "wait.tool_quiet_seconds": "安静几秒算说完",
    "rhythm": "节奏",
    "rhythm.allow_silence": "允许看了不回",
    "rhythm.follow_up_seconds": "跟进窗口（秒）",
    "rhythm.follow_up_max_rounds": "连续跟进轮数上限",
    "rhythm.observe_seconds": "观察期（秒）",
    "rhythm.observe_probability": "观察期叫醒概率",
    "rhythm.ai_wake_control": "允许她自己调节奏",
    "rhythm.max_dive_minutes": "潜水最长（分钟）",
    "rhythm.allow_endless_dive": "允许一直潜水",
    "rhythm.max_active_minutes": "活跃模式最长（分钟）",
    "rhythm.max_wait_minutes": "等人最长（分钟）",
    "rhythm.silent_limit": "连续不回几次退回正常",
    "rhythm.max_wakes_per_minute": "非 @ 叫醒每分钟上限",
    "rhythm.max_wakes_per_hour": "非 @ 叫醒每小时上限",
    "rhythm.question_wake": "提问叫醒",
    "rhythm.question_probability": "提问叫醒概率",
    "rhythm.proactive_enable": "冷场主动开话题",
    "rhythm.proactive_check_seconds": "冷场检查间隔（秒）",
    "rhythm.proactive_idle_minutes": "安静几分钟算冷场",
    "rhythm.proactive_max_idle_minutes": "首次抽签的冷场时长上限（分钟）",
    "rhythm.proactive_probability": "冷场开话题概率",
    "rhythm.proactive_daily_limit": "每群每天开话题上限",
    "rhythm.proactive_start_hour": "开话题开始时间（点）",
    "rhythm.proactive_end_hour": "开话题结束时间（点）",
    "send": "发送限速与停顿",
    "send.enabled": "启用发送限速",
    "send.per_minute": "每分钟最多发几条",
    "send.per_hour": "每小时最多发几条",
    "send.typing_pace": "按打字速度停顿",
    "send.gap_base_ms": "基础停顿（毫秒）",
    "send.gap_per_char_ms": "每个字停顿（毫秒）",
    "send.gap_max_ms": "停顿上限（毫秒）",
    "send.long_pause_probability": "偶尔多停的概率",
    "mention": "引用与 @",
    "mention.allow_quote": "允许引用消息",
    "mention.allow_at": "允许 @ 群友",
    "image": "按消息看图",
    "image.enabled": "允许按消息看图",
    "image.max_per_call": "一次最多看几张",
    "image.max_edge": "图片长边（像素）",
    "image.view_per_minute": "每分钟看图上限",
    "image.view_per_hour": "每小时看图上限",
    "image.gif_frames": "动图拼多帧",
    "silent": "静默模式",
    "silent.enabled": "开启静默模式（只回管理员）",
    "trace": "对话追踪",
    "trace.enabled": "记录对话过程",
    "trace.keep_turns": "保留最近几轮",
    "trace.max_chars": "每段最多记几个字",
    "forward": "合并转发",
    "forward.max_depth": "最多展开几层",
    "forward.max_total": "最多读几条",
    "forward.max_images": "最多附几张图",
    "outbound": "出站拦截",
    "outbound.mask_personal_info": "个人信息打码",
    "outbound.notify_admin": "拦截后通知管理员",
    "outbound.block_notice": "拦截提示文字",
}


class QianwenSettings(BaseModel):
    """机器人扩展设置"""

    whitelist: WhitelistSettings = Field(default_factory=WhitelistSettings, description="白名单")
    admin: AdminSettings = Field(default_factory=AdminSettings, description="机器人管理员")
    silent: SilentSettings = Field(default_factory=SilentSettings, description="静默模式（只回管理员）")
    sticker: StickerSettings = Field(default_factory=StickerSettings, description="表情包")
    persona: PersonaSettings = Field(default_factory=PersonaSettings, description="人设注入")
    time_sense: TimeSenseSettings = Field(default_factory=TimeSenseSettings, description="消息新旧判断")
    wait: WaitSettings = Field(default_factory=WaitSettings, description="等对方说完与看群")
    rhythm: RhythmSettings = Field(
        default_factory=RhythmSettings, description="节奏：不回、跟进、观察期、潜水与活跃、提问叫醒、冷场开话题"
    )
    send: SendSettings = Field(default_factory=SendSettings, description="发送限速与分条停顿")
    mention: MentionSettings = Field(default_factory=MentionSettings, description="引用与 @")
    image: ImageSettings = Field(default_factory=ImageSettings, description="按消息看图")
    forward: ForwardSettings = Field(default_factory=ForwardSettings, description="合并转发")
    memory_auto: MemoryAutoSettings = Field(default_factory=MemoryAutoSettings, title="晚间自动记忆", description="每日一次的长期记忆提炼")
    trace: TraceSettings = Field(default_factory=TraceSettings, description="对话追踪")
    outbound: OutboundSettings = Field(default_factory=OutboundSettings, description="出站拦截")


_cache: dict = {"mtime": None, "settings": QianwenSettings(), "ok": False, "sections": frozenset()}


def load() -> tuple[QianwenSettings, bool]:
    """返回 (设置, 是否成功读到文件)。读不到或格式不对时返回默认值和 False。"""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        _cache.update(mtime=None, settings=QianwenSettings(), ok=False, sections=frozenset())
        return _cache["settings"], False
    if mtime != _cache["mtime"]:
        try:
            data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            _cache.update(mtime=mtime, settings=QianwenSettings.model_validate(data), ok=True, sections=frozenset(data))
        except Exception:
            _cache.update(mtime=mtime, settings=QianwenSettings(), ok=False, sections=frozenset())
    return _cache["settings"], _cache["ok"]


def has_section(section: str) -> bool:
    """设置文件里是否真的写了这一组（没写时 load() 给的是默认值，有的调用方要区分）。"""
    load()
    return section in _cache["sections"]


def value(section: str, key: str, default):
    """取一个设置值；任何问题都回落到 default。"""
    try:
        settings, _ = load()
        return getattr(getattr(settings, section), key)
    except Exception:
        return default


def set_value(section: str, key: str, new_value) -> None:
    """改一个设置并写回 config.toml（管理员命令用，和面板保存写的是同一个文件）。
    先按模型校验，校验不过就抛异常、不写文件；写入用临时文件替换，避免写到一半。"""
    import tomli_w

    data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    data.setdefault(section, {})[key] = new_value
    QianwenSettings.model_validate(data)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(tomli_w.dumps(data), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def whitelist() -> tuple[set[int], set[int]] | None:
    """返回 (群号集合, 私聊 QQ 集合)；文件读不了或格式不对返回 None（调用方应全部拒绝）。"""
    settings, ok = load()
    if not ok:
        return None
    return {int(x) for x in settings.whitelist.groups}, {int(x) for x in settings.whitelist.private}


def _apply_titles(model: type[BaseModel], prefix: str = "") -> None:
    """把 _TITLES 里的中文名挂到字段的 title 上，面板据此显示中文名称。"""
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        if path in _TITLES:
            info.title = _TITLES[path]
        sub = info.annotation
        if isinstance(sub, type) and issubclass(sub, BaseModel):
            _apply_titles(sub, path + ".")


_apply_titles(QianwenSettings)
