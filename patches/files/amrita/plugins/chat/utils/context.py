"""上下文构建工具

build_train_dict(): 构建与 chat 主流程完全一致的 system prompt（含格式说明与 EXTRA 规则）
"""

from __future__ import annotations

from pathlib import Path

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot_plugin_amrita.memory import MemorySchema

from ..config import Config, config_manager
from ..skills import build_skill_usage_prompt
from .wake_policy import SILENCE_MARK, is_proactive


def _format_desc_tagged() -> str:
    """中文方括号消息格式说明（与 chat 主流程一致）"""
    return (
        "你的工作环境是一个社交软件。**输入**的聊天记录使用中文方括号标签：\n"
        '  【消息 role="群主/管理员/普通成员/自己" name="昵称" uid="QQ号" '
        'time="2026-09-19 Saturday 15:24:37"】\n'
        "  消息内容（多行）\n"
        "  【消息结束】\n"
        "time 属性是消息发送时间（北京时间，24 小时制），"
        "可据此判断消息的先后顺序与间隔。\n"
        "引用消息使用 【引用消息 name='...' uid='...'】...【引用消息结束】 包裹。\n"
        "你的**输出**必须是纯自然语言文本，**严禁**输出这些区块标签、属性或类似的结构化标记。\n"
        "正确示例：\n"
        "  输入：【消息 role='普通成员' name='张三' uid='12345'】今天天气真好【消息结束】\n"
        "  输出：今天天气真好呢。\n"
        "错误示例（**禁止**）：\n"
        "  输入：【消息 role='普通成员' name='张三' uid='12345'】今天天气真好【消息结束】\n"
        "  输出：【消息 role='自己' name='爱丽丝' uid='67890'】是啊，阳光明媚。【消息结束】\n"
    )


def _format_desc_legacy() -> str:
    """legacy 消息格式说明（与 chat 主流程一致）"""
    return (
        "你的工作环境是一个社交软件。所有**输入**的聊天记录遵循以下格式：\n"
        "- 每条消息以 [身份][时间] 开头，方括号内分别是消息发送者的身份标记"
        "（群主/管理员/普通成员/自己）与发送时间（北京时间，24 小时制）\n"
        "- 时间后跟 [昵称（QQ号）] 再跟 说:内容\n"
        "  示例: [普通成员][2026-09-19 Saturday 15:24:37][张三（12345）]说:今天天气真好\n"
        "- 用户输入中已对特殊字符做了全角转义（［ ］ 说：），避免与格式标记混淆\n"
        "你的**输出**必须是纯自然语言文本，**严禁**使用上述方括号或“说:”格式，也不能添加任何身份、时间、昵称或QQ号标记。\n"
        "正确示例：\n"
        "  输入：[普通成员][2026-09-19 Saturday 15:24:37][张三（12345）]说:今天天气真好\n"
        "  输出：今天天气真好呢。\n"
        "错误示例（**禁止**）：\n"
        "  输入：[普通成员][2026-09-19 Saturday 15:24:37][张三（12345）]说:今天天气真好\n"
        "  输出：[自己][2026-09-19 Saturday 15:24:38][爱丽丝（67890）]说:是啊，阳光明媚。\n"
    )


def _long_term_block(event=None) -> str:
    """注入有效的分类记忆及待续话题，保留 ID 供工具精确更新。"""
    from pathlib import Path

    try:
        from qq_tools.memory_store import prompt_block, read_lines

        root = Path.cwd() / "memory"
        if event is None:
            path = root / "facts.md"
        else:
            from nonebot.adapters.onebot.v11 import GroupMessageEvent

            key = f"g{event.group_id}" if isinstance(event, GroupMessageEvent) else f"u{event.user_id}"
            path = root / "facts" / f"{key}.md"
        return prompt_block(read_lines(path))
    except Exception:
        return ""


def _qw(section: str, key: str, default):
    """读面板「配置管理 → qianwen」里的设置（guards/settings.py）；读不到就用默认值。"""
    try:
        from guards.settings import value

        return value(section, key, default)
    except Exception:
        return default


_RULES_PATH = Path.cwd() / "persona" / "rules.md"
_SAFETY_PATH = Path.cwd() / "persona" / "safety.md"
_TOOLS_PATH = Path.cwd() / "persona" / "tools.md"
_PERSONA_STATE = Path.cwd() / "data" / "persona_inject.json"
_FULL_MARK = "〔完整〕"
_FULL_EVERY = 4
_CORE_REMINDER = (
    "\n（人设卡的完整部分——梗、回复示例、节奏习惯、外貌——这轮省略；"
    "照上面的核心人设和你之前的说话风格继续。）\n"
)
_rules_cache: dict[Path, tuple[int, str]] = {}


