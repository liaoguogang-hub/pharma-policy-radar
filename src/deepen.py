"""
深度检索模块：给重要条目找一手材料。

背景：
Endpoints / Fierce / STAT 等媒体，RSS 能拿到标题，但正文受付费墙限制
（实测 Endpoints 文章页 200 可访问，但只有 500+ 字符预览）。

解法：
1. 挑出"可能重要但没正文"的条目（按标题判断金额/阶段/首创性）
2. 用 web_search 搜该交易的一手材料（公司官网新闻室、交易所公告）
3. 抓取能访问的一手页面正文

这是从实测得出的经验——BD 的一手材料几乎都在公司 newsroom 和
交易所披露上，而且这些页面通常不反爬。
"""
from __future__ import annotations

import asyncio
import re
import sys
from urllib.parse import quote_plus

import httpx

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:  # pragma: no cover
    from selectolax.parser import HTMLParser

from common import (RawItem, clean_text, now_iso, make_id, split_entity_tags)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}


# ============ 重要性预判（只看标题）============
# 金额：$1.65B / $2.9 billion / $80M / 26亿美元 / 1.65亿美元 等
HIGH_SIGNAL = re.compile(
    r"\$\s?\d[\d.,]*\s*(?:billion|bn|b|million|m\b|thousand|k)\b|"
    r"\d[\d.,]*\s*(?:billion|bn)\b|"
    r"\d[\d.,]*\s*(?:亿|万)\s*(?:美元|美金|元)|"
    r"\b(?:upfront|milestone)[^$]{0,20}\$\s?\d",
    re.I,
)
FIRST_MOVER = re.compile(
    r"\bfirst[- ]in[- ]class\b|\bFIC\b|world[- ]first|\bfirst ever\b|"
    r"全球首个|首创|首次",
    re.I,
)
MILESTONE = re.compile(
    r"\bphase (?:1|2|3)\b|\bPhase I{1,3}\b|\btopline\b|\bprimary endpoint\b|"
    r"\bFDA\b|\bapproval\b|\bapproved\b|"
    r"III ?期|三期|主要终点|获批|临床",
    re.I,
)
MNC = re.compile(
    r"\b(Roche|Novartis|AstraZeneca|AstraZeneca|Pfizer|Merck|MSD|GSK|"
    r"Gilead|AbbVie|Amgen|Bristol|Sanofi|Takeda|Bayer|J&J|Johnson|"
    r"Eli Lilly|Lilly|Novo|Regeneron|Biogen|Boehringer|BMS|Moderna|"
    r"阿斯利康|诺华|辉瑞|默沙东|葛兰素|礼来|诺和|赛诺菲|武田|"
    r"再生元|强生|百时美施贵宝)\b",
    re.I,
)


def looks_important(title: str) -> tuple[bool, list[str]]:
    """
    从标题判断是否值得深挖。
    返回 (是否重要, 命中的信号列表)

    ⚠️ 重要：Endpoints 的标题常省略金额（RSS 摘要才有），
    所以"里程碑+MNC"这种组合也必须算重要。
    """
    signals = []
    has_money = bool(HIGH_SIGNAL.search(title))
    has_first = bool(FIRST_MOVER.search(title))
    has_ms = bool(MILESTONE.search(title))
    has_mnc = bool(MNC.search(title))

    if has_money:
        signals.append("金额")
    if has_first:
        signals.append("首创")
    if has_ms:
        signals.append("里程碑")
    if has_mnc:
        signals.append("MNC")

    # 判定：两个信号、或"金额+交易动词"、或单看就够强的组合
    deal_word = bool(re.search(
        r"\b(deal|acquire|acquisition|license|merger|pay|pays|"
        r"agreed|strike|in-?licens)\b|收购|授权|交易|合作",
        title, re.I))

    important = False
    if len(signals) >= 2:
        important = True
    if has_money and deal_word:
        important = True
    # MNC + 里程碑 单独也够（Endoints 常见句式）
    if has_mnc and has_ms:
        important = True

    return important, signals


# ============ 搜索入口 ============
async def _search(client: httpx.AsyncClient, query: str) -> list[dict]:
    """
    调用搜索接口。
    ⚠️ 本环境无搜索 API key，这里留接口供 Actions 侧或本地插件接入。
    返回 [{title, url, snippet}]
    """
    raise NotImplementedError(
        "搜索需要配置 SEARCH_API_KEY 或接入本地 web_search 插件")


