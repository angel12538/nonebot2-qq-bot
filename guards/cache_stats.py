"""按天累计网关上报的缓存命中，给面板「信息统计」算缓存命中率。

数据在 data/cache_stats.db，一天一行：
- prompt_tokens：上报了缓存信息的那些请求的输入 tokens 合计（分母）
- cached_tokens：其中命中缓存的 tokens 合计（分子）
- requests：上报了缓存信息的请求次数

没上报缓存信息的请求（网关没给 cached_tokens）不计入，免得把命中率拉低成假的 0%。
写入方是 site-packages 里 chat/handlers/chat/usage.py 的补丁（每轮结束时调用 add）；
读取方是 guards/trace_panel.py 的 /api/qianwen/cache_stats。框架自己的统计表不动。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cache_stats.db"
_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily (date TEXT PRIMARY KEY, prompt_tokens INTEGER NOT NULL DEFAULT 0,"
        " cached_tokens INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0)"
    )
    return conn


def add(date: str, prompt_tokens: int, cached_tokens: int, requests: int) -> None:
    """把一轮里上报了缓存信息的请求累加到当天。"""
    if requests <= 0:
        return
    with _lock:
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO daily (date, prompt_tokens, cached_tokens, requests) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(date) DO UPDATE SET prompt_tokens = prompt_tokens + excluded.prompt_tokens,"
                " cached_tokens = cached_tokens + excluded.cached_tokens, requests = requests + excluded.requests",
                (date, max(0, int(prompt_tokens)), max(0, int(cached_tokens)), int(requests)),
            )
            conn.commit()
        finally:
            conn.close()


def rows() -> list[dict]:
    if not DB_PATH.exists():
        return []
    with _lock:
        conn = _db()
        try:
            cur = conn.execute("SELECT date, prompt_tokens, cached_tokens, requests FROM daily ORDER BY date")
            return [dict(zip(("date", "prompt_tokens", "cached_tokens", "requests"), r)) for r in cur]
        finally:
            conn.close()
