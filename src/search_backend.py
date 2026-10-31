"""
搜索后端适配层。

问题：运行环境不同，能用的搜索通道也不同。
- 本地（DeepSeek Harness）：可以用 agent 侧的 web_search
- GitHub Actions：需要外部搜索 API（Serper / Brave / Tavily / Bing）

设计：统一 async 接口，按环境变量自动选择。
所有后端都返回 [{title, url, snippet}]。
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Callable, Awaitable

import httpx

SearchFn = Callable[[str], Awaitable[list[dict]]]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
}


def _norm(hits: list) -> list[dict]:
    out = []
    for h in hits or []:
        if isinstance(h, dict) and h.get("url"):
            out.append({
                "title": (h.get("title") or "")[:300],
                "url": h["url"],
                "snippet": (h.get("snippet") or "")[:500],
            })
    return out


# ============ 后端 1：Serper.dev（推荐，Google 结果）============
async def _serper(q: str) -> list[dict]:
    key = os.getenv("SERPER_API_KEY")
    if not key:
        return []
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": q, "num": 8})
        if r.status_code != 200:
            return []
        d = r.json()
    out = []
    for k in ("organic", "news"):
        for it in d.get(k, []) or []:
            out.append({
                "title": it.get("title", ""),
                "url": it.get("link", ""),
                "snippet": it.get("snippet", "") or it.get("description", ""),
            })
    return _norm(out)


# ============ 后端 2：Brave Search ============
async def _brave(q: str) -> list[dict]:
    key = os.getenv("BRAVE_API_KEY")
    if not key:
        return []
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            params={"q": q, "count": 8})
        if r.status_code != 200:
            return []
        d = r.json()
    return _norm([
        {"title": x.get("title"), "url": x.get("url"),
         "snippet": x.get("description")}
        for x in d.get("web", {}).get("results", [])
    ])


# ============ 后端 3：Tavily（专为 AI 检索设计）============
async def _tavily(q: str) -> list[dict]:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return []
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(
            "https://api.tavily.com/search",
            headers={"Content-Type": "application/json"},
            json={"api_key": key, "query": q, "max_results": 8})
        if r.status_code != 200:
            return []
        d = r.json()
    return _norm([
        {"title": x.get("title"), "url": x.get("url"),
         "snippet": x.get("content")}
        for x in d.get("results", [])
    ])


# ============ 后端 4：DuckDuckGo HTML（免费但不稳）============
async def _ddg(q: str) -> list[dict]:
    try:
        from selectolax.lexbor import LexborHTMLParser as P
    except ImportError:  # pragma: no cover
        from selectolax.parser import P
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
        try:
            r = await c.post("https://html.duckduckgo.com/html/",
                             headers=HEADERS, data={"q": q})
        except Exception:
            return []
        if r.status_code != 200:
            return []
        try:
            tree = P(r.text)
        except Exception:
            return []
    out = []
    for a in tree.css("a.result__a")[:8]:
        href = a.attributes.get("href", "")
        m = re.search(r"uddg=([^&]+)", href)
        if not m:
            continue
        from urllib.parse import unquote
        out.append({"title": a.text(), "url": unquote(m.group(1)),
                    "snippet": ""})
    return _norm(out)


# ============ 后端 5：本地 JSON 桥（Harness 侧导出）============
# 若 agent 侧已用 web_search 拿到结果，可写到 data/search_cache.json
async def _local_cache(q: str) -> list[dict]:
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "data" / "search_cache.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return _norm(data.get(q, []))


BACKENDS = {
    "serper": _serper,
    "brave": _brave,
    "tavily": _tavily,
    "ddg": _ddg,
    "cache": _local_cache,
}


def get_search_fn() -> SearchFn:
    """
    按 SEARCH_BACKEND 环境变量选择后端，默认按可用性探测。
    """
    order = os.getenv("SEARCH_BACKEND")
    if order:
        names = [x.strip() for x in order.split(",") if x.strip()]
    else:
        names = []
        for key, fn in (("SERPER_API_KEY", _serper), ("TAVILY_API_KEY", _tavily),
                        ("BRAVE_API_KEY", _brave)):
            if os.getenv(key):
                names.append(fn.__name__.strip("_"))
        if not names:
            names = ["cache", "ddg"]

    chain = [BACKENDS[n] for n in names if n in BACKENDS]

    async def _search(q: str) -> list[dict]:
        for fn in chain:
            try:
                r = await fn(q)
                if r:
                    return r
            except Exception:
                continue
        return []

    return _search