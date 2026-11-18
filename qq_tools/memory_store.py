"""会话记忆的字段、分类更新和原子读写，供工具、面板与提示词共用。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

KINDS = ("话题", "念头", "印象", "事实")
TTL_SECONDS = {"话题": 48 * 3600, "念头": 7 * 86400}
MAX_TEXT = 500
MAX_ENTRIES = 300
_FIELDS = "  【记忆字段】"
_LINE_RE = re.compile(r"^- \[(\w+)\]\s*\[([^\]\s]+)\]\s*(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)\s+(.*)$")
_TAGS_RE = re.compile(r"\s{2,}#(\S.*)$")
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def clean_text(value, limit: int = MAX_TEXT) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        raise ValueError(f"内容太长，一项最多 {limit} 字")
    return text


def normalized(value: str) -> str:

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold()).rstrip("。.!！?？；;")


def clean_tags(value) -> list[str]:
    parts = value if isinstance(value, list) else re.split(r"[,，、\s]+", str(value or ""))
    out = []
    for part in parts:
        tag = clean_text(part, 80).lstrip("#").strip()
        if tag and tag not in out:
            out.append(tag)
    return out[:10]


def clean_participants(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("参与者必须是列表")
    if len(value) > 10:
        raise ValueError("参与者最多 10 位")
    return list(dict.fromkeys(text for v in value if (text := clean_text(v, 80))))


def parse_time(raw: str) -> float | None:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).timestamp()
        except ValueError:
            continue
    return None


def parse_line(index: int, raw: str, now: float | None = None) -> dict:
    item = {"index": index, "raw": raw, "parsed": False, "id": "", "kind": "事实", "time": "",
            "text": raw.lstrip("- ").strip(), "tags": [], "subject": "", "participants": [],
            "pending_question": "", "expired": False, "expires_at": None}
    match = _LINE_RE.match(raw)
    if not match:
        return item
    rest, fields = match.group(4), {}
    if _FIELDS in rest:
        body, _, suffix = rest.rpartition(_FIELDS)
        try:
            decoded = json.loads(suffix)
            if isinstance(decoded, dict):
                fields, rest = decoded, body
        except ValueError:
            pass
    tags = _TAGS_RE.search(rest)
    if tags:
        item["tags"] = [tag.strip() for tag in tags.group(1).split(",") if tag.strip()]
        rest = rest[:tags.start()]
    if isinstance(fields.get("text"), str):
        rest = fields["text"]
    if isinstance(fields.get("tags"), list):
        item["tags"] = [str(tag) for tag in fields["tags"]]
    kind, stamp = match.group(2), parse_time(match.group(3))
    ttl = TTL_SECONDS.get(kind)
    expires_at = stamp + ttl if ttl and stamp is not None else None
    item.update(parsed=True, id=match.group(1), kind=kind, time=match.group(3), text=rest.strip(),
                expires_at=expires_at, expired=expires_at is not None and (time.time() if now is None else now) > expires_at,
                subject=str(fields.get("subject") or ""),
                participants=[str(p) for p in fields.get("participants", [])] if isinstance(fields.get("participants"), list) else [],
                pending_question=str(fields.get("pending_question") or ""))
    return item


def serialize(item: dict) -> str:

    fields = {key: item[key] for key in ("subject", "participants", "pending_question") if item.get(key)}
    fields["text"] = item["text"]
    fields["tags"] = item.get("tags", [])
    body = item["text"]
    tags = item.get("tags") or []
    return (f"- [{item['id']}] [{item['kind']}] {item['time']} {body}"
            + (f"  #{','.join(tags)}" if tags else "")
            + _FIELDS + json.dumps(fields, ensure_ascii=False, separators=(",", ":")))


def read_lines(path: Path) -> list[str]:
    try:
        return [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def memory_lock(path: Path):
    """面板与 MCP 进程使用同一个 Windows 互斥量，覆盖完整的读、改、写。"""
    key = os.path.normcase(str(path.resolve()))
    with _locks_guard:
        local = _locks.setdefault(key, threading.RLock())
    with local:
        handle, api = None, None
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                api = ctypes.WinDLL("kernel32", use_last_error=True)
                api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
                api.CreateMutexW.restype = wintypes.HANDLE
                api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
                api.WaitForSingleObject.restype = wintypes.DWORD
                api.ReleaseMutex.argtypes = (wintypes.HANDLE,)
                api.CloseHandle.argtypes = (wintypes.HANDLE,)
                name = "Local\\NoneBot2Memory-" + hashlib.sha256(key.encode()).hexdigest()
                handle = api.CreateMutexW(None, False, name)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                status = api.WaitForSingleObject(handle, 10000)
                if status not in (0, 0x80):
                    api.CloseHandle(handle)
                    handle = None
                    raise TimeoutError("记忆正在被其他进程修改，请稍后重试")
            yield
        finally:
            if handle is not None:
                api.ReleaseMutex(handle)
                api.CloseHandle(handle)


def _merge_text(old: str, new: str) -> str:
    parts, seen = [], set()
    for text in re.split(r"[；;]+", old) + re.split(r"[；;]+", new):
        text = text.strip()
        key = normalized(text)
        if key and key not in seen:
            parts.append(text)
            seen.add(key)
    return clean_text("；".join(parts))


def upsert(lines: list[str], *, text: str, tags="", kind: str = "", subject: str = "",
           participants: list[str] | None = None, pending_question: str | None = None,
           memory_id: str = "", when: str = "") -> tuple[list[str], dict]:
    text, subject = clean_text(text), clean_text(subject, 80)
    if not normalized(text):
        raise ValueError("内容不能为空")
    if kind and kind not in KINDS:
        raise ValueError("分类只能是 话题 / 念头 / 印象 / 事实")
    people = clean_participants(participants)
    question = None if pending_question is None else clean_text(pending_question, 200)
    tags = clean_tags(tags)
    when = when or datetime.now().strftime("%Y-%m-%d %H:%M")
    if parse_time(when) is None:
        raise ValueError("记忆时间格式错误")
    records = [parse_line(i, line) for i, line in enumerate(lines)]
    old = None
    if memory_id:
        old = next((item for item in records if item["id"] == memory_id), None)
        if old is None:
            raise ValueError("记忆 ID 不存在，请重新查询后再修改")
    kind = kind or (old["kind"] if old else "事实")
    if kind == "印象" and not (subject or (old and old["subject"])):
        raise ValueError("印象需要 subject 指定群友，优先使用消息中已知的 QQ 号")
    if old is None:
        for item in reversed(records):
            if not item["parsed"] or item["expired"] or item["kind"] != kind:
                continue
            same_subject = normalized(item["subject"]) == normalized(subject)
            same_text = normalized(item["text"]) == normalized(text)
            if (same_subject and same_text) or (subject and same_subject and kind in ("印象", "话题", "念头")):
                old = item
                break
    item = {"id": old["id"] if old else uuid.uuid4().hex[:8], "kind": kind, "time": when,
            "text": text, "tags": tags, "subject": subject,
            "participants": people if kind in ("话题", "念头") else [],
            "pending_question": (question or "") if kind in ("话题", "念头") else ""}
    action = "新增"
    if old:
        item["subject"] = subject or old["subject"]
        item["tags"] = clean_tags(old["tags"] + tags)
        if kind in ("话题", "念头"):
            item["participants"] = (old["participants"] if participants is None else
                                    list(dict.fromkeys(old["participants"] + people))[:10] if people else [])
            item["pending_question"] = old["pending_question"] if question is None else question
        if not memory_id and kind == "印象":
            item["text"] = _merge_text(old["text"], text)
        same = all(item[k] == old[k] for k in ("kind", "text", "tags", "subject", "participants", "pending_question"))
        action = "重复" if same else "合并" if kind == "印象" and not memory_id else "更新"
        if same and kind not in TTL_SECONDS:
            return lines, {"action": action, "item": old, "total": len(lines)}
    line = serialize(item)
    updated = [raw for i, raw in enumerate(lines) if old is None or i != old["index"]] + [line]
    updated = updated[-MAX_ENTRIES:]
    return updated, {"action": action, "item": parse_line(len(updated) - 1, line), "total": len(updated)}


def public_item(item: dict) -> dict:
    return {k: item[k] for k in ("id", "kind", "time", "text", "tags", "subject", "participants", "pending_question")
            if item.get(k)}


def format_item(item: dict) -> str:
    text = item["text"]
    if item.get("subject"):
        text = f"{item['subject']}：{text}"
    details = []
    if item.get("participants"):
        details.append("参与：" + "、".join(item["participants"]))
    if item.get("pending_question"):
        details.append("待追问：" + item["pending_question"])
    return f"- [{item['id']}] {item['time']} {text}" + ("（" + "；".join(details) + "）" if details else "")


def prompt_block(lines: list[str], max_chars: int = 1500) -> str:
    groups = {kind: [] for kind in KINDS}
    used = 0
    for i in range(len(lines) - 1, -1, -1):
        item = parse_line(i, lines[i])
        if item["expired"]:
            continue
        kind = item["kind"] if item["kind"] in KINDS else "事实"
        group = groups[kind]
        rendered = format_item(item)
        cost = len(rendered) + 1 + (len(kind) + 4 if not group else 0)
        if len(group) < 10 and used + cost <= max_chars:
            group.append(rendered)
            used += cost
    body = "\n".join(f"## {kind}\n" + "\n".join(reversed(rows)) for kind, rows in groups.items() if rows)
    return f"【长期记忆】\n{body}\n【长期记忆结束】\n" if body else ""
