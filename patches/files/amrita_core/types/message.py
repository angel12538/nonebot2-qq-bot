from __future__ import annotations

import typing
from collections.abc import Iterable
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Generic, Literal

from pydantic import ConfigDict, Field, model_validator
from typing_extensions import Self

from amrita_core.types.base import BaseModel
from amrita_core.types.content import USER_INPUT, ImageContent, coerce_content
from amrita_core.types.tool import ToolCall, ToolResult

if TYPE_CHECKING:
    from amrita_core.types.memory import MemoryModel

_T = typing.TypeVar("_T", bound=USER_INPUT)


class Message(BaseModel, Generic[_T]):
    model_config = ConfigDict(extra="allow")
    role: Literal["user", "assistant", "system"] = Field(..., description="Role")
    content: _T = Field(..., description="Content")
    summary_source_at: float | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    tool_calls: list[ToolCall] | None = Field(
        default=None, description="Tool calls", exclude_if=lambda x: x is None
    )
    reasoning_content: str | None = Field(
        default=None,
        description="Reasoning/thinking content from model",
        exclude_if=lambda x: x is None,
    )
    reasoning_signature: str | None = Field(
        default=None,
        description="Anthropic thinking signature (required for round-tripping)",
        exclude_if=lambda x: x is None,
    )

    @model_validator(mode="before")
    @classmethod
    def validate_content(cls, data: Any) -> Any:
        """Validate and properly deserialize content based on its structure."""
        if isinstance(data, dict) and "content" in data:
            data["content"] = coerce_content(data["content"])
        return data

    @model_validator(mode="after")
    def validate_reasoning_content(self) -> Self:
        """Ensure reasoning_content is only present on assistant messages."""
        if self.role != "assistant" and self.reasoning_content is not None:
            raise ValueError(
                f"reasoning_content is only allowed on assistant messages, "
                f"got role={self.role!r}"
            )
        return self


CONTENT_LIST_TYPE_ITEM = Message | ToolResult
CONTENT_LIST_TYPE = list[CONTENT_LIST_TYPE_ITEM]
_WEB_HISTORY_TOOLS = frozenset({"search_web", "fetch_url"})


def without_web_tool_history(messages: CONTENT_LIST_TYPE) -> CONTENT_LIST_TYPE:
    """从请求副本移除旧网页工具调用及其结果，保留同批其他工具。"""
    kept: list[CONTENT_LIST_TYPE_ITEM | None] = []
    results: dict[str, int] = {}

    for message in reversed(messages):
        if isinstance(message, ToolResult):
            results[message.tool_call_id] = len(kept)
            kept.append(None if message.name in _WEB_HISTORY_TOOLS else message)
            continue
        if message.role == "assistant" and message.tool_calls:
            calls = []
            for call in message.tool_calls:
                result = results.pop(call.id, None)
                if call.function.name in _WEB_HISTORY_TOOLS:
                    if result is not None:
                        kept[result] = None
                elif result is None or kept[result] is not None:
                    calls.append(call)
            if len(calls) != len(message.tool_calls):
                if not calls and not message.content:
                    continue
                message = message.model_copy(update={"tool_calls": calls or None})
        kept.append(message)
    return [message for message in reversed(kept) if message is not None]


class SendMessageWrap(Iterable[CONTENT_LIST_TYPE_ITEM]):
    """Wrapper class for CONTENT_LIST_TYPE"""

    train: Message[str]
    memory: CONTENT_LIST_TYPE
    user_query: Message
    end_messages: CONTENT_LIST_TYPE
    history_summary: str

    def __init__(
        self,
        train: dict[str, str] | Message[str],
        memory: CONTENT_LIST_TYPE | MemoryModel,
        user_query: Message | None = None,
    ):
        self.train = (
            train if isinstance(train, Message) else Message.model_validate(train)
        )
        self.end_messages = []
        self.history_summary = ""
        self.memory = memory if isinstance(memory, list) else memory.messages
        query = user_query or self.memory[-1]
        if isinstance(query, ToolResult) or query.role != "user":
            raise ValueError("Invalid query message, expecting user message!")
        self.user_query = query
        if not user_query:
            self.memory.pop()

    @classmethod
    def validate_messages(cls, messages: CONTENT_LIST_TYPE) -> SendMessageWrap:
        train = messages[0]
        if train.role != "system":
            for idx, msg in enumerate(messages):
                if msg.role == "system":
                    train = msg
                    messages.pop(idx)
                    memory = messages
                    break
            else:
                raise ValueError("Invalid messages, expecting system message!")
        else:
            memory = messages[1:]
        return cls(
            train,
            memory,
        )

    def __len__(self) -> int:
        return len(self.memory) + 2 + len(self.end_messages)

    def __iter__(self) -> typing.Iterator[CONTENT_LIST_TYPE_ITEM]:
        yield self.train
        yield from self.memory
        yield self.user_query
        yield from self.end_messages

    def copy(self) -> SendMessageWrap:
        return deepcopy(self)

    def unwrap(
        self, exclude_system: bool = False, *, omit_web_history: bool = False
    ) -> CONTENT_LIST_TYPE:
        system_msg: CONTENT_LIST_TYPE = [self.train] if not exclude_system else []
        history = (
            without_web_tool_history(self.memory) if omit_web_history else self.memory
        )
        return [*system_msg, *history, self.user_query, *self.end_messages]

    def unwrap_for_chat_request(self) -> CONTENT_LIST_TYPE:
        """过滤旧网页工具后，保留系统提示和最多 20 条会话消息。"""
        summary = []
        if self.history_summary:
            summary.append(
                Message[str](
                    role="assistant",
                    content=f"【历史摘要】\n{self.history_summary}\n【历史摘要结束】",
                )
            )
        limit = 20 - len(summary)
        followups = self.end_messages[-(limit - 1):]
        history_room = limit - 1 - len(followups)
        history = (
            without_web_tool_history(self.memory)[-history_room:] if history_room else []
        )
        recent = [*history, self.user_query, *followups]

        while True:
            kept: CONTENT_LIST_TYPE = []
            pending_calls: set[str] = set()
            image_rows = 0
            batch_has_images = False
            for message in recent:
                if isinstance(message, ToolResult):
                    if message.tool_call_id not in pending_calls:
                        continue
                    pending_calls.remove(message.tool_call_id)

                    if (
                        not batch_has_images
                        and isinstance(message.content, list)
                        and any(isinstance(block, ImageContent) for block in message.content)
                    ):
                        batch_has_images = True
                        image_rows += 1
                else:
                    batch_has_images = False
                    if message.role == "assistant" and message.tool_calls:
                        pending_calls.update(call.id for call in message.tool_calls)
                kept.append(message)

            if len(kept) + image_rows <= limit:
                return [self.train, *summary, *kept]


            oldest = 1 if recent[0] is self.user_query else 0
            del recent[oldest]

    def get_train(self) -> Message[str]:
        return self.train

    def get_memory(self) -> CONTENT_LIST_TYPE:
        return self.memory

    def get_user_query(self) -> Message:
        return self.user_query

    def append(self, message: CONTENT_LIST_TYPE_ITEM) -> None:
        self.end_messages.append(message)

    def extend(self, messages: CONTENT_LIST_TYPE) -> None:
        self.end_messages.extend(messages)