# ============ 候选搜索词构造 ============
def build_queries(item: RawItem) -> list[str]:
    """
    从标题提取关键实体，构造 3-4 个搜索式。
    策略：一手公告优先 -> 公司新闻室 -> 交易所
    """
    t = item.title.strip()
    # 去掉尾部元信息
    t = re.sub(r"\s*[|｜].*$", "", t)
    t = re.sub(r"\s*\d{4}-\d{2}-\d{2}.*$", "", t).strip()

    # 抽公司名（大写词 + 常见后缀）
    companies = re.findall(
        r"\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,}){0,2})\b", t)
    stop = {"The", "A", "An", "This", "That", "With", "From", "After",
            "What", "Why", "How", "Its", "Who", "New", "First", "Two"}
    companies = [c for c in companies if c not in stop][:3]

    # 抽金额
    money = re.findall(r"\$[\d.]+\s*(?:billion|bn|million|m)?|\d+亿美元", t)
    money = money[:2]

    queries = []
    # 1) 标题原文（最容易命中一手公告）
    queries.append(t)

    # 2) 标题 + 公告后缀，但公司名已在标题里就只加后缀，避免重复
    tail = re.sub(r"^.{0,60}", "", t).strip()
    for suffix in ("press release", "announcement", "newsroom"):
        queries.append(f"{t} {suffix}")

    # 3) 金额 + 交易类型（换一种措辞提高召回）
    if money:
        base = companies[0] if companies else t[:30]
        queries.append(f"{base} {money[0]} deal announcement")
        queries.append(f"{base} {money[0]} 授权 交易")

    # 4) 英文条目的中文补充式
    if item.lang == "en":
        queries.append(f"{companies[0] if companies else t[:25]} 交易 金额 公告"
                       if companies else f"{t[:50]} 交易")

    # 去重且限长
    seen, out = set(), []
    for q in queries:
        q = re.sub(r"\s+", " ", q).strip()[:170]
        if q and q not in seen and len(q) > 12:
            seen.add(q)
            out.append(q)
    return out[:5]


# ============ 一手来源识别 ============
PRIMARY_DOMAINS = re.compile(
    r"(newsroom|investor|ir\.|/news/|/press|hkexnews|sse\.com|szse|"
    r"nasdaq|globenewswire|businesswire|prnewswire|"
    r"akesobio|akeso\.com|hengrui|pharmaron|ascentage|innocare|"
    r"remegen|innoventbio|hspharm|carsgen|junshi|3sbio)",
    re.I,
)


def is_primary(url: str) -> bool:
    return bool(PRIMARY_DOMAINS.search(url))


# ============ 正文抓取（复用 fetch 的逻辑）============
async def fetch_primary(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """返回 (正文, 站点名)"""
    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True,
                             timeout=25)
    except Exception:
        return "", ""
    if r.status_code != 200 or len(r.text) < 800:
        return "", ""

    html = r.text
    # 去掉干扰：脚本、样式、JSON-LD 结构化数据
    for pat in (r"(?is)<script[^>]*>.*?</script>",
                r"(?is)<style[^>]*>.*?</style>",
                r"(?is)<noscript[^>]*>.*?</noscript>",
                r"(?is)<svg[^>]*>.*?</svg>"):
        html = re.sub(pat, " ", html)

    try:
        tree = HTMLParser(html)
    except Exception:
        return "", ""

    best, blen = "", 0
    for sel in ["article", "main", "div.article-body", "div#content",
                "div.news-body", "div.entry-content", "div.rich_media_content",
                "div#js_content", "section.article"]:
        try:
            nodes = tree.css(sel)[:1]
        except Exception:
            continue
        if not nodes:
            continue
        t = clean_text(nodes[0].text())
        # 过滤：含大段代码或乱码的不算正文
        ratio = _text_ratio(t)
        if ratio < 0.85:
            continue
        if len(t) > blen:
            best, blen = t, len(t)

    if blen < 400:
        # 兜底：拼接较长的段落
        try:
            paras = [clean_text(p.text()) for p in tree.css("p")]
        except Exception:
            paras = []
        paras = [p for p in paras if len(p) > 60 and _text_ratio(p) > 0.9]
        if paras:
            best = " ".join(paras)
            blen = len(best)

    host = ""
    m = re.search(r"https?://([^/]+)", url)
    if m:
        host = m.group(1)

    if blen <= 400 or _text_ratio(best) <= 0.8:
        return "", host

    best = _strip_nav(best)
    return best[:12000], host


