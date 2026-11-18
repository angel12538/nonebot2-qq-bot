"""Token 用量统计与持久化（从原 chat.py 抽出）

usage 完全依赖 provider 随响应上报（AmritaCore 1.0 已移除本地分词器），
汇总后写入全局 InsightsModel 与用户/群元数据。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from amrita_core import BillingRecord, debug_log
from amrita_core.utils import gather_usage
from amrita_sense.hook.matcher import MatcherFactory
from nonebot_plugin_amrita.database import InsightsModel

from ...events import ChatUsageRecordedEvent
from ...utils.libchat import add_usage
from ...utils.sql import get_uni_user_id, make_uni_id

if TYPE_CHECKING:
    from amrita_core.chatmanager import ChatObject as CoreChatObject
    from amrita_core.types import UniResponseUsage
    from nonebot.adapters.onebot.v11 import MessageEvent
    from nonebot_plugin_amrita.memory import CachedUserDataRepository

__all__ = ["record_usage"]


def _run_usage(chat: CoreChatObject) -> UniResponseUsage[int] | None:
    """本轮总消耗 = 最终响应 + agentic 工作流内辅助调用。

    ``extra_usage`` 的语义就是工作流内非最终消息的开销和，与最终响应相加才是
    本轮总账。

    ``gather_usage`` 是**原地累加** ``base``，而 ``response.usage`` 与
    ``memory.usage`` 是同一个对象（``LLM_COMPLETION`` 把它直接赋给
    ``memory.usage``）。若把 ``response.usage`` 直接当作 ``base`` 传入，会把
    「单次请求的上下文规模」污染成「本轮累计」：``/session info`` 的上下文占用
    会虚高，下一轮的历史管理也会因为读到虚高值而提前触发。
    因此先拷贝再累加。
    """
    resp = chat._di_resp.response
    if resp is None:
        return None



    extra = chat._di_resp.extra_usage
    if extra is not None and (extra.prompt_tokens or extra.completion_tokens):
        return extra.model_copy()
    if resp.usage is None:
        return None
    return gather_usage(resp.usage.model_copy(), extra)


def _record_cache(chat: CoreChatObject, date: str) -> None:
    """本地补丁：把本轮上报了缓存信息的请求累加到 guards/cache_stats（面板缓存命中率用）。

    取的是本轮账本（每次网关请求一条），不是会话累计账目，不会重复计。
    取不到或出错就跳过，不影响用量统计本身。
    """
    try:
        from guards import cache_stats

        proxy = chat._di_resp.usage
        records = proxy.records if proxy is not None else []
        reported = [r for r in records if r.cache_hit is not None]
        if reported:
            cache_stats.add(
                date,
                sum(r.prompt_tokens for r in reported),
                sum(r.cache_hit or 0 for r in reported),
                len(reported),
            )
    except Exception as e:
        debug_log(f"缓存命中统计跳过：{e}")


def _billing_snapshot(chat: CoreChatObject) -> list[BillingRecord]:
    """读取账目快照；DI 上下文未就绪（异常收尾路径）时返回空列表。"""
    try:
        return list(chat.data.billing)
    except RuntimeError:
        return []


async def record_usage(
    chat: CoreChatObject,
    event: MessageEvent,
    cudr: CachedUserDataRepository,
) -> None:
    """统计本次对话 token 用量并持久化到全局洞察与用户/群元数据

    provider 未上报 usage 时只计次、不计 token。
    """
    if chat._di_resp.response is None:
        return

    insights = await InsightsModel.get()
    debug_log(f"获取洞察数据完成，使用计数: {insights.usage_count}")

    usage = _run_usage(chat)
    add_usage(insights, usage)
    await insights.save()
    _record_cache(chat, insights.date)
    debug_log(f"更新全局统计完成，使用计数: {insights.usage_count}")

    ins = await cudr.get_metadata(get_uni_user_id(event))
    for d in (
        (
            ins,
            await cudr.get_metadata(make_uni_id(event.user_id, is_group=False)),
        )
        if hasattr(event, "group_id")
        else (ins,)
    ):
        d.called_count
        add_usage(d, usage)
        await cudr.update_metadata(d)


    await MatcherFactory.trigger_event(
        ChatUsageRecordedEvent(
            event=event,
            session_id=get_uni_user_id(event),
            usage=usage,
            billing=_billing_snapshot(chat),
        )
    )
