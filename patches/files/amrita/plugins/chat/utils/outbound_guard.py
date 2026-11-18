"""出站消息守卫：所有发往 QQ 的文本在发出前过一遍。

两类规则，处理方式不同：
1. 个人信息（手机号、身份证、银行卡、邮箱、详细住址）→ 打码后照常发送；
2. 本机路径与凭据（Windows/UNC/Unix 路径、token=…、密码是…、已知密钥原文）
   → 整条消息不发，换成一句拦截提示。

判定前先把文本还原成几种等价形式（全角转半角、去零宽字符、去分隔符、
base64 解码），防止用 `C：＼Users`、零宽空格或编码绕过。规则参考
qq-bridge 的 src/sensitive.js。

拦截点挂在 OneBot Bot.call_api 上，普通发送、合并转发都会经过。
qq_tools 走 HTTP 直接发消息，不经过 NoneBot，它会按文件路径加载本模块，
调用 guard_text() 自行检查。本模块顶层不依赖 nonebot，供两边共用。

日志写 logs/outbound_guard.log：只记命中类型和打码后的内容，
被拦截的原文不落盘。
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import time
import unicodedata
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path.cwd()
LOG = ROOT / "logs" / "outbound_guard.log"

BLOCK_NOTICE = "（这条消息疑似包含本机路径或凭据，已被安全策略拦截）"



RULES = [
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("住址", re.compile(r"[\u4e00-\u9fa5]{2,12}(?:路|街|道|巷)\s*\d+\s*号")),
    ("住址", re.compile(r"[\u4e00-\u9fa5]{2,8}(?:省|市)[\u4e00-\u9fa5]{2,10}(?:区|县)")),
]


def scrub(text: str) -> tuple[str, list[str]]:
    hits: list[str] = []
    out = text
    for name, pattern in RULES:
        def _mask(m):
            hits.append(name)
            s = m.group(0)
            return s[:2] + "*" * max(len(s) - 2, 1)

        out = pattern.sub(_mask, out)
    return out, hits




_KEYWORDS = (
    r"token|密码|密钥|口令|password|passwd|secret|api[_-]?key|authorization"
    r"|bearer|access[_-]?key|credential|cookie"
)
SECRET_RULES = [

    ("本机路径", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]+")),

    ("本机路径", re.compile(r"\\\\[^\\\s\"'<>|]+\\[^\s\"'<>|]+")),

    ("本机路径", re.compile(r"(?<![A-Za-z0-9])(?:/home/|/Users/|/etc/|/var/|/root/)[^\s\"'<>|]*")),


    ("凭据", re.compile(
        rf"(?:{_KEYWORDS})[\"']?\s*(?:[:=：]|是|为)\s*[\"']?[A-Za-z0-9_\-./+=]{{4,}}", re.I
    )),

    ("凭据", re.compile(
        rf"(?:{_KEYWORDS})\s+(?=[A-Za-z_\-./+]*\d)[A-Za-z0-9_\-./+]{{8,}}", re.I
    )),
]

_INVISIBLE = re.compile(r"[\u00ad\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
_B64_CANDIDATE = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")


def _normalize(text: str) -> str:
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text or ""))


def _compact(text: str) -> str:
    return re.sub(r"[\s._-]+", "", text)


def _squash(text: str) -> str:
    """只留字母、数字和汉字：已知凭据被标点、空格、换行拆开（自动分句就是按这些拆的）也能对上。"""
    return re.sub(r"[\W_]+", "", text)


def _forms(text: str) -> list[str]:
    """文本的几种等价形式：规范形式、紧凑形式，以及其中 base64 片段的解码结果。"""
    canonical = _normalize(text)
    forms = [canonical, _compact(canonical)]
    for form in list(forms):
        for m in _B64_CANDIDATE.finditer(form):
            raw = m.group(0).replace("-", "+").replace("_", "/").rstrip("=")
            if len(raw) % 4 == 1:
                continue
            try:
                buf = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
            except Exception:
                continue

            if not buf or base64.b64encode(buf).decode().rstrip("=") != raw:
                continue
            try:
                decoded = _normalize(buf.decode("utf-8"))
            except UnicodeDecodeError:
                continue
            forms += [decoded, _compact(decoded)]
    return forms



_SECRET_KEY_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|COOKIE)", re.I)
_known_cache: dict = {"sig": None, "values": []}


def _secret_sources() -> list[Path]:
    paths = [ROOT / ".env", ROOT / "qq_tools" / "config.json", ROOT / "config" / "chat" / "config.toml"]
    paths += sorted((ROOT / "config" / "chat" / "models").glob("*.json"))
    return [p for p in paths if p.exists()]


def _collect_json_secrets(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and _SECRET_KEY_RE.search(str(k)):
                out.append(v)
            else:
                _collect_json_secrets(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_json_secrets(v, out)


def _known_secrets() -> list[str]:
    sources = _secret_sources()
    sig = tuple((str(p), p.stat().st_mtime) for p in sources)
    if sig == _known_cache["sig"]:
        return _known_cache["values"]
    values: list[str] = []
    for p in sources:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if p.suffix == ".json":
            try:
                _collect_json_secrets(json.loads(text), values)
            except Exception:
                pass
            continue
        for line in text.splitlines():
            m = re.match(r"\s*([A-Za-z_][\w.]*)\s*=\s*(.+?)\s*$", line)
            if m and _SECRET_KEY_RE.search(m.group(1)):
                values.append(m.group(2).strip().strip("\"'"))

    cleaned = sorted({v for v in values if len(v) >= 8 and not v.lower().startswith(("http://", "https://"))})
    _known_cache.update(sig=sig, values=cleaned)
    return cleaned


def secret_reason(text: str) -> str:
    """命中路径/凭据时返回类型（本机路径 / 凭据 / 已知凭据），否则返回空串。"""
    if not text or not text.strip():
        return ""
    forms = _forms(text)
    squashed = _squash(forms[0])
    for secret in _known_secrets():
        compact_secret = _compact(secret)
        if any(secret in f or (len(compact_secret) >= 8 and compact_secret in f) for f in forms):
            return "已知凭据"
        squashed_secret = _squash(secret)
        if len(squashed_secret) >= 8 and squashed_secret in squashed:
            return "已知凭据"
    for name, pattern in SECRET_RULES:
        if any(pattern.search(f) for f in forms):
            return name
    return ""


def _qw(section: str, key: str, default):
    """读面板「配置管理 → qianwen」里的设置（guards/settings.py）；读不到就用默认值。"""
    try:
        from guards.settings import value

        return value(section, key, default)
    except Exception:
        return default


def block_notice() -> str:
    text = str(_qw("outbound", "block_notice", BLOCK_NOTICE) or "").strip()

    return text if text and not secret_reason(text) else BLOCK_NOTICE


def guard_text(text: str) -> tuple[str, list[str], bool]:
    """检查一段出站文本，返回 (要发送的文本, 命中类型, 是否整条拦截)。"""
    reason = secret_reason(text)
    if reason:
        return block_notice(), [reason], True
    if not _qw("outbound", "mask_personal_info", True):
        return text, [], False
    cleaned, hits = scrub(text)
    return cleaned, hits, False


def log_event(
    where: str, hits: list[str], blocked: bool, preview: str = "", length: int = 0, target: str = ""
) -> None:
    """只记命中类型；被拦截时只记长度，不记原文。target（g群号 / pQQ号）有值且整条拦截时，私聊通知机器人管理员。"""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        detail = f"已拦截 原文{length}字" if blocked else preview[:200]
        with LOG.open("a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{where}\t{','.join(dict.fromkeys(hits))}\t{detail}\n"
            )
    except Exception:
        pass
    if blocked and target:
        _notify_admins(target, hits)




_notified: dict[str, float] = {}


def _notify_admins(target: str, hits: list[str]) -> None:
    """她的消息被整条拦下时，私聊告诉机器人管理员（.env 的 SUPERUSERS）。
    只说在哪、什么类型、几点，不转发原文；同一个群/私聊 5 分钟内最多通知一次。面板「qianwen → 出站拦截」可关。"""
    try:
        if not _qw("outbound", "notify_admin", True):
            return
        now = time.time()
        if now - _notified.get(target, 0) < 300:
            return
        import asyncio

        import nonebot

        admins = [int(x) for x in nonebot.get_driver().config.superusers if str(x).isdigit()]
        bots = list(nonebot.get_bots().values())
        if not admins or not bots:
            return
        _notified[target] = now
        where = ("群 " + target[1:] + " ") if target.startswith("g") else ("和 " + target[1:] + " 的私聊")
        kinds = "、".join(dict.fromkeys(h for h in hits if h)) or "疑似敏感内容"
        text = (
            f"【拦截通知】{datetime.now():%H:%M} 她在{where}里有一条回复疑似含「{kinds}」，已拦下没有发出去。"
            "原文不转发，需要排查可以看面板「对话追踪」和日志。"
        )

        async def _send() -> None:
            for uid in admins:
                with contextlib.suppress(Exception):
                    await bots[0].send_private_msg(user_id=uid, message=text)

        asyncio.get_running_loop().create_task(_send())
    except Exception:
        pass




_SEND_APIS = {
    "send_msg",
    "send_group_msg",
    "send_private_msg",
    "send_group_forward_msg",
    "send_private_forward_msg",
    "send_forward_msg",
}


class _Result:
    def __init__(self) -> None:
        self.hits: list[str] = []
        self.blocked = False
        self.length = 0


def _clean_message(message, res: _Result):
    """遍历一条消息（str / Message / 段列表 / 段字典），对文本段做检查。"""
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    def one_text(text: str) -> str:
        text = strip_mention_marks(text)
        res.length += len(text)
        cleaned, hits, blocked = guard_text(text)
        res.hits += hits
        res.blocked = res.blocked or blocked
        return cleaned

    if isinstance(message, str):
        return one_text(message)
    if isinstance(message, MessageSegment):
        if message.type == "text":
            return MessageSegment.text(one_text(message.data.get("text", "")))
        if message.type == "node" and "content" in message.data:
            data = dict(message.data)
            data["content"] = _clean_message(data["content"], res)
            return MessageSegment("node", data)
        return message
    if isinstance(message, dict):
        seg_type = message.get("type")
        data = message.get("data") or {}
        if seg_type == "text":
            return {**message, "data": {**data, "text": one_text(str(data.get("text", "")))}}
        if seg_type == "node" and "content" in data:
            return {**message, "data": {**data, "content": _clean_message(data["content"], res)}}
        return message
    if isinstance(message, (list, tuple)):
        items = [_clean_message(item, res) for item in message]
        return Message(items) if isinstance(message, Message) else items
    return message


def _blocked_payload(api: str, data: dict) -> dict:
    """整条拦截：普通消息换成提示；合并转发保留第一个节点的署名，内容换成提示。"""
    from nonebot.adapters.onebot.v11 import Message

    data = dict(data)
    if "messages" in data:
        nodes = data.get("messages") or []
        first = nodes[0] if nodes else {}
        node_data = first.get("data", {}) if isinstance(first, dict) else getattr(first, "data", {})
        notice = block_notice()
        data["messages"] = [{
            "type": "node",
            "data": {
                "name": node_data.get("name") or node_data.get("nickname") or "",
                "uin": str(node_data.get("uin") or node_data.get("user_id") or ""),
                "content": notice,
            },
        }]
    if "message" in data:
        data["message"] = Message(block_notice())
    return data




_SILENCE_RE = re.compile(r"[〔【\[(（]\s*不回\s*[〕】\])）]")
_SILENCE_APIS = {"send_msg", "send_group_msg", "send_private_msg"}
_FILLER = " \t\r\n。，,.!！?？~～…、"


def _strip_silence(message):
    """去掉消息里的「〔不回〕」标记。返回 (新消息, 是否有标记, 去掉后是否还有内容)。"""
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    if isinstance(message, str):
        new = _SILENCE_RE.sub("", message)
        return new, new != message, bool(new.strip(_FILLER))
    if isinstance(message, (list, tuple)):
        items, found, content = [], False, False
        for seg in message:
            seg_type = seg.type if isinstance(seg, MessageSegment) else (seg.get("type") if isinstance(seg, dict) else None)
            data = seg.data if isinstance(seg, MessageSegment) else (seg.get("data") or {} if isinstance(seg, dict) else {})
            if seg_type == "text":
                text = str(data.get("text", ""))
                new = _SILENCE_RE.sub("", text)
                found = found or new != text
                content = content or bool(new.strip(_FILLER))
                if new.strip(_FILLER) or new == text:
                    items.append(MessageSegment.text(new) if isinstance(seg, MessageSegment) else {**seg, "data": {**data, "text": new}})
            else:

                content = content or seg_type not in ("reply", "at")
                items.append(seg)
        return (Message(items) if isinstance(message, Message) else items), found, content
    return message, False, True




_pending_ref: dict[int, tuple] = {}
_REF_RE = re.compile(r"[〔【\[]\s*(?:引用|回复)\s*[:：]\s*(-?\d+)\s*[〕】\]]")
_AT_RE = re.compile(r"[〔【\[]\s*@\s*(\d{5,12})\s*[〕】\]]")
_MAX_AT = 3


def strip_mention_marks(text: str) -> str:
    return _AT_RE.sub("", _REF_RE.sub("", text))


def _to_segments(message) -> list:
    from nonebot.adapters.onebot.v11 import MessageSegment

    if isinstance(message, str):
        return [MessageSegment.text(message)]
    if isinstance(message, MessageSegment):
        return [message]
    out = []
    for seg in message or []:
        if isinstance(seg, MessageSegment):
            out.append(seg)
        elif isinstance(seg, dict):
            out.append(MessageSegment(str(seg.get("type")), dict(seg.get("data") or {})))
    return out


def _fake_or_empty(message_id: int) -> bool:
    """0 或冷场叫醒的假消息 id。QQ 的真消息 id 可能是负数，不能按正负判断。"""
    if not message_id:
        return True
    try:
        from .wake_policy import is_proactive

        return is_proactive(message_id)
    except Exception:
        return False


async def _valid_ref(bot, original, group_id: int, message_id: int) -> bool:
    if _fake_or_empty(message_id):
        return False
    from .wake_policy import known_message

    if known_message(group_id, message_id):
        return True
    try:
        res = await original(bot, "get_msg", message_id=message_id)
        return int((res or {}).get("group_id") or 0) == int(group_id)
    except Exception:
        return False


async def _valid_at(bot, original, group_id: int, user_id: int) -> bool:
    if str(user_id) == str(getattr(bot, "self_id", "")):
        return False
    from .wake_policy import known_speaker

    if known_speaker(group_id, user_id):
        return True
    try:
        await original(bot, "get_group_member_info", group_id=group_id, user_id=user_id)
        return True
    except Exception:
        return False


async def _apply_mentions(bot, original, message, group_id: int | None):
    """处理引用和 @ 标记。返回 (新消息, 是否改动, 是否还有内容)。

    只在群里生效；标记里的消息 id 必须是本群的消息，QQ 号必须是本群成员，否则标记直接去掉。
    假事件（冷场叫醒）带来的引用也在这里去掉。
    """
    from nonebot import logger
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    segs = _to_segments(message)

    def _rid(seg) -> int:
        try:
            return int(seg.data.get("id"))
        except (TypeError, ValueError):
            return 0

    has_marks = any(
        s.type == "text" and (_REF_RE.search(str(s.data.get("text", ""))) or _AT_RE.search(str(s.data.get("text", ""))))
        for s in segs
    )
    bad_reply = any(s.type == "reply" and _fake_or_empty(_rid(s)) for s in segs)
    if not has_marks and not bad_reply:
        return message, False, True

    allow_quote = bool(_qw("mention", "allow_quote", True))
    allow_at = bool(_qw("mention", "allow_at", True))
    out: list = []
    ref_ids: list[int] = []
    dropped: list[str] = []
    at_count = 0
    for seg in segs:
        if seg.type == "reply":
            if not _fake_or_empty(_rid(seg)):
                out.append(seg)
            continue
        if seg.type != "text":
            out.append(seg)
            continue
        text = str(seg.data.get("text", ""))
        ref_ids += [int(m.group(1)) for m in _REF_RE.finditer(text)]
        text = _REF_RE.sub("", text)
        pos = 0
        for m in _AT_RE.finditer(text):
            if text[pos:m.start()]:
                out.append(MessageSegment.text(text[pos:m.start()]))
            uid = int(m.group(1))
            if allow_at and group_id and at_count < _MAX_AT and await _valid_at(bot, original, group_id, uid):
                out.append(MessageSegment.at(uid))
                at_count += 1
            else:
                dropped.append(f"@{uid}")
            pos = m.end()
        if text[pos:]:
            out.append(MessageSegment.text(text[pos:]))
    if ref_ids:
        rid = ref_ids[0]
        if allow_quote and group_id and await _valid_ref(bot, original, group_id, rid):
            out = [s for s in out if s.type != "reply"]
            out.insert(0, MessageSegment.reply(rid))
        else:
            dropped.append(f"引用{rid}")

    tidy: list = []
    for seg in out:
        if seg.type == "text":
            t = str(seg.data.get("text", ""))
            if not t.strip():
                continue
            prev = tidy[-1].type if tidy else None
            t = t.lstrip() if prev in (None, "reply") else t
            if prev == "at" and not t.startswith(" "):
                t = " " + t.lstrip()
            seg = MessageSegment.text(t)
        tidy.append(seg)
    if dropped:
        logger.info(f"[引用/@] 去掉了无效标记：{'、'.join(dropped)}")
    has_content = any(
        s.type == "at" or (s.type == "text" and str(s.data.get("text", "")).strip(_FILLER)) or s.type not in ("text", "reply", "at")
        for s in tidy
    )
    return Message(tidy), True, has_content







_RECENT_SECONDS = 120
_RECENT_MAX = 10
_recent_out: dict[str, deque] = {}


def _target_key(api: str, data: dict) -> str | None:
    if api not in _SILENCE_APIS:
        return None
    gid = _group_of(api, data)
    if gid:
        return f"g{gid}"
    if api == "send_private_msg" or (api == "send_msg" and data.get("message_type") == "private"):
        uid = data.get("user_id")
        return f"p{uid}" if uid else None
    return None


def _plain_text(message) -> str:
    if isinstance(message, str):
        return message
    return "".join(str(s.data.get("text", "")) for s in _to_segments(message) if s.type == "text")


def _joined_hit(api: str, data: dict) -> bool:
    """把这条和最近两分钟内发给同一目标的几条拼起来查；没命中就把这条记进去。"""
    key = _target_key(api, data)
    if not key or "message" not in data:
        return False
    text = _plain_text(data["message"])
    if not text.strip():
        return False
    now = time.time()
    q = _recent_out.setdefault(key, deque(maxlen=_RECENT_MAX))
    while q and now - q[0][0] > _RECENT_SECONDS:
        q.popleft()
    if q and secret_reason("".join(t for _, t in q) + text):
        q.clear()
        return True
    q.append((now, text))
    return False


def _forget_recent(api: str, data: dict) -> None:
    key = _target_key(api, data)
    if key:
        _recent_out.pop(key, None)








_send_times: dict[str, deque] = {}
_rate_logged: dict[str, float] = {}


def rate_key(api: str, data: dict) -> str | None:
    if api not in _SEND_APIS:
        return None
    gid = _group_of(api, data)
    if gid:
        return f"g{gid}"
    if api in ("send_private_msg", "send_private_forward_msg") or (
        api in ("send_msg", "send_forward_msg") and data.get("message_type") == "private"
    ):
        uid = data.get("user_id")
        return f"p{uid}" if uid else None
    return None


def event_rate_key(event) -> str | None:
    gid = getattr(event, "group_id", None)
    if gid:
        return f"g{gid}"
    uid = getattr(event, "user_id", None)
    return f"p{uid}" if uid else None


def send_quota(key: str | None) -> int:
    """这个群/私聊现在还能发几条。关掉限速或不限时返回一个很大的数。"""
    if not key or not _qw("send", "enabled", True):
        return 1 << 30
    per_min = int(_qw("send", "per_minute", 8) or 0)
    per_hour = int(_qw("send", "per_hour", 60) or 0)
    now = time.time()
    q = _send_times.setdefault(key, deque(maxlen=2000))
    while q and now - q[0] > 3600:
        q.popleft()
    left = 1 << 30
    if per_min > 0:
        left = min(left, per_min - sum(1 for t in q if now - t < 60))
    if per_hour > 0:
        left = min(left, per_hour - len(q))
    return max(left, 0)


def note_sent(key: str | None) -> None:
    if key:
        _send_times.setdefault(key, deque(maxlen=2000)).append(time.time())


def log_rate_drop(key: str, count: int = 1) -> None:
    """超限丢弃时记一笔（同一个群/私聊一分钟最多记一次，免得刷日志）。"""
    now = time.time()
    if now - _rate_logged.get(key, 0) < 60:
        return
    _rate_logged[key] = now
    try:
        from nonebot import logger

        logger.warning(f"[发送限速] {key} 已达上限，丢弃 {count} 条（面板「qianwen → 发送限速」可调）")
    except Exception:
        pass




def typing_gap(next_text: str) -> float | None:
    """发下一条之前停几秒：基础停顿 + 下一条字数 × 每字时间，封顶，±15% 抖动，偶尔多停 2~5 秒。

    关掉「按打字速度停顿」时返回 None，调用方用原来的随机停顿。
    """
    import random

    if not _qw("send", "typing_pace", True):
        return None
    base = max(0, int(_qw("send", "gap_base_ms", 800) or 0))
    per_char = max(0, int(_qw("send", "gap_per_char_ms", 100) or 0))
    cap = max(500, int(_qw("send", "gap_max_ms", 8000) or 8000))

    chars = len(re.sub(r"\s+", "", strip_mention_marks(_SILENCE_RE.sub("", next_text or ""))))
    ms = min(cap, base + chars * per_char) * random.uniform(0.85, 1.15)
    if random.random() < float(_qw("send", "long_pause_probability", 0.1) or 0):
        ms += random.uniform(2000, 5000)
    return ms / 1000


def _trace_sent(api: str, data: dict, outcome: str, started: float | None = None) -> None:
    """本地补丁：对话追踪记一条「发送」（guards/trace.py）。
    记的是最终要发出去的内容；被拦截的只记拦截提示和命中类型，不记原文。"""
    try:
        from guards import trace

        if not trace.current():
            return
        if "messages" in data:
            text = f"[合并转发 {len(data.get('messages') or [])} 段]"
        else:
            text = _plain_text(data.get("message") or "")
        target = rate_key(api, data) or ""
        trace.sent(api, target, text, outcome, started=started)
    except Exception:
        pass


def _clean_marks_only(message):
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    if isinstance(message, str):
        return strip_mention_marks(message)
    return Message(
        MessageSegment.text(strip_mention_marks(str(s.data.get("text", "")))) if s.type == "text" else s
        for s in _to_segments(message)
    )


def _group_of(api: str, data: dict) -> int | None:
    if api in ("send_group_msg", "send_group_forward_msg"):
        gid = data.get("group_id")
    elif api == "send_msg" and data.get("message_type") != "private":
        gid = data.get("group_id")
    else:
        gid = None
    try:
        return int(gid) if gid else None
    except (TypeError, ValueError):
        return None


_installed = False


def install() -> None:
    """给 OneBot Bot.call_api 打补丁：所有发送类接口在发出前过守卫。"""
    global _installed
    if _installed:
        return
    try:
        from nonebot.adapters.onebot.v11 import Bot
    except Exception:
        return

    original = Bot.call_api

    async def _call_api(self, api: str, **data):
        group_id = _group_of(api, data) if api in _SEND_APIS else None
        if api in _SILENCE_APIS and "message" in data:
            try:
                new_message, found, has_content = _strip_silence(data["message"])
                if found:
                    data = {**data, "message": new_message}
                    if not has_content:

                        if group_id:
                            from .wake_policy import note_silence

                            note_silence(group_id)
                        _trace_sent(api, data, "选择不回（〔不回〕不发出）")
                        return {"message_id": 0}
            except Exception:
                pass
            try:
                new_message, changed, has_content = await _apply_mentions(self, original, data["message"], group_id)
                if changed:
                    if not has_content:

                        rid = next((s.data.get("id") for s in new_message if s.type == "reply"), None)
                        if rid and group_id:
                            _pending_ref[group_id] = (rid, time.time())
                        _trace_sent(api, data, "只有引用标记，并到下一条")
                        return {"message_id": 0}
                    data = {**data, "message": new_message}
                if group_id and group_id in _pending_ref:
                    rid, ts = _pending_ref.pop(group_id)
                    segs = _to_segments(data["message"])
                    if time.time() - ts <= 15 and not any(s.type == "reply" for s in segs):
                        from nonebot.adapters.onebot.v11 import Message, MessageSegment

                        data = {**data, "message": Message([MessageSegment.reply(rid), *segs])}
            except Exception:

                with contextlib.suppress(Exception):
                    data = {**data, "message": _clean_marks_only(data["message"])}
        outcome = "已发送"
        if api in _SEND_APIS:
            try:
                res = _Result()
                cleaned = dict(data)
                for field in ("message", "messages"):
                    if field in cleaned:
                        cleaned[field] = _clean_message(cleaned[field], res)
                if res.blocked:
                    data = _blocked_payload(api, data)
                    log_event(api, res.hits, True, length=res.length, target=rate_key(api, data) or "")
                    _forget_recent(api, data)
                    outcome = f"拦截（{'、'.join(res.hits)}），改发提示"
                elif _joined_hit(api, cleaned):

                    data = _blocked_payload(api, data)
                    log_event(api, ["分条拼接后命中"], True, length=res.length, target=rate_key(api, data) or "")
                    outcome = "拦截（分条拼接后命中），改发提示"
                else:
                    if res.hits:
                        log_event(api, res.hits, False, preview=str(cleaned.get("message") or cleaned.get("messages")))
                        outcome = f"已发送（打码：{'、'.join(res.hits)}）"
                    data = cleaned
            except Exception:

                data = _blocked_payload(api, data)
                log_event(api, ["守卫异常"], True, target=rate_key(api, data) or "")
                outcome = "拦截（守卫异常），改发提示"
        key = rate_key(api, data)
        if key and send_quota(key) <= 0:
            log_rate_drop(key)
            _trace_sent(api, data, "超过发送限速，丢弃")
            return {"message_id": 0}
        _t_send = time.time()
        result = await original(self, api, **data)
        note_sent(key)
        if api in _SEND_APIS:
            _trace_sent(api, data, outcome, started=_t_send)
        if group_id:

            try:
                from .wake_policy import note_spoke

                note_spoke(group_id)
            except Exception:
                pass
        return result

    Bot.call_api = _call_api
    _installed = True