# 常见的导航/页脚前缀噪声
NAV_PATTERNS = [
    r"^(?:Localized information for \w+.*?\n)+",
    r"^(?:Accept all cookies.*?\n)+",
    r"^(?:The page you are about to visit.*?\n)+",
    r"^(?:Home\s*\|?\s*News.*?\n)+",
    r"^(?:Skip to main content.*?\n)+",
    r"^(?:Share to Twitter.*?(?:Reddit|Print)\n)+",
]


def _strip_nav(s: str) -> str:
    """去掉开头的导航/cookie 提示"""
    for pat in NAV_PATTERNS:
        s = re.sub(pat, "", s, flags=re.I)
    return s.strip()


def _text_ratio(s: str) -> float:
    """
    文本"干净度"：正常中英文字符占比。
    JS/JSON 里有大量 {} := ; 等符号，比值会很低。
    """
    if not s:
        return 0.0
    good = len(re.findall(r"[\w\s\u4e00-\u9fff，。、；：（）《》\"'%$.,!?-]", s))
    return good / len(s)


async def enrich(item: RawItem, search_fn) -> RawItem:
    """
    给条目补一手材料。
    search_fn: async (query) -> [{title, url, snippet}]
    """
    if len(item.content or "") > 800:
        return item          # 已有正文

    qs = build_queries(item)
    found: list[tuple[str, str]] = []

    for q in qs:
        try:
            hits = await search_fn(q)
        except Exception as e:
            print(f"    [search fail] {q[:50]} -> {type(e).__name__}")
            continue
        for h in (hits or [])[:5]:
            u = h.get("url", "")
            if u and is_primary(u):
                found.append((u, h.get("title", "")))
        if len(found) >= 3:
            break

    if not found:
        # 没有一手来源，至少把搜索到的摘要存下来
        snippets = []
        for q in qs[:2]:
            try:
                for h in (await search_fn(q)) or []:
                    snippets.append(f"· {h.get('title','')}\n  {h.get('snippet','')[:300]}")
            except Exception:
                pass
        if snippets:
            item.summary = ("\n".join(snippets[:6])[:2500])
            item.topics = list(set(item.topics + ["enriched_no_primary"]))
        return item

    # 抓取第一个可访问的一手页面
    async with httpx.AsyncClient(timeout=25, http2=False) as c:
        for u, t in found[:3]:
            body, host = await fetch_primary(c, u)
            if body:
                # 一手页面同样可能带新闻站实体标签行，一并剥离（见 common）
                body2, etags = split_entity_tags(body)
                item.entity_tags = list(set(item.entity_tags + etags))
                item.content = body2
                item.topics = list(set(item.topics + [f"primary:{host}"]))
                print(f"    ✓ 一手材料: {host} ({len(body2)} 字)")
                return item
            print(f"    ✗ 无法抓取: {u[:60]}")

    item.topics = list(set(item.topics + ["primary_not_accessible"]))
    return item


def run(items: list[RawItem], search_fn, max_items: int = 8) -> list[RawItem]:
    """
    对没有正文但看起来重要的条目，补一手材料。
    """
    cands = []
    for it in items:
        if len(it.content or "") > 800:
            continue
        ok, signals = looks_important(it.title)
        if ok:
            cands.append(it)

    # 按信号数量和来源权重排序
    cands.sort(key=lambda x: (len(looks_important(x.title)[1]), x.weight),
               reverse=True)
    cands = cands[:max_items]

    print(f"[enrich] 候选 {len(cands)} 条（无正文但信号强）")
    for c in cands:
        print(f"    {'+'.join(looks_important(c.title)[1]):12} {c.title[:56]}")

    async def _run():
        out = []
        for it in cands:
            print(f"  → {it.title[:60]}")
            try:
                out.append(await enrich(it, search_fn))
            except Exception as e:
                print(f"    失败 {type(e).__name__}")
                out.append(it)
        return out

    return asyncio.run(_run()) if cands else items