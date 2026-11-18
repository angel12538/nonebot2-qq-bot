from nonebot import logger
import contextlib
import random
import time

import nonebot
from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11.event import (
    Event,
    GroupMessageEvent,
    MessageEvent,
)
from nonebot_plugin_amrita.memory import MemorySchema
from typing_extensions import override

from amrita.plugins.chat.utils.libchat import usage_enough
from amrita.plugins.chat.utils.lock import get_group_lock, get_private_lock
from amrita.plugins.chat.utils.sql import make_uni_id
from amrita.plugins.perm.API.admin import is_lp_admin

from .config import config_manager
from .utils.context_store import append_context_record
from .utils.data_access import get_group_config, get_memory, update_memory
from .utils.debounce import has_active_batch
from .utils.format import format_msg_tagged
from .utils.functions import format_current_datetime, synthesize_message
from .utils.preset import is_multimodal_enabled

nb_config = get_driver().config


class FakeEvent(Event):
    """伪事件类，用于模拟用户事件"""

    user_id: int

    @override
    def get_user_id(self) -> str:
        return str(self.user_id)


async def is_bot_globally_enabled(event: Event) -> bool:
    """仅检查全局开关与功能开关，不检查每群的 enable 标记。

    供聊天开关（/chat）等命令使用，避免 /chat off 关闭群聊后
    is_bot_enabled 失效，导致无法再次执行 /chat on。
    """
    gid = getattr(event, "group_id", None)
    is_in_group = gid is not None
    if not config_manager.config.enable:
        return False
    elif is_in_group and not config_manager.config.function.enable_group_chat:
        return False
    elif not is_in_group and not config_manager.config.function.enable_private_chat:
        return False
    with contextlib.suppress(Exception):
        bots = nonebot.get_bots()
        if event.get_user_id() in bots:

            from amrita.plugins.chat.utils import wake_policy

            if not wake_policy.is_proactive(getattr(event, "message_id", 0)):
                return False
    return True


async def is_bot_enabled(event: Event) -> bool:
    gid = getattr(event, "group_id", None)
    if not await is_bot_globally_enabled(event):
        return False
    if gid is not None:
        data = await get_group_config(gid)
        return data.enable
    return True


async def is_group_admin(event: GroupMessageEvent, bot: Bot) -> bool:
    try:
        role: str = (
            (
                await bot.get_group_member_info(
                    group_id=event.group_id, user_id=event.user_id
                )
            )["role"]
            if not event.sender.role
            else event.sender.role
        )
        if role != "member":
            return True
        if await is_bot_admin(event):
            return True
    except Exception:
        logger.warning(f"获取群成员信息失败: {event.group_id} {event.user_id}")
    return False


async def is_bot_admin(event: Event) -> bool:
    return await is_lp_admin(event)


async def is_group_admin_if_is_in_group(event: MessageEvent, bot: Bot) -> bool:
    if isinstance(event, GroupMessageEvent):
        return await is_group_admin(event, bot)
    return True


