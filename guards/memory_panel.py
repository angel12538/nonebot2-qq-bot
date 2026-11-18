"""面板页面「聊天管理 → 她的记忆」：查看、新增、修改、删除她的长期记忆。

- 页面：/qianwen-memory（独立 HTML，面板用 iframe 嵌入，见 guards/memory_page.html）
- 数据：/api/qianwen/memory/sessions、/items、/save、/delete

记忆就是 memory/facts 下每个会话一个文本文件（群 g{群号}.md，私聊 u{QQ号}.md），一行一条，行尾保存对象、参与者和待追问字段：
``- [id] [分类] 2026-11-11 04:24 内容  #标签1,标签2``
她自己通过 qq_tools 的 memory_save / memory_forget 读写同一批文件，注入提示词的是
site-packages 里 chat/utils/context.py 的补丁。这里只按行增删改，不动别的行，
也不替她清理过期的话题和念头（那是她下次写记忆时顺手做的）。

改动都不留底：修改直接覆盖那一行；删单条和删整个会话都是页面二次确认后直接删。
这些路径都不在 /static 下，面板的登录中间件会拦：没登录访问接口返回 401。
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from qq_tools import memory_store as _memory

import nonebot
from fastapi import Body, Query
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
MEM_DIR = ROOT / "memory" / "facts"
PAGE_PATH = Path(__file__).resolve().parent / "memory_page.html"

KINDS = _memory.KINDS
TTL_SECONDS = _memory.TTL_SECONDS
MAX_TEXT = _memory.MAX_TEXT
_SESSION_RE = re.compile(r"[gu]\d{3,12}")
_LINE_RE = _memory._LINE_RE

app = nonebot.get_app()


def _ok(data) -> JSONResponse:
    return JSONResponse({"code": 200, "success": True, "message": "ok", "data": data})


def _fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"code": status, "success": False, "message": message, "data": None}, status_code=status)


_parse_time = _memory.parse_time
_parse_line = _memory.parse_line
_read_lines = _memory.read_lines
_write_lines = _memory.write_lines


def _locate(lines: list[str], index, raw: str) -> int:
    """按页面拿到的「第几行 + 原文」找回这一行；行号对不上就按原文找，找不到返回 -1。"""
    if isinstance(index, int) and 0 <= index < len(lines) and lines[index] == raw:
        return index
    try:
        return lines.index(raw)
    except ValueError:
        return -1


_clean_tags = _memory.clean_tags


def _session_info(path: Path, now: float) -> dict:
    key = path.stem
    items = [_parse_line(i, raw, now) for i, raw in enumerate(_read_lines(path))]
    return {
        "key": key,
        "type": "group" if key.startswith("g") else "private",
        "id": key[1:],
        "count": len(items),
        "expired": sum(1 for it in items if it["expired"]),
        "updated": path.stat().st_mtime,
    }


@app.get("/qianwen-memory", include_in_schema=False)
async def _memory_page() -> HTMLResponse:

    return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.get("/api/qianwen/memory/sessions", include_in_schema=False)
async def _memory_sessions() -> JSONResponse:
    now = time.time()
    files = sorted(MEM_DIR.glob("*.md")) if MEM_DIR.is_dir() else []
    sessions = [_session_info(p, now) for p in files if _SESSION_RE.fullmatch(p.stem)]
    sessions.sort(key=lambda s: s["updated"], reverse=True)
    return _ok({"sessions": sessions, "kinds": list(KINDS), "ttl_hours": {k: v // 3600 for k, v in TTL_SECONDS.items()}})


@app.get("/api/qianwen/memory/items", include_in_schema=False)
async def _memory_items(session: str = Query("")) -> JSONResponse:
    if not _SESSION_RE.fullmatch(session):
        return _fail("会话写法不对：群是 g 加群号，私聊是 u 加 QQ 号")
    now = time.time()
    path = MEM_DIR / f"{session}.md"
    items = [_parse_line(i, raw, now) for i, raw in enumerate(_read_lines(path))]
    return _ok({"session": session, "exists": path.exists(), "items": items})


@app.post("/api/qianwen/memory/save", include_in_schema=False)
async def _memory_save(body: dict = Body(...)) -> JSONResponse:
    """新增或修改一条。带 raw 是修改（按原文找到那一行替换），不带是新增（加在最后）。"""
    session = str(body.get("session") or "")
    if not _SESSION_RE.fullmatch(session):
        return _fail("会话写法不对：群是 g 加群号，私聊是 u 加 QQ 号")
    try:
        text = _memory.clean_text(body.get("text"))
        if not _memory.normalized(text):
            return _fail("内容不能为空")
        kind = str(body.get("kind") or "")
        if kind not in KINDS:
            return _fail("分类只能是 话题 / 念头 / 印象 / 事实")
        when = str(body.get("time") or "").strip().replace("T", " ")
        if when and _parse_time(when) is None:
            return _fail("时间写法不对，应为 2026-11-13 08:30 或 2026-11-13")
        when = when or datetime.now().strftime("%Y-%m-%d %H:%M")
        tags = _clean_tags(body.get("tags"))
        old_raw = body.get("raw")
        path = MEM_DIR / f"{session}.md"
        with _memory.memory_lock(path):
            lines = _read_lines(path)
            if old_raw is None:
                updated, result = _memory.upsert(lines, text=text, kind=kind, tags=tags, when=when,
                                                 subject=str(body.get("subject") or ""),
                                                 participants=body.get("participants"), pending_question=body.get("pending_question"))
                item, action = result["item"], result["action"]
            else:
                pos = _locate(lines, body.get("index"), str(old_raw))
                if pos < 0:
                    return _fail("这条记忆刚才被改过或删掉了，请刷新后再试", 409)
                item = _parse_line(pos, lines[pos], time.time())
                item.update(id=item["id"] or uuid.uuid4().hex[:8], kind=kind, text=text, tags=tags, time=when)
                item["subject"] = _memory.clean_text(body.get("subject", item["subject"]), 80)
                item["participants"] = _memory.clean_participants(body.get("participants", item["participants"]))
                item["pending_question"] = _memory.clean_text(body.get("pending_question", item["pending_question"]), 200)
                if kind not in ("话题", "念头"):
                    item["participants"], item["pending_question"] = [], ""
                if kind == "印象" and not item["subject"]:
                    return _fail("印象需要填写对象，优先使用已知 QQ 号")
                updated = lines.copy()
                updated[pos] = _memory.serialize(item)
                item = _parse_line(pos, updated[pos], time.time())
                action = "更新"
            if updated != lines:
                _write_lines(path, updated)
        return _ok({"item": item, "total": len(updated), "action": action})
    except (ValueError, OSError, TimeoutError) as exc:
        return _fail(str(exc))


@app.post("/api/qianwen/memory/delete", include_in_schema=False)
async def _memory_delete(body: dict = Body(...)) -> JSONResponse:
    session = str(body.get("session") or "")
    if not _SESSION_RE.fullmatch(session):
        return _fail("会话写法不对：群是 g 加群号，私聊是 u 加 QQ 号")
    raw = body.get("raw")
    if not isinstance(raw, str) or not raw:
        return _fail("没说要删哪一条")
    path = MEM_DIR / f"{session}.md"
    with _memory.memory_lock(path):
        lines = _read_lines(path)
        pos = _locate(lines, body.get("index"), raw)
        if pos < 0:
            return _fail("这条记忆刚才被改过或删掉了，请刷新后再试", 409)
        del lines[pos]
        _write_lines(path, lines)
    return _ok({"total": len(lines)})


@app.post("/api/qianwen/memory/clear", include_in_schema=False)
async def _memory_clear(body: dict = Body(...)) -> JSONResponse:
    """删掉一个会话的全部记忆（连文件一起）。直接删、不留底，页面上有二次确认弹窗把关。

    页面要把确认弹窗里显示的条数带过来（expect）：弹窗开着的时候她又写了新的，
    条数对不上就不删，免得删掉用户没看到的内容。
    """
    session = str(body.get("session") or "")
    if not _SESSION_RE.fullmatch(session):
        return _fail("会话写法不对：群是 g 加群号，私聊是 u 加 QQ 号")
    path = MEM_DIR / f"{session}.md"
    with _memory.memory_lock(path):
        if not path.exists():
            return _fail("这个会话已经没有记忆了，请刷新", 409)
        lines = _read_lines(path)
        if body.get("expect") != len(lines):
            return _fail(f"记忆条数变了（现在 {len(lines)} 条），请刷新确认后再删", 409)
        path.unlink()
    return _ok({"removed": len(lines)})


@nonebot.get_driver().on_startup
async def _register_menu() -> None:
    try:
        from amrita.plugins.webui.API import register_page

        register_page("/qianwen/memory", "她的记忆", category="聊天管理", icon="history", external_url="/qianwen-memory")
    except Exception as e:
        nonebot.logger.warning(f"[她的记忆] 面板菜单注册失败（{e}），可直接打开 /qianwen-memory")
