import json
import os
from datetime import datetime
from typing import Any

import pytz
from amrita_core.utils import remove_think_tag
from amrita_sense.logging import debug_log
from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
)

from ..config import config_manager


async def is_member(event: GroupMessageEvent, bot: Bot) -> bool:
    """判断用户是否为群组普通成员"""

    user_role = (
        (
            await bot.get_group_member_info(
                group_id=event.group_id, user_id=event.user_id
            )
        )["role"]
        if not event.sender.role
        else event.sender.role
    )
    return user_role == "member"


def format_datetime_timestamp(time: int) -> str:
    """将时间戳格式化为日期、星期和时间字符串"""
    now = datetime.fromtimestamp(time)
    formatted_date = now.strftime("%Y-%m-%d")
    formatted_weekday = now.strftime("%A")
    formatted_time = now.strftime("%I:%M:%S %p")
    return f"[{formatted_date} {formatted_weekday} {formatted_time}]"


def split_message_into_chats(text: str, max_length: int = 100) -> list[str]:
    """
    根据标点符号分割文本为句子

    Args:
        text: 要分割的文本
        max_length: 单个句子的最大长度，默认100个字符

    Returns:
        list[str]: 分割后的句子列表
    """
    if not text or not text.strip():
        return []

    sentences = []
    start = 0
    for match in config_manager.config.function.pattern.finditer(text):
        end = match.end()
        if sentence := text[start:end].strip():
            sentences.append(sentence)
        start = end


    if start < len(text):
        if remaining := text[start:].strip():
            sentences.append(remaining)


    result = []
    for sentence in sentences:
        if len(sentence) <= max_length:
            result.append(sentence)
        else:

            chunks = [
                sentence[i : i + max_length]
                for i in range(0, len(sentence), max_length)
            ]
            result.extend(chunks)

    return result


async def synthesize_forward_message(forward_msg: dict, bot: Bot) -> str:
    """合成消息数组内容为字符串
    这是一个示例的消息集合/数组：
    [
        {
            "type": "node",
            "data": {
                "user_id": "0",
                "nickname": "某人",
                "content": "[CQ:face,id=123]哈喽～",
            }
        },
        {
            "type": "node",
            "data": {
                "user_id": "0",
                "nickname": "某人",
                "content": [
                    {"type": "face", "data": {"id": "123"}},
                    {"type": "text", "data": {"text": "哈喽～"}},
                ]
            }
        }
    ]
    """
    return await _render_forward(forward_msg, bot, depth=1)


def _qw(section: str, key: str, default):
    """读面板「配置管理 → qianwen」里的设置（guards/settings.py）；读不到就用默认值。"""
    try:
        from guards.settings import value

        return value(section, key, default)
    except Exception:
        return default



FORWARD_MAX_DEPTH = 3
FORWARD_MAX_TOTAL = 60


def forward_max_depth() -> int:
    return int(_qw("forward", "max_depth", FORWARD_MAX_DEPTH))


def forward_nodes(forward_msg) -> list:
    """get_forward_msg 的返回可能是列表，也可能是 {"messages": [...]}，统一成节点列表。"""
    if isinstance(forward_msg, list):
        return forward_msg
    if isinstance(forward_msg, dict):
        for key in ("messages", "message", "data"):
            value = forward_msg.get(key)
            if isinstance(value, list):
                return value
    return []


def forward_node_content(node) -> list | str:
    """取一个转发节点的内容：兼容 {type:node,data:{content}} 和 {sender,message} 两种形状。"""
    if not isinstance(node, dict):
        return ""
    data = node.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return data
    data = data if isinstance(data, dict) else {}
    for value in (node.get("message"), node.get("content"), data.get("message"), data.get("content")):
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value] if value.get("type") else value.get("content") or []
        if isinstance(value, str) and value:
            return value
    return ""


def forward_node_sender(node) -> tuple[str, str]:
    data = node.get("data") if isinstance(node, dict) else None
    data = data if isinstance(data, dict) else {}
    sender = node.get("sender") if isinstance(node, dict) else None
    sender = sender if isinstance(sender, dict) else {}
    name = (
        data.get("nickname") or data.get("name") or sender.get("card") or sender.get("nickname")
        or (node.get("nickname") if isinstance(node, dict) else "") or ""
    )
    uid = data.get("user_id") or data.get("uin") or sender.get("user_id") or (node.get("user_id") if isinstance(node, dict) else "") or ""
    return str(name), str(uid)


def forward_id_of(segment) -> str:
    """从 forward 段里取 id，只允许安全字符。"""
    data = segment.get("data") if isinstance(segment, dict) else getattr(segment, "data", None)
    data = data if isinstance(data, dict) else {}
    fid = str(data.get("id") or data.get("res_id") or data.get("file_id") or "").strip()
    return fid if fid and len(fid) <= 256 and all(c.isalnum() or c in "_:.-/=+@" for c in fid) else ""


