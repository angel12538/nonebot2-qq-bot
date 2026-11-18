"""白名单 + 机器人扩展设置的面板注册。

1. 启动时把 guards/settings.py 里的 QianwenSettings 注册到 AmritaBot 面板
   （「配置管理 → qianwen」），面板保存后写回 config/qianwen/config.toml；
2. 在所有插件之前拦截（event_preprocessor）：只处理设置里「白名单」列出的群聊和私聊，
   名单外的消息和通知不记录、不进上下文、不回复。好友/加群请求和心跳等元事件不受影响。

名单改动后下一条消息自动生效；设置文件读不了或格式不对时一律拒绝——宁可不回，也不越界。

3. 机器人管理员：以面板「qianwen → 管理员」为准，保存后立即生效。做法是原地改写
   driver.config.superusers 这个集合——权限系统（is_lp_admin 的 ENV_ADMINS）、NoneBot 的
   SUPERUSER、出站拦截通知拿的都是同一个集合对象，所以改它就全部生效，不用重启。
   .env 的 SUPERUSERS 只在设置文件里还没有这一项时用来填初始值，之后改它不再起作用。
"""

from __future__ import annotations

from nonebot import get_driver, logger
from nonebot.adapters import Event
from nonebot.exception import IgnoredException
from nonebot.message import event_preprocessor

from guards import settings

_logged: set[str] = set()
_last_sizes: list = [None]


def _sync_admins() -> None:
    """把面板里的管理员名单同步到运行中的 superusers 集合；设置文件读不了时保持现状。"""
    try:
        conf, ok = settings.load()

        if not ok or not settings.has_section("admin"):
            return
        wanted = set(conf.admin.superusers)
        current = get_driver().config.superusers
        if current != wanted:
            current.clear()
            current.update(wanted)
            logger.info(f"[管理员] 已更新机器人管理员：{', '.join(sorted(wanted)) or '（无）'}")
    except Exception as e:
        logger.warning(f"[管理员] 同步管理员名单失败：{e}")


async def _on_reload(_owner: str, _path) -> None:
    _sync_admins()


@get_driver().on_startup
async def _register_settings() -> None:
    """注册到面板。注册失败不影响白名单本身（白名单直接读文件）。"""
    try:
        from nonebot_plugin_uniconf import UniConfigManager


        try:
            import tomllib

            raw = tomllib.loads(settings.CONFIG_PATH.read_text(encoding="utf-8")) if settings.CONFIG_PATH.exists() else {}
            if "admin" not in raw:
                settings.set_value("admin", "superusers", sorted(get_driver().config.superusers))
                logger.info("[管理员] 已用 .env 的 SUPERUSERS 填好面板里的管理员初始值")
        except Exception as e:
            logger.warning(f"[管理员] 填初始值失败（{e}），管理员仍按 .env 生效")
        await UniConfigManager().add_config(settings.QianwenSettings, owner_name=settings.OWNER, on_reload=_on_reload)
        logger.info("[机器人设置] 已注册到面板：配置管理 → qianwen")
    except Exception as e:
        logger.warning(f"[机器人设置] 注册到面板失败（{e}），设置仍可直接改 {settings.CONFIG_PATH}")


@event_preprocessor
async def _whitelist(event: Event) -> None:
    if getattr(event, "post_type", "") not in ("message", "notice", "message_sent"):
        return

    _sync_admins()
    names = settings.whitelist()
    if names is None:
        if "broken" not in _logged:
            _logged.add("broken")
            logger.warning(f"[白名单] 读不了 {settings.CONFIG_PATH}，所有群聊和私聊都不会处理")
        raise IgnoredException("白名单不可用")
    _logged.discard("broken")
    groups, private = names
    sizes = (len(groups), len(private))
    if sizes != _last_sizes[0]:
        _last_sizes[0] = sizes
        logger.info(f"[白名单] 当前：群 {sizes[0]} 个，私聊 {sizes[1]} 个")
    group_id = getattr(event, "group_id", None)
    user_id = getattr(event, "user_id", None)
    if group_id:
        allowed, key = int(group_id) in groups, f"群 {group_id}"
    else:
        allowed, key = user_id is not None and int(user_id) in private, f"私聊 {user_id}"
    if allowed:
        return

    if key not in _logged:
        _logged.add(key)
        logger.info(f"[白名单] 忽略{key}的消息（不在面板「qianwen → 白名单」里）")
    raise IgnoredException("不在白名单")
