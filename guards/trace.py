"""对话追踪：把每一轮从收到消息到发出回复的每一步记下来，供面板「系统信息 → 对话追踪」查看。

一轮（turn）= 一次叫醒她的聊天处理。每一轮记这些步骤（step），每步带开始时间和耗时：
- 收到消息：叫醒她的消息、叫醒原因、等对方说完（防抖）用的时间；
- 构建提示词：系统提示、这一轮交给模型的用户输入（含未读预览）；
- 开始运行：排队拿会话锁用的时间；
- 网关请求：发给网关的消息和工具列表、网关返回的文字 / 思考 / 工具调用 / 用量、首字耗时和总耗时；
- 工具：工具名、参数、返回、耗时、报错；
- 发送：实际发出去的每一条，以及被拦截、限速、〔不回〕的情况；
- 结束：总耗时、是否出错。

记录点散在 Amrita 源码里（libchat / strategy / 聊天入口 / 出站钩子），都只调用这里的函数，
这里任何异常都吞掉，不影响聊天。数据存 data/trace.db，只保留最近若干轮；
长文本截断，内嵌的 base64 图片换成占位符。面板「qianwen → 对话追踪」可关、可调保留条数和截断长度。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trace.db"

_cur: ContextVar[dict | None] = ContextVar("qianwen_trace_turn", default=None)
_lock = threading.Lock()
_conn: list[sqlite3.Connection | None] = [None]
_last_prune = [0.0]




def _cfg(key: str, default):
    try:
        from guards import settings

        return settings.value("trace", key, default)
    except Exception:
        return default


def enabled() -> bool:
    return bool(_cfg("enabled", True))


def _db() -> sqlite3.Connection:
    if _conn[0] is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS turns ("
            "id TEXT PRIMARY KEY, session TEXT, started REAL, ended REAL, duration_ms INTEGER,"
            "status TEXT, trigger TEXT, reason TEXT, reply TEXT, llm_calls INTEGER DEFAULT 0,"
            "tool_calls INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0, error TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS steps ("
            "turn_id TEXT, seq INTEGER, kind TEXT, title TEXT, started REAL, duration_ms INTEGER,"
            "data TEXT, PRIMARY KEY (turn_id, seq))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_started ON turns(started)")


        have = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
        for col in ("cache_prompt", "cache_hit"):
            if col not in have:
                conn.execute(f"ALTER TABLE turns ADD COLUMN {col} INTEGER")
        conn.commit()
        _conn[0] = conn
    return _conn[0]


def _exec(sql: str, args: tuple = ()) -> None:
    try:
        with _lock:
            conn = _db()
            conn.execute(sql, args)
            conn.commit()
    except Exception:
        pass


def _prune() -> None:
    """只保留最近 N 轮（每分钟最多清理一次）。"""
    now = time.time()
    if now - _last_prune[0] < 60:
        return
    _last_prune[0] = now
    keep = max(20, int(_cfg("keep_turns", 300) or 300))
    try:
        with _lock:
            conn = _db()
            row = conn.execute("SELECT started FROM turns ORDER BY started DESC LIMIT 1 OFFSET ?", (keep,)).fetchone()
            if row:
                conn.execute("DELETE FROM steps WHERE turn_id IN (SELECT id FROM turns WHERE started <= ?)", (row[0],))
                conn.execute("DELETE FROM turns WHERE started <= ?", (row[0],))
                conn.commit()
    except Exception:
        pass




_DATA_URI = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=_-]{64,}")
_LONG_B64 = re.compile(r"[A-Za-z0-9+/]{2000,}={0,2}")


def _mask(text: str) -> str:
    text = _DATA_URI.sub(lambda m: f"[图片 约 {len(m.group(0)) * 3 // 4 // 1024} KB]", text)
    return _LONG_B64.sub(lambda m: f"[二进制数据 约 {len(m.group(0)) * 3 // 4 // 1024} KB]", text)


def clean(obj: Any, max_chars: int | None = None, _depth: int = 0) -> Any:
    """转成能存 JSON 的结构：pydantic 对象展开，长字符串截断，图片数据换成占位符。"""
    limit = max_chars or max(1000, int(_cfg("max_chars", 20000) or 20000))
    if _depth > 12:
        return "…（层级太深）"
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        s = _mask(obj)
        return s if len(s) <= limit else s[:limit] + f"…（共 {len(s)} 字，已截断）"
    if isinstance(obj, (bytes, bytearray)):
        return f"[二进制 {len(obj)} 字节]"
    if isinstance(obj, dict):
        return {str(k): clean(v, limit, _depth + 1) for k, v in list(obj.items())[:200]}
    if isinstance(obj, (list, tuple, set)):
        items = list(obj)
        out = [clean(v, limit, _depth + 1) for v in items[:300]]
        if len(items) > 300:
            out.append(f"…（共 {len(items)} 项，只记前 300 项）")
        return out
    for attr in ("model_dump",):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return clean(fn(exclude_none=True), limit, _depth + 1)
            except Exception:
                try:
                    return clean(fn(), limit, _depth + 1)
                except Exception:
                    pass
    if hasattr(obj, "__dict__"):
        try:
            return clean({k: v for k, v in vars(obj).items() if not k.startswith("_")}, limit, _depth + 1)
        except Exception:
            pass
    return clean(str(obj), limit, _depth + 1)


def _dumps(data: Any) -> str:
    try:
        return json.dumps(clean(data), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"记录失败": repr(e)}, ensure_ascii=False)




def begin(session: str, trigger: str = "", reason: str = "", steps: list[tuple] | None = None) -> dict | None:
    """开始记录一轮。steps 是开始前已经发生的步骤：[(类型, 标题, 开始时间, 结束时间, 数据)]。"""
    try:
        if not enabled():
            return None
        now = time.time()
        first = min((s[2] for s in steps or [] if s[2]), default=now)
        turn = {
            "id": uuid.uuid4().hex[:12], "session": session, "t0": first, "seq": 0,
            "llm": 0, "tool": 0, "tokens": 0, "reply": [],
            "cache_prompt": 0, "cache_hit": 0, "cache_reported": False,
        }
        _exec(
            "INSERT INTO turns (id, session, started, status, trigger, reason) VALUES (?, ?, ?, ?, ?, ?)",
            (turn["id"], session, first, "进行中", str(trigger)[:200], reason),
        )
        turn["token"] = _cur.set(turn)
        for kind, title, t_start, t_end, data in steps or []:
            step(kind, title, data, started=t_start, duration_ms=int(((t_end or now) - (t_start or now)) * 1000), turn=turn)
        _prune()
        return turn
    except Exception:
        return None


def current() -> dict | None:
    return _cur.get()


def step(
    kind: str, title: str, data: Any = None, *, started: float | None = None,
    duration_ms: int | None = None, turn: dict | None = None,
) -> None:
    try:
        turn = turn or _cur.get()
        if not turn:
            return
        turn["seq"] += 1
        _exec(
            "INSERT OR REPLACE INTO steps (turn_id, seq, kind, title, started, duration_ms, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (turn["id"], turn["seq"], kind, str(title)[:200], started or time.time(), duration_ms, _dumps(data)),
        )
    except Exception:
        pass


def mark(kind: str, title: str, since: float | None = None, data: Any = None) -> None:
    """记一个「从 since 到现在」的步骤（不传 since 就是从上一步算起不了，只记时间点）。"""
    now = time.time()
    step(kind, title, data, started=since or now, duration_ms=int((now - since) * 1000) if since else None)


def end(error: BaseException | None = None) -> None:
    try:
        turn = _cur.get()
        if not turn:
            return
        now = time.time()
        status = "出错" if error is not None else "完成"
        reply = " / ".join(turn["reply"])[:300]
        if error is not None:
            step("结束", f"出错：{type(error).__name__}", {"错误": repr(error)}, started=now, duration_ms=0, turn=turn)
        _exec(
            "UPDATE turns SET ended=?, duration_ms=?, status=?, reply=?, llm_calls=?, tool_calls=?, tokens=?, error=?,"
            " cache_prompt=?, cache_hit=? WHERE id=?",
            (now, int((now - turn["t0"]) * 1000), status, reply, turn["llm"], turn["tool"], turn["tokens"],
             repr(error)[:500] if error is not None else None,
             turn["cache_prompt"] if turn["cache_reported"] else None,
             turn["cache_hit"] if turn["cache_reported"] else None, turn["id"]),
        )
        try:
            _cur.reset(turn["token"])
        except Exception:
            _cur.set(None)
    except Exception:
        pass




def llm_start(kind: str, preset: Any, messages: Any, tools: Any = None) -> dict | None:
    if not _cur.get():
        return None
    try:
        tool_names = [getattr(getattr(t, "function", t), "name", None) or (t.get("function", {}).get("name") if isinstance(t, dict) else str(t)) for t in tools or []]
    except Exception:
        tool_names = []
    return {
        "kind": kind, "t0": time.time(), "first": None, "text": [], "final": None,
        "preset": getattr(preset, "name", ""), "model": getattr(preset, "model", ""),
        "messages": messages, "tools": tool_names,
    }


def llm_chunk(h: dict | None, chunk: Any) -> None:
    if not h:
        return
    if h["first"] is None:
        h["first"] = time.time()
    if isinstance(chunk, str):
        h["text"].append(chunk)
    elif hasattr(chunk, "usage") and hasattr(chunk, "metadata"):
        h["final"] = chunk


def llm_end(h: dict | None, resp: Any = None, error: BaseException | None = None) -> None:
    try:
        turn = _cur.get()
        if not h or not turn:
            return
        now = time.time()
        resp = resp if resp is not None else h["final"]
        content = getattr(resp, "content", None)
        if not content and h["text"]:
            content = "".join(h["text"])
        reasoning = getattr(resp, "reasoning_content", None)
        calls = []
        for tc in getattr(resp, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            args = getattr(fn, "arguments", "")
            try:
                args = json.loads(args)
            except Exception:
                pass
            calls.append({"工具": getattr(fn, "name", ""), "参数": args})
        usage = getattr(resp, "usage", None)
        meta = getattr(resp, "metadata", None)
        model = getattr(meta, "model", None) or h["model"]
        if model == "__NOT_GIVEN__":
            model = h["model"]
        total = getattr(usage, "total_tokens", None)
        if isinstance(total, int):
            turn["tokens"] += total
        hit = getattr(usage, "cache_hit", None)
        prompt = getattr(usage, "prompt_tokens", None)
        if isinstance(hit, int) and isinstance(prompt, int):
            turn["cache_prompt"] += prompt
            turn["cache_hit"] += hit
            turn["cache_reported"] = True
        turn["llm"] += 1
        msgs = h["messages"]
        data = {
            "预设": h["preset"], "模型": model,
            "耗时": {
                "首字（毫秒）": int((h["first"] - h["t0"]) * 1000) if h["first"] else None,
                "总共（毫秒）": int((now - h["t0"]) * 1000),
            },
            "请求": {"消息": msgs, "工具": h["tools"]},
            "返回": {
                "文字": content, "思考": reasoning, "工具调用": calls,
                "结束原因": getattr(meta, "stop_reason", None), "用量": usage,
            },
        }
        request_options = h.get("request_options") or {}
        data["请求"].update(request_options)
        output_format = request_options.get("response_format") or {}
        if output_format.get("json_schema", {}).get("name") == "tool_judgment_decision" and error is None:
            if calls:
                data["返回"]["工具判定"] = {"启用工具": True, "工具": [call["工具"] for call in calls]}
            else:
                try:
                    decision = json.loads(content or "")
                except (ValueError, TypeError):
                    decision = None
                valid = isinstance(decision, dict) and set(decision) == {"启用工具", "工具"} and decision["启用工具"] is False and decision["工具"] == []
                data["返回"]["工具判定"] = decision if valid else {"格式有效": False, "说明": "网关未遵守工具判断输出约束，请核对原始返回"}
        if error is not None:
            data["错误"] = repr(error)
        n = len(msgs) if isinstance(msgs, (list, tuple)) else "?"
        title = f"{h['kind']} · {model} · {n} 条消息"
        if calls:
            title += " → 调用 " + "、".join(c["工具"] for c in calls)
        elif error is not None:
            title += f" → 出错 {type(error).__name__}"
        step("网关", title, data, started=h["t0"], duration_ms=int((now - h["t0"]) * 1000), turn=turn)
    except Exception:
        pass


async def trace_call(kind: str, preset: Any, messages: Any, tools: Any, coro, *, request_options: dict | None = None):
    """包一次非流式请求；保留真实响应以及本次请求的输出约束。"""
    h = llm_start(kind, preset, messages, tools)
    if h is not None and request_options:
        h["request_options"] = request_options
    try:
        resp = await coro
    except BaseException as e:
        llm_end(h, error=e)
        raise
    llm_end(h, resp)
    return resp


async def trace_stream(kind: str, preset: Any, messages: Any, tools: Any, agen):
    """包一次流式请求：记首字时间、拼出全文、拿到结尾的用量。"""
    h = llm_start(kind, preset, messages, tools)
    try:
        async for chunk in agen:
            llm_chunk(h, chunk)
            yield chunk
    except BaseException as e:
        llm_end(h, error=e)
        raise
    llm_end(h)




def tool_start(name: str, arguments: Any) -> dict | None:
    if not _cur.get():
        return None
    return {"name": name, "args": arguments, "t0": time.time()}


def tool_end(h: dict | None, result: Any = None, error: BaseException | None = None) -> None:
    try:
        turn = _cur.get()
        if not h or not turn:
            return
        now = time.time()
        turn["tool"] += 1
        args = h["args"]
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                pass
        data = {"参数": args, "返回": result}
        if error is not None:
            data["错误"] = repr(error)
        title = h["name"] + (f" → 出错 {type(error).__name__}" if error is not None else "")
        step("工具", title, data, started=h["t0"], duration_ms=int((now - h["t0"]) * 1000), turn=turn)
    except Exception:
        pass




def sent(api: str, target: str, text: str, outcome: str, started: float | None = None) -> None:
    try:
        turn = _cur.get()
        if not turn:
            return
        now = time.time()
        if outcome == "已发送" and text:
            turn["reply"].append(text[:100])
        preview = (text or "").replace("\n", " ")
        step("发送", f"{outcome}：{preview[:40]}", {"接口": api, "目标": target, "内容": text, "结果": outcome},
             started=started or now, duration_ms=int((now - started) * 1000) if started else None, turn=turn)
    except Exception:
        pass




def list_turns(limit: int = 50, before: float | None = None, session: str = "") -> list[dict]:
    sql = ("SELECT id, session, started, duration_ms, status, trigger, reason, reply, llm_calls, tool_calls, tokens, error, cache_prompt, cache_hit "
           "FROM turns WHERE 1=1")
    args: list = []
    if before:
        sql += " AND started < ?"
        args.append(before)
    if session:
        sql += " AND session LIKE ?"
        args.append(f"%{session}%")
    sql += " ORDER BY started DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    cols = ["id", "session", "started", "duration_ms", "status", "trigger", "reason", "reply", "llm_calls", "tool_calls", "tokens", "error", "cache_prompt", "cache_hit"]
    with _lock:
        rows = _db().execute(sql, args).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def get_turn(turn_id: str) -> dict | None:
    with _lock:
        conn = _db()
        row = conn.execute(
            "SELECT id, session, started, ended, duration_ms, status, trigger, reason, reply, llm_calls, tool_calls, tokens, error, cache_prompt, cache_hit "
            "FROM turns WHERE id = ?", (turn_id,),
        ).fetchone()
        if not row:
            return None
        steps = conn.execute(
            "SELECT seq, kind, title, started, duration_ms, data FROM steps WHERE turn_id = ? ORDER BY seq", (turn_id,)
        ).fetchall()
    cols = ["id", "session", "started", "ended", "duration_ms", "status", "trigger", "reason", "reply", "llm_calls", "tool_calls", "tokens", "error", "cache_prompt", "cache_hit"]
    out = dict(zip(cols, row))
    out["steps"] = []
    for seq, kind, title, started, dur, data in steps:
        try:
            parsed = json.loads(data) if data else None
        except Exception:
            parsed = data
        out["steps"].append({"seq": seq, "kind": kind, "title": title, "started": started, "duration_ms": dur, "data": parsed})
    return out
