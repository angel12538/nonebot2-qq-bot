from __future__ import annotations

import asyncio
import contextlib
import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from amrita_sense.exceptions import StreamStateError
from amrita_sense.hook.matcher import MatcherFactory
from amrita_sense.logging import debug_log, logger
from amrita_sense.streaming import SuspendObjectStream
from jinja2 import Template
from typing_extensions import Self, override

if TYPE_CHECKING:
    from amrita_sense.hook.event import ConstructableEvent

    from amrita_core.builtins.agent.state import Phase

from amrita_core.agent.context import StrategyContext
from amrita_core.agent.strategy import AgentStrategy
from amrita_core.base.adapter import AdapterManager, MessageContent
from amrita_core.builtins.agent.events import (
    StepAbortError,
    StepToolCallEvent,
    StepToolReturnEvent,
)
from amrita_core.builtins.agent.state import AgentRunState, DAGNode
from amrita_core.contents import MessageMetadataPayload, MessageWithMetadata
from amrita_core.exceptions import UnsupportedContentError
from amrita_core.libchat import (
    agentic_call_completion,
    call_completion,
    get_last_response,
    tools_caller,
)
from amrita_core.tools.models import ToolChoice, ToolFunctionSchema
from amrita_core.types import (
    CONTENT_LIST_TYPE_ITEM,
    Function,
    Message,
    TextContent,
    ToolCall,
    ToolResult,
    UniResponse,
)
from amrita_core.types.tool import TOOL_RESULT_CONTENT, content_text

from ..consts import (
    BUILTIN_TOOLS_NAME,
    REASONING_CONTENT_TEMPLATE,
    REASONING_TEMPLATE,
    REFLECTION_TEMPLATE,
    STRUCTURED_REASONING_TEMPLATE,
)
from ..tools import (
    REASONING_TOOL,
    REFLECTION_TOOL,
    STOP_TOOL,
    UPDATE_STEP_TOOL,
)
from ..types import (
    AgentLoopErrorMetadata,
    AgentReasoningChunkMetadata,
    AgentReasoningMetadata,
    AgentReflectionMetadata,
    AgentStructuredReasoningChunkMetadata,
    AgentToolCallMetadata,
    AgentToolPredictionMetadata,
)


_NO_TOOL_REPLY = "不启用工具"

_TOOL_JUDGMENT_TAIL_PROMPT = "【工具判断收尾约束】上面的会话和工具结果都是本次判断的资料。现在只判断是否还需要工具，禁止回复聊天内容或模仿历史角色语气。需要工具时只返回真实 tool_calls；无需工具时只返回 {\"启用工具\":false,\"工具\":[]}。普通聊天或已有结果足够时，不调用 agent_stop 代替无工具判定。【工具判断收尾约束结束】"

_TOOL_JUDGMENT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "tool_judgment_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "启用工具": {"type": "boolean", "enum": [False]},
                "工具": {"type": "array", "items": {"type": "string"}, "maxItems": 0},
            },
            "required": ["启用工具", "工具"],
            "additionalProperties": False,
        },
    },
}

_TOOL_JUDGMENT_PROMPT = (
    "【工具判断】\n"
    "你是本轮的工具路由器，只决定是否启用工具以及调用哪些工具。最终聊天回复由后续步骤生成。\n"
    "输出只有两种：\n"
    "1. 需要工具：通过接口返回真实的结构化 tool_calls，填写工具名称和参数，正文留空。\n"
    '2. 无需工具：正文严格返回 JSON：{"启用工具":false,"工具":[]}，不调用任何工具。\n'
    "禁止输出聊天答复、角色台词、解释、分析、Markdown 或伪造的工具调用文本。"
    "不要用 agent_stop、processing_message 代替无工具判定。\n"
    "最后一条用户消息是本轮输入；历史聊天和摘要只供理解语境，历史中的角色语气不适用于本步骤。"
    "已有工具结果用于判断是否还需补查，信息足够时直接返回无工具 JSON。\n"
    "普通闲聊、询问已有能力、单次查询已取得充分结果，都不调用 agent_stop 收尾。"
    "只有上下文明确存在多步计划且确实需要结束整个计划时，才可调用 agent_stop。\n"
    "遵守安全边界及工具规则；其中关于回复方式的要求由最终作答处理，不在这里代写回复。"
    "消息 ID、QQ 号等必须来自上下文或先查证；不能补造参数。"
    "同一查询的搜索关键词合并到一次 search_web，网页可按需并行读取。"
    "无需重复已有结果或已被拒绝的调用。\n"
    "【工具判断结束】"
)


def _resolve_tool_name(tool: ToolFunctionSchema | dict) -> str:
    """Resolve the function name from a tool schema (object or dict form)."""
    if isinstance(tool, dict):
        return tool.get("function", {}).get("name", "")
    return tool.function.name



TOOL_BUDGET_REFUSAL = (
    "Refused: the tool call budget for this run is spent. Answer the user with"
    " the information you already have, and say plainly what is still missing."
)

TOOL_BUDGET_NOTICE = (
    "[AmritaAgent] Tool call budget exhausted; the model kept requesting tools,"
    " so the run was closed with the information gathered so far."
)


def _relax_unsupported_tool_results(
    messages: list[CONTENT_LIST_TYPE_ITEM], exc: UnsupportedContentError
) -> list[CONTENT_LIST_TYPE_ITEM] | None:
    """Replace untransportable tool results with a text notice on a copy.

    Stored history must stay as it is: this only affects one request, so the
    original image is still there once the model returns text. ``None`` means
    nothing was changed and the caller should re-raise the original error.
    """
    targets = set(exc.tool_call_ids)
    repaired: list[CONTENT_LIST_TYPE_ITEM] = []
    changed = False
    for msg in messages:
        if (
            isinstance(msg, ToolResult)
            and isinstance(msg.content, list)
            and (not targets or msg.tool_call_id in targets)
        ):
            blocks = [block for block in msg.content if isinstance(block, TextContent)]
            blocks.append(TextContent(text=exc.notice))
            repaired.append(msg.model_copy(update={"content": blocks}))
            changed = True
        else:
            repaired.append(msg)
    return repaired if changed else None


