from __future__ import annotations

import secrets
import string
from typing import Literal

from pydantic import Field, model_validator
from typing_extensions import LiteralString

from amrita_core.types import BaseModel


def random_alnum_string(length: int) -> str:
    if length < 0:
        raise ValueError("Length can't be smaller than zero!")

    chars: LiteralString = string.ascii_letters + string.digits

    return "".join(secrets.choice(chars) for _ in range(length))


class CookieConfig(BaseModel):
    """Amrita Core's cookie config"""

    enable_cookie: bool = Field(title="启用泄露检测",
        default=True, description="是否启用 Cookie 泄露检测机制"
    )
    cookie: str = Field(title="检测用 Cookie",
        default_factory=lambda: random_alnum_string(16),
        description="用于安全检测的 Cookie 字符串",
    )


class FunctionConfig(BaseModel):
    use_minimal_context: bool = Field(title="最小上下文模式",
        default=False,
        description="是否使用最小上下文，即只发送系统提示词和用户最后一条消息。关闭时会带上消息列表里的全部上下文，Agent 工作流执行时可能消耗大量 Token；开启可以明显减少 Token 用量",
    )
    agent_tool_call_limit: int = Field(title="工具调用轮数上限",
        default=10,
        ge=1,
        description="单次运行的工具调用轮数预算。模型的一次回复算一轮，一轮里请求三个工具也只算一轮；运行可能在预算用完前就结束。预算用完后，内置策略会收尾而不是在调用中途停下，让模型还能用已有信息作答；收尾最多几轮由 `agent_tool_refusal_rounds` 决定。自定义策略如果重写了 `on_limited()`，必须调用 `super()` 或自行设置 `_budget_exhausted`，否则会直接中断、没有收尾。",
    )
    agent_tool_parallel_limit: int = Field(title="单轮工具并发上限",
        default=0,
        ge=0,
        description="同一轮里最多同时执行几个并发安全的工具调用。一轮是模型一次请求工具的回复，所以这里限制的是单轮内的调用数，不是轮数预算（见 `agent_tool_call_limit`）。`0` 保持原有行为：这一轮的安全调用全部并发执行；`1` 强制逐个执行。工具自己声明为不安全的调用和内置的流程控制工具，不管这里设多少都单独执行。",
    )
    agent_tool_refusal_rounds: int = Field(title="超限后收尾轮数",
        default=2,
        ge=1,
        description="达到 `agent_tool_call_limit` 后，Agent 还能继续请求工具几轮才结束本次运行。这几轮里的工具请求都不会真正执行，而是统一返回拒绝结果，让模型仍能用已有信息作答。",
    )
    validate_tool_arguments: bool = Field(title="校验工具参数",
        default=True,
        description="工具执行前，是否按工具的参数定义检查模型给出的参数。检查不通过会以 `ERR:` 工具结果返回给模型，让它改正调用；关闭则参数原样传入。",
    )

    agent_step_token_budget: int = Field(title="单步 Token 预算",
        default=-1,
        ge=-1,
        description="内置 Step 循环中每个 Step 的提示词 Token 预算。设为 -1（或 0）表示不限制。某个 Step 累计的提示词 Token 达到预算时，迭代循环停止（``TokenBudget.exhausted``）。",
    )

    agent_middle_message: bool = Field(title="允许中途发消息",
        default=True,
        description="是否允许 Agent 在调用工具的过程中给用户发送中间消息",
    )
    agent_mcp_client_enable: bool = Field(title="启用 MCP 客户端",
        default=False, description="是否启用 MCP 客户端"
    )
    agent_mcp_server_scripts: list[str] = Field(title="MCP 服务脚本列表",
        default=[], description="MCP 服务脚本列表"
    )


