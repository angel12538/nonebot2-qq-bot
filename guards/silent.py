"""静默模式的管理员命令：/静默（/silent）开启，/恢复（/active）关闭。

静默模式下群友和私聊消息照常记录（进上下文、未读预览照样有），但不叫醒她；只有管理员能和她对话，
冷场也不主动开话题。判断在 amrita 聊天插件的 check_rule 和 wake_policy 里，开关存在面板设置
「配置管理 → qianwen → 静默模式」（config/qianwen/config.toml），命令和面板改的是同一个值。

管理员 = Amrita 权限系统里的管理员（.env 里配的管理员，或有 lp.admin 权限的人）；别人发命令不理。
"""

from __future__ import annotations

from nonebot import logger, on_command
from nonebot.adapters import Event
from nonebot.matcher import Matcher
from nonebot.permission import Permission

from guards import settings


async def _is_admin(event: Event) -> bool:
    try:
        from amrita.plugins.perm.API.admin import is_lp_admin

        return await is_lp_admin(event)
    except Exception:
        return False


_ADMIN = Permission(_is_admin)

_silent_on = on_command("静默", aliases={"silent"}, permission=_ADMIN, priority=5, block=True)
_silent_off = on_command("恢复", aliases={"active"}, permission=_ADMIN, priority=5, block=True)


def _set(enabled: bool) -> str:
    try:
        settings.set_value("silent", "enabled", enabled)
    except Exception as e:
        logger.warning(f"[静默模式] 写设置失败：{e!r}")
        return "改设置失败了，去面板「配置管理 → qianwen → 静默模式」里改吧"
    logger.info(f"[静默模式] 已{'开启' if enabled else '关闭'}")
    if enabled:
        return "已开启静默模式：群友的消息只记录不回复，只有管理员能和我说话。发 /恢复 关闭"
    return "已关闭静默模式，恢复正常聊天"


@_silent_on.handle()
async def _(matcher: Matcher) -> None:
    if settings.value("silent", "enabled", False):
        await matcher.finish("已经是静默模式了。发 /恢复 关闭")
    await matcher.finish(_set(True))


@_silent_off.handle()
async def _(matcher: Matcher) -> None:
    if not settings.value("silent", "enabled", False):
        await matcher.finish("现在没开静默模式")
    await matcher.finish(_set(False))