class BaseReActAgentStrategy(AgentStrategy, ABC):
    """
    Abstract base class for ReAct agent strategies with common execution logic.

    This class provides shared functionality for ReAct-style agents including:
    - Tool calling orchestration and execution flow control
    - Reasoning message generation and processing
    - Loop detection and recovery mechanisms
    - Tool call notification handling
    - Common error handling patterns
    - Unified stop state management via `_suggested_stop` flag

    ## Stop State Management

    The `_suggested_stop` flag controls the `tool_choice` parameter behavior:
    - When `False` (default): `tool_choice` can be set to "required" to force tool calls
    - When `True`: `tool_choice` switches to "auto", allowing the model to decide whether to call tools

    This flag is automatically set to `True` when the STOP_TOOL is invoked, enabling
    a smooth transition from mandatory tool execution to free-form response generation.

    Subclasses should implement strategy-specific behaviors like message formatting
    and context management while inheriting the core execution framework.

    ## Class Attributes

    - `agent_last_step`: Tracks the last reasoning step or action taken
    - `call_count`: Tool-call batch counter, starting at 1 and incremented once per
      batch that reaches the end of the call loop (cancelled calls included,
      reasoning-tool calls excluded); not the round budget counter and not a count
      of tool calls that actually ran
    - `tools`: List of available tools for the agent
    - `origin_msg`: Original user message content
    - `origin_instruction`: System instruction from training context
    - `reasoning_pc`: Reasoning process counter for loop detection
    - `_suggested_stop`: Flag indicating whether to switch tool_choice to auto mode
    """

    agent_last_step: str | None = None
    call_count = 1
    tools: list[ToolFunctionSchema]
    origin_msg: str = ""
    origin_instruction: str = ""
    reasoning_pc = 0
    _suggested_stop: bool = False
    _reasoning_tool_template: Template = REASONING_TEMPLATE
    _reasoning_content_template: Template = REASONING_CONTENT_TEMPLATE
    _structured_reasoning_template: Template = STRUCTURED_REASONING_TEMPLATE
    _reflection_template: Template = REFLECTION_TEMPLATE


    _predicted_tools: list[str]
    """Tools predicted during structured reasoning."""


    _run_state: "AgentRunState | None" = None
    """Semantic step-level run state, bridged from the framework loop state."""

    def _is_native_thinking_enabled(self) -> bool:
        """Return True when the model preset has native thinking enabled.

        Native thinking (Claude Extended Thinking, OpenAI o-series, etc.)
        may not support ``tool_choice``, so forced tool-choice values must
        be downgraded to ``"auto"`` to avoid provider errors.
        """
        preset = self.preset
        if preset is None or isinstance(preset, str):
            return False
        thinking = preset.thinking_config
        if thinking is None:
            return False

        return thinking.thinking_type == "enabled" or bool(thinking.enable_thinking)

    def _resolve_tool_choice(self, desired: ToolChoice) -> ToolChoice:
        """Resolve the *actual* ``tool_choice`` to send to the provider.

        When native thinking is enabled the provider may reject forced
        values (``"required"`` or a specific tool schema).  In that case
        we fall back to ``"auto"`` and rely on prompt instructions instead.
        """
        if not self._is_native_thinking_enabled():
            return desired
        if desired == "required" or isinstance(desired, ToolFunctionSchema):
            return "auto"
        return desired

    def _use_agentic_path(self) -> bool:
        """Whether this run uses the streaming agentic gateway.

        Only native-thinking presets on adapters that implement
        ``agentic_call_api`` take it; everything else keeps the legacy tool
        round plus a separate completion, so behaviour is unchanged elsewhere.
        """
        if not self._is_native_thinking_enabled():
            return False
        adapter_class = AdapterManager().safe_get_adapter(self.preset.protocol)
        if adapter_class and adapter_class.features.agentic_call:
            return True
        if not self._agentic_warned:
            logger.warning(
                f"Preset `{self.preset.name}` runs native thinking on adapter"
                f" `{self.preset.protocol}`, which does not implement"
                " agentic_call_api; falling back to the legacy tool round plus a"
                " separate completion. A tool call the model still wants at that"
                " point can surface as plain text instead of a tool_call."
            )
            self._agentic_warned = True
        return False

    def _store_final_response(self, response: UniResponse) -> None:
        """Hand the turn's answer to the workflow.

        ``LLM_COMPLETION`` reads the workflow's ``WorkingState``, which the
        strategy cannot reach directly, so the answer travels through the
        strategy context and ``AGENT_POST_PROCESS`` copies it across. The
        chat-object handle is kept in step for callers that read it directly.
        """
        self.ctx.final_response = response
        if self.chat_object is not None:
            self.chat_object._di_resp.response = response

    async def _model_round(
        self,
        messages: list[CONTENT_LIST_TYPE_ITEM],
        tools: list[ToolFunctionSchema],
        tool_choice: ToolChoice | None = None,
    ) -> UniResponse[str | None, list[ToolCall] | None]:
        """Run one model round, repairing untransportable tool results once.

        When the adapter cannot carry a tool result's non-text blocks the gateway
        raises :class:`UnsupportedContentError` before sending anything. The
        offending blocks are replaced with the error's ``notice`` on a copy and
        the round is retried, so the model reads why the media is missing and
        decides what to do - ask the tool for text, or pick another tool -
        instead of the run dying on a capability mismatch.
        """
        try:
            return await self._model_round_once(messages, tools, tool_choice)
        except UnsupportedContentError as exc:
            repaired = _relax_unsupported_tool_results(messages, exc)
            if repaired is None:
                raise
            logger.warning(
                f"{exc.message} The tool result(s) are retried as text so the"
                " model can choose a different return shape."
            )
            return await self._model_round_once(repaired, tools, tool_choice)

    async def _model_round_once(
        self,
        messages: list[CONTENT_LIST_TYPE_ITEM],
        tools: list[ToolFunctionSchema],
        tool_choice: ToolChoice | None = None,
    ) -> UniResponse[str | None, list[ToolCall] | None]:
        """Run one model round, streaming text live on the agentic path.

        The legacy path only ever asks for tool calls; the agentic path gets
        text and tool calls out of the same stream, so the answer reaches the
        user while it is produced instead of after a second, tool-less request.
        """
        if not self._use_agentic_path():
            tool_context = ""
            if self.chat_object is not None:
                tool_context = self.chat_object.tool_judgment_context
            instruction = (
                f"{_TOOL_JUDGMENT_PROMPT}\n\n{tool_context.strip()}"
                if tool_context.strip()
                else _TOOL_JUDGMENT_PROMPT
            )
            judgment_messages = [
                Message(role="system", content=instruction),
                *(message for message in messages if message.role != "system"),
                Message(role="system", content=_TOOL_JUDGMENT_TAIL_PROMPT),
            ]

            judgment_preset = self.preset
            if hasattr(judgment_preset, "model_copy"):
                judgment_preset = judgment_preset.model_copy(deep=True)
                judgment_preset.config.temperature = 0.2
            judgment = await tools_caller(
                judgment_messages,
                tools,
                tool_choice="auto",
                preset=judgment_preset,
                usage=self.usage,
                response_format=_TOOL_JUDGMENT_RESPONSE_FORMAT,
            )

            decision_content = None if judgment.tool_calls else _NO_TOOL_REPLY
            if judgment.content != decision_content:
                judgment = judgment.model_copy(update={"content": decision_content})
            return judgment
        response: UniResponse[str, list[ToolCall] | None] | None = None
        stream = agentic_call_completion(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            preset=self.preset,
            config=self.config,
            usage=self.usage,
        )
        try:
            async for chunk in stream:
                if isinstance(chunk, UniResponse):
                    response = cast(UniResponse[str, list[ToolCall] | None], chunk)
                elif isinstance(chunk, (str, MessageContent)):
                    await self.io_stream.yield_response(chunk)
        finally:

            await stream.aclose()
        if response is None:
            raise RuntimeError("No final response from agentic call.")
        return cast(UniResponse[str | None, list[ToolCall] | None], response)

    def __init__(self, ctx: StrategyContext):
        super().__init__(ctx)
        self.tools = []
        self.origin_instruction = self.train_content
        config = self.config
        if config.builtin.tool_calling_mode == "agent":
            self.tools.append(STOP_TOOL)
        self.tools.extend(self.tools_manager.tools_meta().values())

        self._predicted_tools = []
        self._run_state = None

        self._step_tools_injected = False

        self._last_plan_snapshot: str | None = None

        self._peer_input_gen: AsyncGenerator[Any, None] | None = None
        self._peer_input_closed: bool = False
        self.origin_msg: str = (
            "".join(
                chunk.text
                for chunk in ctx.original_context.user_query.content
                if isinstance(chunk, TextContent)
            )
            if isinstance(ctx.original_context.user_query.content, list)
            else ctx.original_context.user_query.content
        )



    @property
    def run_state(self) -> "AgentRunState | None":
        """Semantic step-level run state, bridged from the loop state."""
        return self._run_state

    @run_state.setter
    def run_state(self, value: "AgentRunState | None") -> None:
        self._run_state = value

    def _init_run_state(self) -> "AgentRunState":
        """Create (or reuse) the semantic run state for this strategy.

        The framework bridge (``AGENT_ENTRY``) shares one instance between
        ``AgentLoopState.run_state`` and the strategy; when running outside
        the framework (unit tests), a fresh instance is created here.
        """
        if self._run_state is None:
            self._run_state = AgentRunState()
            self._run_state.tokens.budget = (
                self.config.function_config.agent_step_token_budget
            )
        return self._run_state

    async def intro_step(self, phase: "Phase" = "analyze") -> None:
        """Enter a Step boundary (default no-op; subclasses may override).

        Pending peer messages (reverse stream) are drained and appended to
        the context before the Step starts, so the next LLM request sees
        them as the latest input.

        Args:
            phase: the reasoning phase being entered.
        """

        self._ensure_step_tools()
        await self._drain_peer_input()
        rs = self._init_run_state()
        rs.begin_step(phase)

    def _ensure_step_tools(self) -> None:
        """Expose the ``update_step`` built-in once (idempotent).

        Called from ``intro_step``, which only the native step loop reaches;
        legacy runs therefore keep the original tool list.  The model needs
        to *see* the tool (plus the plan status injected at decomposition)
        before it can autonomously revise the plan mid-run.
        """
        if self._step_tools_injected:
            return
        self._step_tools_injected = True
        if UPDATE_STEP_TOOL not in self.tools:
            self.tools.append(UPDATE_STEP_TOOL)
            logger.info(
                "Step loop active: exposed 'update_step' built-in to the model."
            )

    async def _drain_peer_input(self) -> None:
        """Drain pending peer messages at a Step boundary (non-blocking).

        Consumes every object the consumer pushed over the reverse stream
        (``send_to_producer``) and appends it to the context as a ``user``
        message marked ``[peer message]``.  Messages pushed while the agent
        is inside a Step stay queued until the next boundary; messages
        pushed after the run finishes are dropped (queue closed).
        """
        if self._peer_input_closed:
            return
        stream = self.io_stream
        if not isinstance(stream, SuspendObjectStream):
            self._peer_input_closed = True
            return
        if self._peer_input_gen is None:
            try:
                self._peer_input_gen = stream.get_producer_input_generator()
            except StreamStateError:

                self._peer_input_closed = True
                return
        while True:

            try:
                item = await asyncio.wait_for(
                    self._peer_input_gen.__anext__(), timeout=0.001
                )
            except asyncio.TimeoutError:
                break
            except StopAsyncIteration:
                self._peer_input_closed = True
                break
            self.ctx.message.append(
                Message(role="user", content=f"[peer message]\n{item}")
            )

    async def _close_peer_input(self) -> None:
        """Notify the peer that the agent run is finished (idempotent).

        After this, further ``send_to_producer`` calls from the consumer
        side fail fast instead of blocking on the queue timeout.
        """
        if self._peer_input_closed:
            return
        self._peer_input_closed = True
        stream = self.io_stream
        if not isinstance(stream, SuspendObjectStream):
            return
        with contextlib.suppress(StreamStateError):
            await stream.send_done_to_producer()

    @override
    async def on_post_process(self) -> None:
        """Agent run finished: close the peer input channel (idempotent)."""
        await self._close_peer_input()

    async def leave_step(self, phase: "Phase | None" = None) -> None:
        """Leave a Step boundary (default no-op; subclasses may override)."""

        return

    async def after_iteration(self) -> None:
        """Per-iteration hook, called after each successful tool round.

        Runs *inside* the execute-phase iteration loop (STEP_EXEC), so it can
        stop the loop early — e.g. stall detection with give-up prompt
        injection — before more tokens are burned.  Default no-op; subclasses
        may override.
        """
        return

    async def _trigger_step_event(
        self,
        event: "ConstructableEvent",
        *,
        exception_ignored: tuple[type[BaseException], ...] = (),
    ) -> None:
        """Dispatch a step-lifecycle event to registered matchers.

        Handlers may mutate the event (the caller reads fields back) or raise
        an exception type listed in ``exception_ignored`` — it propagates out
        of ``trigger_event`` back to the calling lifecycle hook.
        """
        await MatcherFactory.trigger_event(event, exception_ignored=exception_ignored)

    async def _trigger_tool_call_event(self, tool_call: ToolCall) -> tuple[str, bool]:
        """Fire the pre-call event for a regular tool.

        Returns ``(arguments, cancel)`` — matchers may rewrite ``arguments``
        or set ``cancel`` (or raise :class:`StepAbortError`) to cancel the
        call without executing it.
        """
        rs = self._init_run_state()
        ev = StepToolCallEvent.constructor(
            rs,
            tool_name=tool_call.function.name,
            tool_id=tool_call.id,
            arguments=tool_call.function.arguments,
        )
        try:
            await self._trigger_step_event(ev, exception_ignored=(StepAbortError,))
        except StepAbortError:
            logger.info(f"Tool call {tool_call.function.name} aborted by matcher.")
            return (ev.arguments, True)
        return (ev.arguments, ev.cancel)

    async def _trigger_tool_return_event(
        self, tool_call: ToolCall, result: TOOL_RESULT_CONTENT
    ) -> tuple[TOOL_RESULT_CONTENT, bool]:
        """Fire the post-call event for a regular tool.

        Returns ``(result, skip_append)`` — matchers may rewrite ``result``
        or set ``skip_append`` (or raise :class:`StepAbortError`) to skip
        writing the result back to the context.
        """
        rs = self._init_run_state()
        ev = StepToolReturnEvent.constructor(
            rs,
            tool_name=tool_call.function.name,
            tool_id=tool_call.id,
            result=result,
        )
        try:
            await self._trigger_step_event(ev, exception_ignored=(StepAbortError,))
        except StepAbortError:
            logger.info(f"Tool return {tool_call.function.name} skipped by matcher.")
            return (ev.result, True)
        return (ev.result, ev.skip_append)

    async def _emit_step_event(
        self,
        content: str,
        metadata: MessageMetadataPayload,
    ) -> None:
        """Push a step-loop lifecycle event as stream metadata.

        Shared helper for the step-loop lifecycle hooks (intro_step /
        leave_step / decomposition / stall / compression); the payloads are
        the ``AgentStep*Metadata`` TypedDicts from ``builtins.types``.

        Note: distinct from ``_emit_step_metadata(step: dict)`` which emits
        a *structured-reasoning* step parsed from the model output.
        """
        await self.io_stream.yield_response(
            MessageWithMetadata(content=content, metadata=metadata)
        )

    def _record_tool_signature(self, tool_call: ToolCall) -> None:
        """Record a tool-call signature in the current Step (stall detection).

        Base implementation is a no-op; the built-in ReAct strategy records
        signatures into ``run_state.step_tool_signatures``.
        """
        return

    def _assistant_fields_from_response(
        self,
        response_msg: UniResponse[str | None, list[ToolCall] | None] | None,
    ) -> dict[str, Any]:
        """Extract assistant-message fields from the provider response verbatim.

        ``Message`` allows extra fields, so every field (``reasoning_content``,
        ``reasoning_signature`` and any provider-specific extra) is carried
        over as-is — nothing is hard-coded here.  ``role``/``content``/
        ``tool_calls``/``usage``/``metadata`` are excluded because the caller
        supplies them explicitly (the executed ``tool_calls``, the provider's
        ``content`` and the fixed ``assistant`` role).
        """
        if response_msg is None:
            return {}
        return response_msg.model_dump(
            exclude={"role", "content", "tool_calls", "usage", "metadata"},
            exclude_none=True,
        )

    def _maybe_inject_tool_failure_hint(
        self, tool_call: ToolCall, func_response: TOOL_RESULT_CONTENT
    ) -> None:
        """Optionally append failure guidance after a hard ``ERROR`` result.

        Base implementation is a no-op; the built-in ReAct strategy appends
        a one-shot revise hint when the result starts with ``ERROR``.
        """
        return

    def _detect_step_stall(self) -> bool:
        """Check whether the current Step is stalled (duplicate tool calls).

        Base implementation always reports ``False``; the built-in ReAct
        strategy performs real signature-based detection.
        """
        return False

    def _should_cancel_tool_call(self, tool_call: ToolCall) -> bool:
        """Whether this tool call should be cancelled *before* execution.

        Base implementation always returns ``False``.  Subclasses (e.g. the
        built-in ReAct strategy) may override this to return ``True`` when
        the call would trip the stall detector — the caller then returns a
        ``"Cancelled: ..."`` result instead of executing the tool, so the
        model sees an explicit cancellation rather than a normal result.
        """
        return False

    async def _handle_update_step(self, args: dict[str, Any]) -> None:
        """Handle the ``update_step`` built-in tool.

        Updates the semantic plan in ``run_state`` only — the Sense workflow
        itself is not modified (DAG is a hint layer, execution stays linear).

        Args:
            args: tool arguments with ``action`` (replan/mark_done/add_step/
                remove_step) and optional ``dag`` / ``node`` / ``node_id``.
        """
        rs = self._init_run_state()
        action = args.get("action")

        if action == "replan" and args.get("dag"):
            rs.plan = [DAGNode.model_validate(n) for n in args["dag"]]
            rs.completed_step_ids = []
            rs.current_step_id = None
        elif action == "mark_done":
            rs.complete_current_node()
        elif action == "add_step" and args.get("node"):
            node = DAGNode.model_validate(args["node"])
            if rs.plan is None:
                rs.plan = []
            rs.plan.append(node)
        elif action == "remove_step" and args.get("node_id"):
            nid = args["node_id"]
            if rs.plan:
                rs.plan = [n for n in rs.plan if n.id != nid]
            if nid in rs.completed_step_ids:
                rs.completed_step_ids.remove(nid)
        rs.plan_revision += 1
        logger.info(
            f"update_step({action}) applied, plan_revision={rs.plan_revision}, "
            f"plan={[n.id for n in (rs.plan or [])]}, done={rs.completed_step_ids}"
        )

    async def _generate_reasoning_content(
        self, tool_call: ToolCall, reasoning_trigger_msg: list[CONTENT_LIST_TYPE_ITEM]
    ) -> UniResponse[str, None]:
        tools = [
            {
                "name": tool.function.name,
                "description": tool.function.description,
            }
            for tool in self.tools
        ]
        resp_msg: dict[str, Any] = json.loads(tool_call.function.arguments)
        last_step: str = resp_msg.get("last_step", "(Err: Not given)")
        summary: str = resp_msg.get("summary", "(Err: Not given)")
        self.agent_last_step = last_step

        await self.io_stream.yield_response(
            MessageWithMetadata(
                summary,
                AgentReasoningMetadata(
                    type="reasoning",
                    extra_type="pre_resolve",
                    last_step=last_step,
                    summary=summary,
                ),
            )
        )

        use_structured = self._should_use_structured_reasoning()
        predict_tools = self._should_predict_tools()

        if use_structured:
            depth = self.config.builtin.react_config.reasoning_depth
            template_content = await asyncio.to_thread(
                self._structured_reasoning_template.render,
                tools=tools,
                last_step=last_step,
                summary=summary,
                stg=self,
                depth=depth,
                predict_tools=predict_tools,
            )
        else:
            template_content = await asyncio.to_thread(
                self._reasoning_content_template.render,
                tools=tools,
                last_step=last_step,
                summary=summary,
                stg=self,
            )

        reasoning_trigger_msg[0] = Message(
            role="system",
            content=template_content,
        )


        def _yield_wrapper(chunk):
            if isinstance(chunk, str):
                return MessageWithMetadata(
                    chunk,
                    metadata=AgentReasoningChunkMetadata(
                        type="text",
                        extra_type="reasoning_chunk",
                        content=chunk,
                    ),
                )
            return chunk

        ct: UniResponse[str, None] = await get_last_response(
            call_completion(
                reasoning_trigger_msg,
                preset=self.preset,
                config=self.config,
                usage=self.usage,
            ),
            yield_to=self.io_stream,
            yield_to_wrapper=_yield_wrapper,
        )


        reasoning_text = ct.content or ct.reasoning_content or ""
        if use_structured:

            steps = self._parse_reasoning_steps(reasoning_text)
            if steps:

                for step in steps:
                    await self._emit_step_metadata(step)

            if predict_tools:
                predicted = self._parse_tool_prediction(reasoning_text)
                if predicted:
                    self._predicted_tools = predicted
                    await self.io_stream.yield_response(
                        MessageWithMetadata(
                            content=f"[ToolPrediction] Expecting: {', '.join(predicted)}",
                            metadata=AgentToolPredictionMetadata(
                                type="tool_prediction",
                                extra_type="reasoning_prediction",
                                predicted_tools=predicted,
                                predicted_next_action="See reasoning steps",
                            ),
                        )
                    )
        return ct

    def _should_use_structured_reasoning(self) -> bool:
        """Check whether structured reasoning should be used."""
        config = self.config
        return (
            hasattr(config, "builtin")
            and hasattr(config.builtin, "react_config")
            and config.builtin.react_config is not None
            and config.builtin.react_config.structured_reasoning
        )

    def _should_predict_tools(self) -> bool:
        """Check whether tool prediction during reasoning should be used."""
        config = self.config
        return self._should_use_structured_reasoning() and (
            hasattr(config.builtin.react_config, "tool_prediction")
            and config.builtin.react_config.tool_prediction
        )

    def _should_enable_reflection(self) -> bool:
        """Check whether post-reasoning reflection should be used."""
        config = self.config
        return (
            hasattr(config, "builtin")
            and hasattr(config.builtin, "react_config")
            and config.builtin.react_config is not None
            and config.builtin.react_config.enable_reflection
        )

    @staticmethod
    def _parse_reasoning_steps(text: str) -> list[dict[str, str | None]]:
        """Parse structured reasoning output into step dictionaries.

        Extracts ``[Step N/M] [phase]`` markers and their associated content.
        (``[TOOL_PREDICTION]`` blocks are parsed separately in ``_parse_tool_prediction``.)

        Returns:
            List of dicts with keys: step_idx, total, phase, content
        """


        step_pattern = re.compile(
            r"\[Step\s+(\d+)(?:/(\d+))?\]\s*\[(\w+)\]\s*\n(.*?)(?=\n\[Step\s+\d|\n\[TOOL_PREDICTION\]|\Z)",
            re.DOTALL,
        )
        steps: list[dict[str, str | None]] = [
            {
                "step_idx": m.group(1),
                "total": m.group(2),
                "phase": m.group(3),
                "content": m.group(4).strip(),
            }
            for m in step_pattern.finditer(text)
        ]

        return steps

    @staticmethod
    def _parse_tool_prediction(text: str) -> list[str] | None:
        """Parse the [TOOL_PREDICTION] block from structured reasoning output."""
        pred_match = re.search(
            r"\[TOOL_PREDICTION\]\s*\ntools:\s*(.+?)\nnext_action:\s*(.+?)(?:\n|\Z)",
            text,
            re.IGNORECASE,
        )
        if pred_match:
            tools_str = pred_match.group(1).strip()

            return [t.strip() for t in tools_str.split(",") if t.strip()]
        return None

    async def _emit_step_metadata(self, step: dict[str, str | None]) -> None:
        """Emit a structured reasoning step as stream metadata."""
        phase_str = step.get("phase")
        phase: Literal["analyze", "plan", "execute", "verify"] | None = (
            phase_str if phase_str in ("analyze", "plan", "execute", "verify") else None
        )
        step_idx_raw = step.get("step_idx", "0")
        step_idx = int(step_idx_raw) if step_idx_raw is not None else 0
        total_raw = step.get("total")
        total = int(total_raw) if total_raw is not None else None
        await self.io_stream.yield_response(
            MessageWithMetadata(
                content=cast(str, step.get("content", "")),
                metadata=AgentStructuredReasoningChunkMetadata(
                    type="text",
                    extra_type="structured_reasoning_step",
                    step_index=step_idx,
                    total_steps=total,
                    sub_problem=None,
                    phase=phase,
                ),
            )
        )

    async def _run_reflection(
        self,
        reason_context: list[CONTENT_LIST_TYPE_ITEM],
    ) -> list[dict[str, str]]:
        """Run the post-reasoning reflection flow.

        Calls ``verify_reasoning`` tool up to ``reflection_depth`` times,
        streaming each result as ``AgentReflectionMetadata``.

        Returns:
            List of reflection result dicts with keys:
            check_type, result, detail
        """
        config = self.config
        max_rounds = config.builtin.react_config.reflection_depth
        reflection_results: list[dict[str, str]] = []

        for _ in range(max_rounds):

            reflection_system_msg = await asyncio.to_thread(
                self._reflection_template.render,
                last_step=self.agent_last_step or "No previous step",
                original_msg=self.origin_msg,
            )
            msg_list: list[CONTENT_LIST_TYPE_ITEM] = [
                Message(role="system", content=reflection_system_msg),
                *reason_context,
            ]

            tool_response = await tools_caller(
                msg_list,
                [REFLECTION_TOOL],
                tool_choice=self._resolve_tool_choice(REFLECTION_TOOL),
                preset=self.preset,
                usage=self.usage,
            )
            if not tool_response.tool_calls:
                break

            tc: ToolCall = tool_response.tool_calls[0]
            try:
                args: dict[str, str] = json.loads(tc.function.arguments)
            except json.JSONDecodeError as exc:
                logger.warning(
                    f"Failed to parse reflection tool arguments: {exc!s}. "
                    f"Raw: {tc.function.arguments!r}"
                )
                continue
            check_type: str = args.get("check_type", "self_check")
            result: str = args.get("result", "warning")
            detail: str = args.get("detail", "")

            reflection_results.append(
                {
                    "check_type": check_type,
                    "result": result,
                    "detail": detail,
                }
            )


            await self.io_stream.yield_response(
                MessageWithMetadata(
                    content=f"[Reflection] {check_type}: {result}",
                    metadata=AgentReflectionMetadata(
                        type="reflection",
                        extra_type=check_type,
                        reflection_type=cast(
                            Literal[
                                "self_check",
                                "contradiction_check",
                                "completeness_check",
                            ],
                            check_type,
                        ),
                        result=cast(Literal["pass", "warning", "fail"], result),
                        detail=detail,
                    ),
                )
            )

            logger.info(f"Reflection {check_type}: {result} — {detail}")


            if result == "pass":
                break

        return reflection_results

    async def _generate_reasoning_msg(
        self,
        tools_ctx: list[ToolFunctionSchema],
        /,
        then: Callable[
            [
                Self,
                ToolCall,
                UniResponse[str, None],
            ],
            Awaitable[Any],
        ],
    ):
        last_step = self.agent_last_step or "No previous step"
        original_msg = self.origin_msg
        reasoning_trigger_msg: list[CONTENT_LIST_TYPE_ITEM] = [
            Message(
                role="system",
                content=self._reasoning_tool_template.render(
                    stg=self,
                    last_step=last_step,
                    original_msg=original_msg,
                ),
            ),
            *self.ctx.message.unwrap(exclude_system=True, omit_web_history=True),
        ]
        tool_response: UniResponse[
            str | None, list[ToolCall] | None
        ] = await tools_caller(
            reasoning_trigger_msg,
            [REASONING_TOOL, *tools_ctx],
            tool_choice=self._resolve_tool_choice(REASONING_TOOL),
            preset=self.preset,
            usage=self.usage,
        )
        if not tool_response.tool_calls:
            logger.warning(
                "No tool calls returned from reasoning trigger"
                " (native thinking may have suppressed tool_choice=REASONING_TOOL)"
            )
            return
        tool_call: ToolCall = tool_response.tool_calls[0]
        response = await self._generate_reasoning_content(
            tool_call, reasoning_trigger_msg
        )

        await then(self, tool_call, response)

    @staticmethod
    def _build_stop_response(function_args: dict[str, Any]) -> str:
        """Build the stop tool response message.

        Args:
            function_args: Arguments passed to the stop tool

        Returns:
            The instruction message for final answer generation
        """
        func_response = (
            "【指令】\n"
            + "You have indicated readiness to provide the final answer. "
            + "Please now generate the final, comprehensive response for the user."
            + "You must NOT call any tools again."
            + "\n【指令结束】"
        )
        if "result" in function_args:
            debug_log(f"[Done] {function_args['result']}")
            func_response += f"\nWork summary :\n{function_args['result']}"
        return func_response

    def _check_and_handle_loop_reasoning(self) -> str | None:
        """Check if loop reasoning threshold has been exceeded and build prompt.

        Returns:
            Prompt message if loop is detected, None otherwise
        """
        config = self.config
        if self.reasoning_pc > config.builtin.loop_reasoning_trigger:
            prompt = f"Loop reasoning triggered. Trying to give up the tool call at ChatObject `{self.stream_id}`."
            logger.error(prompt)
            self.ctx.message.append(
                Message(
                    role="user",
                    content="【补充内容】\n\n"
                    + "You had called too many duplicate reasoning, which may indicate that you are stuck in a loop."
                    + "Please try to give up the current tool calling and directly answer the user query based on the information you have."
                    + "\n\n【补充内容结束】\n",
                )
            )
            return prompt
        return None

    async def _notify_tool_calls(
        self,
        result_msg_list: list[ToolResult],
        function_name: str,
        tool_call_id: str,
    ):
        """Send tool call completion notifications to user.

        Emitted unconditionally: the stream carries structured
        ``function_call`` events and consumers decide whether to render them.

        Args:
            result_msg_list: List of tool results to notify
            function_name: Name of the called function
            tool_call_id: ID of the tool call
        """
        for rslt in result_msg_list:
            await self.io_stream.yield_response(
                MessageWithMetadata(
                    content=f"Called tool {rslt.name}\n",
                    metadata=AgentToolCallMetadata(
                        type="function_call",
                        extra_type=None,
                        function_name=function_name,
                        is_done=True,
                        tool_id=tool_call_id,
                        err=None,
                    ),
                )
            )

    async def _build_stop_response_and_append(
        self,
        function_args: dict[str, Any],
        response_msg: UniResponse[str | None, list[ToolCall] | None],
        function_name: str,
        function_call_id: str,
        function_response: str,
    ):
        """Build stop response and append to message list (strategy-specific).

        Subclasses can override this to customize how the stop response is handled.
        Default implementation does nothing - subclasses should implement their own logic.

        Args:
            function_args: Arguments passed to the stop tool
            response_msg: The original response message
            function_name: Name of the function being called
            function_call_id: ID of the function call
            function_response: Response from the function
        """
        pass

    @abstractmethod
    async def _append_reasoning(
        self, tool_call: ToolCall, reasoning_content: UniResponse[str, None]
    ):
        """Append reasoning content to context (strategy-specific).

        Subclasses must implement this to define how reasoning results are added to context.

        Args:
            tool_call: The tool call object containing the reasoning request
            reasoning_content: The response containing the generated reasoning content
        """
        ...

    async def _handle_loop_reasoning_cleanup(self, prompt: str):
        """Clean up strategy-specific state when loop reasoning is detected.

        Subclasses can override this to perform cleanup operations.

        Args:
            prompt: The loop detection prompt message
        """
        pass

    async def _run_tool_calls_concurrently(
        self,
        tool_calls: list[ToolCall],
    ) -> list[tuple[ToolCall, TOOL_RESULT_CONTENT, BaseException | None]]:
        """Execute one round of tool calls, overlapping the ones declared safe.

        Calls are scanned in model order and split into runs: consecutive
        concurrency-safe calls form one pool, every unsafe call is a pool of
        one that also acts as a barrier for the calls after it. Pools run one
        after another; inside a pool at most
        ``function_config.agent_tool_parallel_limit`` calls are in flight, and
        a settled call immediately makes room for the next one. A limit of
        ``0`` removes the cap and lets the whole pool overlap, which is the
        historical behaviour.

        The stall guard and the pre-call event run in :meth:`_prepare_tool_calls`
        before the round is grouped, so a call a matcher rewrites into a
        state-mutating one is classified on the arguments it will actually
        receive. Built-in flow-control tools keep their specialised handlers,
        skip that event, and are always unsafe, so they never overlap a sibling.
        Regular tools use :meth:`call_tool`. A failure in one call cancels the
        calls still in flight, capped or not. Results are returned in the same
        order as the input list, no matter which call settled first.

        Subclasses may override this to change the execution strategy (e.g. add
        throttling, retries, or switch to sequential fallback).

        Args:
            tool_calls: ToolCall objects to execute. Callers should order them
                so that built-in tools appear **after** regular tools so that
                the side-effects of built-in handlers do not race with the
                context modifications produced by regular-tool result
                processing.

        Returns:
            List of ``(tool_call, result_string, original_exception)`` tuples.
            ``original_exception`` is the exception instance when an error
            occurred, ``None`` otherwise. On error ``result_string`` is
            formatted as ``"ERR: Tool {name} execution failed\\n{error}"``.
        """

        async def _exec_one(
            tc: ToolCall,
        ) -> tuple[ToolCall, TOOL_RESULT_CONTENT, BaseException | None]:
            fn = tc.function.name
            try:
                if fn == REASONING_TOOL.function.name:
                    content: UniResponse[
                        str, None
                    ] = await self._generate_reasoning_content(
                        tc, self.ctx.original_context.unwrap(omit_web_history=True)
                    )
                    await self._append_reasoning(tc, content)
                    return (tc, content.content, None)
                if fn == UPDATE_STEP_TOOL.function.name:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    await self._handle_update_step(args)

                    rs = self._init_run_state()
                    plan_desc = (
                        ", ".join(n.id for n in (rs.plan or []))
                        if rs.plan
                        else "(none)"
                    )
                    return (
                        tc,
                        (
                            "【计划已更新】 revised plan: "
                            f"{plan_desc}; done: {rs.completed_step_ids}"
                        ),
                        None,
                    )
                if fn == STOP_TOOL.function.name:
                    args: dict[str, Any] = json.loads(tc.function.arguments)
                    self.agent_last_step = "Stopped"
                    self.reasoning_pc = 0
                    self._suggested_stop = True
                    logger.info("Agent work has been terminated.")

                    if self._should_enable_reflection():
                        logger.debug("Running post-reasoning reflection...")
                        reason_ctx = self.ctx.original_context.unwrap(omit_web_history=True)
                        reflection_results = await self._run_reflection(reason_ctx)
                        failures = [
                            r for r in reflection_results if r["result"] == "fail"
                        ]
                        if failures:
                            correction_msg = (
                                "【复核修正】\n"
                                + "Your reasoning was checked and the following issues were found:\n"
                                + "\n".join(
                                    f"- [{r['check_type']}] {r['detail']}"
                                    for r in failures
                                )
                                + "\nPlease re-examine your reasoning and correct these issues "
                                + "before providing the final answer.\n"
                                + "【复核修正结束】"
                            )
                            self.ctx.message.append(
                                Message(role="user", content=correction_msg)
                            )

                            return (tc, "", None)

                    result = self._build_stop_response(args)
                    return (tc, result, None)


                result = await self.call_tool(tc)
                result, skip_append = await self._trigger_tool_return_event(tc, result)
                if skip_append:
                    return (tc, "", None)
                return (tc, result, None)
            except Exception as e:
                error_content = await self._handle_tool_error_common(fn, e, tc.id)
                return (tc, error_content, e)

        results: list[
            tuple[ToolCall, TOOL_RESULT_CONTENT, BaseException | None] | None
        ] = [None] * len(tool_calls)
        prepared, cancelled = await self._prepare_tool_calls(tool_calls)
        for index, reason in enumerate(cancelled):
            if reason is not None:
                results[index] = (tool_calls[index], reason, None)
        runnable = [index for index, reason in enumerate(cancelled) if reason is None]
        for group in self._group_tool_calls([prepared[index] for index in runnable]):
            await self._run_tool_pool(
                _exec_one, prepared, [runnable[offset] for offset in group], results
            )
        return [
            cast(tuple[ToolCall, TOOL_RESULT_CONTENT, BaseException | None], r)
            for r in results
        ]

    async def _prepare_tool_calls(
        self, tool_calls: list[ToolCall]
    ) -> tuple[list[ToolCall], list[str | None]]:
        """Run the per-call decisions a round needs before anything executes.

        The stall guard and the pre-call event both ran inside the executor once,
        which meant the concurrency decision was taken on arguments a matcher
        could still rewrite: a call rewritten into a state-mutating one stayed in
        a shared pool and raced its siblings. Both decisions are therefore taken
        here, in model order, before the round is grouped, so classification sees
        the arguments the tool will actually receive.

        Built-in flow-control tools skip the event, exactly as they did when the
        event ran inside the executor.

        Args:
            tool_calls: Calls of the round, in model order.

        Returns:
            The prepared calls, with any matcher rewrite applied, and a parallel
            list holding the result text of a call cancelled before dispatch, or
            ``None`` when the call proceeds.
        """
        prepared: list[ToolCall] = []
        cancelled: list[str | None] = []
        for tool_call in tool_calls:

            self._record_tool_signature(tool_call)
            if self._should_cancel_tool_call(tool_call):
                prepared.append(tool_call)
                cancelled.append(
                    "Cancelled: Reach the max limit of repeatly calling tool."
                )
                continue
            if tool_call.function.name in BUILTIN_TOOLS_NAME:
                prepared.append(tool_call)
                cancelled.append(None)
                continue
            args_str, cancel = await self._trigger_tool_call_event(tool_call)
            if cancel:
                prepared.append(tool_call)
                cancelled.append(
                    "Cancelled: Reach the max limit of repeatly calling tool."
                )
                continue
            if args_str != tool_call.function.arguments:
                tool_call = ToolCall(
                    id=tool_call.id,
                    function=Function(
                        name=tool_call.function.name,
                        arguments=args_str,
                    ),
                )
            prepared.append(tool_call)
            cancelled.append(None)
        return prepared, cancelled

    def _tool_call_is_concurrency_safe(self, tool_call: ToolCall) -> bool:
        """Tell whether one call may overlap its siblings in the same round.

        Built-in flow-control tools are always unsafe: they mutate the message
        list or the output stream directly. An unknown, disabled or
        unparseable call is unsafe as well, so the scheduler isolates it rather
        than letting it race.

        Args:
            tool_call: The call to classify.

        Returns:
            True only when the registration declares the call safe.
        """
        name = tool_call.function.name
        if name in BUILTIN_TOOLS_NAME:
            return False
        try:
            arguments: dict[str, Any] = json.loads(tool_call.function.arguments)
        except (json.JSONDecodeError, RecursionError):

            return False
        if not isinstance(arguments, dict):
            return False
        return self.tools_manager.is_concurrency_safe(name, arguments)

    def _group_tool_calls(self, tool_calls: list[ToolCall]) -> list[list[int]]:
        """Split one round into ordered pools of call indexes.

        Consecutive safe calls share a pool; each unsafe call becomes its own
        pool and therefore a barrier between the calls before and after it.

        Args:
            tool_calls: Calls in model order.

        Returns:
            Pools of indexes into ``tool_calls``, in execution order.
        """
        groups: list[list[int]] = []
        current: list[int] = []
        for index, tool_call in enumerate(tool_calls):
            if self._tool_call_is_concurrency_safe(tool_call):
                current.append(index)
                continue
            if current:
                groups.append(current)
                current = []
            groups.append([index])
        if current:
            groups.append(current)
        return groups

    async def _run_tool_pool(
        self,
        exec_one: Callable[
            [ToolCall],
            Awaitable[tuple[ToolCall, TOOL_RESULT_CONTENT, BaseException | None]],
        ],
        tool_calls: list[ToolCall],
        group: list[int],
        results: list[
            tuple[ToolCall, TOOL_RESULT_CONTENT, BaseException | None] | None
        ],
    ) -> None:
        """Run one pool of calls with a rolling cap, writing results in place.

        Slots are filled in model order up to the configured limit and
        replenished as calls settle, so one slow call never idles the pool. A
        failing worker propagates after the in-flight calls are cancelled,
        mirroring ``asyncio.gather`` without a limit.

        Args:
            exec_one: Coroutine running a single call.
            tool_calls: All calls of the round, indexed by ``group``.
            group: Indexes of the calls in this pool.
            results: Round-sized output list, written at each call's own index.
        """
        limit = self.config.function_config.agent_tool_parallel_limit

        cap = len(group) if limit <= 0 else min(limit, len(group))

        pending: list[int] = list(group)
        in_flight: dict[
            asyncio.Future[tuple[ToolCall, TOOL_RESULT_CONTENT, BaseException | None]],
            int,
        ] = {}
        try:
            while pending or in_flight:
                while pending and len(in_flight) < cap:
                    index = pending.pop(0)
                    task = asyncio.ensure_future(exec_one(tool_calls[index]))
                    in_flight[task] = index
                done, _ = await asyncio.wait(
                    in_flight, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    results[in_flight.pop(task)] = task.result()
        except BaseException:
            for task in in_flight:
                task.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            raise

    async def _execute_tool_loop(
        self,
        response_msg: UniResponse[str | None, list[ToolCall] | None],
    ) -> bool:
        """Execute the main tool calling loop.

        All tool calls are executed concurrently via
        :meth:`_run_tool_calls_concurrently`.  Built-in tools are ordered last
        so that their message-list side-effects (reasoning append, stop
        response) are applied after regular-tool results.

        Args:
            response_msg: The response from tools_caller containing tool calls.

        Returns:
            True if execution should continue, False if it should stop.
        """
        if not (tool_calls := response_msg.tool_calls):
            if self._use_agentic_path():

                self._store_final_response(response_msg)
            return False

        if self._budget_exhausted:

            if (
                self._refusal_rounds
                >= self.config.function_config.agent_tool_refusal_rounds
            ):

                logger.warning(
                    "Model kept requesting tools after the budget was spent;"
                    " closing the turn with a notice."
                )
                self._store_final_response(
                    UniResponse(
                        role="assistant",
                        content=TOOL_BUDGET_NOTICE,
                        tool_calls=None,
                    )
                )
                return False
            self._refusal_rounds += 1
            await self._append_tool_results_batch(
                response_msg, [(tc, TOOL_BUDGET_REFUSAL, None) for tc in tool_calls]
            )
            return True


        tool_calls.sort(key=lambda tc: tc.function.name in BUILTIN_TOOLS_NAME)


        for tc in tool_calls:
            await self.io_stream.yield_response(
                MessageWithMetadata(
                    content=f"Calling function {tc.function.name}\n",
                    metadata=AgentToolCallMetadata(
                        type="function_call",
                        extra_type=None,
                        function_name=tc.function.name,
                        is_done=False,
                        tool_id=tc.id,
                        err=None,
                    ),
                )
            )

        concurrent_results: list[
            tuple[ToolCall, TOOL_RESULT_CONTENT, BaseException | None]
        ] = await self._run_tool_calls_concurrently(tool_calls)


        result_msg_list: list[ToolResult] = []
        collected: list[tuple[ToolCall, TOOL_RESULT_CONTENT, BaseException | None]] = []
        should_continue = True
        for tc, func_response, exc in concurrent_results:
            function_name = tc.function.name
            is_reasoning = function_name == REASONING_TOOL.function.name
            is_stop = function_name == STOP_TOOL.function.name

            if is_reasoning:
                if content_text(func_response).startswith("ERR:"):

                    self.reasoning_pc = 0
                    await self._handle_error_append(
                        tc,
                        content_text(func_response),
                        original_exception=exc,
                        response_msg=response_msg,
                    )
                else:

                    should_continue = True

                continue

            if is_stop:
                if not content_text(func_response):

                    continue

            else:
                self.reasoning_pc = 0

            collected.append((tc, func_response, exc))
            logger.debug(
                f"Function {function_name} returned: {content_text(func_response)}"
            )
            result_msg_list.append(
                ToolResult(
                    role="tool",
                    content=func_response,
                    name=function_name,
                    tool_call_id=tc.id,
                )
            )
            self.call_count += 1

            prompt = self._check_and_handle_loop_reasoning()
            if prompt is not None:

                await self._append_tool_results_batch(response_msg, collected)
                collected.clear()
                await self._handle_loop_reasoning_cleanup(prompt)
                await self.io_stream.yield_response(
                    MessageWithMetadata(
                        content=prompt,
                        metadata=AgentLoopErrorMetadata(
                            type="error",
                            extra_type="loop_reasoning",
                            chat_object_id=self.stream_id,
                            error=prompt,
                        ),
                    )
                )

                should_continue = False
                break

        await self._append_tool_results_batch(response_msg, collected)


        if result_msg_list:
            await self._notify_tool_calls(
                result_msg_list,
                result_msg_list[-1].name,
                result_msg_list[-1].tool_call_id,
            )

        return should_continue

    async def _append_tool_results_batch(
        self,
        response_msg: UniResponse[str | None, list[ToolCall] | None],
        results: list[tuple[ToolCall, TOOL_RESULT_CONTENT, BaseException | None]],
    ) -> None:
        """Append ONE assistant message with all tool_calls, then all results.

        Concurrent tool calls are never split: the fabricated assistant
        message carries every executed ``tool_call`` plus the provider's
        original fields verbatim (``Message`` allows extra, so nothing is
        hard-coded here), followed by one ``ToolResult`` per call in input
        order — success, ``ERR:`` and stop responses alike.
        """
        if not results:
            return
        msg_list = self.ctx.message
        msg_list.append(
            Message(
                role="assistant",
                content=response_msg.content if response_msg else None,
                tool_calls=[tc for tc, _, _ in results],
                **self._assistant_fields_from_response(response_msg),
            )
        )
        for tc, func_response, _exc in results:
            msg_list.append(
                ToolResult(
                    role="tool",
                    name=tc.function.name,
                    content=func_response,
                    tool_call_id=tc.id,
                )
            )
            self._maybe_inject_tool_failure_hint(tc, func_response)

    async def _handle_error_append(
        self,
        tool_call: ToolCall,
        error_content: str,
        original_exception: BaseException | None = None,
        response_msg: UniResponse[str | None, list[ToolCall] | None] | None = None,
    ):
        """Handle appending error messages to context (strategy-specific).

        Args:
            tool_call: The failed tool call, kept verbatim on the fabricated
                assistant message so the round-trip stays faithful.
            error_content: Formatted error message to append.
            original_exception: The original exception, or ``None`` when the error
                was captured as a string during concurrent execution.
            response_msg: The provider response, used to carry ``reasoning_content``
                and ``reasoning_signature`` back on the fabricated assistant
                message (thinking-mode round-trip).
        """
        ...

    async def _handle_tool_error_common(
        self,
        function_name: str,
        err: BaseException,
        tool_call_id: str,
    ) -> str:
        """Common error handling logic for tool execution failures.

        Args:
            function_name: Name of the failed function
            err: The exception that occurred
            tool_call_id: ID of the tool call

        Returns:
            Error message string
        """
        logger.opt(exception=err).error(
            f"Function {function_name} execution failed: {err}"
        )
        config = self.config
        if (
            config.builtin.tool_calling_mode == "agent"
            and function_name not in BUILTIN_TOOLS_NAME
        ):
            await self.io_stream.yield_response(
                MessageWithMetadata(
                    content=f"Error: {function_name} failed.",
                    metadata=AgentToolCallMetadata(
                        type="function_call",
                        extra_type=None,
                        function_name=function_name,
                        is_done=True,
                        tool_id=tool_call_id,
                        err=err,
                    ),
                )
            )
        return f"ERR: Tool {function_name} execution failed\n{err!s}"

    @override
    async def on_exception(self, exc: BaseException) -> None:
        """No action to do, because we had already handled the exception in the agent strategy"""
        return


class NoActionAgentStrategy(AgentStrategy):
    """No action agent strategy. Use this strategy to give up the tool calling proces."""

    async def run(self) -> None:
        """No action"""
        return

    @override
    async def on_exception(self, exc: BaseException) -> None:
        """No action to do, because we had already handled the exception in the agent strategy"""
        return

    @classmethod
    def get_category(
        cls,
    ) -> Literal["workflow"]:
        return "workflow"


__all__ = [
    "BaseReActAgentStrategy",
    "NoActionAgentStrategy",
]
