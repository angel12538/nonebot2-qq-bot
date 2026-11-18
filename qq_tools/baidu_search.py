"""百度千帆搜索与本机面板设置。"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "baidu_search.json"
SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"
KEY_NAMES = ("BAIDU_API_KEY", "QIANFAN_API_KEY")


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class SearchSettings:
    enabled: bool = False
    protected_key: str = ""
    bing_enabled: bool = True

    def key(self) -> str:
        if not self.protected_key:
            return _external_key()
        import win32crypt
        from pywintypes import error as WindowsCryptoError

        try:
            encrypted = base64.b64decode(self.protected_key, validate=True)
            return win32crypt.CryptUnprotectData(encrypted, None, None, None, 1)[1].decode("utf-8")
        except (ValueError, OSError, WindowsCryptoError) as exc:
            raise SettingsError("无法读取已保存的 API Key，请重新填写后保存。") from exc


def _external_key() -> str:
    for name in KEY_NAMES:
        if key := os.environ.get(name, "").strip():
            return key
    for path in (BASE_DIR / ".baidu-search.env", BASE_DIR.parent / ".env"):
        try:
            values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
        except (OSError, UnicodeError):
            continue
        for name in KEY_NAMES:
            if key := (values.get(name) or "").strip():
                return key
    return ""


def read_settings() -> SearchSettings:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SearchSettings()
    except (OSError, ValueError) as exc:
        raise SettingsError("网页搜索设置文件无法读取。") from exc
    if not isinstance(raw, dict) or type(raw.get("enabled")) is not bool or not isinstance(raw.get("protected_key"), str):
        raise SettingsError("网页搜索设置文件格式有误。")
    bing = raw.get("bing_enabled", True)
    if type(bing) is not bool:
        raise SettingsError("网页搜索设置文件格式有误。")
    return SearchSettings(raw["enabled"], raw["protected_key"], bing)


def save_settings(enabled: bool, new_key: str, *, bing_enabled: bool | None = None) -> SearchSettings:
    current = read_settings()
    state = replace(
        current, enabled=enabled,
        bing_enabled=current.bing_enabled if bing_enabled is None else bing_enabled,
    )
    if new_key:
        if len(new_key) > 256 or any(not 33 <= ord(char) <= 126 for char in new_key):
            raise SettingsError("API Key 格式有误。请从千帆控制台复制完整密钥，只粘贴密钥本身。")
        import win32crypt
        from pywintypes import error as WindowsCryptoError

        try:
            encrypted = win32crypt.CryptProtectData(new_key.encode("utf-8"), "NoneBot2 Baidu Search", None, None, None, 1)
        except (OSError, WindowsCryptoError) as exc:
            raise SettingsError("Windows 无法保存这个 API Key，请检查机器人进程的运行账户。") from exc
        state = replace(state, protected_key=base64.b64encode(encrypted).decode("ascii"))
    if enabled and not state.key():
        raise SettingsError("请先填写千帆 API Key，再启用百度搜索。")

    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=SETTINGS_PATH.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"enabled": state.enabled, "protected_key": state.protected_key,
                       "bing_enabled": state.bing_enabled}, stream)
        temporary.replace(SETTINGS_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return state


def search(query: str, limit: int, key: str, diagnostics: list[str]) -> list[dict] | None:
    """发起一次标准版网页搜索，失败时仅记录状态。"""
    try:
        response = httpx.post(
            SEARCH_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "messages": [{"role": "user", "content": query}],
                "edition": "standard",
                "search_source": "baidu_search_v2",
                "resource_type_filter": [{"type": "web", "top_k": limit}],
            },
            timeout=httpx.Timeout(9, connect=4),
            follow_redirects=False,
            trust_env=False,
        )
    except (httpx.RequestError, ValueError) as exc:
        diagnostics.append(f"百度千帆: {type(exc).__name__}")
        return None
    if response.status_code != 200:
        diagnostics.append(f"百度千帆: HTTP {response.status_code}")
        return None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    references = payload.get("references") if isinstance(payload, dict) else None
    if not isinstance(references, list):
        diagnostics.append("百度千帆: 响应没有有效的网页结果列表")
        return None

    results = []
    seen = set()
    for item in references:
        if not isinstance(item, dict):
            continue
        title, url, snippet = (item.get(name) or "" for name in ("title", "url", "snippet"))
        if not isinstance(title, str) or not title.strip() or not isinstance(url, str):
            continue
        url = url.strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or url in seen:
            continue
        result = {"title": title.strip()[:300], "url": url,
                  "snippet": snippet.strip()[:200] if isinstance(snippet, str) else ""}
        for field in ("website", "date"):
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                result[field] = value.strip()[:100]
        results.append(result)
        seen.add(url)
        if len(results) == limit:
            break
    if references and not results:
        diagnostics.append("百度千帆: 返回的网页条目不可用")
        return None
    return results