class ReactConfig(BaseModel):
    """ReAct agent reasoning enhancement configuration.

    All options default to False/off for full backward compatibility.
    When enabled, these options progressively strengthen the ReAct agent's
    reasoning capabilities through structured chain-of-thought decomposition,
    post-reasoning self-reflection, and reasoning-aware tool selection.
    """

    structured_reasoning: bool = Field(title="结构化分步推理",
        default=False,
        description="启用带明确阶段标记（分析/计划/执行/验证）的分步结构化推理。开启后，推理模板会引导模型把问题拆成编号步骤。",
    )
    reasoning_depth: int = Field(title="推理深度",
        default=3,
        ge=1,
        le=10,
        description="推理链的最大深度（子问题数量），在结构化推理模板里作为软提示使用。",
    )

    enable_reflection: bool = Field(title="启用反思",
        default=False,
        description="启用推理后的自我反思。开启后，Agent 会在 STOP_TOOL 之后调用 verify_reasoning 工具，检查有没有自相矛盾、是否完整、是否符合用户目标，然后再生成最终回答。",
    )
    reflection_depth: int = Field(title="反思深度",
        default=1,
        ge=1,
        le=5,
        description="最多反思几轮。每一轮可能产生一处修正，再送回推理循环。",
    )

    reasoning_aware_tools: bool = Field(title="工具感知推理",
        default=False,
        description="启用按推理结果排列工具。开启后，结构化推理中预测到的工具会排在工具列表前面，引导模型优先使用最相关的工具。",
    )
    tool_prediction: bool = Field(title="工具预测",
        default=False,
        description="启用推理时的工具预测。开启后，结构化推理模板会让模型列出预计要用的工具。需要同时开启 structured_reasoning 才有效。",
    )


class BuiltinAgentConfig(BaseModel):
    tool_calling_mode: Literal["agent", "rag", "none"] = Field(json_schema_extra={"option_labels": {'agent': 'Agent 模式（agent）', 'rag': 'RAG 模式（rag）', 'none': '不调用工具（none）'}}, title="工具调用模式",
        default="agent",
        description="Amrita 内置 Agent 策略的工具调用模式",
    )
    agent_thought_mode: Literal[
        "reasoning", "chat", "reasoning-required", "reasoning-optional"
    ] = Field(json_schema_extra={"option_labels": {'reasoning': '先推理再执行（reasoning）', 'chat': '直接执行（chat）', 'reasoning-required': '每次调用工具前必须分析（reasoning-required）', 'reasoning-optional': '可推理可不推理（reasoning-optional）'}}, title="思考模式",
        default="chat",
        description="内置 Agent 策略的思考模式：reasoning 先推理再执行任务；reasoning-required 每次调用工具前都要做任务分析；reasoning-optional 不强制推理但允许推理；chat 直接执行任务",
    )
    loop_reasoning_trigger: int = Field(title="重复推理放弃次数",
        default=5,
        ge=1,
        description="重复推理多少次后放弃",
    )
    react_config: ReactConfig = Field(title="ReAct 推理",
        default_factory=ReactConfig,
        description="ReAct Agent 推理增强设置，控制结构化推理、自我反思和按推理结果选工具。为了向后兼容，所有选项默认关闭。",
    )


