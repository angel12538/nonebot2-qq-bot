"""唤醒调度：活跃度自适应、语义等待、按序号的已读水位。

1. current_rate：按群活跃度、夜间时段、连续插话动态调整接话概率；
2. settle_window：按尾句是否说完给出防抖静默时长，由 debounce 使用
   （等待放在防抖里，不放在消息规则里：规则持有群锁，在锁里等会挡住新消息）；
3. 已读水位：每条消息分配单调递增序号，水位记录「读到第几条」。
   只在机器人真正回复、构建提示词时推进（take_unread），
   对应 qq-bridge 的 readThroughSeq。
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path

WINDOW = 600
QUIET_START = 1
QUIET_END = 7
MIN_GAP = 45
FLOOD_WINDOW = 180
FLOOD_LIMIT = 2
MAX_RATE = 0.25

SETTLE_QUIET_UNFINISHED = 6.0
SETTLE_CAP_LONG = 10.0

DB_PATH = Path.cwd() / "data" / "wake_state.db"

UNFINISHED_TAIL_RE = re.compile(
    r"(你知道|等一下|我跟你讲|其实吧|但是|所以说|然后|那个|就是|我想说|对了|等我|等等|"
    r"我看看|还有|再说|主要是|毕竟|因为|所以|回头|回头说|待会|晚点|等会|再说吧)$"
)
UNFINISHED_PUNCT_RE = re.compile(r"[，、；：,;:]$")
FINISHED_TAIL_RE = re.compile(r"[。！？!?…～~]+$")


_recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=800))
_bot_replies: dict[int, deque] = defaultdict(lambda: deque(maxlen=200))
_seen_seq: dict[int, int] = defaultdict(int)




def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS read_state ("
        "chat_id INTEGER PRIMARY KEY,"
        "seen_seq INTEGER NOT NULL DEFAULT 0,"
        "last_read_seq INTEGER NOT NULL DEFAULT 0,"
        "last_read_ts REAL NOT NULL DEFAULT 0,"
        "updated_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "chat_id INTEGER NOT NULL,"
        "seq INTEGER NOT NULL,"
        "ts REAL NOT NULL,"
        "text TEXT NOT NULL DEFAULT '',"
        "PRIMARY KEY (chat_id, seq))"
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(read_state)")}
    if "seen_seq" not in cols:
        conn.execute("ALTER TABLE read_state ADD COLUMN seen_seq INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE read_state ADD COLUMN last_read_seq INTEGER NOT NULL DEFAULT 0")
    return conn


def _next_seq(chat_id: int) -> int:
    """分配下一条消息的序号（持久化，重启后继续递增）。"""
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO read_state (chat_id, seen_seq, last_read_seq, last_read_ts, updated_at) "
                "VALUES (?, 1, 0, 0, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "seen_seq = read_state.seen_seq + 1, updated_at = excluded.updated_at",
                (chat_id, time.time()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT seen_seq FROM read_state WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        seq = int(row[0]) if row else 1
    except Exception:
        seq = _seen_seq[chat_id] + 1
    _seen_seq[chat_id] = seq
    return seq




def note_message(chat_id: int, text: str = "") -> int:
    """记录一条消息：内存用于活跃度/等待判定，SQLite 用于重启后回放。"""
    text = text or ""
    seq = _next_seq(chat_id)
    now = time.time()
    _recent[chat_id].append((seq, now, text))
    try:
        with _db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages (chat_id, seq, ts, text) VALUES (?, ?, ?, ?)",
                (chat_id, seq, now, text),
            )
            conn.execute("DELETE FROM messages WHERE chat_id = ? AND ts < ?", (chat_id, now - 7 * 86400))
            conn.commit()
    except Exception:
        pass
    return seq


def note_bot_reply(chat_id: int) -> None:
    _bot_replies[chat_id].append(time.time())


def _trim(chat_id: int, now: float, window: int) -> list[tuple]:
    q = _recent[chat_id]
    while q and now - q[0][1] > window:
        q.popleft()
    return list(q)


def activity(chat_id: int, now: float | None = None) -> int:
    now = now or time.time()
    return len(_trim(chat_id, now, WINDOW))


def current_rate(base: float, chat_id: int, now: float | None = None) -> float:
    now = now or time.time()

    if QUIET_START <= time.localtime(now).tm_hour < QUIET_END:
        return 0.0

    replies = list(_bot_replies[chat_id])
    while replies and now - replies[0] > FLOOD_WINDOW:
        replies.pop(0)
    if len(replies) >= FLOOD_LIMIT:
        return 0.0
    if replies and now - replies[-1] < MIN_GAP:
        return 0.0

    n = activity(chat_id, now)
    if n >= 30:
        factor = 0.4
    elif n >= 12:
        factor = 0.8
    elif n <= 3:
        factor = 2.0
    else:
        factor = 1.0

    return min(base * factor, MAX_RATE)




def mark_read(chat_id: int, seq: int | None = None) -> int:
    """把水位推进到指定序号（默认：当前已见的最新一条）。"""
    target = seq if seq is not None else _seen_seq[chat_id]
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO read_state (chat_id, seen_seq, last_read_seq, last_read_ts, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "last_read_seq = MAX(read_state.last_read_seq, excluded.last_read_seq), "
                "last_read_ts = excluded.last_read_ts, updated_at = excluded.updated_at",
                (chat_id, max(target, 0), max(target, 0), time.time(), time.time()),
            )
            conn.commit()
    except Exception:
        pass
    return target


def take_unread(chat_id: int) -> tuple[int, float]:
    """回复前「看一眼群」：返回 (未读条数, 上次看群时间戳)，并把水位推进到最新。

    每次回复只应调用一次（build_train_dict 里）。上次时间为 0 表示从没看过。
    """
    last_ts = 0.0
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT last_read_ts FROM read_state WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        last_ts = float(row[0]) if row else 0.0
    except Exception:
        pass
    count = unread_count(chat_id)
    mark_read(chat_id)
    return count, last_ts


def last_read_time(chat_id: int) -> float:
    """上次「看群」（她回复）的时间戳，只读不推进水位；没看过返回 0。"""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT last_read_ts FROM read_state WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0


def read_seq(chat_id: int) -> int:
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT last_read_seq FROM read_state WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def unread_count(chat_id: int) -> int:
    mark = read_seq(chat_id)
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND seq > ?", (chat_id, mark)
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return max(_seen_seq[chat_id] - mark, 0)


def unread_messages(chat_id: int, limit: int = 20) -> list[dict]:
    """回放水位之后的消息（查库，重启后仍有效）。"""
    mark = read_seq(chat_id)
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT seq, ts, text FROM messages WHERE chat_id = ? AND seq > ? "
                "ORDER BY seq DESC LIMIT ?",
                (chat_id, mark, max(1, min(int(limit), 100))),
            ).fetchall()
        return [{"seq": r[0], "ts": r[1], "text": r[2]} for r in reversed(rows)]
    except Exception:
        return [
            {"seq": seq, "ts": ts, "text": text}
            for seq, ts, text in _recent[chat_id]
            if seq > mark
        ][-limit:]




def looks_like_unfinished(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if FINISHED_TAIL_RE.search(t):
        return False
    if UNFINISHED_PUNCT_RE.search(t):
        return True
    return bool(UNFINISHED_TAIL_RE.search(t))


def _qw(section: str, key: str, default):
    """读面板「配置管理 → qianwen」里的设置（guards/settings.py）；读不到就用默认值。"""
    try:
        from guards.settings import value

        return value(section, key, default)
    except Exception:
        return default


def settle_window(text: str, window: float, max_wait: float) -> tuple[float, float]:
    """防抖用：尾句像没说完就把静默窗口和总时长拉长，等对方把话说完。"""
    if looks_like_unfinished(text):
        quiet = float(_qw("wait", "unfinished_quiet_seconds", SETTLE_QUIET_UNFINISHED))
        cap = float(_qw("wait", "unfinished_max_wait_seconds", SETTLE_CAP_LONG))
        return max(window, quiet), max(max_wait, cap)
    return window, max_wait













import json as _json
import random as _random

SILENCE_MARK = "〔不回〕"
WAKE_CONFIG_PATH = Path.cwd() / "data" / "wake_config.json"

DIRECT_REASONS = {"at", "keyword"}
REASON_TEXT = {
    "at": "有人 @ 了你",
    "keyword": "有人叫了你的名字",
    "ai_keyword": "有人提到了你自己设的关键词",
    "wait_for": "你在等的人说话了",
    "follow_up": "刚才和你聊的人接着说话了（没 @ 你）",
    "active": "你开着活跃模式，群里有人说话",
    "observe": "你刚说完话还在看群（观察期），有人说话了",
    "dive": "你在潜水，这是潜水时的随机冒泡机会",
    "random": "随机冒泡机会（没人叫你）",
    "question": "群里有人提了个问题（没 @ 你，不一定是问你）",
    "proactive": "群里冷场了，这是主动开话题的机会（没人在等你）",
    "proactive_reply": "你刚才主动开了话题，有人说话了",
}


class _GroupRhythm:
    def __init__(self) -> None:
        self.last_spoke = 0.0
        self.engaged: dict[int, float] = {}
        self.pending: set[int] = set()
        self.follow_rounds = 0
        self.silent_streak = 0
        self.wakes: deque = deque(maxlen=2000)
        self.proactive_at = 0.0
        self.proactive_open = False
        self.msg_ids: deque = deque(maxlen=500)


_rhythm: dict[int, _GroupRhythm] = defaultdict(_GroupRhythm)
_reasons: dict[tuple[int, int], tuple[str, float]] = {}
_wake_cache: dict = {"mtime": None, "data": {}}


def _session_key(group_id: int) -> str:
    return f"QQPlatform_Group_{int(group_id)}"


def load_wake_configs() -> dict:
    try:
        mtime = WAKE_CONFIG_PATH.stat().st_mtime
    except OSError:
        _wake_cache.update(mtime=None, data={})
        return {}
    if mtime != _wake_cache["mtime"]:
        try:
            data = _json.loads(WAKE_CONFIG_PATH.read_text(encoding="utf-8"))
            _wake_cache.update(mtime=mtime, data=data if isinstance(data, dict) else {})
        except Exception:
            _wake_cache.update(mtime=mtime, data={})
    return _wake_cache["data"]


def _save_wake_configs(data: dict) -> None:
    try:
        WAKE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = WAKE_CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WAKE_CONFIG_PATH)
    except Exception:
        pass


def wake_config(group_id: int, now: float | None = None) -> dict:
    """她自己为这个群设的节奏（已过期的部分去掉）。面板关掉自主节奏时返回空。"""
    if not _qw("rhythm", "ai_wake_control", True):
        return {}
    now = now or time.time()
    cfg = dict(load_wake_configs().get(_session_key(group_id)) or {})
    if cfg.get("mode") and float(cfg.get("until", 0)) <= now:
        for k in ("mode", "until", "probability"):
            cfg.pop(k, None)
    if cfg.get("wait_for") and float(cfg.get("wait_until", 0)) <= now:
        cfg.pop("wait_for", None)
        cfg.pop("wait_until", None)
    return cfg


def _wake_rate_ok(st: _GroupRhythm, now: float) -> bool:
    per_min = int(_qw("rhythm", "max_wakes_per_minute", 2))
    per_hour = int(_qw("rhythm", "max_wakes_per_hour", 30))
    while st.wakes and now - st.wakes[0] > 3600:
        st.wakes.popleft()
    if per_min and sum(1 for t in st.wakes if now - t < 60) >= per_min:
        return False
    return not (per_hour and len(st.wakes) >= per_hour)



_QUESTION_TAIL_RE = re.compile(r"([?？]|[吗嘛么呢])[\s~～。.!！?？]*$")
_QUESTION_WORD_RE = re.compile(
    r"(为什么|为啥|怎么办|怎么样|怎么回事|咋办|咋整|是不是|有没有|能不能|会不会|要不要|"
    r"可不可以|行不行|有人知道|谁知道|谁懂|求问|请问|有谁)"
)


def looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 4:
        return False
    return bool(_QUESTION_TAIL_RE.search(t) or _QUESTION_WORD_RE.search(t))


def decide_wake(
    group_id: int,
    user_id: int,
    message_id: int,
    text: str,
    direct_reason: str | None,
    base_rate: float,
    addressed_other: bool = False,
) -> str | None:
    """决定这条群消息要不要叫醒她，返回叫醒原因（REASON_TEXT 的键）或 None。

    @ 和叫名字（direct_reason）始终叫醒；其余按优先级：自设关键词 > 等的人 > 跟进 >
    活跃模式 > 潜水 / 接主动话题 / 提问 / 观察期 / 随机，并受「非直接叫醒」的频率上限约束。
    addressed_other：这条消息引用或 @ 的是别人，提问叫醒不算它。
    """
    now = time.time()
    st = _rhythm[int(group_id)]
    uid = int(user_id or 0)
    reason: str | None = None
    if direct_reason:
        reason = direct_reason
        st.follow_rounds = 0
    else:
        ai = wake_config(group_id, now)
        keywords = [k for k in ai.get("keywords") or [] if k]
        if keywords and any(k in (text or "") for k in keywords):
            reason = "ai_keyword"
        elif uid and uid in {int(x) for x in ai.get("wait_for") or []}:
            reason = "wait_for"
        elif (
            int(_qw("rhythm", "follow_up_seconds", 60)) > 0
            and st.engaged.get(uid, 0) >= now
            and st.follow_rounds < int(_qw("rhythm", "follow_up_max_rounds", 6))
        ):
            reason = "follow_up"
        elif ai.get("mode") == "active":
            reason = "active"
        elif ai.get("mode") == "dive":
            if _random.random() < float(ai.get("probability", 0) or 0):
                reason = "dive"
        else:
            observe = int(_qw("rhythm", "observe_seconds", 300))
            in_observe = observe > 0 and st.last_spoke and now - st.last_spoke <= observe
            if st.proactive_open:

                st.proactive_open = False
                if in_observe or now - st.last_spoke <= 600:
                    reason = "proactive_reply"
            if (
                reason is None
                and not addressed_other
                and _qw("rhythm", "question_wake", True)
                and looks_like_question(text)
                and _random.random() < float(_qw("rhythm", "question_probability", 0.5))
            ):
                reason = "question"
            if reason is None and in_observe:
                if _random.random() < float(_qw("rhythm", "observe_probability", 0.3)):
                    reason = "observe"
            if reason is None and base_rate > 0 and _random.random() <= base_rate:
                reason = "random"
        if reason and not _wake_rate_ok(st, now):
            reason = None
        if reason:
            st.wakes.append(now)
            if reason == "follow_up":
                st.follow_rounds += 1
    if reason:
        if uid:
            st.pending.add(uid)
        _reasons[(int(group_id), int(message_id or 0))] = (reason, now)
        for k in [k for k, (_, t) in _reasons.items() if now - t > 900]:
            _reasons.pop(k, None)
    return reason


def wake_reason(group_id: int, message_id: int) -> str | None:
    hit = _reasons.get((int(group_id), int(message_id or 0)))
    return hit[0] if hit else None


def note_spoke(group_id: int) -> None:
    """她在这个群真正发出了消息：叫醒她的人进入跟进窗口，观察期从现在开始算。"""
    now = time.time()
    st = _rhythm[int(group_id)]
    st.last_spoke = now
    st.silent_streak = 0
    if st.proactive_at and now - st.proactive_at <= PROACTIVE_TIMEOUT_SECONDS:
        st.proactive_open = True
    st.proactive_at = 0.0
    window = int(_qw("rhythm", "follow_up_seconds", 60))
    if window > 0:
        for uid in st.pending:
            st.engaged[uid] = now + window
    st.pending.clear()
    for uid in [u for u, t in st.engaged.items() if t < now]:
        st.engaged.pop(uid, None)


def note_silence(group_id: int) -> bool:
    """她这次选择不回。连续不回到上限时退回正常节奏，返回是否发生了重置。"""
    st = _rhythm[int(group_id)]
    st.pending.clear()
    st.proactive_at = 0.0
    st.silent_streak += 1
    if st.silent_streak < int(_qw("rhythm", "silent_limit", 3)):
        return False
    st.silent_streak = 0
    st.last_spoke = 0.0
    st.engaged.clear()
    data = load_wake_configs()
    key = _session_key(group_id)
    cfg = dict(data.get(key) or {})
    if cfg.get("mode") == "active":
        for k in ("mode", "until", "probability"):
            cfg.pop(k, None)
        _save_wake_configs({**data, key: cfg})
    return True


def describe_rhythm(group_id: int) -> str:
    """当前节奏的一句话描述，写进提示词。"""
    now = time.time()
    st = _rhythm[int(group_id)]
    ai = wake_config(group_id, now)

    def hm(t) -> str:
        return time.strftime("%H:%M", time.localtime(float(t)))

    parts = []
    if ai.get("mode") == "dive":
        until = "一直潜水，直到你改回正常" if ai.get("endless") else f"潜水到 {hm(ai['until'])}"
        parts.append(f"{until}（冒泡概率 {float(ai.get('probability', 0) or 0):.0%}）")
    elif ai.get("mode") == "active":
        parts.append(f"活跃到 {hm(ai['until'])}（群里任何人说话都会叫醒你）")
    else:
        parts.append("正常")
    if ai.get("wait_for"):
        parts.append(f"在等 {', '.join(str(x) for x in ai['wait_for'])} 说话（到 {hm(ai['wait_until'])}）")
    if ai.get("keywords"):
        parts.append(f"自设关键词：{'、'.join(ai['keywords'])}")
    if st.engaged and max(st.engaged.values()) >= now:
        parts.append("刚才和你聊的人再说话会叫醒你")
    observe = int(_qw("rhythm", "observe_seconds", 300))
    if observe > 0 and st.last_spoke and now - st.last_spoke <= observe:
        parts.append(f"观察期还剩约 {int((observe - (now - st.last_spoke)) // 60) + 1} 分钟")
    return "；".join(parts)




def note_message_id(group_id: int, message_id: int, user_id: int) -> None:
    """check_rule 每条群消息都记一笔，出站时只认这些 id 和 QQ 号（重启后清空，再查接口兜底）。"""
    try:
        mid, uid = int(message_id or 0), int(user_id or 0)
    except (TypeError, ValueError):
        return
    if mid and not is_proactive(mid):
        _rhythm[int(group_id)].msg_ids.append((mid, uid, time.time()))


def seen_in_wait(chat: str, message_id) -> bool:
    """她在 wait_for_messages 里看过这条（qq_tools 写进 seen_wait 表），这一轮结束后不再为它叫醒她。
    chat 形如 g群号 / p QQ号。"""
    try:
        mid = int(message_id or 0)
        if not mid:
            return False
        with _db() as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='seen_wait'"
            ).fetchone()
            if not row:
                return False
            hit = conn.execute(
                "SELECT 1 FROM seen_wait WHERE chat = ? AND message_id = ? AND ts > ?",
                (chat, mid, time.time() - 1800),
            ).fetchone()
        return bool(hit)
    except Exception:
        return False


def silent_mode() -> bool:
    """静默模式：群友消息只记录不叫醒她，只有管理员能和她对话（面板「qianwen → 静默模式」或管理员命令 /静默）。"""
    return bool(_qw("silent", "enabled", False))


def known_message(group_id: int, message_id: int) -> bool:
    return any(m == int(message_id) for m, _, _ in _rhythm[int(group_id)].msg_ids)


def known_speaker(group_id: int, user_id: int, within: float = 86400) -> bool:
    now = time.time()
    return any(u == int(user_id) and now - t <= within for _, u, t in _rhythm[int(group_id)].msg_ids)








PROACTIVE_STATE_PATH = Path.cwd() / "data" / "proactive_state.json"
PROACTIVE_TEXT = "〔冷场〕"
PROACTIVE_RETRY_SECONDS = 30 * 60
PROACTIVE_TIMEOUT_SECONDS = 10 * 60
_proactive: dict[int, dict] = {}


def last_message_ts(group_id: int) -> float:
    try:
        with _db() as conn:
            row = conn.execute("SELECT MAX(ts) FROM messages WHERE chat_id = ?", (int(group_id),)).fetchone()
        return float(row[0]) if row and row[0] else 0.0
    except Exception:
        q = _recent[int(group_id)]
        return q[-1][1] if q else 0.0


def _load_proactive_state() -> dict:
    try:
        data = _json.loads(PROACTIVE_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_proactive_state(data: dict) -> None:
    try:
        PROACTIVE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROACTIVE_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(PROACTIVE_STATE_PATH)
    except Exception:
        pass


def _in_hours(hour: int) -> bool:
    start = int(_qw("rhythm", "proactive_start_hour", 9))
    end = int(_qw("rhythm", "proactive_end_hour", 23))
    if start == end:
        return True
    return start <= hour < end if start < end else (hour >= start or hour < end)


def proactive_due(group_id: int, now: float | None = None) -> tuple[int, float] | None:
    """这个群现在要不要给她一次主动开话题的机会。要就返回 (冷场分钟数, 最后一条消息时间)。"""
    if not _qw("rhythm", "proactive_enable", True) or silent_mode():
        return None
    now = now or time.time()
    if not _in_hours(time.localtime(now).tm_hour):
        return None
    gid = int(group_id)
    last = last_message_ts(gid)
    if last <= 0:
        return None
    idle = now - last
    if idle < int(_qw("rhythm", "proactive_idle_minutes", 40)) * 60:
        return None
    st = _rhythm[gid]
    if st.last_spoke >= last or st.proactive_at:
        return None
    if wake_config(gid, now).get("mode") == "dive":
        return None
    data = _load_proactive_state()
    s = dict(data.get(str(gid)) or {})
    if s.get("rolled_for") == last:

        if s.get("hit", True) or now - s["rolled_at"] < PROACTIVE_RETRY_SECONDS:
            return None
    elif idle > int(_qw("rhythm", "proactive_max_idle_minutes", 180)) * 60:
        return None
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    count = int(s.get("count", 0)) if s.get("date") == today else 0
    limit = int(_qw("rhythm", "proactive_daily_limit", 3))
    if limit and count >= limit:
        return None
    hit = _random.random() < float(_qw("rhythm", "proactive_probability", 0.5))
    s.update(rolled_for=last, rolled_at=now, hit=hit, date=today, count=count + int(hit))
    _save_proactive_state({**data, str(gid): s})
    return (int(idle // 60), last) if hit else None


def new_proactive(group_id: int, idle_minutes: int, last_ts: float) -> int:
    """登记一次冷场叫醒，返回给假事件用的消息 id。

    真消息 id 是 32 位整数（可能为负），这里取 -10^12 以下，落在 32 位范围外，不会和真消息冲突；
    判断真假只看是否登记过（is_proactive），不看正负。
    """
    now = time.time()
    gid = int(group_id)
    mid = -(10**12 + int(now * 1000) % 10**12)
    _proactive[mid] = {"group": gid, "ts": now, "idle": int(idle_minutes), "last": float(last_ts)}
    for k in [k for k, v in _proactive.items() if now - v["ts"] > 900]:
        _proactive.pop(k, None)
    _reasons[(gid, mid)] = ("proactive", now)
    _rhythm[gid].proactive_at = now
    return mid


def finish_proactive(message_id: int) -> None:
    """释放本次冷场事件的标记，避免旧事件清掉后续事件的标记。"""
    info = _proactive.get(message_id)
    if info is None:
        return
    st = _rhythm[info["group"]]
    if st.proactive_at == info["ts"]:
        st.proactive_at = 0.0


def is_proactive(message_id) -> bool:
    try:
        return int(message_id or 0) in _proactive
    except (TypeError, ValueError):
        return False


def proactive_input(message_id) -> str:
    """提供冷场事件的时间和来源，开话题的要求由系统提示说明。"""
    info = _proactive.get(int(message_id)) or {}
    last = info.get("last") or 0
    last_hm = time.strftime("%H:%M", time.localtime(last)) if last else "很久以前"
    return (
        f"【定时冷场检查】这不是群友发来的消息。群里已经安静了约 {info.get('idle', 0)} 分钟，"
        f"最后有人说话是 {last_hm}。当前没有人在等你回复。"
    )
