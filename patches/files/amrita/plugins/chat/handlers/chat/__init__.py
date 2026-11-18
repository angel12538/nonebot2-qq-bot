"""聊天处理器入口（编排）

将原 chat.py 的上帝文件拆分为子包：
- message.py   消息合成/格式化/引用/角色/多模态
- strategy.py  Agent 策略选择与 workflow 装配
- lock.py      pending_mode 锁策略（State 模式）
- streaming.py ChatStreamSender 生命周期与长任务监控
- recovery.py  Panic-Recover 处理
- usage.py     Token 用量统计与持久化

本模块仅保留 entry() 编排主函数。
"""

from __future__ import annotations

import time as _time
from asyncio import CancelledError

from amrita_core import debug_log, logger
from amrita_core.base.backend import BackendSlots
from amrita_core.chatmanager import ChatObject as CoreChatObject
from amrita_core.chatmanager.chat_object import DatabackendOptions
from amrita_core.types import USER_INPUT, Content, Message, ToolResult
from amrita_sense.hook.exception import MatcherException as ChatException
from amrita_sense.hook.matcher import MatcherFactory
from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11.event import GroupMessageEvent, MessageEvent
from nonebot.exception import MatcherException, NoneBotException, ProcessException
from nonebot.matcher import Matcher
from nonebot_plugin_amrita.memory import CachedUserDataRepository, MemorySchema

from amrita.plugins.chat.backends import ChatMemoryBackend, NoopAbilityBackend
from amrita.plugins.chat.config import config_manager
from amrita.plugins.chat.events import ChatEntryEvent, ChatRequestEvent
from amrita.plugins.chat.runtime import (
    AMRITA_CTX_KEY,
    AmritaBotContext,
    bot_chat_manager,
    pending_chatobj,
)
from amrita.plugins.chat.runtime_session import SessionManager
from amrita.plugins.chat.utils.context import build_chat_prompts
from amrita.plugins.chat.utils.context_store import (
    MediaExpandBudget,
    expand_media_in_content,
    expand_media_in_messages,
)
from amrita.plugins.chat.utils.context_vars import current_uni_id
from amrita.plugins.chat.utils.debounce import collect_batch
from amrita.plugins.chat.utils.lock import get_group_lock, get_private_lock
from amrita.plugins.chat.utils.preset import is_multimodal_enabled, resolve_preset
from amrita.plugins.chat.utils.sql import get_uni_user_id

from .lock import get_pending_mode_strategy
from .message import build_user_input_safe, merge_user_inputs
from .recovery import RecoveryResult, try_panic_recover
from .strategy import build_workflow, select_agent_strategy
from .streaming import StreamSession
from .usage import record_usage

__all__ = ["entry"]

command_prefix = get_driver().config.command_start or "/"


def _trace_begin(events, session_id, t0, t_batch, train, user_input):
    """本地补丁：对话追踪开始（guards/trace.py）。返回 trace 模块，没开或出错返回 None。"""
    try:
        from guards import trace

        if not trace.enabled():
            return None
        from amrita.plugins.chat.utils import wake_policy as _wp

        first = events[0]
        reason = ""
        if isinstance(first, GroupMessageEvent):
            key = _wp.wake_reason(first.group_id, first.message_id) or ""
            reason = _wp.REASON_TEXT.get(key, key)
        msgs = [
            {
                "发送者": f"{getattr(ev.sender, 'card', None) or getattr(ev.sender, 'nickname', '') or ''}（{ev.user_id}）",
                "消息id": ev.message_id,
                "内容": str(ev.get_message()),
            }
            for ev in events
        ]
        name = msgs[0]["发送者"]
        text = first.get_message().extract_plain_text().strip() or str(first.get_message())
        now = _time.time()
        title = f"收到 {len(events)} 条消息" + (f"（{reason}）" if reason else "")
        turn = trace.begin(
            session_id,
            trigger=f"{name}：{text}",
            reason=reason,
            steps=[
                ("收到", title, t0, t_batch, {"叫醒原因": reason, "消息": msgs, "说明": "耗时是等对方把话说完（防抖）用的时间"}),
                ("提示词", "构建上下文和提示词", t_batch, now, {"系统提示": train, "用户输入": user_input}),
            ],
        )
        return trace if turn else None
    except Exception:
        return None


