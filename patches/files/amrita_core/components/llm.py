"""LLM-related workflow nodes — template rendering and completion calling.

```mermaid
graph LR
    LOAD_STATE --> JINJA2_RENDER
    LOAD_STATE --> BUILD_MESSAGE --> LLM_COMPLETION
    JINJA2_RENDER --> BUILD_MESSAGE
```
"""

import asyncio
import re
from time import time

from amrita_sense import Node, WorkflowInterpreter
from amrita_sense.logging import debug_log, logger

from amrita_core.base.adapter import MessageContent
from amrita_core.components.compaction import ContextCompactor
from amrita_core.contexts import (
    AbilityState,
    GeneralInput,
    MemoryContext,
    RespState,
    WorkingState,
)
from amrita_core.enums import SuspendEnum
from amrita_core.exceptions import ContextOverflowError
from amrita_core.libchat import call_completion
from amrita_core.types.message import Message
from amrita_core.types.response import UniResponse


_FINAL_REPLY_INSTRUCTION = (
    "当前阶段只生成面向用户的最终答复，工具调用阶段已结束。"
    "直接用已有对话和已返回的工具结果作答；资料不足时如实说明。"
    "不要输出工具调用、参数或协议标记，也不要声称已执行没有结果的搜索。"
)
_DSML_RETRY_INSTRUCTION = "上一份草稿只有工具协议内容或包含残缺标记。请重新生成一份可以直接发送的正文。"
_DSML_MARKER = re.compile(r"</?[|｜]DSML[|｜]")
_DSML_BLOCK = re.compile(
    r"<[|｜]DSML[|｜][ \t]*(?P<tag>function_calls|tool_calls|calls|invoke|parameter)\b[^<>]*>"
    r".*?</[|｜]DSML[|｜][ \t]*(?P=tag)[ \t]*>",
    re.DOTALL,
)


def strip_dsml(text: str) -> str:
    """移除误入正文的工具调用，残缺标记按整段异常处理。"""
    if not _DSML_MARKER.search(text):
        return text
    cleaned = _DSML_BLOCK.sub("", text)
    return "" if _DSML_MARKER.search(cleaned) else cleaned.strip()


@Node(SuspendEnum.TRAIN_RENDER)
async def JINJA2_RENDER(
    ability: AbilityState,
    mem: MemoryContext,
    ip: GeneralInput,
):
    """Render the train template via Jinja2 and append the user message to memory.

    Appends `ip.user_input` to `memory.messages`, then uses the Jinja2 template
    to render `ip.train` with `memory`, `config`, and custom `jinja2_vars`
    injected as template context variables.

    Context Dependencies:
        * AbilityState — provides runtime config.
        * MemoryContext — provides session memory.
        * GeneralInput  — provides user input, template, and render vars.

    Upstream:
        * LOAD_STATE — must have set `mem.memory`.

    Downstream:
        * BUILD_MESSAGE — consumes the rendered `ip.train`.

    Suspend Point:
        `SuspendEnum.TRAIN_RENDER` — intercepted during template rendering.
    """
    logger.debug("Starting JINJA2 template rendering..")
    data = mem.memory
    if data is None:
        raise RuntimeError(
            "Memory is not set, please run `LOAD_STATE` before rendering"
        )
    config = ability.config

    data.messages.append(Message(role="user", content=ip.user_input, summary_source_at=time()))

    logger.debug(
        f"Added user message to memory, current message count: {len(data.messages)}"
    )

    ip.train = Message.model_validate(ip.train, from_attributes=True)
    ip.train.content = await asyncio.to_thread(
        ip.template.render,
        train=ip.train,
        memory=data,
        config=config,
        **ip.jinja2_vars,
    )
    debug_log(ip.train.content)


async def _stream_completion(
    ability: AbilityState,
    wok: WorkingState,
    intp: WorkflowInterpreter,
    resp: RespState,
    *,
    retry_dsml: bool = False,
) -> UniResponse[str, None]:
    """Consume one completion stream, forwarding chunks to the client.

    The trailing ``UniResponse`` is returned; every other chunk is pushed onto
    the response stream. Provider usage is reported to the run ledger.
    """
    wrap = wok.context_wrap
    assert wrap is not None, (
        "Context wrap is not set, please run `BUILD_MESSAGE` before commit"
    )
    preset = ability.preset
    assert preset is not None, (
        "Preset is not set, please run `LOAD_STATE` before calling LLM"
    )
    messages = wrap.unwrap_for_chat_request()
    instruction = _FINAL_REPLY_INSTRUCTION
    if retry_dsml:
        instruction = f"{instruction}\n{_DSML_RETRY_INSTRUCTION}"
    system = wrap.train
    messages[0] = system.model_copy(
        update={"content": f"{system.content}\n\n{instruction}"},
    )
    response: UniResponse[str, None] | None = None
    async for chunk in call_completion(
        messages,
        config=ability.config,
        preset=preset,
        usage=resp.usage,
    ):
        if isinstance(chunk, UniResponse):
            response = chunk
        elif not retry_dsml and isinstance(chunk, MessageContent | str):
            await intp.object_io.yield_response(chunk)
    if response is None:
        raise RuntimeError("No final response from chat adapter.")
    if not response.content and not response.tool_calls and response.reasoning_content:

        used = response.usage.completion_tokens if response.usage else "?"
        logger.warning(
            "Provider returned reasoning but no answer and no tool calls "
            f"({used} output tokens). The output budget was likely exhausted by "
            "the reasoning, leaving nothing for the answer. Raise "
            "`LLMConfig.max_tokens` or the preset's `max_output`."
        )
    return response