class LLMConfig(BaseModel):
    require_tools: bool = Field(title="强制每次调用工具",
        default=False,
        description="是否强制每次调用至少使用一个工具",
    )
    max_tokens: int = Field(title="输出 Token 兜底上限",
        default=10000,
        ge=1,
        description="回复输出 Token 的最后兜底值。实际发给模型服务的数值来自预设的 `max_output`（默认 28000）；只有预设把 `max_output` 明确设为 `None` 时才用这个值。",
    )
    compaction_max_tokens: int = Field(title="压缩摘要输出上限",
        default=2048,
        ge=0,
        description="压缩历史时生成摘要那次调用的输出 Token 上限。推理模型会先把输出预算花在思考上，摘要如果沿用很小的 `max_output`，就会返回空内容，压缩悄悄失效。设为 0 表示摘要调用使用预设自己的值。",
    )
    session_tokens_windows: int = Field(title="上下文窗口兜底值",
        default=65536,
        ge=1,
        description="当前预设没有声明 `max_context` 时使用的上下文窗口兜底值（默认 64k）。预设声明了自己的窗口时以预设为准，这样实际上限跟着模型走，而不是这个全局数字。",
    )
    llm_timeout: int = Field(title="请求超时（秒）",
        default=60,
        ge=1,
        description="API 请求超时时间（秒）",
    )
    max_retries: int = Field(title="最大重试次数",
        default=3,
        ge=0,
        description="最大重试次数",
    )
    max_fallbacks: int = Field(title="预设回退次数上限",
        default=5,
        ge=1,
        description="预设回退的最大次数",
    )
    context_strategy: Literal["compact", "slide", "none"] = Field(json_schema_extra={"option_labels": {'compact': '压缩成摘要（compact）', 'slide': '丢弃最早的消息（slide）', 'none': '不处理（none）'}}, title="历史超长处理方式",
        default="compact",
        description="历史超出预算时的处理方式。`compact` 把最早的一段折叠成 LLM 摘要（多一次调用，但要点能保留）；`slide` 按加权 Token 估算丢弃最早的消息（不多调用，保留的部分原样不变，丢掉的内容就没了）；`none` 不限制历史，超出窗口后由模型服务拒绝请求。",
    )
    compaction_trigger_ratio: float = Field(title="触发压缩比例",
        default=0.9,
        gt=0,
        le=1,
        description="历史达到预设 `max_context` 的多少比例时强制处理。设得低于 1.0，是为了留出上次实测的提示词大小和下次请求实际大小之间的差距。`compact` 和 `slide` 都适用。",
    )
    slide_target_ratio: float = Field(title="滑动裁剪目标比例",
        default=0.7,
        gt=0,
        le=1,
        description="`slide` 把历史裁剪到预设 `max_context` 的多少比例。必须低于 `compaction_trigger_ratio`，否则裁完又落回触发线，每次请求都会重新裁剪。",
    )
    memory_length_limit: int = Field(title="历史消息条数上限",
        default=200,
        ge=0,
        description="保留历史的消息条数上限，不管 Token 怎么计算都生效。需要它是因为 Token 触发依赖模型服务回报用量：网关不回报用量时，历史会无限增长。设为 0 表示关闭这个兜底，只靠 Token 触发。",
    )
    enable_overflow_recovery: bool = Field(title="超窗口自动恢复",
        default=True,
        description="模型服务因超出上下文窗口拒绝请求时，是否压缩历史后重试一次。压缩方式跟随 `context_strategy`，和两轮对话之间的处理一致。`context_strategy` 为 `none` 时忽略此项，因为它从不改写历史。",
    )
    enable_multi_modal: bool = Field(title="启用多模态（图片）",
        default=True,
        description="是否启用多模态支持（目前只支持图片）",
    )

    @model_validator(mode="after")
    def _slide_target_stays_below_the_trigger(self) -> LLMConfig:
        """Reject a `slide` target that sits at or above the trigger.

        `slide` trims down to `slide_target_ratio` and `needs_management`
        fires at `compaction_trigger_ratio`. If the target is not strictly
        lower, the trimmed history lands back on the trigger and every
        request slides again, dropping turns for nothing. The two numbers
        are only meaningful as a pair, so the check belongs next to them
        rather than in the docs.
        """
        if self.slide_target_ratio >= self.compaction_trigger_ratio:
            raise ValueError(
                "`slide_target_ratio` must stay below `compaction_trigger_ratio`: "
                f"got {self.slide_target_ratio} and {self.compaction_trigger_ratio}"
            )
        return self


class AmritaConfig(BaseModel):
    function_config: FunctionConfig = Field(title="Agent 与工具",
        default_factory=FunctionConfig,
        description="Agent 与工具调用设置",
    )
    llm: LLMConfig = Field(title="模型请求",
        default_factory=LLMConfig,
        description="模型请求设置",
    )
    cookie: CookieConfig = Field(title="Cookie 泄露检测",
        default_factory=CookieConfig, description="Cookie 泄露检测设置"
    )
    builtin: BuiltinAgentConfig = Field(title="内置 Agent 策略",
        default_factory=BuiltinAgentConfig,
        description="内置 Agent 策略设置",
    )


__config = AmritaConfig()
__inited: bool = False


def get_config() -> AmritaConfig:
    """Get the global amrita config

    Raises:
        RuntimeError: Raise it if amrita core is not initialized.

    Returns:
        AmritaConfig: Amrita core config
    """
    if not __inited:
        raise RuntimeError(
            "Global AmritaConfig is not initialized. Please use `set_config` set config first."
        )
    return __config


def set_config(config: AmritaConfig):
    """Override the global config.

    Args:
        config (AmritaConfig): Configuration object to set
    """
    global __config, __inited
    if not __inited:
        __inited = True
    __config = config
