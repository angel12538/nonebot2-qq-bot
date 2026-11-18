from collections.abc import AsyncGenerator, Iterable, Sequence
from typing import Literal, cast

import openai
from amrita_sense.logging import debug_log
from openai._types import SequenceNotStr
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from openai.types.chat.chat_completion_named_tool_choice_param import (
    ChatCompletionNamedToolChoiceParam,
)
from openai.types.chat.chat_completion_named_tool_choice_param import (
    Function as OPENAI_Function,
)
from openai.types.chat.chat_completion_tool_choice_option_param import (
    ChatCompletionToolChoiceOptionParam,
)
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam
from typing_extensions import override

from amrita_core.base.adapter import (
    AGENTIC_RETURNING,
    COMPLETION_RETURNING,
    FeatureFlag,
    ModelAdapter,
)
from amrita_core.config import AmritaConfig
from amrita_core.contents import MessageMetadataPayload, MessageWithMetadata
from amrita_core.exceptions import normalize_adapter_errors
from amrita_core.tools.models import (
    ToolChoice,
    ToolFunctionSchema,
)
from amrita_core.types import (
    EmbeddingChunk,
    ModelConfig,
    ModelInfo,
    ModelPreset,
    ToolCall,
    UniResponse,
    UniResponseUsage,
)
from amrita_core.types.preset import resolve_max_output
from amrita_core.types.response import STOP_REASON, RequestMetadata
from amrita_core.utils import model_dump

R2R_MAP: dict[
    Literal["stop", "length", "tool_calls", "content_filter", "function_call"],
    STOP_REASON,
] = {
    "stop": "stop_sequence",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
    "function_call": "tool_use",
}


def _convert_tools(tools: list[ToolFunctionSchema]) -> list[ChatCompletionToolParam]:
    """Serialize tool schemas into the Chat Completions shape explicitly.

    Handing the pydantic objects to the SDK is not enough: it serializes them
    with ``exclude_unset``, and ``type`` / ``strict`` carry defaults, so they are
    dropped and the provider rejects the request with "tools[0]: missing field
    `type`". ``strict`` also belongs inside ``function`` here — at the tool's top
    level the schema ignores it.
    """
    converted: list[dict] = []
    for tool in tools:
        function = tool.function.model_dump(exclude_none=True)
        function["strict"] = tool.strict
        converted.append({"type": "function", "function": function})
    return cast(list[ChatCompletionToolParam], converted)


def _mask_base64(raw: str) -> str:
    """落盘前把内联图片的 base64 换成占位符。"""
    import re as _re

    return _re.sub(r"data:image/[^;\"']+;base64,[A-Za-z0-9+/=\s]{200,}", "[base64 omitted]", raw)


_TOOL_IMAGE_NOTE = "（这次工具返回了 {n} 张图片，放在紧接着的下一条消息里）"
_TOOL_IMAGE_HEADER = "（以下 {n} 张图片是上面的工具调用返回的，不是有人新发的消息）"


def _hoist_tool_images(messages: Sequence) -> list:
    """本地补丁：把工具结果里的图片挪到紧跟其后的一条 user 消息里再发。

    OpenAI 兼容接口的 tool 消息只认文字，图片直接放进去会被网关拒绝或丢掉；
    user 消息是能带图的。所以 tool 消息只留文字并注明图片在下一条，同一批
    连续的 tool 消息结束后补一条 user 消息装这些图。只改这一次请求的副本，
    存下来的上下文不变。
    """
    out: list = []
    pending: list = []

    def flush() -> None:
        if pending:
            header = {"type": "text", "text": _TOOL_IMAGE_HEADER.format(n=len(pending))}
            out.append({"role": "user", "content": [header, *pending]})
            pending.clear()

    for msg in messages:
        is_tool = isinstance(msg, dict) and msg.get("role") == "tool"
        if not is_tool:
            flush()
            out.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        texts: list[str] = []
        images: list = []
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text":
                texts.append(str(block.get("text", "")))
            elif kind == "image_url":
                images.append(block)
            elif block is not None:
                texts.append(str(block))
        if images:
            texts.append(_TOOL_IMAGE_NOTE.format(n=len(images)))
            pending.extend(images)
        out.append({**msg, "content": "\n".join(t for t in texts if t)})
    flush()
    return out


