from amrita_sense.logging import logger

from amrita_core.config import get_config
from amrita_core.contents import MessageWithMetadata
from amrita_core.tools.manager import on_tools
from amrita_core.tools.models import (
    FunctionDefinitionSchema,
    FunctionParametersSchema,
    FunctionPropertySchema,
    ToolContext,
    ToolFunctionSchema,
)

from .types import AgentMiddleMessageMetadata

PROCESS_MESSAGE_TOOL = FunctionDefinitionSchema(
    name="processing_message",
    description=(
        "向当前会话发送一条用户可见的进度说明。"
        "只在任务要等一会儿或确实需要告知当前操作时使用。"
        "只说正在做什么，最终答复交给后续回复阶段。"
    ),
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "content": FunctionPropertySchema(
                description="一条简短、适合直接发到当前会话的进度消息。",
                type="string",
            ),
        },
        required=["content"],
    ),
)
PROCESS_MESSAGE = ToolFunctionSchema(
    type="function",
    function=PROCESS_MESSAGE_TOOL,
    strict=True,
)
STOP_TOOL = ToolFunctionSchema(
    type="function",
    function=FunctionDefinitionSchema(
        name="agent_stop",
        description=(
            "结束上下文中明确存在的多步任务或计划。只有确需结束整个计划时才调用。"
            "普通聊天、能力询问或单次查询已有充分结果时，不调用本工具，走正常的无工具收尾路径。"
            "result 只写简短的任务状态，不代写最终聊天回复。\n"
            "如果存在多步计划（对话里显示 [Plan status]），等所有步骤都是 done 才用。"
            "只完成当前一步时，停止本步的工具调用，由程序推进到下一步。"
            "若剩余步骤确实可以省略，先用 update_step 的 remove_step 或 replan 调整计划。"
            "只有确实要放弃整个任务时，才可以在仍有 pending 或 current 步骤时使用。"
        ),
        parameters=FunctionParametersSchema(
            type="object",
            properties={
                "result": FunctionPropertySchema(
                    type="string",
                    description="可选，简要说明已完成的操作。",
                )
            },
            required=[],
        ),
    ),
    strict=True,
)

REASONING_TOOL = ToolFunctionSchema(
    type="function",
    function=FunctionDefinitionSchema(
        name="think_and_reason",
        description="Think about what you should do next, always call this tool to think when completing a tool call.",
        parameters=FunctionParametersSchema(
            type="object",
            properties={
                "last_step": FunctionPropertySchema(
                    description="The last step you took (if there are no steps that you had done, please leave this blank).",
                    type="string",
                    default="(No last step)",
                ),
                "summary": FunctionPropertySchema(
                    description="What are you thinking about (not thinking content)",
                    type="string",
                ),
            },
            required=[
                "summary",
                "last_step",
            ],
        ),
    ),
    strict=True,
)


REFLECTION_TOOL = ToolFunctionSchema(
    type="function",
    function=FunctionDefinitionSchema(
        name="verify_reasoning",
        description="Verify your reasoning chain before delivering the final answer. "
        "Check for logical soundness, internal consistency, and completeness. "
        "Call this tool after you believe you are ready to answer, to catch errors "
        "before they reach the user.",
        parameters=FunctionParametersSchema(
            type="object",
            properties={
                "check_type": FunctionPropertySchema(
                    type="string",
                    enum=["self_check", "contradiction_check", "completeness_check"],
                    description="Type of verification to perform: "
                    "'self_check' for logical soundness, "
                    "'contradiction_check' for internal consistency, "
                    "'completeness_check' for coverage of all user requirements.",
                ),
                "result": FunctionPropertySchema(
                    type="string",
                    enum=["pass", "warning", "fail"],
                    description="Outcome of the verification. "
                    "'pass' if the reasoning is sound, "
                    "'warning' if there is a minor issue, "
                    "'fail' if there is a significant problem.",
                ),
                "detail": FunctionPropertySchema(
                    type="string",
                    description="Explanation of the finding (one or two sentences). "
                    "If result is 'fail', describe what went wrong and how to fix it.",
                ),
            },
            required=["check_type", "result", "detail"],
        ),
    ),
    strict=True,
)

UPDATE_STEP_TOOL = ToolFunctionSchema(
    type="function",
    function=FunctionDefinitionSchema(
        name="update_step",
        description=(
            "Adjust the remaining sub-steps of the current task plan. "
            "Use when you discover the original decomposition was wrong, the task "
            "has changed, or a sub-step is done or unnecessary. "
            'The plan is a DAG: [{"id": "step-1", "description": "...", "depends_on": ["step-0"]}, ...]'
        ),
        parameters=FunctionParametersSchema(
            type="object",
            properties={
                "action": FunctionPropertySchema(
                    type="string",
                    enum=["replan", "mark_done", "add_step", "remove_step"],
                    description=(
                        "replan: replace the whole plan with `dag`; "
                        "mark_done: finish the current sub-step; "
                        "add_step: append the node in `node`; "
                        "remove_step: remove the node whose id is `node_id`."
                    ),
                ),
                "dag": FunctionPropertySchema(
                    type="array",
                    items=FunctionPropertySchema(
                        type="object",
                        description="A single DAG node",
                        properties={
                            "id": FunctionPropertySchema(
                                type="string", description="Sub-step id"
                            ),
                            "description": FunctionPropertySchema(
                                type="string", description="What this sub-step does"
                            ),
                            "depends_on": FunctionPropertySchema(
                                type="array",
                                items=FunctionPropertySchema(
                                    type="string",
                                    description="Dependency sub-step id",
                                ),
                                description="Ids this sub-step depends on",
                            ),
                        },
                    ),
                    description="New DAG for `replan` (optional).",
                ),
                "node": FunctionPropertySchema(
                    type="object",
                    properties={
                        "id": FunctionPropertySchema(
                            type="string", description="Sub-step id"
                        ),
                        "description": FunctionPropertySchema(
                            type="string", description="What this sub-step does"
                        ),
                        "depends_on": FunctionPropertySchema(
                            type="array",
                            items=FunctionPropertySchema(
                                type="string",
                                description="Dependency sub-step id",
                            ),
                            description="Ids this sub-step depends on",
                        ),
                    },
                    description="Single DAG node for `add_step` (optional).",
                ),
                "node_id": FunctionPropertySchema(
                    type="string",
                    description="Sub-step id for `remove_step` (optional).",
                ),
                "note": FunctionPropertySchema(
                    type="string",
                    description="Short note explaining the revision (optional).",
                ),
            },
            required=["action"],
        ),
    ),
    strict=True,
)


@on_tools(
    data=PROCESS_MESSAGE_TOOL,
    custom_run=True,
    enable_if=lambda: get_config().function_config.agent_middle_message,
)
async def _(ctx: ToolContext) -> str | None:
    msg: str = ctx.data["content"]
    logger.debug(f"[LLM-ProcessMessage] {msg}")

    stream = ctx.ctx.io_stream
    if stream is None:
        if ctx.ctx.chat_object is None:
            raise RuntimeError(
                "LLM-ProcessMessage: no io_stream and no chat_object in StrategyContext"
            )
        stream = ctx.ctx.chat_object.io_stream
    await stream.yield_response(
        MessageWithMetadata(
            content=msg,
            metadata=AgentMiddleMessageMetadata(
                type="middle_message", content=msg, extra_type=None
            ),
        )
    )
    return f"Sent a message to user:\n\n```text\n{msg}\n```\n"
