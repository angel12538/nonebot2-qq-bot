"""面板页面「系统信息 → 对话追踪」：看每一轮从收到消息到发出回复的完整过程和每步耗时。

- 页面：/qianwen-trace（独立 HTML，面板用 iframe 嵌入，见 guards/trace_page.html）
- 数据：/api/qianwen/trace/turns（列表）、/api/qianwen/trace/turn/{id}（某一轮的全部步骤）
- 显示设置：/api/qianwen/ui（「信息统计」页读 token 数分隔方式，设置在「配置管理 → webui」的 token_number_grouping）

这些路径都不在 /static 下，面板的登录中间件会拦：没登录访问接口返回 401，访问页面跳回登录页。
数据由 guards/trace.py 记录。
"""

from __future__ import annotations

from pathlib import Path

import nonebot
from fastapi import Query
from fastapi.responses import HTMLResponse, JSONResponse

from guards import trace

PAGE_PATH = Path(__file__).resolve().parent / "trace_page.html"
WEBUI_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "webui" / "config.toml"

app = nonebot.get_app()


def _ok(data) -> JSONResponse:
    return JSONResponse({"code": 200, "success": True, "message": "ok", "data": data})


@app.middleware("http")
async def _static_revalidate(request, call_next):
    """面板的前端文件名不带版本号，浏览器会按自己的估算缓存很久，改了补丁也看不到。
    加上 no-cache 后浏览器每次先问服务器有没有变，没变就回 304，不会重复下载。"""
    response = await call_next(request)
    if request.url.path.startswith("/static/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/qianwen-trace", include_in_schema=False)
async def _trace_page() -> HTMLResponse:

    return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.get("/api/qianwen/trace/turns", include_in_schema=False)
async def _trace_turns(limit: int = Query(50), before: float = Query(0), session: str = Query("")) -> JSONResponse:
    return _ok({"enabled": trace.enabled(), "turns": trace.list_turns(limit, before or None, session.strip())})


@app.get("/api/qianwen/trace/turn/{turn_id}", include_in_schema=False)
async def _trace_turn(turn_id: str) -> JSONResponse:
    data = trace.get_turn(turn_id)
    if data is None:
        return JSONResponse({"code": 404, "success": False, "message": "找不到这一轮（可能已被清理）", "data": None}, status_code=404)
    return _ok(data)


@app.get("/api/qianwen/ui", include_in_schema=False)
async def _ui_settings() -> JSONResponse:
    """面板页面用的显示设置（现在只有「信息统计」的 token 数分隔方式）。

    设置在「配置管理 → webui」，这里直接读 config/webui/config.toml：面板一保存就是新值，
    读不到或值不认识时按千分位。
    """
    import tomllib

    grouping = "thousand"
    try:
        data = tomllib.loads(WEBUI_CONFIG_PATH.read_text(encoding="utf-8"))
        if data.get("token_number_grouping") in ("thousand", "wan", "none"):
            grouping = data["token_number_grouping"]
    except Exception:
        pass
    return _ok({"number_grouping": grouping})


@app.get("/api/qianwen/cache_stats", include_in_schema=False)
async def _cache_stats() -> JSONResponse:
    """「信息统计」页的缓存命中数据，按天给，页面按所选日期范围自己合计（见 guards/cache_stats.py）。"""
    from guards import cache_stats

    try:
        return _ok({"rows": cache_stats.rows()})
    except Exception as e:
        return JSONResponse({"code": 500, "success": False, "message": f"读取缓存统计失败：{e}", "data": None}, status_code=500)




FEEDBACK_PATH = Path(__file__).resolve().parent.parent / "data" / "feedback.jsonl"

_FEEDBACK_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>她的反馈</title>
<style>
:root{--bg:#fff;--line:#e4e4e7;--text:#18181b;--muted:#71717a;--panel:#f7f7f8}
:root[data-theme=dark]{--bg:#0b0b0d;--line:#27272a;--text:#e4e4e7;--muted:#a1a1aa;--panel:#141417}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}
.wrap{padding:16px;max-width:1100px}.tip{color:var(--muted);font-size:13px;margin:0 0 12px}
.item{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0}
.meta{font-size:12px;color:var(--muted);display:flex;gap:12px;flex-wrap:wrap}
.kind{font-weight:600;color:var(--text)}.body{margin-top:4px;white-space:pre-wrap;word-break:break-word}
.empty{color:var(--muted);padding:40px;text-align:center}
</style></head><body><div class="wrap">
<p class="tip">她用 report_feedback 留下的问题和建议（最新在前）。内容是她自己写的，只作参考；完整过程可以到「对话追踪」里按时间找。</p>
<div id="list"><div class="empty">读取中…</div></div></div>
<script>
try{var t=localStorage.getItem("amrita-theme")||parent.localStorage.getItem("amrita-theme");
if(t==="dark"||(t!=="light"&&matchMedia("(prefers-color-scheme: dark)").matches))document.documentElement.dataset.theme="dark"}catch(e){}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
fetch("/api/qianwen/feedback",{cache:"no-store"}).then(function(r){return r.json()}).then(function(j){
var el=document.getElementById("list");if(!j.success){el.innerHTML='<div class="empty">'+esc(j.message||"读取失败")+'</div>';return}
if(!j.data.length){el.innerHTML='<div class="empty">她还没有留过反馈</div>';return}
el.innerHTML=j.data.map(function(f){var d=new Date(f.time*1000);return '<div class="item"><div class="meta"><span class="kind">'+esc(f.kind)+
'</span><span>'+d.toLocaleString("zh-CN",{hour12:false})+'</span><span>'+esc(f.session)+'</span></div><div class="body">'+esc(f.content)+'</div></div>'}).join("")
}).catch(function(e){document.getElementById("list").innerHTML='<div class="empty">读取失败：'+esc(e.message)+'</div>'});
</script></body></html>"""


@app.get("/qianwen-feedback", include_in_schema=False)
async def _feedback_page() -> HTMLResponse:
    return HTMLResponse(_FEEDBACK_PAGE, headers={"Cache-Control": "no-cache"})


@app.get("/api/qianwen/feedback", include_in_schema=False)
async def _feedback_list(limit: int = Query(200)) -> JSONResponse:
    import json as _json

    items = []
    try:
        for line in FEEDBACK_PATH.read_text(encoding="utf-8").splitlines():
            try:
                items.append(_json.loads(line))
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return _ok(list(reversed(items))[: max(1, min(int(limit), 1000))])


@nonebot.get_driver().on_startup
async def _register_menu() -> None:
    try:
        from amrita.plugins.webui.API import register_page

        register_page(
            "/qianwen/trace", "对话追踪", category="系统信息", icon="message-square-text", external_url="/qianwen-trace"
        )
        register_page(
            "/qianwen/feedback", "她的反馈", category="系统信息", icon="scroll-text", external_url="/qianwen-feedback"
        )
    except Exception as e:
        nonebot.logger.warning(f"[对话追踪] 面板菜单注册失败（{e}），可直接打开 /qianwen-trace 和 /qianwen-feedback")
