"""
采集模块：RSS / HTML / PDF 三通道。

设计要点（来自实测教训）：
1. 每个源声明 access_level，抓不到时自动降级而不是丢弃
2. blocked 的源不浪费时间重试，只记录"有此报道"的事实
3. CloudFront 403 的源走 RSS 绕开
4. 单源失败不影响整体，用 try/except 隔离
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import feedparser
import httpx
try:                                  # selectolax >= 1.0 用 lexbor 后端
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:                 # 旧版回退
    from selectolax.parser import HTMLParser

from common import (RawItem, clean_text, ensure_dirs, load_yaml, make_id,
                    now_iso, split_entity_tags, RAW_DIR, DATA_DIR, write_json,
                    today_cst)
from noise import filter_items
from deepen import looks_important
from search_backend import get_search_fn

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}


# ============ 底层抓取 ============
async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True)
        if r.status_code == 200:
            return r
        # 403/406 等 → 记日志但不抛错，让上层决定
        return None
    except Exception as e:
        print(f"  [fetch] {url} -> {type(e).__name__}")
        return None


# ============ RSS 通道 ============
async def fetch_rss(client: httpx.AsyncClient, src: dict) -> list[RawItem]:
    r = await _get(client, src["url"])
    if not r:
        return []
    feed = feedparser.parse(r.content)
    out: list[RawItem] = []
    for e in feed.entries[:40]:
        title = clean_text(getattr(e, "title", ""))
        if not title:
            continue
        published = getattr(e, "published", "") or getattr(e, "updated", "")
        try:
            dt = datetime(*e.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            dt = now_iso()
        out.append(RawItem(
            id=make_id(title, getattr(e, "link", "")),
            source=src["id"], lang=src["lang"],
            title=title, url=getattr(e, "link", ""),
            published=dt, access="rss", mode="rss",
            summary=clean_text(getattr(e, "summary", ""))[:1500],
            weight=src.get("weight", 1.0),
            topics=src.get("topics", []),
            fetch_time=now_iso(),
            # ⚠️ RSS 源的 summary 大面积为空（实测 ESMO 14/20 空）。
            #    配了 fetch_body 的源要去文章页抓正文（2026-10-25）。
            fetch_body=bool(src.get("fetch_body")),
        ))
    return out


# ============ HTML 通道 ============
# 各站点的文章链接提取规则（按 access 分级）
LIST_PATTERNS = {
    "biopharmadive_deals": {"base": "https://www.biopharmadive.com",
                       "item": "a[href*='/news/']"},
    "biopharmadive_home":  {"base": "https://www.biopharmadive.com",
                       "item": "a[href*='/news/']"},
    "bioworld_deals":      {"base": "https://www.bioworld.com",
                       "item": "a[href*='/articles/']"},
    "bioworld_asia":       {"base": "https://www.bioworld.com",
                       "item": "a[href*='/articles/']"},
    "stat_biotech":        {"base": "https://www.statnews.com",
                       "item": "a[href*='/2026/']"},
    "stat_pharma":         {"base": "https://www.statnews.com",
                       "item": "a[href*='/2026/']"},
    "phirda_industry":     {"base": "https://www.phirda.com",
                       "item": "a[href*='/artilce_']"},
    "pharnexcloud":        {"base": "https://www.pharnexcloud.com",
                       "item": "a[href*='/zixun/']"},
    "stcn_medicine":       {"base": "https://stcn.com",
                       "item": "a[href*='/article/detail/']"},
    "cnstock_health":      {"base": "https://www.cnstock.com",
                       "item": "a[href*='commonDetail/']"},
    "nbdd_cn":             {"base": "https://www.nbd.com.cn",
                       "item": "a[href*='/articles/']"},
    "pharmcube":           {"base": "https://bydrug.pharmcube.com",
                       "item": "a[href*='/news/detail/']"},
    # ⚠️ PharmExec 的文章路径是 /view/<slug>，不是 /news/。
    # 曾误写成 /news/ 导致判定为"数据中心 IP 被拦"，实际是选择器错误。
    "pharmexec_partnership": {"base": "https://www.pharmexec.com",
                       "item": "a[href*='/view/']"},
    "pharmexec_irlp":      {"base": "https://www.pharmexec.com",
                       "item": "a[href*='/view/']"},
}

# 标题里粘连的日期前缀（PharmExec 这类站点的列表页常见：
# "October 5th 2026Roundup: CSL & Alentis..."）。
# 不清理会污染标题、影响去重相似度。
LEADING_DATE = re.compile(
    r"^\s*(?:"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
    r"Oct|Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}"
    r"|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
    r")\s*[·\-—|:：]?\s*",
    re.I,
)

# 列表页序号粘连："2AstraZeneca..." / "4AstraZeneca..."。
# 只剥 1-2 位数字紧跟大写字母的情况，避免误伤 "52周" "7家药企"。
LEADING_INDEX = re.compile(r"^\d{1,2}(?=[A-Z])")


# ============ 真实发布日期提取 ============
#
# 为什么需要：
# 列表页（尤其 BioPharma Dive 的 /topic/deals/ 这类"专题归档页"）
# 会把几个月前的旧文一直挂在上面。早期版本给所有 HTML 条目
# 一律填 now_iso()，于是旧文被当成当天新闻——实测把一篇
# 2026-08-18 发的 Leo Pharma 稿当成 2026-10-11 的新闻写了出来。
#
# 解决办法：去文章页取真实日期。优先级：
#   1. JSON-LD 的 datePublished / dateCreated（最可靠）
#   2. <time datetime="...">
#   3. 正文里的 "Published Aug. 18, 2026"
#   4. URL 里的 /2026/08/18/ 路径
RE_JSONLD_DATE = re.compile(
    r'"(?:datePublished|dateCreated|uploadDate)"\s*:\s*"([^"]+)"', re.I)
RE_TIME_TAG = re.compile(
    r'<time[^>]+datetime="([^"]+)"', re.I)
RE_PUBLISHED_TEXT = re.compile(
    r"(?:Published|Posted|Updated|发布|发表于|发布于|发布时间)\s*[:：]?\s*"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"\d{1,2},\s*\d{4}|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)", re.I)
# 中文页面常见 "发布于" 之间夹字，单独给一条宽松规则
RE_PUBLISHED_CN = re.compile(
    r"(?:发布|发表于?|更新)[^0-9]{0,6}(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
RE_URL_DATE = re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/")


def _norm_date(s: str) -> str:
    """把各种日期写法归一成 ISO8601（UTC）。失败返回空串。"""
    if not s:
        return ""
    s = s.strip()
    # 2026-08-18T10:38:00 / 2026-08-18
    m = re.match(r"(20\d{2})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        # Aug. 18, 2026
        m2 = re.match(
            r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d{2})", s)
        if m2:
            mon = m2.group(1)[:3].lower()
            months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5,
                      "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
                      "nov": 11, "dec": 12}
            if mon not in months:
                return ""
            y, mo, d = int(m2.group(3)), months[mon], int(m2.group(2))
        else:
            # 2026年8月18日
            m3 = re.match(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", s)
            if not m3:
                return ""
            y, mo, d = (int(x) for x in m3.groups())
    try:
        return datetime(y, mo, d, tzinfo=timezone.utc).isoformat()
    except ValueError:
        return ""


def extract_pub_date(html: str, url: str = "") -> str:
    """从文章页提取真实发布日期。取不到返回空串（表示未知，不等于今天）。"""
    for rx in (RE_JSONLD_DATE, RE_TIME_TAG):
        for m in rx.finditer(html):
            iso = _norm_date(m.group(1))
            if iso:
                return iso
    for m in RE_PUBLISHED_TEXT.finditer(html):
        iso = _norm_date(m.group(1))
        if iso:
            return iso
    m = RE_PUBLISHED_CN.search(html)
    if m:
        iso = _norm_date("-".join(m.groups()))
        if iso:
            return iso
    if url:
        m = RE_URL_DATE.search(url)
        if m:
            iso = _norm_date("-".join(m.groups()))
            if iso:
                return iso
    return ""


def clean_title(t: str) -> str:
    """去掉标题开头的日期，并压缩空白"""
    s = re.sub(r"\s+", " ", t or "").strip()
    # 最多剥两层（有些标题连写两个日期）
    for _ in range(2):
        new = LEADING_DATE.sub("", s)
        if new == s:
            break
        s = new.strip()
    s = LEADING_INDEX.sub("", s).strip()
    return s or t


RELEVANT = re.compile(
    r"\b(deal|licens|deal(?:s|ing)?|acqui|merger|partner|collaborat|option|"
    r"upfront|milestone|trial|phase [123]|approv|FDA|NMPA|royalt|pipeline|"
    r"spin-?off|in-?licens|out-?licens|asset)\b"
    r"|交易|授权|收购|合作|许可|里程碑|临床|获批|首付款|上市申请|三期|研发|"
    r"引进|对外授权|共同开发|独家|权益|管线|适应症|新药|创新药|专利|"
    r"License-out|License-in",
    re.I,
)


async def fetch_html(client: httpx.AsyncClient, src: dict) -> list[RawItem]:
    r = await _get(client, src["url"])
    if not r:
        return []
    tree = HTMLParser(r.text)
    cfg = LIST_PATTERNS.get(src["id"])
    if not cfg:
        # 兜底：抓所有 h2/h3 里的链接
        nodes = tree.css("h2 a, h3 a")
        base = src["url"]
    else:
        nodes = tree.css(cfg["item"])
        base = cfg["base"]

    seen: set[str] = set()
    out: list[RawItem] = []
    for n in nodes[:120]:
        href = (n.attributes.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        url = urljoin(base, href)
        title = clean_title(clean_text(n.text()))
        if len(title) < 12:
            continue
        if url in seen:
            continue
        seen.add(url)
        if not RELEVANT.search(title):
            continue
        out.append(RawItem(
            id=make_id(title, url), source=src["id"], lang=src["lang"],
            title=title, url=url,
            # ⚠️ 不填 now_iso()。列表页没有日期，填抓取时刻会把
            # 几个月前的旧文伪装成当天新闻。留空表示"日期未知"，
            # 由后续的日期补全步骤去文章页取真值。
            published="",
            access=src.get("access", "headline"), mode="html",
            weight=src.get("weight", 1.0),
            topics=src.get("topics", []), fetch_time=now_iso(),
            # ⚠️ headline 级的源（stat_pharma / fierce_*_search）URL 是完整
            #    文章页，只是默认不抓。配了 fetch_body 就抓（2026-10-25）。
            fetch_body=bool(src.get("fetch_body")),
        ))
    return out


# ============ 正文抓取（access: full，或配了 fetch_body 的源）===========
async def fetch_article(client: httpx.AsyncClient, item: RawItem) -> RawItem:
    """
    抓文章正文，顺手取发布日期。

    ⚠️ 2026-10-25 修正：原来这里第一行就 `if item.access != "full" or
    item.mode != "html": return item` —— 于是
      · headline 级源（stat_pharma / fierce_*_search）**永远拿不到正文**
      · RSS 源（endpoints_rss）即使配了 fetch_body 也进不来
    实测后果：10-25 的 67 条候选里 59 条 content 是空串（88%），
    选题层因此判定"没有可写的"，而真正的原因是**正文没抓回来**。

    现在改成：源配了 `fetch_body: true` 就允许抓（哪怕它 access=headline
    或走 RSS）——它们的 URL 是完整的文章页，抓得到正文。
    """
    if item.mode != "html" and not getattr(item, "fetch_body", False):
        return item
    r = await _get(client, item.url)
    if not r:
        item.access = "headline"          # 降级
        return item
    # 顺手取真实发布日期（列表页拿不到，文章页才有）
    if not item.published:
        d = extract_pub_date(r.text, item.url)
        if d:
            item.published = d
    tree = HTMLParser(r.text)
    # 正文容器候选
    best, best_len = "", 0
    for sel in ["article", "div.article-body", "div#article-body",
                "div.content", "main", "div.article__content"]:
        for node in tree.css(sel)[:1]:
            t = clean_text(node.text())
            if len(t) > best_len:
                best, best_len = t, len(t)
    if best_len > 200:
        raw = best[:12000]
        # ⚠️ 新闻站常把自动实体标签塞在正文最前面（见 common.split_entity_tags）。
        #    留着它，成稿模型会把标签里的靶点/公司当成交易内容——实测出过事故
        #    （postmortem_b4985179）。这里剥离，标签单独存。
        body, tags = split_entity_tags(raw)
        item.entity_tags = tags
        item.content = body
    else:
        item.access = "headline"
    return item


def _edgar_eligible(title: str) -> bool:
    """
    判断这条值不值得去 SEC EDGAR 查。

    名额有限，查不到的要提前排除，否则白白占掉请求额度：
      - 必须是并购/授权类（IPO、融资轮、股价评论在 SEC 没有对应 8-K）
      - 主体得可能是美国注册公司（中文 A股/新三板 主体查不到）
    """
    t = title or ""
    # 融资类排除（本来也不算 BD）
    if re.search(r"\bIPO\b|prices?\s+\$[\d.]+\s*[MB]\b|financing|Series\s+[A-D]\b"
                 r"|上市募资|定增|融资轮|种子轮|A轮|B轮|C轮", t, re.I):
        return False
    # 必须像并购/授权
    if not re.search(r"acqui|merger|buyout|takeover|licens|option|"
                     r"collaborat|收购|并购|授权|引进", t, re.I):
        return False
    # 主体判断：标题里得有拉丁字母公司名（EDGAR 只收美国注册主体）
    if not re.search(r"[A-Za-z]{3,}", t):
        return False
    # 纯 A股/新三板 语境排除
    if re.search(r"A股|新三板|科创板|创业板|北交所|ST公司", t) and \
       not re.search(r"NASDAQ|NYSE|Nasdaq|NYSE", t):
        return False
    return True


def _enrich_priority(it: RawItem) -> tuple:
    """
    一手材料补全的优先级（越小越先补）。

    名额有限（SEC 限速），必须补最值得补的。
    实测教训：早先按列表顺序取前 12 条，结果 Viatris 那条排在
    12 名之外没补到——而它恰恰是当天唯一有 SEC 备案可查的大额并购。
    优先级排错，等于白做。

    排序依据：
      1. 整体收购 > 资产交易 > 授权（8-K 对整体收购最完整）
      2. 金额越大越优先（大额交易才有备案）
      3. 正文越少越优先（缺得越多越需要补）
    """
    t = it.title or ""
    score = 0.0
    if re.search(r"\b(?:acqui|merger|buyout|takeover)\w*\b", t, re.I):
        score += 6.0
    elif re.search(r"收购|并购|买下", t):
        score += 6.0
    if re.search(r"\b(?:licens|option|collaborat|deal)\w*\b", t, re.I):
        score += 2.5
    if re.search(r"授权|引进|合作|许可", t):
        score += 2.5
    # 金额量级
    m = re.search(r"\$\s?([\d.]+)\s*(billion|bn|million|m)\b", t, re.I)
    if m:
        v = float(m.group(1))
        score += 4.0 if m.group(2).lower() in ("billion", "bn") else 2.0
        score += min(v, 20) / 20.0
    if re.search(r"[\d.]+\s*亿\s*(?:美元|美金)", t):
        score += 3.0
    # 缺得越多越优先
    gap = 1.0 - min(len(it.content or ""), 500) / 500.0
    score += gap
    # 综述类不补（单份备案代表不了多笔交易）
    if re.search(r"\broundup\b|综述|一周|daily:", t, re.I):
        score -= 8.0
    return (-score,)


# ============ 日期补全 / 陈旧过滤 ============
# 日期补全要逐篇请求，并发太高会被源站限流。
_PROBE_SEM = asyncio.Semaphore(8)


async def _probe_date(client: httpx.AsyncClient, item: RawItem) -> bool:
    """取单条文章的真实发布日期。成功返回 True。"""
    async with _PROBE_SEM:
        r = await _get(client, item.url)
    if not r:
        return False
    d = extract_pub_date(r.text, item.url)
    if d:
        item.published = d
        return True
    return False


def _drop_stale(items: list[RawItem], max_age_days: int) -> tuple[list, int]:
    """
    丢弃发布日期过旧的条目。

    ⚠️ 只丢【已知日期且过旧】的。日期未知的保留——
    未知不等于过旧（早期实现把未知当 999 小时，误杀大量条目）。
    """
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    kept, dropped = [], 0
    for it in items:
        if not it.published:
            kept.append(it)
            continue
        try:
            dt = _dt.fromisoformat(it.published)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            if (now - dt).days > max_age_days:
                dropped += 1
                continue
        except Exception:
            pass
        kept.append(it)
    return kept, dropped


# ============ 主流程 ============
async def _run_one(client: httpx.AsyncClient, src: dict) -> list[RawItem]:
    # blocked：站点确认封爬（Cloudflare 403 等）
    # disabled：源无有效产出（地址失效/内容不对口），停用以免浪费请求
    if src.get("access") in ("blocked", "disabled"):
        print(f"  [skip] {src['id']} ({src['access']})")
        return []
    # search 模式依赖外部检索，本环境无法直接抓，留钩子给 Actions 侧实现
    if src.get("mode") == "search" or not src.get("url"):
        print(f"  [skip] {src['id']} (search fallback，Actions 侧处理)")
        return []
    try:
        if src["mode"] == "rss":
            items = await fetch_rss(client, src)
        else:
            items = await fetch_html(client, src)
        print(f"  [ok]   {src['id']}: {len(items)} 条")
        return items
    except Exception as e:
        print(f"  [err]  {src['id']}: {type(e).__name__}: {e}")
        return []


async def run(date: str | None = None) -> list[RawItem]:
    date = date or today_cst()
    ensure_dirs()
    cfg = load_yaml("sources.yaml")

    print(f"\n=== 采集 {date} ===")
    tracker_insights: dict = {}
    async with httpx.AsyncClient(timeout=TIMEOUT, http2=False) as client:
        results = await asyncio.gather(
            *[_run_one(client, s) for s in cfg["sources"]],
            return_exceptions=True
        )

        items: list[RawItem] = []
        for r in results:
            if isinstance(r, list):
                items.extend(r)

        # 噪声过滤（综合财经源收紧，医药垂直源放宽）
        items, dropped = filter_items(items)
        print(f"\n=== 噪声过滤：{len(items)+len(dropped)} -> {len(items)} 条"
              f"（丢弃 {len(dropped)}）===")

        # 抓正文：按源配额，保证中文源也有素材
        #
        # ⚠️ 2026-10-25 修正：原来是**硬编码**的 6 源 21 篇配额
        #   {"biopharmadive_deals":6, "pharnexcloud":6, ...}，
        #   结果 9 个 full 源里有 3 个（biopharmadive_home / pharmexec_*）
        #   **完全没有额度**，headline 源更是一篇都抓不到。
        #   实测 10-25：67 条候选里 59 条 content 为空（88%），
        #   选题层因此判"没得写"——而根因是正文没抓回来，不是没有交易。
        #
        # 现在：配额从 config 读（article_quota，缺省 8），
        # 且允许抓 access=headline 但配了 fetch_body 的源。
        # ⚠️ 抓取顺序按【重要性】排（_enrich_priority），不按列表顺序——
        #    名额有限时必须先抓最该写的那几条（Viatris 那次的教训）。
        quota = (cfg.get("fetch", {}) or {}).get("article_quota", {}) or {}
        body_srcs = {s["id"] for s in cfg["sources"] if s.get("fetch_body")}
        need = [i for i in items
                if (i.access == "full" and i.mode == "html")
                or i.source in body_srcs]
        need.sort(key=_enrich_priority)
        by_src: dict[str, int] = {}
        picked = []
        for it in need:
            cap = quota.get(it.source, 8)
            if by_src.get(it.source, 0) >= cap:
                continue
            by_src[it.source] = by_src.get(it.source, 0) + 1
            picked.append(it)
        if picked:
            print(f"\n=== 抓正文 {len(picked)} 篇（{len(need)} 条可抓）===")
            for it in picked[:5]:
                print(f"    优先: {it.title[:58]}")
            enriched = await asyncio.gather(
                *[fetch_article(client, i) for i in picked],
                return_exceptions=True
            )
            ok = 0
            for i, e in zip(picked, enriched):
                if isinstance(e, RawItem) and e.content:
                    i.content = e.content
                    i.access = "full"
                    ok += 1
                elif not i.summary:
                    i.access = "headline"
            print(f"  正文成功 {ok}/{len(picked)}")
            if ok < len(picked) * 0.6:
                print(f"  ⚠️ 成功率偏低（{ok}/{len(picked)}），"
                      f"多半是源站改版或选择器失效——查 fetch_article 的正文容器候选")

        # ---- 日期补全 ----
        # 列表页拿不到发布日期，必须去文章页取。不补的话，
        # BioPharma Dive 的专题归档页会把几个月前的旧文
        # 当成今天的新闻（实测把一篇 2026-08-18 的稿写成 10-11 的头条）。
        need_date = [i for i in items if not i.published
                     and i.mode == "html" and i.url]
        limit = cfg.get("fetch", {}).get("date_probe_limit", 120)
        if need_date:
            probe = need_date[:limit]
            print(f"\n=== 补全发布日期 {len(probe)} 条"
                  f"（共 {len(need_date)} 条缺日期）===")
            got = await asyncio.gather(
                *[_probe_date(client, i) for i in probe],
                return_exceptions=True)
            n_ok = sum(1 for g in got if g is True)
            print(f"  取得日期 {n_ok}/{len(probe)}")

        # ---- 陈旧内容过滤 ----
        max_age = cfg.get("fetch", {}).get("max_age_days", 21)
        if max_age:
            before = len(items)
            items, stale = _drop_stale(items, max_age)
            if stale:
                print(f"\n=== 陈旧过滤：丢弃 {stale} 条"
                      f"（发布日期超过 {max_age} 天）"
                      f"，{before} -> {len(items)} 条 ===")
            else:
                print(f"\n=== 陈旧过滤：无超过 {max_age} 天的条目 ===")

        # ---- 一手材料补全（SEC EDGAR，免费无需 API key）----
        #
        # 为什么必须做：RSS 摘要只有 200 字符。实测一篇
        # "Viatris 收购 Pacira" 的稿子写完了都不知道每股收购价是
        # $36.50、也不知道公告日是 10-08——读者根本看不出这是新闻。
        # 一手材料就在 SEC 8-K 的 Exhibit 99.1 里，全文免费公开。
        #
        # ⚠️ 补全名额有限（SEC 限速），必须按重要性排序。
        # 实测教训：早先取列表前 12 条，结果 Viatris 那条排在 12 名之外
        # 没被补到——而它恰恰是当天最需要补的。优先级排错了，
        # 等于白做。
        ed_limit = cfg.get("fetch", {}).get("edgar_limit", 12)
        thin_all = [i for i in items
                    if len(i.content or "") < 500 and i.url
                    and _edgar_eligible(i.title)]
        thin = sorted(thin_all, key=_enrich_priority)[:ed_limit]
        if thin:
            print(f"\n=== 补一手材料（SEC EDGAR）"
                  f"候选 {len(thin_all)} 条，取前 {len(thin)} 条 ===")
            for i in thin[:4]:
                print(f"    优先: {i.title[:60]}")
            try:
                from edgar import enrich_with_filing
                got = 0
                for it in thin:
                    try:
                        body, note = await enrich_with_filing(client, it.title)
                    except Exception:
                        continue
                    if body and len(body) > 500:
                        it.content = body
                        it.access = "full"
                        it.topics = list(set(it.topics + ["primary:sec-edgar"]))
                        got += 1
                        print(f"  ✓ {it.title[:44]} <- {note[:60]}")
                print(f"  取得一手材料 {got}/{len(thin)}")
            except Exception as e:
                print(f"  EDGAR 补全跳过: {type(e).__name__}: {e}")

        # ---- 结构化追踪数据库（BioPharma Dive 中国授权交易库）----
        #
        # ⚠️ 关键设计：这张表是【参考数据库】，不是新闻。
        # 实测它的数据覆盖 2025-01 ~ 2025-08，最新一笔是 2025-08-11，
        # 是一份 2025 年的快照。早期版本把它当成普通条目扔进选题池，
        # 于是几年前的旧交易被写成当天的头条。
        #
        # 现在的处理：
        #   1. 用数据库里的真实交易日期（不填抓取时刻）
        #   2. 单独存成 tracker_deals.json，供成稿时做对比参照
        #   3. 不进入评分/选题池——历史数据不该和今日新闻抢位置
        try:
            from trackers import run_all as tracker_run
            titems, insights = await tracker_run()
            if titems:
                print(f"\n=== 追踪数据库 {len(titems)} 笔结构化交易 ===")
                dated = [t for t in titems if t.published]
                if dated:
                    lo = min(t.published for t in dated)[:10]
                    hi = max(t.published for t in dated)[:10]
                    print(f"  交易日期范围 {lo} ~ {hi}")
                write_json(DATA_DIR / "tracker_deals.json",
                           [t.to_dict() for t in titems])
                print(f"  存入 data/tracker_deals.json（不进入选题池）")
                if insights:
                    tracker_insights.clear()
                    tracker_insights.update(insights)
        except Exception as e:
            print(f"  追踪数据库跳过: {type(e).__name__}: {e}")

    # ---- 第二轮：给"重要但没正文"的条目搜一手材料 ----
    #
    # 只要配了任意一个搜索 key 就自动启用，不必再单独设 SEARCH_BACKEND
    # （少配一个变量，少一处出错的地方）。
    search_keys = ("SERPER_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY")
    has_search = bool(os.getenv("SEARCH_BACKEND")) or \
        any(os.getenv(k) for k in search_keys)
    if has_search:
        print("\n=== 深度检索一手材料 ===")
        try:
            from deepen import run as deepen_run
            items = deepen_run(items, get_search_fn(), max_items=8)
        except Exception as e:
            print(f"  深度检索跳过: {type(e).__name__}: {e}")
    else:
        cands = [i for i in items
                 if len(i.content or "") <= 800
                 and looks_important(i.title)[0]]
        print(f"\n[提示] {len(cands)} 条无正文但信号强。"
              f"配置 SERPER_API_KEY / TAVILY_API_KEY / BRAVE_API_KEY "
              f"任一即可自动搜一手材料")

    write_json(RAW_DIR / f"{date}.json", [i.to_dict() for i in items])
    print(f"\n共 {len(items)} 条 -> data/raw/{date}.json")

    if tracker_insights:
        write_json(DATA_DIR / "tracker_insights.json", tracker_insights)
        print(f"  追踪页洞察 -> data/tracker_insights.json")

    # 统计
    by_access: dict[str, int] = {}
    for i in items:
        by_access[i.access] = by_access.get(i.access, 0) + 1
    print("  access 分布:", by_access)
    return items


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出特殊符号会崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    d = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run(d))