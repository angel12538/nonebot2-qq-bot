"""聊天流式消息发送器

将流式消息的处理与最终响应的发送抽离为独立类：

- 流式元信息消息（MessageWithMetadata）经 _ReasoningCollector 收集/过滤后转发
- 最终响应发送前触发 `SendMessageEvent` 钩子，允许修改 MessageSegment
  或抛出 `NoMessageSendError` 静默拦截（不回复、不报错）
"""

from __future__ import annotations

import asyncio
import random
from io import StringIO
from typing import TYPE_CHECKING, Any

from amrita_core.contents import (
    ImageMessage,
    MessageWithMetadata,
    StringMessageContent,
)
from amrita_core.components.llm import strip_dsml
from amrita_sense.hook.event import BaseEvent
from amrita_sense.hook.matcher import MatcherFactory
from nonebot import logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from amrita.utils.admin import send_to_admin
from amrita.utils.send import send_forward_msg

from ..config import Config
from .functions import split_message_into_chats

if TYPE_CHECKING:
    from amrita_core.base.adapter import COMPLETION_RETURNING
    from amrita_core.chatmanager import ChatObject as CoreChatObject
    from nonebot.adapters.onebot.v11 import Bot, MessageEvent
    from nonebot.matcher import Matcher


def _strip_dsml(text: str) -> str:
    cleaned = strip_dsml(text)
    if cleaned != text:
        if cleaned:
            logger.warning("发送前已移除正文中的 DSML 工具标记")
        else:
            logger.warning("发送前已拦下含 DSML 工具标记的异常正文")
    return cleaned


class NoMessageSendError(BaseException):
    """钩子抛出以静默拦截最终消息发送（不回复、不报错）"""


class SendMessageEvent(BaseEvent[str]):
    """最终消息发送前触发的事件

    content: 构建好的 MessageSegment，钩子可直接修改或替换
    """

    def __init__(
        self,
        content: Message,
        *,
        chat: CoreChatObject,
        event: MessageEvent,
        matcher: Matcher,
        bot: Bot,
    ):
        self.content = content
        self.chat = chat
        self.event = event
        self.matcher = matcher
        self.bot = bot

    def get_event_type(self) -> str:
        return "SEND_MESSAGE"

    @property
    def event_type(self) -> str:
        return "SEND_MESSAGE"


def _split_forward_chunks(text: str, min_chunk: int) -> list[str]:
    """按连续换行拆分合并转发分块

    以 "\n\n" 为分隔拆分；若某块长度不足 min_chunk，则与下一块合并。
    全部块都满足 min_chunk 或整体不足时返回单块。
    """
    raw_chunks = [c.strip() for c in text.split("\n\n") if c.strip()]
    if not raw_chunks:
        return [text]
    chunks: list[str] = []
    current = ""
    for chunk in raw_chunks:
        if not current:
            current = chunk
        elif len(current) < min_chunk:
            current += "\n\n" + chunk
        else:
            chunks.append(current)
            current = chunk
    if current:
        chunks.append(current)
    return chunks


class _ReasoningCollector:
    """流式思考块收集器：单 token CoT chunk 累积到 StringIO，flush 时合并返回"""

    def __init__(self) -> None:
        self.buffer = StringIO()
        self.type: str | None = None

    def flush(self) -> str | None:
        text = self.buffer.getvalue()
        self.buffer = StringIO()
        self.type = None
        return text or None

    def write(self, content: str, extra_type: str | None) -> str | None:
        """写入一个 chunk；若 extra_type 切换，返回需发送的旧段文本（已清除）"""
        if self.type is not None and self.type != extra_type:
            old = self.flush()
            self.buffer.write(content)
            self.type = extra_type
            return old
        self.buffer.write(content)
        self.type = extra_type
        return None