async def should_respond_to_message(event: MessageEvent, bot: Bot) -> bool:
    """根据配置和消息事件判断是否需要回复"""
    lock = (
        get_group_lock(event.group_id)
        if isinstance(event, GroupMessageEvent)
        else get_private_lock(event.user_id)
    )
    async with lock:
        message = event.get_message()
        message_text = message.extract_plain_text().strip()

        from amrita.plugins.chat.utils import wake_policy

        if not isinstance(event, GroupMessageEvent):

            if wake_policy.seen_in_wait(f"p{event.user_id}", getattr(event, "message_id", 0)):
                return False
            if wake_policy.silent_mode() and not await is_bot_admin(event):
                return False
            return True

        is_group = True
        ins_id: int = event.group_id


        if wake_policy.is_proactive(getattr(event, "message_id", 0)):
            return True



        wake_policy.note_message(ins_id, message_text)

        wake_policy.note_message_id(ins_id, getattr(event, "message_id", 0), event.user_id)


        if has_active_batch(event):
            return True


        direct = None
        if "at" in config_manager.config.autoreply.keywords:
            if event.is_tome():
                direct = "at"
        if direct is None and config_manager.config.autoreply.keywords_mode == "starts_with":
            if message_text.startswith(
                tuple(i for i in config_manager.config.autoreply.keywords if i != "at")
            ):
                direct = "keyword"
        elif direct is None and config_manager.config.autoreply.keywords_mode == "contains":
            if any(
                keyword in message_text
                for keyword in config_manager.config.autoreply.keywords
                if keyword != "at"
            ):
                direct = "keyword"


        rate = 0.0
        if direct is None and config_manager.config.autoreply.enable:
            fk = (await get_group_config(ins_id)).autoreply
            if config_manager.config.autoreply.global_enable or fk:
                rate = wake_policy.current_rate(config_manager.config.autoreply.probability, ins_id)



        addressed_other = False
        with contextlib.suppress(Exception):
            reply_uid = event.reply.sender.user_id if event.reply else None
            addressed_other = bool(reply_uid and int(reply_uid) != int(event.self_id)) or any(
                seg.type == "at" and str(seg.data.get("qq")) != str(event.self_id) for seg in message
            )

        skip_wake = wake_policy.seen_in_wait(f"g{ins_id}", getattr(event, "message_id", 0)) or (
            wake_policy.silent_mode() and not await is_bot_admin(event)
        )
        reason = None if skip_wake else wake_policy.decide_wake(
            ins_id,
            event.user_id,
            getattr(event, "message_id", 0),
            message_text,
            direct,
            rate,
            addressed_other=addressed_other,
        )
        if reason:
            if reason not in wake_policy.DIRECT_REASONS:
                memory_data: MemorySchema = await get_memory(make_uni_id(ins_id, is_group))
                memory_data.memory_json.time = time.time()
                await update_memory(memory_data)

            wake_policy.note_bot_reply(ins_id)
            return True


        if not config_manager.config.context.enable:

            return False

        content = await synthesize_message(message, bot)

        try:
            _mid = getattr(event, "message_id", "")
            if _mid:
                content += f"（消息id:{_mid}）"
        except Exception:
            pass


        role_code = (
            event.sender.role
            if event.sender.role
            else (
                await bot.get_group_member_info(
                    group_id=event.group_id, user_id=event.user_id
                )
            ).get("role", "")
        )
        role = {
            "admin": "群管理员",
            "owner": "群主",
            "member": "普通成员",
        }.get(str(role_code), "普通成员")

        with contextlib.suppress(Exception):
            if await is_bot_admin(event):
                role += "·机器人管理员"


        user_id = event.user_id
        user_name = (
            (await bot.get_group_member_info(group_id=event.group_id, user_id=user_id))[
                "nickname"
            ]
            if not config_manager.config.function.use_user_nickname
            else event.sender.nickname
        )

        uni_id = make_uni_id(ins_id, is_group)


        media_ids: list[str] = []
        image_notices: list[str] = []
        if await is_multimodal_enabled():

            from .handlers.chat.message import _build_image_contents
            from .utils.context_store import PlaceholderContent, TextContent

            try:
                parts = await _build_image_contents(bot, message, uni_id)
            except Exception as e:

                logger.opt(exception=e).warning("上下文图片采集失败，本次只记录文本")
            else:

                try:
                    from .utils.functions import (
                        forward_id_of,
                        forward_max_depth,
                        forward_node_content,
                        forward_nodes,
                    )

                    segs: list = []
                    max_depth = forward_max_depth()
                    seen_fwd: set[str] = set()
                    queue = [
                        (forward_id_of({"data": seg.data}), 1)
                        for seg in message
                        if getattr(seg, "type", "") == "forward"
                    ]
                    while queue and len(seen_fwd) < 10 and len(segs) < 40:
                        fid, depth = queue.pop(0)
                        if not fid or fid in seen_fwd:
                            continue
                        seen_fwd.add(fid)
                        fw = await bot.get_forward_msg(id=fid)
                        for node in forward_nodes(fw)[:20]:
                            c = forward_node_content(node)
                            if isinstance(c, str):
                                continue
                            for s in c:
                                if not isinstance(s, dict):
                                    continue
                                if s.get("type") in ("image", "mface"):
                                    segs.append(s)
                                elif s.get("type") == "forward" and depth < max_depth:
                                    queue.append((forward_id_of(s), depth + 1))
                    if segs:

                        from nonebot.adapters.onebot.v11 import MessageSegment

                        extra = await _build_image_contents(
                            bot,
                            [MessageSegment(s["type"], dict(s.get("data") or {})) for s in segs],
                            uni_id,
                        )
                        parts.extend(extra)
                        if extra:
                            logger.info(f"转发内采集到 {len(extra)} 项图片内容（{len(seen_fwd)} 个转发）")
                except Exception as e:
                    logger.opt(exception=e).warning("转发内图片采集失败")

                media_ids = [
                    item.media_id
                    for item in parts
                    if isinstance(item, PlaceholderContent)
                ]

                image_notices = [
                    item.text
                    for item in parts
                    if isinstance(item, TextContent) and item.text
                ]


        await append_context_record(
            uni_id=uni_id,
            user_id=str(user_id),
            nickname=str(user_name),
            role=role,
            content=format_msg_tagged(
                role,
                str(user_name),
                str(user_id),
                "\n".join([content, *image_notices]) if image_notices else content,
                time=format_current_datetime(),
            ),
            media_ids=media_ids,
        )

        return False


async def should_respond_with_usage_check(event: MessageEvent, bot: Bot) -> bool:
    if await should_respond_to_message(event, bot):
        if not await usage_enough(event) or not (
            await usage_enough(
                FakeEvent(time=0, self_id=0, post_type="", user_id=event.user_id)
            )
            if isinstance(event, GroupMessageEvent)
            else True
        ):
            if event.is_tome():
                with contextlib.suppress(Exception):
                    await bot.send(
                        event,
                        random.choice(config_manager.config.usage_limit.limit_msg),
                    )
                    return False
            return False
        return True
    return False