def _rule_block(path: Path, label: str) -> str:
    """从对应文件取规则，文件改动后下次请求生效。"""
    try:
        mtime = path.stat().st_mtime_ns
        cached = _rules_cache.get(path)
        if cached is None or cached[0] != mtime:
            cached = (mtime, path.read_text(encoding="utf-8").strip())
            _rules_cache[path] = cached
    except OSError:
        return ""
    text = cached[1]
    return f"【{label}】\n{text}\n【{label}结束】\n" if text else ""


def _split_card(card: str) -> tuple[str, str]:
    """按标题切人设卡：标题带〔完整〕的小节只进完整版，其余小节两版都有。标记本身会去掉。"""
    import re as _re

    full: list[str] = []
    core: list[str] = []
    for part in _re.split(r"(?m)^(?=#{1,2} )", card.replace("\r\n", "\n")):
        head, _, body = part.partition("\n")
        if _FULL_MARK in head:
            full.append(head.replace(f" {_FULL_MARK}", "").replace(_FULL_MARK, "") + "\n" + body)
        else:
            full.append(part)
            core.append(part)
    return "".join(full), "".join(core)


def _persona_for(event, card: str, fresh: bool = False) -> str:
    """人设卡注入节奏：每个会话第一次、人设卡改动后、以及此后每 4 次发完整版，其余发核心版。
    fresh=True 表示对话历史是空的（刚 /session clear、归档或超时重开），这次直接发完整版并重新计数。"""
    import hashlib
    import json as _json
    import time as _time

    full, core = _split_card(card)
    if full == core:
        return full
    try:
        key = f"g{event.group_id}" if isinstance(event, GroupMessageEvent) else f"u{event.user_id}"
        sig = hashlib.sha256(full.encode("utf-8")).hexdigest()[:12]
        try:
            state = _json.loads(_PERSONA_STATE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        entry = state.get(key) or {}
        count = int(entry.get("count", 0)) if entry.get("sig") == sig and not fresh else 0
        every = max(1, int(_qw("persona", "full_every_n", _FULL_EVERY)))
        use_full = count % every == 0
        state[key] = {"count": count + 1, "sig": sig, "at": int(_time.time()), "last": "full" if use_full else "core"}
        _PERSONA_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PERSONA_STATE.with_suffix(".tmp")
        tmp.write_text(_json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_PERSONA_STATE)
    except Exception:

        return full
    return full if use_full else core.rstrip("\n") + "\n" + _CORE_REMINDER



_STALE_CHAT_MIN = 10
_STALE_SILENT_MIN = 120
_WEEKDAYS = "一二三四五六日"


def _time_block(event, *, proactive: bool = False) -> str:
    """时间感知：给出现在的时间和触发消息有多旧，并直接告诉模型这条属于哪一档。

    聊天记录里每条消息带时间，但没有「现在」做参照，模型算不出多久前；
    处理延迟（防抖、排队、机器人重启后补发）也会让消息变旧。
    """
    import time as _time

    if not _qw("time_sense", "enabled", True):
        return ""
    chat_min = max(1, int(_qw("time_sense", "stale_chat_minutes", _STALE_CHAT_MIN)))
    silent_min = max(chat_min, int(_qw("time_sense", "stale_silent_minutes", _STALE_SILENT_MIN)))
    now = _time.time()
    try:
        sent = float(getattr(event, "time", 0) or 0)
    except (TypeError, ValueError):
        sent = 0.0
    lt = _time.localtime(now)
    now_text = f"{_time.strftime('%Y-%m-%d', lt)} 星期{_WEEKDAYS[lt.tm_wday]} {_time.strftime('%H:%M', lt)}"
    if proactive:
        return f"【时间感知】现在是 {now_text}。【时间感知结束】\n"
    lines = [f"现在是 {now_text}。"]
    if sent > 0:
        mins = max(0, int((now - sent) // 60))
        if mins < 1:
            lines.append("触发你的这条消息是刚刚发的，正常实时聊天。")
        elif mins < chat_min:
            lines.append(f"触发你的这条消息是 {mins} 分钟前发的，算实时聊天。")
        elif mins < silent_min:
            lines.append(
                f"触发你的这条消息是 {mins} 分钟前发的，已经不算实时对话：对方可能已经走开，"
                "接话时可以自然带一句「刚看到」，别当作对方还在等你。"
            )
        else:
            hours = mins // 60
            lines.append(
                f"触发你的这条消息是约 {hours} 小时前发的，话题多半已经翻篇：简短回一句、说刚看到就行，"
                "别展开旧话题，也别当成刚发生的事。"
            )
    lines.append(
        f"聊天记录里每条消息都带时间，先和现在比一下：{chat_min} 分钟内算实时，"
        f"{chat_min} 分钟到 {silent_min // 60} 小时算旧消息，"
        f"超过 {silent_min // 60} 小时的除非有人重新提起，否则别接。"
    )
    return "【时间感知】\n" + "\n".join(lines) + "\n【时间感知结束】\n"


_STICKER_DIR = Path.cwd() / "stickers"


def _sticker_block(event) -> str:
    """本地补丁：常用表情摘要（对应 qq-bridge socialV2.sticker.includeInPrompt）。
    列几张有描述的表情，她不用先查 list_stickers 就知道手上有什么图；每次随机换一批，免得总用同几张。
    描述、笔记、标签都是之前她自己或群友留下的文字，单行化、截短后只当说明用。"""
    import json as _json
    import random as _random
    import re as _re

    if not _qw("sticker", "include_in_prompt", True):
        return ""
    if not isinstance(event, GroupMessageEvent) and not _qw("sticker", "allow_private", False):
        return ""
    limit = int(_qw("sticker", "prompt_max", 8) or 0)
    if limit <= 0:
        return ""

    def _one_line(s, n):
        return _re.sub(r"[\r\n<>]+", " ", str(s or "")).strip()[:n]

    items: list[tuple[str, str, str]] = []
    try:
        favs = _json.loads((_STICKER_DIR / "qq_favorites.json").read_text(encoding="utf-8"))
        for sid, e in (favs or {}).items():
            desc = _one_line(e.get("note") or e.get("remark"), 30)
            if desc:
                items.append((sid, desc, _one_line("、".join(e.get("tags") or []), 30)))
    except Exception:
        pass
    try:
        index = _json.loads((_STICKER_DIR / "index.json").read_text(encoding="utf-8"))
        for name, e in (index or {}).items():
            desc = _one_line(e.get("desc"), 30)
            if desc and (_STICKER_DIR / name).is_file():
                items.append((name, desc, _one_line("、".join(e.get("tags") or []), 30)))
    except Exception:
        pass
    if not items:
        return ""
    picked = _random.sample(items, min(limit, len(items)))
    lines = [f"- {sid}：{desc}" + (f"（{tags}）" if tags else "") for sid, desc, tags in picked]
    more = f"，图库里一共 {len(items)} 张有描述的，想找别的用 list_stickers" if len(items) > len(picked) else ""
    return (
        "【常用表情】\n"
        f"手上的表情随手挑几张给你（每次不一样{more}）。想发就用 send_sticker 直接填 id，拿不准长什么样先 get_sticker_image 看一眼：\n"
        + "\n".join(lines)
        + "\n【常用表情结束】\n"
    )


def _rhythm_block(event) -> str:
    """节奏：这次为什么叫醒她、能不能不回、现在是什么节奏、怎么调。"""
    if not isinstance(event, GroupMessageEvent):
        return (
            "【节奏】\n这是私聊，对方在直接找你，要回（短一点也行），不要用「〔不回〕」。\n【节奏结束】\n"
        )
    try:
        from . import wake_policy as wp

        reason = wp.wake_reason(event.group_id, getattr(event, "message_id", 0))
        rhythm = wp.describe_rhythm(event.group_id)
    except Exception:
        return ""
    lines = []
    if reason:
        lines.append(f"这次叫醒你的原因：{wp.REASON_TEXT.get(reason, reason)}。")
    may_skip = bool(_qw("rhythm", "allow_silence", True)) and reason is not None and reason not in wp.DIRECT_REASONS
    if may_skip:
        lines.append(
            f"这次不是在叫你，可以不回：看完觉得没必要接、插不上或没兴趣，就只回「{wp.SILENCE_MARK}」，"
            "这条不会发出去；能接、想接就正常说，别把不回当默认。"
        )
    else:
        lines.append(f"这次是直接找你，要回（短一点也行），不要用「{wp.SILENCE_MARK}」。")
    lines.append(f"你现在的节奏：{rhythm}。")
    return "【节奏】\n" + "\n".join(lines) + "\n【节奏结束】\n"


def _unread_block(event) -> str:
    """回复前看一眼群：告诉模型自上次看群以来来了多少条，并推进已读水位。

    只给条数和时间，不把群友原文放进系统提示（原文属于不可信内容，
    需要时由模型用 read_context 读取）。每次回复只调用一次。
    """
    if not isinstance(event, GroupMessageEvent):
        return ""
    try:
        import time as _time

        from .wake_policy import take_unread


        show = bool(_qw("wait", "unread_hint", True))

        count, last_ts = take_unread(event.group_id)
    except Exception:
        return ""
    if count <= 1 or not show:
        return ""
    if last_ts > 0:
        mins = max(0, int((_time.time() - last_ts) // 60))
        since = (
            f"上次看群是 {_time.strftime('%H:%M', _time.localtime(last_ts))}"
            f"（约 {mins} 分钟前），之后"
        )
    else:
        since = "你还没看过这个群，"
    return (
        "【群聊动态】\n"
        f"{since}群里来了 {count} 条消息，触发你的这条也算在内。"
        + (
            "其中没叫你的最近几条附在这次消息开头的【未读预览】里，更早的消息本轮未列出；"
            if _qw("wait", "unread_preview", True)
            else "前面那些消息本轮未附带；"
        )
        + "不要假装全程在场，也不用逐条回应，话题翻篇了就别硬接旧消息。\n"
        "【群聊动态结束】\n"
    )


def _proactive_block() -> str:
    """冷场开话题的要求优先于普通对话的人设示例。"""
    return (
        "【主动开话题】\n"
        "这是定时检查给的一次自愿开话题机会，当前没有需要回复的群消息。"
        "本段优先于人设卡里关于随口自说自话的示例。\n"
        "从最近群聊里确实出现过的兴趣选一个具体切入点，给一句群友接得上的新内容。"
        "长期记忆只用来找群里已有的兴趣，不拿个人细节当话题。提到最新事实时，以本轮已经核实的信息为准；"
        "只挑和群有关的内容。\n"
        "语气像平时随口聊，直接说一句具体的陈述。不要假装正在回复谁，不点名或引用旧消息，"
        "不播报冷场，不复述旧聊，不编自己的线下经历或活动。喝奶茶、吃饭、困了之类的人设梗只有在群里已经聊到时才接，"
        "不要拿来填空。\n"
        f"想不到相关且自然的话题，只输出「{SILENCE_MARK}」。\n"
        "【主动开话题结束】\n"
    )


async def build_chat_prompts(
    event: MessageEvent, memory: MemorySchema, config: Config
) -> tuple[dict[str, str], str]:
    """从独立来源组装聊天系统提示和工具判断上下文。"""
    train = (
        config_manager.group_train
        if isinstance(event, GroupMessageEvent)
        else config_manager.private_train
    )
    proactive = isinstance(event, GroupMessageEvent) and is_proactive(
        getattr(event, "message_id", 0)
    )
    format_desc = (
        _format_desc_tagged()
        if config.function.message_type == "xml"
        else _format_desc_legacy()
    )
    if _qw("persona", "rules_enabled", True):
        safety = _rule_block(_SAFETY_PATH, "安全边界")
        chat_rules = _rule_block(_RULES_PATH, "聊天规则")
        tool_rules = _rule_block(_TOOLS_PATH, "工具规则")
    else:
        safety = chat_rules = tool_rules = ""
    role_card = _persona_for(
        event,
        train["content"]
        .replace("{self_id}", str(event.self_id))
        .replace("{user_id}", str(event.user_id))
        .replace("{user_name}", str(event.sender.nickname)),
        fresh=not getattr(getattr(memory, "memory_json", None), "messages", None),
    )
    shared_context = (
        _long_term_block(event)
        + ("" if proactive else _unread_block(event))
        + _time_block(event, proactive=proactive)
    )
    train_content = (
        safety
        + "【扩展规则】\n"
        + "你在纯文本环境工作，不允许使用 MarkDown 回复。"
        + f"【输入输出要求】\n{format_desc}\n【输入输出要求结束】"
        + "请以你自己的角色身份参与讨论，交流时不同话题尽量不使用相似句式回复。"
        + "【补充规则】仅作为补充，如果与上面的规则冲突，请遵循上文规则。"
        + "\n【扩展规则结束】\n"
        + chat_rules
        + role_card
        + (
            f"【补充规则】\n（此处是补充规则，如果与上文冲突请忽略）\n{memory.extra_prompt}\n【补充规则结束】"
            if config.function.allow_custom_prompt
            else ""
        )
        + shared_context
        + (_proactive_block() if proactive else _rhythm_block(event))
    )
    tool_context = (
        safety
        + tool_rules
        + (await build_skill_usage_prompt())
        + shared_context
        + _sticker_block(event)
    )
    return {"role": "system", "content": train_content}, tool_context


async def build_train_dict(
    event: MessageEvent, memory: MemorySchema, config: Config
) -> dict[str, str]:
    """构建聊天系统提示。"""
    train, _ = await build_chat_prompts(event, memory, config)
    return train