async def _shrink_context(
    ability: AbilityState,
    mem: MemoryContext,
    wok: WorkingState,
    resp: RespState,
) -> bool:
    """Shrink the oldest run context so a retry fits the window.

    Dispatches on ``context_strategy``: ``compact`` folds the prefix into one
    summary message, ``slide`` drops the oldest messages outright — the same
    split the between-turn node makes, so an overflow cannot quietly rewrite a
    history the user asked to keep verbatim.

    Returns ``False`` when there is nothing to shrink or the model produced no
    summary, which tells the caller to give up and surface the original error.

    Also returns ``False`` when ``context_strategy`` is ``none``: the user
    asked for history to be left alone, so the overflow surfaces as an error
    rather than being silently papered over with a fold.
    """
    wrap = wok.context_wrap
    assert wrap is not None
    compactor = ContextCompactor(
        config=ability.config,
        preset=ability.preset,
        usage=resp.usage,
    )
    if not compactor.enabled:
        logger.warning(
            "Context overflow with `context_strategy = none`; "
            "history is left untouched and the request will fail."
        )
        return False
    if compactor.strategy == "slide":

        dropped = compactor.slide(wrap.memory, compactor.budget)
        if dropped <= 0:
            return False
        logger.warning(
            f"Context overflow; slid the {dropped} oldest messages out, retrying once."
        )
        return True
    parts = mem.memory.live_summary_parts() if mem.memory is not None else []
    result = await compactor.fold_summary(wrap.memory, parts)
    if result is None:
        return False
    wrap.memory, parts = result
    if mem.memory is not None:
        mem.memory.replace_summary(parts)
    wrap.history_summary = "\n\n".join(part.text for part in parts)
    return True


@Node(SuspendEnum.LLM_CALL)
async def LLM_COMPLETION(
    ability: AbilityState,
    mem: MemoryContext,
    wok: WorkingState,
    intp: WorkflowInterpreter,
    resp: RespState,
):
    """Call the LLM completion API and stream chunks to the client.

    ```mermaid
    flowchart TD
        A[call_completion gateway] --> B[stream chunks]
        B -->|UniResponse| C[resp.response = UniResponse]
        B -->|text chunk| D[yield to client]
        B -->|context overflow| E[shrink context, retry once]
    ```

    Preset fallback is handled inside the ``call_completion`` gateway (it
    fires ``CompletionFallbackContext`` on failure), so this node only consumes
    the stream and forwards the final ``UniResponse``. A provider
    context-window rejection is not retried by the gateway; when
    ``config.llm.enable_overflow_recovery`` is on, this node folds the run
    context once and retries. The retry lives inside the node so that
    ``LLM_COMPLETION`` stays usable as a standalone workflow step, where a
    deferred retry would silently produce no response.

    Context Dependencies:
        * AbilityState — provides config and current preset.
        * MemoryContext — receives the measured prompt usage.
        * WorkingState — provides built `context_wrap`.
        * WorkflowInterpreter — streams chunks to the client.
        * RespState — receives the final `UniResponse` and the run ledger.

    Upstream:
        * LOAD_STATE — must have set `ability.preset`.
        * BUILD_MESSAGE — must have built `wok.context_wrap`.

    Downstream:
        * APPEND_RESPONSE — consumes `resp.response`.

    Suspend Point:
        `SuspendEnum.LLM_CALL` — intercepted during the LLM call.
    """
    if resp.response is None and wok.final_response is not None:
        resp.response = wok.final_response
    if resp.response is not None:

        if mem.memory is not None and resp.response.usage is not None:
            mem.memory.usage = resp.response.usage
        return

    logger.debug("Calling chat model..")
    recoveries = 1 if ability.config.llm.enable_overflow_recovery else 0
    for attempt in range(recoveries + 1):
        try:
            response = await _stream_completion(ability, wok, intp, resp)
        except ContextOverflowError as e:
            if attempt >= recoveries:
                raise
            logger.warning(
                "Provider rejected the request for exceeding its context "
                f"window ({e!s}); shrinking the history and retrying once."
            )
            if not await _shrink_context(ability, mem, wok, resp):
                raise
            continue
        cleaned = strip_dsml(response.content)
        if cleaned != response.content:
            response.content = cleaned
            if cleaned:
                logger.warning("最终回复含 DSML 工具标记，已移除标记并保留正文")
            else:
                logger.warning("最终回复没有可发送的正文，正在重生成一次")
                try:
                    regenerated = await _stream_completion(ability, wok, intp, resp, retry_dsml=True)
                except Exception as exc:
                    logger.warning(f"最终回复重生成失败（{type(exc).__name__}），本轮静默")
                else:
                    regenerated.content = strip_dsml(regenerated.content)
                    if not regenerated.content.strip():
                        logger.warning("重生成后仍没有可发送的正文，本轮静默")
                    response = regenerated
        resp.response = response
        if mem.memory is not None and response.usage is not None:

            mem.memory.usage = response.usage
        return