async def _render_forward(forward_msg, bot: Bot, depth: int, budget: list | None = None) -> str:
    """把一个合并转发渲染成文本：图片标成 [图片]，嵌套转发按层缩进展开。"""
    budget = budget if budget is not None else [int(_qw("forward", "max_total", FORWARD_MAX_TOTAL))]
    max_depth = int(_qw("forward", "max_depth", FORWARD_MAX_DEPTH))
    nodes = forward_nodes(forward_msg)
    try:
        limit = int(os.getenv("FORWARD_MAX_NODES", "20"))
    except ValueError:
        limit = 20
    indent = "  " * (depth - 1)
    result = ""
    shown = 0
    has_media = False
    for node in nodes[:limit]:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        shown += 1
        try:
            nickname, qq = forward_node_sender(node)
            result += f"{indent}[{nickname}({qq})]说："
            content = forward_node_content(node)
            if isinstance(content, str):
                result += content
            for seg in content if isinstance(content, list) else []:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type")
                data = seg.get("data") or {}
                if seg_type == "text":
                    result += str(data.get("text", ""))
                elif seg_type == "at":
                    result += f" [@{data.get('qq', '')}]"
                elif seg_type in ("image", "mface"):
                    has_media = True
                    result += "[图片]" if seg_type == "image" else "[表情]"
                elif seg_type == "face":
                    result += "[表情]"
                elif seg_type in ("json", "xml"):
                    title, urls = _extract_card(str(data.get("data") or ""))
                    result += "[分享卡片：" + (title or "无标题") + "".join(f" {u}" for u in urls) + "]"
                elif seg_type == "forward":
                    fid = forward_id_of(seg)
                    if depth >= max_depth or not fid:
                        result += f"[嵌套的合并转发 id={fid}，太深了没展开]"
                    else:
                        try:
                            inner = await bot.get_forward_msg(id=fid)
                            result += f"[嵌套的合并转发 id={fid}]\n" + await _render_forward(inner, bot, depth + 1, budget)
                        except Exception:
                            result += f"[嵌套的合并转发 id={fid}，读取失败]"
                elif seg_type:
                    result += f"[{seg_type}]"
        except Exception as e:
            logger.opt(colors=True, exception=e, raw=True).warning(f"解析转发节点出错：{e!s}")
            result += "<!--该消息段无法被解析-->"
        result += "\n"
    omitted = len(nodes) - shown
    if omitted > 0:
        result += f"{indent}……（其余 {omitted} 条已省略）\n"
    if has_media and depth == 1:
        result += "（转发里有图片，想看就用 get_forward_msg 打开这个转发）\n"
    return result


def _extract_card(raw: str) -> tuple[str, list[str]]:
    """从 QQ 分享卡片（json/xml）里抠出标题和链接。"""
    import re as _re

    title = ""
    urls: list[str] = []
    text = raw or ""

    def _add(u: str) -> None:
        u = u.strip().rstrip("),.;")
        if not u:
            return
        if not _re.match(r"https?://", u):
            u = "https://" + u

        if _re.match(r"https?://b23\.tv/", u):
            u = u.split("?", 1)[0]
        if u not in urls:
            urls.append(u)



    link_keys = ("qqdocurl", "jumpUrl", "jumpurl", "url")

    def _walk(node) -> None:
        if isinstance(node, dict):
            for key in link_keys:
                value = node.get(key)
                if isinstance(value, str) and "." in value:
                    _add(value)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    try:
        obj = json.loads(text) if text.strip().startswith("{") else None
        if isinstance(obj, dict):
            title = str(obj.get("prompt") or "")
            meta = obj.get("meta") or {}
            if isinstance(meta, dict):
                for value in meta.values():
                    if isinstance(value, dict):

                        for extra in (value.get("title"), value.get("desc")):
                            extra = str(extra or "").strip()
                            if extra and extra not in title:
                                title = f"{title}（{extra}）" if title else extra
            _walk(meta)
    except Exception:
        pass
    if not urls:

        for u in _re.findall(r"https?://[^\s\"'<>\\]+", text.replace("\\/", "/")):
            if not _re.search(r"\.(png|jpe?g|gif|webp)(\?|$)", u, _re.I):
                _add(u)
    return title[:80], urls[:3]


async def synthesize_message(message: Message, bot: Bot) -> str:
    """合成消息内容为字符串"""
    content = ""
    for segment in message:
        if segment.type == "text":
            content += segment.data["text"]
        elif segment.type == "at":
            content += f"\\（at: @{segment.data.get('name')}(QQ:{segment.data['qq']}))"
        elif (
            segment.type == "forward"
            and config_manager.config.function.synthesize_forward_message
        ):
            fid = forward_id_of(segment)
            forward = await bot.get_forward_msg(id=segment.data["id"])
            debug_log(str(forward))
            content += (
                f" \\（合并转发 id={fid}\n"
                + await synthesize_forward_message(forward, bot)
                + "）\\\n"
            )
        elif segment.type in ("json", "xml"):
            title, urls = _extract_card(str(segment.data.get("data") or ""))
            if title or urls:
                content += "\\（分享卡片：" + (title or "无标题")
                for u in urls:
                    content += f" {u}"
                content += "）"
    return content


def split_list(lst: list, threshold: int) -> list[Any]:
    """将列表分割为多个子列表，每个子列表长度不超过阈值"""
    if len(lst) <= threshold:
        return [lst]
    return [lst[i : i + threshold] for i in range(0, len(lst), threshold)]


def format_current_datetime(utc_time: datetime | None = None) -> str:
    """当前时间，格式 ``YYYY-MM-DD 星期 HH:MM:SS``（Asia/Shanghai，不带方括号）"""
    utc_time = utc_time or datetime.now(pytz.utc)
    now = utc_time.astimezone(pytz.timezone("Asia/Shanghai"))
    formatted_date = now.strftime("%Y-%m-%d")
    formatted_weekday = now.strftime("%A")
    formatted_time = now.strftime("%H:%M:%S")
    return f"{formatted_date} {formatted_weekday} {formatted_time}"


def get_current_datetime_timestamp(utc_time: datetime | None = None) -> str:
    """获取当前时间并格式化为日期、星期和时间字符串（带方括号，供 legacy 文本格式使用）"""
    return f"[{format_current_datetime(utc_time)}]"


async def get_friend_name(qq_number: int, bot: Bot) -> str:
    """获取好友昵称"""
    friend_list = await bot.get_friend_list()
    return next(
        (
            friend["nickname"]
            for friend in friend_list
            if friend["user_id"] == qq_number
        ),
        "",
    )


__all__ = ["remove_think_tag"]