def _to_usage(raw) -> UniResponseUsage:
    """本地补丁：转换网关上报的用量，顺带读出缓存命中的 tokens。

    OpenAI 兼容接口把它放在 prompt_tokens_details.cached_tokens（DeepSeek 是
    prompt_cache_hit_tokens），框架原来不读，cache_hit 一直是 None。
    网关没给这个字段时保持 None，表示「没上报」，不是 0。
    """
    usage = UniResponseUsage.model_validate(raw, from_attributes=True)
    if usage.cache_hit is None:
        try:
            details = raw.get("prompt_tokens_details") if isinstance(raw, dict) else getattr(raw, "prompt_tokens_details", None)
            cached = details.get("cached_tokens") if isinstance(details, dict) else getattr(details, "cached_tokens", None)
            if not isinstance(cached, int):
                cached = raw.get("prompt_cache_hit_tokens") if isinstance(raw, dict) else getattr(raw, "prompt_cache_hit_tokens", None)
            if isinstance(cached, int):
                usage.cache_hit = cached
        except Exception:
            pass
    return usage


class OpenAIAdapter(ModelAdapter):
    """OpenAI Protocol Adapter

    NOTE: Through OpenAI SDK, we are actually hard to get cache info, each providers' cache informations are different.
        So we will not provide `cache info` from OpenAIAdapter. By the way, `stop sequence` is not provided by OpenAI SDK,
        so we will not provide this too.

    """

    __override__ = True
    features = FeatureFlag(
        agentic_call=True,
        input_multimodal=True,

        tool_result_multimodal=True,
        fetch_models=True,
    )

    @override
    @normalize_adapter_errors("openai")
    async def call_api(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        stop: str | list[str] | None = None,
        **kwargs,
    ) -> AsyncGenerator[COMPLETION_RETURNING, None]:
        """Call OpenAI API to get chat responses"""
        preset: ModelPreset = self.preset
        preset_config: ModelConfig = preset.config
        config: AmritaConfig = self.config
        messages = _hoist_tool_images(model_dump(messages))
        client = openai.AsyncOpenAI(
            base_url=preset.base_url,
            api_key=preset.api_key,
            timeout=config.llm.llm_timeout,
            max_retries=config.llm.max_retries,
        )
        completion: ChatCompletion | openai.AsyncStream[ChatCompletionChunk] | None = (
            None
        )

        kwargs.setdefault("extra_body", {})
        if preset.thinking_config is not None:
            if preset.thinking_config.thinking_type is not None:
                kwargs["extra_body"].setdefault("thinking", {}).setdefault(
                    "type", preset.thinking_config.thinking_type
                )
            if preset.thinking_config.enable_thinking:
                kwargs["extra_body"].setdefault("enable_thinking", True)

            if preset.thinking_config.thinking_effort:
                kwargs.update(
                    {"reasoning_effort": preset.thinking_config.thinking_effort}
                )

        if stream := preset.config.stream:
            kwargs.update({"stream_options": {"include_usage": True}})
        if stop:
            kwargs["stop"] = stop

        try:
            import json as _json, os as _os, time as _time
            from pathlib import Path as _Path
            if _os.getenv("DUMP_LLM_PAYLOAD", "false").lower() == "true":
                _dir = _Path.cwd() / "logs" / "llm_payloads"
                _dir.mkdir(parents=True, exist_ok=True)
                _payload = {
                    "model": preset.model,
                    "base_url": preset.base_url,
                    "params": {
                        "max_tokens": resolve_max_output(preset, config),
                        "top_p": preset_config.top_p,
                        "temperature": preset_config.temperature,
                        "stream": stream,
                        "stop": stop,
                    },
                    "message_count": len(messages),
                    "tools": kwargs.get("tools"),
                    "extra_body": kwargs.get("extra_body"),
                    "messages": messages,
                }
                _f = _dir / f"{_time.strftime('%Y%m%d-%H%M%S')}-{int(_time.time()*1000)%1000:03d}.json"
                _f.write_text(_mask_base64(_json.dumps(_payload, ensure_ascii=False, indent=1)), encoding="utf-8")
                for _old in sorted(_dir.glob("*.json"))[:-20]:
                    try:
                        _old.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

        completion = await client.chat.completions.create(
            model=preset.model,
            messages=messages,
            max_tokens=resolve_max_output(preset, config),
            top_p=preset_config.top_p,
            temperature=preset_config.temperature,
            stream=stream,
            **kwargs,
        )
        response: str = ""
        reasoning: str | None = None
        uni_usage = None
        model_name: str = preset.model
        meta: RequestMetadata | None = None


        if self.preset.config.stream and isinstance(completion, openai.AsyncStream):
            async with completion as completion:

                headers = completion.response.headers
                req_id: str | None = (
                    headers.get("x-request-id")
                    or headers.get("x-ds-trace-id")
                    or headers.get("eo-log-uuid")
                    or getattr(completion, "_request_id", None)
                )
                async for chunk in completion:
                    try:


                        if chunk.usage:
                            uni_usage = _to_usage(chunk.usage)
                        if not chunk.choices:
                            continue
                        if chunk.choices[0].finish_reason is not None:
                            model_name = chunk.model
                            meta = RequestMetadata(
                                model=model_name,
                                original_request_id=req_id,
                                stop_sequence=None,
                                stop_reason=(
                                    R2R_MAP.get(chunk.choices[0].finish_reason)
                                    if chunk.choices[0].finish_reason
                                    else None
                                ),
                            )

                        if (
                            reas_chunk := getattr(
                                chunk.choices[0].delta, "reasoning_content", None
                            )
                        ) is not None:
                            reas_chunk = str(reas_chunk)
                            if reasoning is None:
                                reasoning = reas_chunk
                            else:
                                reasoning += reas_chunk
                            yield MessageWithMetadata(
                                content=reas_chunk,
                                metadata=MessageMetadataPayload(
                                    type="reasoning_chunk", extra_type="cot_chunk"
                                ),
                            )
                        if (chunk := chunk.choices[0].delta.content) is not None:
                            response += chunk
                            yield chunk
                            debug_log(chunk)
                    except IndexError:
                        break

        else:
            if isinstance(completion, ChatCompletion):
                if (
                    reas_chunk := getattr(
                        completion.choices[0].message, "reasoning_content", None
                    )
                ) is not None:
                    reasoning = str(reas_chunk)
                    yield MessageWithMetadata(
                        content=reasoning,
                        metadata=MessageMetadataPayload(
                            type="reasoning_chunk", extra_type="cot_chunk"
                        ),
                    )
                meta = RequestMetadata(
                    model=completion.model,
                    original_request_id=completion._request_id,
                    stop_sequence=None,
                    stop_reason=(
                        R2R_MAP.get(completion.choices[0].finish_reason)
                        if completion.choices[0].finish_reason
                        else None
                    ),
                )
                response = (
                    completion.choices[0].message.content
                    if completion.choices[0].message.content is not None
                    else ""
                )
                debug_log(response)
                yield response
                if completion.usage:
                    uni_usage = _to_usage(completion.usage)
            else:
                raise RuntimeError("Received unexpected response type")
        yield UniResponse(
            role="assistant",
            content=response,
            usage=uni_usage,
            tool_calls=None,
            reasoning_content=reasoning,
            metadata=meta or RequestMetadata(model=preset.model),
        )

    @override
    @normalize_adapter_errors("openai")
    async def agentic_call_api(
        self,
        messages: Iterable,
        tools: list[ToolFunctionSchema] | None = None,
        tool_choice: ToolChoice | None = None,
        stop: str | list[str] | None = None,
        **kwargs,
    ) -> AsyncGenerator[AGENTIC_RETURNING, None]:
        """Stream text *and* tool calls from a single request.

        ``call_api`` streams text only and ``call_tools`` returns tool calls
        without streaming, so neither can drive an agent loop that has to show
        output while still accepting another tool round. Text deltas are yielded
        as they arrive and the trailing ``UniResponse`` carries the assembled
        ``tool_calls``; empty ``tool_calls`` means the model produced the final
        answer instead of another tool round.
        """
        preset: ModelPreset = self.preset
        preset_config: ModelConfig = preset.config
        config: AmritaConfig = self.config
        messages = _hoist_tool_images(model_dump(messages))

        if not tool_choice:
            choice: ChatCompletionToolChoiceOptionParam = "auto"
        elif isinstance(tool_choice, ToolFunctionSchema):
            choice = ChatCompletionNamedToolChoiceParam(
                function=OPENAI_Function(name=tool_choice.function.name),
                type=tool_choice.type,
            )
        else:
            choice = tool_choice

        client = openai.AsyncOpenAI(
            base_url=preset.base_url,
            api_key=preset.api_key,
            timeout=config.llm.llm_timeout,
            max_retries=config.llm.max_retries,
        )

        kwargs.setdefault("extra_body", {})
        if preset.thinking_config is not None:
            if preset.thinking_config.thinking_type is not None:
                kwargs["extra_body"].setdefault("thinking", {}).setdefault(
                    "type", preset.thinking_config.thinking_type
                )
            if preset.thinking_config.enable_thinking:
                kwargs["extra_body"].setdefault("enable_thinking", True)
            if preset.thinking_config.thinking_effort:
                kwargs.update(
                    {"reasoning_effort": preset.thinking_config.thinking_effort}
                )

        stream = bool(preset_config.stream)
        if stream:
            kwargs.update({"stream_options": {"include_usage": True}})

        request_kwargs: dict = {
            "model": preset.model,
            "messages": messages,
            "max_tokens": resolve_max_output(preset, config),
            "top_p": preset_config.top_p,
            "temperature": preset_config.temperature,
            "stream": stream,
        }

        if stop:
            request_kwargs["stop"] = stop
        if tools:
            request_kwargs["tools"] = _convert_tools(tools)
            request_kwargs["tool_choice"] = choice
        request_kwargs.update(kwargs)


        try:
            import json as _json, os as _os, time as _time
            from pathlib import Path as _Path
            if _os.getenv("DUMP_LLM_PAYLOAD", "false").lower() == "true":
                _dir = _Path.cwd() / "logs" / "llm_payloads"
                _dir.mkdir(parents=True, exist_ok=True)
                _f = _dir / f"{_time.strftime('%Y%m%d-%H%M%S')}-{int(_time.time()*1000)%1000:03d}-agentic.json"
                _f.write_text(_mask_base64(_json.dumps(request_kwargs, ensure_ascii=False, indent=1, default=str)), encoding="utf-8")
                for _old in sorted(_dir.glob("*.json"))[:-40]:
                    try:
                        _old.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

        completion = await client.chat.completions.create(**request_kwargs)

        response: str = ""
        reasoning: str | None = None
        uni_usage = None
        meta: RequestMetadata | None = None

        pending: dict[int, dict] = {}

        if stream and isinstance(completion, openai.AsyncStream):
            async with completion as stream_resp:
                headers = stream_resp.response.headers
                req_id: str | None = (
                    headers.get("x-request-id")
                    or headers.get("x-ds-trace-id")
                    or headers.get("eo-log-uuid")
                    or getattr(stream_resp, "_request_id", None)
                )
                async for chunk in stream_resp:
                    try:
                        if chunk.usage:
                            uni_usage = _to_usage(chunk.usage)
                        if not chunk.choices:
                            continue
                        choice_obj = chunk.choices[0]
                        delta = choice_obj.delta
                        if choice_obj.finish_reason is not None:
                            meta = RequestMetadata(
                                model=chunk.model,
                                original_request_id=req_id,
                                stop_sequence=None,
                                stop_reason=(
                                    R2R_MAP.get(choice_obj.finish_reason)
                                    if choice_obj.finish_reason
                                    else None
                                ),
                            )
                        reas_chunk = getattr(delta, "reasoning_content", None)
                        if reas_chunk is not None:
                            reas_chunk = str(reas_chunk)
                            reasoning = (reasoning or "") + reas_chunk
                            yield MessageWithMetadata(
                                content=reas_chunk,
                                metadata=MessageMetadataPayload(
                                    type="reasoning_chunk", extra_type="cot_chunk"
                                ),
                            )
                        if delta.content is not None:
                            response += delta.content
                            yield delta.content
                        for tc_delta in getattr(delta, "tool_calls", None) or []:
                            idx = tc_delta.index
                            if idx is None:
                                idx = len(pending)
                            slot = pending.setdefault(
                                idx,
                                {
                                    "id": None,
                                    "type": "function",
                                    "function": {"name": None, "arguments": ""},
                                },
                            )
                            if tc_delta.id:
                                slot["id"] = tc_delta.id
                            fn = getattr(tc_delta, "function", None)
                            if fn is not None:
                                if fn.name:
                                    slot["function"]["name"] = fn.name
                                if fn.arguments:
                                    slot["function"]["arguments"] += fn.arguments
                    except IndexError:
                        break
        elif isinstance(completion, ChatCompletion):
            choice_obj = completion.choices[0]
            msg = choice_obj.message
            reas = getattr(msg, "reasoning_content", None)
            if reas is not None:
                reasoning = str(reas)
                yield MessageWithMetadata(
                    content=reasoning,
                    metadata=MessageMetadataPayload(
                        type="reasoning_chunk", extra_type="cot_chunk"
                    ),
                )
            meta = RequestMetadata(
                model=completion.model,
                original_request_id=completion._request_id,
                stop_sequence=None,
                stop_reason=(
                    R2R_MAP.get(choice_obj.finish_reason)
                    if choice_obj.finish_reason
                    else None
                ),
            )
            response = msg.content or ""
            if response:
                yield response
            if completion.usage:
                uni_usage = _to_usage(completion.usage)
            for idx, tc in enumerate(msg.tool_calls or []):
                fn = getattr(tc, "function", None)
                if fn is None:

                    continue
                pending[idx] = {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {"name": fn.name, "arguments": fn.arguments},
                }
        else:
            raise RuntimeError("Received unexpected response type")

        tool_calls: list[ToolCall] = []
        for idx in sorted(pending):
            slot = pending[idx]
            if not slot.get("id"):
                slot["id"] = f"call_{idx}"
            tool_calls.append(ToolCall.model_validate(slot))
        yield UniResponse(
            role="assistant",
            content=response,
            usage=uni_usage,
            tool_calls=tool_calls or None,
            reasoning_content=reasoning,
            metadata=meta or RequestMetadata(model=preset.model),
        )

    @override
    @normalize_adapter_errors("openai")
    async def call_tools(
        self,
        messages: Iterable,
        tools: list,
        tool_choice: ToolChoice | None = None,
        **kwargs,
    ) -> UniResponse[str | None, list[ToolCall] | None]:
        if not tool_choice:
            choice: ChatCompletionToolChoiceOptionParam = "auto"
        elif isinstance(tool_choice, ToolFunctionSchema):
            choice = ChatCompletionNamedToolChoiceParam(
                function=OPENAI_Function(name=tool_choice.function.name),
                type=tool_choice.type,
            )
        else:
            choice = tool_choice
        messages = _hoist_tool_images(model_dump(messages))
        config: AmritaConfig = self.config
        preset: ModelPreset = self.preset
        preset_config: ModelConfig = preset.config
        base_url: str = preset.base_url
        key: str = preset.api_key
        model: str = preset.model
        client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key=key,
            timeout=config.llm.llm_timeout,
        )
        kwargs.setdefault("extra_body", {})
        if preset.thinking_config is not None:
            if preset.thinking_config.thinking_type is not None:
                kwargs["extra_body"].setdefault("thinking", {}).setdefault(
                    "type", preset.thinking_config.thinking_type
                )
            if preset.thinking_config.enable_thinking:
                kwargs["extra_body"].setdefault("enable_thinking", True)

            if preset.thinking_config.thinking_effort:
                kwargs.update(
                    {"reasoning_effort": preset.thinking_config.thinking_effort}
                )
        completion: ChatCompletion = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=False,
            tool_choice=choice,
            tools=_convert_tools(tools),
            top_p=preset_config.top_p,
            temperature=preset_config.temperature,
            **kwargs,
        )
        msg = completion.choices[0].message
        metadata = RequestMetadata(
            model=completion.model,
            original_request_id=completion._request_id,
            stop_sequence=None,
            stop_reason=(
                R2R_MAP.get(completion.choices[0].finish_reason)
                if completion.choices[0].finish_reason
                else None
            ),
        )
        return UniResponse(
            role="assistant",
            tool_calls=(
                [
                    ToolCall.model_validate(i, from_attributes=True)
                    for i in msg.tool_calls
                ]
                if msg.tool_calls
                else None
            ),

            content=msg.content or None,
            reasoning_content=getattr(msg, "reasoning_content", None),
            usage=(
                _to_usage(completion.usage)
                if completion.usage
                else None
            ),
            metadata=metadata,
        )

    @override
    @normalize_adapter_errors("openai")
    async def call_embed(
        self, texts: Sequence[str], **kwargs
    ) -> Sequence[EmbeddingChunk]:
        """Embedding interface"""
        if isinstance(texts, str):
            raise ValueError(
                "Texts cannot be string, please pass a sequence of strings"
            )
        config: AmritaConfig = self.config
        preset: ModelPreset = self.preset

        base_url: str = preset.base_url
        key: str = preset.api_key
        model: str = preset.model
        client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key=key,
            timeout=config.llm.llm_timeout,
        )
        text_seq = cast(SequenceNotStr, texts)
        response = await client.embeddings.create(input=text_seq, model=model, **kwargs)
        return [
            EmbeddingChunk.model_validate(i, from_attributes=True)
            for i in response.data
        ]

    @classmethod
    @normalize_adapter_errors("openai")
    async def fetch_models(cls, preset: ModelPreset) -> list[ModelInfo]:
        """List the models the endpoint advertises on ``/models``."""
        client = openai.AsyncOpenAI(
            base_url=preset.base_url,
            api_key=preset.api_key,
        )
        page = await client.models.list()
        return [ModelInfo.model_validate(item.model_dump()) for item in page.data]

    @staticmethod
    def get_adapter_protocol() -> tuple[str, str]:
        return "openai", "__main__"
