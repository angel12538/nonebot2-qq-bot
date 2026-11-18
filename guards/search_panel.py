"""面板「聊天管理 → 网页搜索」。"""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic

import nonebot
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from qq_tools import baidu_search, web_search

app = nonebot.get_app()
PAGE_PATH = Path(__file__).with_name("search_page.html")
_last_test = float("-inf")


class InvalidOrigin(ValueError):
    pass


def _response(data=None, message="", status=200) -> JSONResponse:
    return JSONResponse({"code": status, "success": status == 200, "message": message, "data": data},
                        status_code=status, headers={"Cache-Control": "no-store"})


def _state(settings: baidu_search.SearchSettings) -> dict:
    try:
        configured = bool(settings.key())
        key_state = ("saved" if settings.protected_key else "external") if configured else "missing"
    except baidu_search.SettingsError:
        key_state = "unreadable"
    return {"enabled": settings.enabled, "key_state": key_state, "bing_enabled": settings.bing_enabled}


async def _json_input(request: Request) -> dict:
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("origin") != expected_origin:
        raise InvalidOrigin
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise ValueError
        body.extend(chunk)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError
    return data


@app.get("/qianwen-search", include_in_schema=False)
async def _page() -> HTMLResponse:
    return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"), headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                   "connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'",
    })


@app.get("/api/qianwen/search/settings", include_in_schema=False)
async def _settings() -> JSONResponse:
    try:
        return _response(_state(baidu_search.read_settings()))
    except baidu_search.SettingsError as exc:
        return _response(message=str(exc), status=500)


@app.put("/api/qianwen/search/settings", include_in_schema=False)
async def _save(request: Request) -> JSONResponse:
    try:
        data = await _json_input(request)
        enabled, key = data.get("enabled"), data.get("api_key", "")
        if type(enabled) is not bool or not isinstance(key, str):
            raise ValueError
        bing_enabled = data.get("bing_enabled")
        if "bing_enabled" in data and type(bing_enabled) is not bool:
            raise ValueError
        state = baidu_search.save_settings(enabled, key.strip(), bing_enabled=bing_enabled)
    except InvalidOrigin:
        return _response(message="请在机器人面板内保存设置。", status=403)
    except baidu_search.SettingsError as exc:
        return _response(message=str(exc), status=400)
    except ValueError:
        return _response(message="设置格式有误，请刷新页面后重试。", status=400)
    except OSError:
        return _response(message="写入网页搜索设置失败，请检查机器人目录的写入权限。", status=500)
    return _response(_state(state), "设置已保存。")


@app.post("/api/qianwen/search/test", include_in_schema=False)
async def _test(request: Request) -> JSONResponse:
    global _last_test
    try:
        data = await _json_input(request)
        raw_query = data.get("query")
        if not isinstance(raw_query, str) or not 1 <= len(query := raw_query.strip()) <= 120:
            raise ValueError
        provider = data.get("provider", "baidu_qianfan")
        if not isinstance(provider, str) or provider not in web_search.LABELS:
            return _response(message="请选择一个搜索渠道。", status=400)
        key = baidu_search.read_settings().key() if provider == "baidu_qianfan" else ""
    except InvalidOrigin:
        return _response(message="请在机器人面板内测试搜索。", status=403)
    except baidu_search.SettingsError as exc:
        return _response(message=str(exc), status=400)
    except ValueError:
        return _response(message="请输入 1 到 120 个字符的搜索词。", status=400)
    if provider == "baidu_qianfan" and not key:
        return _response(message="请先保存千帆 API Key。", status=400)
    now = monotonic()
    if now - _last_test < 2:
        return _response(message="请稍后再测试一次。", status=429)
    _last_test = now
    diagnostics: list[str] = []
    label = web_search.LABELS[provider]
    results = await run_in_threadpool(web_search.search_provider, provider, query, 5, diagnostics, baidu_key=key)
    if results is None:
        issue = "；".join(diagnostics) if diagnostics else "请求失败"
        if provider == "baidu_qianfan" and issue.endswith(("HTTP 401", "HTTP 403")):
            message = "百度千帆拒绝了请求。请检查 API Key 和搜索服务权限。"
        elif provider == "baidu_qianfan" and issue.endswith("HTTP 429"):
            message = "百度千帆返回 429。请检查搜索额度或稍后重试。"
        else:
            message = f"{label} 测试失败（{issue}）。"
        return _response(message=message, status=502)
    return _response({"provider": provider, "results": results, "seconds": round(monotonic() - now, 2)},
                     f"{label} 返回 {len(results)} 条结果。")


@nonebot.get_driver().on_startup
async def _register_menu() -> None:
    try:
        from amrita.plugins.webui.API import register_page

        register_page("/qianwen/search", "网页搜索", category="聊天管理", icon="search", external_url="/qianwen-search")
    except Exception:
        nonebot.logger.warning("网页搜索菜单注册失败，可直接打开 /qianwen-search。")
