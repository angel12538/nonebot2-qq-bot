"""网页搜索渠道，在聊天工具和管理面板之间共用。"""

from __future__ import annotations

import re
import time
import urllib.request
from html import unescape

from qq_tools import baidu_search

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
LABELS = {"baidu_qianfan": "百度千帆", "bing": "Bing"}


def _parse_bing(html: str, limit: int) -> list[dict]:
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
        m = re.search(r'<h2><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        title = unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        snip = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        snippet = unescape(re.sub(r"<[^>]+>", "", snip.group(1))).strip() if snip else ""
        if title:
            out.append({"title": title, "url": unescape(m.group(1)), "snippet": snippet[:200]})
        if len(out) >= limit:
            break
    return out


def _search_bing_page(query: str, diagnostics: list[str]):
    """连接失败时使用现有代理或重建直连，最多请求两次。"""
    import httpx

    host = "www.bing.com"
    proxies = urllib.request.getproxies()
    proxy = None
    if not urllib.request.proxy_bypass(host):
        proxy = proxies.get("https") or proxies.get("all") or proxies.get("http")
    for attempt, route in enumerate((None, proxy)):
        if attempt:
            time.sleep(0.3)
        label = f"{host}/{'代理' if route else '直连'}#{attempt + 1}"
        try:
            response = httpx.get(
                f"https://{host}/search",
                params={"q": query},
                headers={"User-Agent": UA},
                timeout=httpx.Timeout(7, connect=4),
                proxy=route,
                trust_env=False,
                follow_redirects=True,
            )
        except (httpx.RequestError, ImportError, ValueError) as exc:

            diagnostics.append(f"{label}: {type(exc).__name__}")
            continue
        if response.status_code == 200:
            return response
        diagnostics.append(f"{label}: HTTP {response.status_code}")
        if response.status_code not in {408, 429, 500, 502, 503, 504}:
            break
    return None


def search_provider(provider: str, query: str, limit: int, diagnostics: list[str], *,
                    baidu_key: str = "") -> list[dict] | None:
    """只搜索指定渠道。None 表示请求失败，空列表表示搜索源确认无结果。"""
    if provider == "baidu_qianfan":
        return baidu_search.search(query, limit, baidu_key, diagnostics)
    if provider != "bing":
        raise ValueError("未知的网页搜索渠道")
    response = _search_bing_page(query, diagnostics)
    if response is None:
        return None
    results = _parse_bing(response.text, limit)
    if results:
        return results
    if re.search(r'class=["\'][^"\']*\bb_no\b', response.text.lower()):
        return []
    diagnostics.append(f"{LABELS[provider]}: 未返回可解析结果")
    return None