class ChatStreamSender:
    """流式消息发送器

    用法:
        sender = ChatStreamSender(matcher, bot, event, config, chat)
        chat.io_stream.set_callback_func(sender.handle)
        ...
        await sender.send_final(response)   # 触发钩子后发送最终响应
    """

    def __init__(
        self,
        matcher: Matcher,
        bot: Bot,
        event: MessageEvent,
        config: Config,
        chat: CoreChatObject,
    ):
        self._matcher = matcher
        self._bot = bot
        self._event = event
        self._config = config
        self._chat = chat
        self._reasoning = _ReasoningCollector()
        self._blocked = False

    async def _flush_reasoning(self) -> None:
        if text := self._reasoning.flush():
            await self._matcher.send(f"💭 {text}")

    async def _send_middle(self, content: str) -> None:
        """本地补丁：一轮中途说的话，按最终回复的规矩发——整段查路径和凭据、发送限速预检、
        自动分句、按打字速度停顿，不加任何前缀（出站钩子照样逐条检查、处理〔不回〕和引用/@ 标记）。"""
        text = _strip_dsml(str(content or "")).strip()
        if not text or self._blocked:
            return
        try:
            from .outbound_guard import (
                _FILLER,
                _SILENCE_RE,
                block_notice,
                event_rate_key,
                log_event,
                log_rate_drop,
                secret_reason,
                send_quota,
                typing_gap,
            )
        except Exception:
            await self._matcher.send(MessageSegment.text(text))
            return
        reason = secret_reason(text)
        if reason:
            log_event("middle_message", [reason], True, length=len(text), target=event_rate_key(self._event) or "")
            await self._matcher.send(block_notice())
            return
        parts = (
            split_message_into_chats(text) if self._config.function.nature_chat_style else [text]
        ) or [text]
        need = sum(1 for p in parts if _SILENCE_RE.sub("", p).strip(_FILLER))
        rkey = event_rate_key(self._event)
        if need and send_quota(rkey) < need:
            log_rate_drop(rkey, need)
            return
        for i, part in enumerate(parts):
            await self._matcher.send(MessageSegment.text(part))
            if i == len(parts) - 1:
                break
            gap = typing_gap(parts[i + 1])
            if gap is None:
                gap = random.randint(1, 3) + (len(part) // random.randint(80, 100))
            await asyncio.sleep(gap)

    async def handle(self, message: "COMPLETION_RETURNING") -> None:
        """流式消息回调：处理元信息消息、收集思考块、转发文本/图片"""
        if isinstance(message, str):
            return
        if isinstance(message, MessageWithMetadata):
            await self._handle_metadata(message)
            return
        if isinstance(message, StringMessageContent):
            await self._flush_reasoning()
            await self._matcher.send(message.get_content())
            return
        if isinstance(message, ImageMessage):
            await self._flush_reasoning()
            await self._matcher.send(MessageSegment.image(await message.get_image()))
            return

        await self._matcher.send(str(message))

    async def _handle_metadata(self, message: MessageWithMetadata) -> None:
        meta = self._config.meta
        metadata: dict[str, Any] = dict(message.metadata)
        mtype = metadata.get("type", "")
        extra_type = metadata.get("extra_type")
        is_reasoning_chunk = mtype == "reasoning_chunk" or (
            mtype == "text" and extra_type == "reasoning_chunk"
        )
        if is_reasoning_chunk and meta.enable and meta.stream_reasoning:

            if old := self._reasoning.write(message.content, extra_type):
                await self._matcher.send(f"💭 {old}")
            return

        await self._flush_reasoning()
        match mtype:
            case "system":
                if metadata.get("extra_type") == "tool_call_limit":

                    logger.info("本轮已达工具调用上限，接下来基于已获取的信息作答（未发到聊天里）")
                else:
                    await self._matcher.send(message.content)
            case "reasoning":

                if meta.enable and meta.reasoning:
                    await self._matcher.send(f"💭 {message.content}")
            case "tool_prediction":
                if meta.enable and meta.tool_call:
                    await self._matcher.send("⏩ 优化了工具选择")
            case "middle_message":

                await self._send_middle(message.content)
            case "function_call":
                if metadata.get("is_done") and meta.enable and meta.tool_call:
                    function_name = metadata.get("function_name")
                    if (err := metadata.get("err")) is not None:
                        logger.opt(exception=err, colors=True, raw=True).exception(
                            f"Tool {function_name} execution failed: {err}"
                        )
                        await self._matcher.send(f"ERR: {function_name} 执行失败")
                    else:
                        await self._matcher.send(f"调用了工具：{function_name}")
            case "step":

                if (
                    meta.enable
                    and extra_type is not None
                    and getattr(meta.step, extra_type, False)
                ):
                    await self._matcher.send(f"🪜 {message.content}")
            case "reflection":

                if meta.enable and meta.reflection:
                    detail = metadata.get("detail")
                    text = f"🔍 {message.content}"
                    if detail:
                        text += f"\n{detail}"
                    await self._matcher.send(text)
            case "skill_call":

                if meta.enable and meta.skill_trigger:
                    await self._matcher.send(f"🧠 {message.content}")
            case "text":
                if (
                    extra_type == "structured_reasoning_step"
                    and meta.enable
                    and meta.reasoning
                ):
                    await self._matcher.send(message.content)
            case "error":
                if metadata.get("extra_type") == "cookie":
                    await self._handle_cookie_error()
                    return
                error = metadata.get("error")
                logger.opt(exception=error, colors=True, raw=True).exception(
                    f"有错误发生:{error}"
                )
                if meta.enable and meta.error_report:
                    await self._matcher.send(f"❌ {message.content}")

    async def _handle_cookie_error(self) -> None:
        """cookie 泄露：通知管理员并熔断（不再发送最终响应）"""
        chat = self._chat
        response_content = ""
        if (resp := chat._di_resp.response) is not None:
            response_content = resp.content or ""
        await send_to_admin(
            f"安全警告：用户请求导致了可能的Prompt泄露。已在response检测到cookie泄露，请检查！\n用户请求：\n{chat.user_input!s}\n模型模型输出：\n{response_content!s}"
        )
        self._blocked = True
        await self._matcher.send(random.choice(self._config.llm.block_msg))

    async def send_final(self, content: str) -> None:
        """发送最终响应：先触发 SendMessageEvent 钩子，再发送

        发送策略：
        - 文本超过 200 字符：改用合并转发（分句拆分节点）
        - nature_chat_style：按分句规则拆分发送（带随机间隔）
        - 否则引用回复一次性发送

        Args:
            content: 模型最终回复文本

        Raises:
            NoMessageSendError: 钩子拦截，静默不发送
        """
        if self._blocked:
            return

        await self._flush_reasoning()

        content = _strip_dsml(content)
        if not content.strip():
            return


        message: Message = MessageSegment.reply(
            self._event.message_id
        ) + MessageSegment.text(content)


        ev = SendMessageEvent(
            message,
            chat=self._chat,
            event=self._event,
            matcher=self._matcher,
            bot=self._bot,
        )
        await MatcherFactory.trigger_event(ev, exception_ignored=(NoMessageSendError,))


        final_text = ev.content.extract_plain_text()


        try:
            from .outbound_guard import block_notice, event_rate_key, log_event, secret_reason

            reason = secret_reason(final_text)
        except Exception:
            reason = "守卫异常"
            from .outbound_guard import block_notice, event_rate_key, log_event
        if reason:
            log_event("send_final", [reason], True, length=len(final_text), target=event_rate_key(self._event) or "")
            await self._matcher.send(block_notice())
            return
        forward_threshold = self._config.function.forward_threshold

        response_list: list[str] = []
        if forward_threshold > 0 and len(final_text) > forward_threshold:
            need = 1
        elif not self._config.function.nature_chat_style:
            need = 1
        else:
            response_list = split_message_into_chats(final_text)
            need = len(response_list)
        try:
            from .outbound_guard import _FILLER, _SILENCE_RE, event_rate_key, log_rate_drop, send_quota


            if response_list:
                need = sum(1 for p in response_list if _SILENCE_RE.sub("", p).strip(_FILLER))
            elif not _SILENCE_RE.sub("", final_text).strip(_FILLER):
                need = 0
            rkey = event_rate_key(self._event)
            if need and send_quota(rkey) < need:
                log_rate_drop(rkey, need)
                return
        except Exception:
            pass
        if forward_threshold > 0 and len(final_text) > forward_threshold:

            min_chunk = self._config.function.forward_min_chunk
            parts = [
                MessageSegment.text(part)
                for part in _split_forward_chunks(final_text, min_chunk)
            ]
            await send_forward_msg(
                self._bot,
                self._event,
                name="Amrita",
                uin=str(self._event.self_id),
                msgs=parts,
            )
        elif not self._config.function.nature_chat_style:
            await self._matcher.send(ev.content)
        elif response_list:
            for i, part in enumerate(response_list):
                await self._matcher.send(MessageSegment.text(part))
                if i == len(response_list) - 1:
                    break


                try:
                    from .outbound_guard import typing_gap

                    gap = typing_gap(response_list[i + 1])
                except Exception:
                    gap = None
                if gap is None:
                    gap = random.randint(1, 3) + (len(part) // random.randint(80, 100))
                await asyncio.sleep(gap)