async def entry(event: MessageEvent, matcher: Matcher, bot: Bot):
    """
    聊天处理器入口函数。

    新版流程（初始化与执行完全隔离）：
      1. 会话超时检测与归档（SessionManager）
      2. 加载 memory、合成消息、构建 prompt
      3. 创建 CoreChatObject，通过 hook_kwargs 传递上下文
      4. chat.begin() -> lock -> await chat
      5. 后处理：usage 统计、memory 持久化
    """
    if any(
        event.message.extract_plain_text().strip().startswith(prefix)
        for prefix in command_prefix
        if prefix.strip()
    ):
        matcher.skip()
    session_id = get_uni_user_id(event)
    config = config_manager.config
    cudr = CachedUserDataRepository()
    _trace_t0 = _time.time()


    debounce_window: float = config.function.chat_debounce_window
    events: list[MessageEvent] = [event]
    if debounce_window > 0:
        batch = await collect_batch(
            event,
            matcher,
            debounce_window,
            max_messages=config.function.chat_debounce_max_messages,
            max_wait=config.function.chat_debounce_max_wait,
        )
        if batch is None:
            return
        events = batch


    is_group: bool = isinstance(event, GroupMessageEvent)


    approved: list[MessageEvent] = []
    for ev in events:
        entry_event = ChatEntryEvent(
            event=ev,
            matcher=matcher,
            bot=bot,
            session_id=session_id,
            is_group=is_group,
        )
        await MatcherFactory.trigger_event(entry_event)
        if entry_event.cancelled:
            debug_log("ChatEntryEvent 否决本条消息，已跳过")
            continue
        approved.append(ev)
    if not approved:
        return
    events = approved
    _trace_t_batch = _time.time()

    memory: MemorySchema = await cudr.get_memory(
        get_uni_user_id(event),
    )
    data = memory.memory_json


    for mem in data.messages:
        if not isinstance(mem, (Message, ToolResult)):
            continue
        if mem.content is None or isinstance(mem.content, str):
            continue
        mem.content = [i for i in mem.content if isinstance(i, Content)]


    await SessionManager(
        event=event,
        data=data,
        memory=memory,
        matcher=matcher,
        bot=bot,
        config=config,
        events=events,
    ).manage()



    built = [await build_user_input_safe(ev, bot) for ev in events]
    parts = [part for part in built if part is not None]
    if not parts:
        return
    final_content: USER_INPUT = merge_user_inputs(parts)
    debug_log(f"合成消息完成，共 {len(parts)}/{len(events)} 条")


    try:
        from amrita_core import TextContent

        from .message import unread_preview

        preview = await unread_preview(event)
        if preview:
            final_content = (
                f"{preview}\n{final_content}"
                if isinstance(final_content, str)
                else [TextContent(text=preview), *final_content]
            )
    except Exception as e:
        logger.warning(f"未读预览附加失败，本次不附带：{e!r}")


    multimodal = await is_multimodal_enabled()


    media_budget = MediaExpandBudget(config.context.max_images_per_request)
    final_content = await expand_media_in_content(
        final_content, multimodal=multimodal, budget=media_budget
    )
    memory_view = memory.memory_json.model_copy()
    memory_view.messages = await expand_media_in_messages(
        memory_view.messages, multimodal=multimodal, budget=media_budget
    )


    strategy = select_agent_strategy(config.llm.agent_strategy)


    train_dict, tool_context = await build_chat_prompts(event, memory, config)


    request_event = ChatRequestEvent(
        event=event,
        matcher=matcher,
        bot=bot,
        session_id=session_id,
        user_input=final_content,
        train=train_dict,
    )
    await MatcherFactory.trigger_event(request_event)
    final_content = request_event.user_input
    train_dict = request_event.train


    ctx: AmritaBotContext = {
        "matcher": matcher,
        "bot": bot,
        "event": event,
        "bot_config": config_manager.config,
    }
    chat: CoreChatObject = CoreChatObject(
        train=train_dict,
        user_input=final_content,
        tool_judgment_context=tool_context,
        session_id=session_id,
        preset=await resolve_preset(),
        hook_args=(event, matcher, bot),
        hook_kwargs={AMRITA_CTX_KEY: ctx},
        exception_ignored=(ProcessException, MatcherException),
        agent_strategy=strategy,
        workflow=build_workflow(config.llm.agent_workflow),
        chat_man=bot_chat_manager,
        backend=BackendSlots(
            NoopAbilityBackend(),
            ChatMemoryBackend(memory, memory_view),
        ),
        backend_options=DatabackendOptions(
            skip_mcp_fetch=True,

            skip_tools_fetch=False,
        ),
    )


    _trace = _trace_begin(events, session_id, _trace_t0, _trace_t_batch, train_dict, final_content)
    _trace_t_built = _time.time()
    _trace_err: BaseException | None = None


    stream = StreamSession(
        matcher,
        bot,
        event,
        config,
        chat,
        notify_sec=config.session.session_long_running_notify_seconds,
        is_group=is_group,
    )
    lock = (
        get_group_lock(event.group_id) if is_group else get_private_lock(event.user_id)
    )


    if await get_pending_mode_strategy(config.function.chat_pending_mode).handle_locked(
        lock=lock,
        matcher=matcher,
        event=event,
        session_id=session_id,
    ):
        return

    try:
        pending_chatobj[session_id].append(chat)
        try:
            async with lock:
                pending_chatobj[session_id].remove(chat)
                debug_log("继续运行...")
                if _trace:
                    _trace.mark("运行", "拿到会话锁，开始运行", since=_trace_t_built)


                stream.start_monitor()
                token = current_uni_id.set(session_id)
                try:
                    async with chat.begin():
                        await chat
                finally:
                    current_uni_id.reset(token)
                    await stream.stop_monitor()

                stream.mark_received()
                await stream.send_final()
        finally:

            if chat in pending_chatobj[session_id]:
                pending_chatobj[session_id].remove(chat)

    except BaseException as e:
        if isinstance(e, (NoneBotException, ChatException)):
            raise
        _trace_err = e

        if isinstance(e, CancelledError):
            return


        result = await try_panic_recover(chat, e, config)
        if result is RecoveryResult.RECOVERED:

            stream.mark_received()
            await stream.send_final()
            return
        if result is RecoveryResult.ABANDONED:
            return

        await matcher.send("出错了稍后试试吧（错误已反馈）")
        logger.opt(exception=e, colors=True, raw=True).exception(
            "程序发生了未捕获的异常"
        )
    finally:
        if _trace:
            _trace.end(_trace_err)
        await record_usage(chat, event, cudr)
