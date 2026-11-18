"""冷场时主动开话题。

每隔一段时间（默认 60 秒，面板可调）看一遍白名单里的群：群里安静够久（面板「qianwen → 节奏」里设），又没冷到大家都不在，
最后说话的不是她、她没在潜水、在设定的时段内、今天次数没用完，就按概率给她一次机会
（判断逻辑在 amrita 聊天插件的 wake_policy.proactive_due 里，未抽中则间隔半小时重试）。

给她机会的方式：造一条「冷场」群消息事件交给聊天流程。这条事件不记进群聊记录，
交给模型的是一段说明（不是群友消息），叫醒原因是「冷场开话题」，她也可以回〔不回〕。
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from nonebot import get_bots, get_driver, logger

from guards import settings

_CHECK_SECONDS = 60
_task: list = [None]


def _interval() -> int:
    try:
        return max(30, min(3600, int(settings.value("rhythm", "proactive_check_seconds", _CHECK_SECONDS))))
    except Exception:
        return _CHECK_SECONDS


def _make_event(bot, group_id: int, message_id: int):
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
    from nonebot.adapters.onebot.v11.event import Sender

    from amrita.plugins.chat.utils.wake_policy import PROACTIVE_TEXT

    self_id = int(bot.self_id)
    msg = Message(PROACTIVE_TEXT)
    return GroupMessageEvent(
        time=int(time.time()),
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=self_id,
        message_type="group",
        message_id=message_id,
        message=msg,
        original_message=msg,
        raw_message=PROACTIVE_TEXT,
        font=0,
        sender=Sender(user_id=self_id, nickname="", role="member"),
        to_me=False,
        group_id=int(group_id),
    )


async def _dispatch(bot, group_id: int, message_id: int) -> None:
    from nonebot.message import handle_event

    from amrita.plugins.chat.utils import wake_policy

    task = asyncio.current_task()

    def expire() -> None:

        wake_policy.finish_proactive(message_id)
        logger.warning(f"[冷场] 群 {group_id} 的开话题处理超时，已释放标记")
        task.cancel()

    timer = asyncio.get_running_loop().call_later(wake_policy.PROACTIVE_TIMEOUT_SECONDS, expire)
    try:
        await handle_event(bot, _make_event(bot, group_id, message_id))
    except Exception as e:
        logger.opt(exception=e).warning(f"[冷场] 群 {group_id} 的开话题处理失败")
    finally:
        timer.cancel()
        wake_policy.finish_proactive(message_id)


async def _tick() -> None:
    from nonebot.adapters.onebot.v11 import Bot

    from amrita.plugins.chat.utils import wake_policy

    names = settings.whitelist()
    if not names:
        return
    bots = [b for b in get_bots().values() if isinstance(b, Bot)]
    if not bots:
        return
    bot = bots[0]
    for gid in sorted(names[0]):
        due = wake_policy.proactive_due(gid)
        if not due:
            continue
        idle, last = due
        mid = wake_policy.new_proactive(gid, idle, last)
        logger.info(f"[冷场] 群 {gid} 安静了约 {idle} 分钟，给她一次主动开话题的机会")
        asyncio.create_task(_dispatch(bot, gid, mid))
        return


async def _loop() -> None:
    await asyncio.sleep(_interval())
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.opt(exception=e).warning("[冷场] 检查失败")
        await asyncio.sleep(_interval())


@get_driver().on_startup
async def _start() -> None:
    _task[0] = asyncio.create_task(_loop())


@get_driver().on_shutdown
async def _stop() -> None:
    if _task[0]:
        _task[0].cancel()
        with contextlib.suppress(BaseException):
            await _task[0]
