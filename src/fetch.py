"""
采集模块：医药政策雷达的抓取层。

四个通道（政策源和 BD 新闻源的结构差别很大，所以通道比模式多）：
  html    普通服务端渲染的列表页 + 文章页（NMPA / 上海药事所 / 中国政府网 / EMA / HHS）
  rss     FDA press releases（Chrome UA 会被 404 反爬页拦，必须用阅读器 UA）
  hanweb  汉王/TRS jpage 栏目（国家医保局政策法规：列表在页面内嵌 XML 里，不在 DOM 里）
  prov    省级政府站通用通道（省医保局/卫健委/药监局）：不写死 CSS，按
          "标题命中 RELEVANT + 条目祖先带日期 + href 像文章页"收口，
          页面内嵌 Hanweb <record> 时自动切换；见 fetch_prov。
  正文的三类特殊情况：CDE 的 202 挑战页、医保局的 .txt 附件、工信部的 jpaas CMS 接口
  省级站的第四类：正文类名各不相同（newsTex/newsinfo/zwxl-article/…），
  CONTENT_SELECTORS 按实测补齐，最后还有一层类名+链接密度兜底。

设计要点（都来自实测教训）：
1. 每个源声明 access_level，抓不到时降级为 headline 而不是丢弃
2. blocked 的源不发请求；412/429 是限流，隔 2 秒重试一次再判
3. UA 决定成败：fda.gov / hhs.gov 用 Chrome UA 是 404/403，换阅读器 UA 就 200
4. 同 host 限速（HOST_MIN_INTERVAL）：NMPA 连发会被 WAF 判 412
5. 单源失败不影响整体，用 try/except 隔离
6. **先过滤陈旧、再抓正文**：反过来会把正文配额烧在几周前的旧稿上
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlencode, urljoin, urlparse

import feedparser
import httpx
try:                                  # selectolax >= 1.0 用 lexbor 后端
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:                 # 旧版回退
    from selectolax.parser import HTMLParser

from common import (RawItem, clean_text, ensure_dirs, load_yaml, make_id,
                    now_iso, split_entity_tags, RAW_DIR, write_json,
                    today_cst)
from noise import NOISE_RE, filter_items
from deepen import looks_important
from search_backend import get_search_fn

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ⚠️ 阅读器 UA 不是随便写的：实测 fda.gov / hhs.gov 用 Chrome UA 是
# HTTP 404（反爬拦截页）/ 403，换成阅读器 UA 就 200。源配置里写 ua: reader
# 即按源覆盖（见 _src_headers）。判据是响应形态，不是"站点封了我们"。
UA_READER = "Feedbin feed reader/1.0"

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}
HEADERS_READER = {"User-Agent": UA_READER,
                  "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}


def _src_headers(src: dict | None) -> dict:
    """按源取请求头。src 为 None / 未配 ua 时用默认 Chrome UA。"""
    if src and src.get("ua") == "reader":
        return HEADERS_READER
    return HEADERS


# 省级站 href 里常混入零宽字符/BOM（实测某省外链带 U+200B，httpx 直接抛
# InvalidURL: Invalid IDNA hostname），统一在 urljoin 之前清掉。
RE_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00a0]")


def _clean_href(h: str | None) -> str:
    return RE_INVISIBLE.sub("", (h or "")).strip()


# 同 host 的最小请求间隔（秒）。
#
# ⚠️ 必须限速：NMPA（www.nmpa.gov.cn）的 WAF 在连续快发时会返回 HTTP 412。
# 实测同一批 10 个文章页：并发/连发 → 一片 412；间隔 0.7 秒 → 10/10 全 200。
# 412 本身不是拒绝，隔 2 秒重试通常能过（_get 里有重试），但一开始就别打太猛。
HOST_MIN_INTERVAL = 0.7
_HOST_LOCKS: dict[str, asyncio.Lock] = {}
_HOST_LAST: dict[str, float] = {}


async def _host_pace(url: str) -> None:
    """同一 host 的请求之间留间隔，避免被 WAF 判成爬虫。"""
    host = urlparse(url).netloc
    lock = _HOST_LOCKS.setdefault(host, asyncio.Lock())
    async with lock:
        loop = asyncio.get_running_loop()
        wait = _HOST_LAST.get(host, 0.0) + HOST_MIN_INTERVAL - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _HOST_LAST[host] = loop.time()


# ============ 底层抓取 ============
async def _get(client: httpx.AsyncClient, url: str,
               headers: dict | None = None) -> httpx.Response | None:
    """
    取一个 URL，只认 200。

    ⚠️ 202 要重试一次：CDE（www.cde.org.cn）是瑞数/通付盾 WAF，
    文章页第一次请求返回 202 + JS 挑战页，**同时把 WAF cookie 种进 client 的
    cookie jar**；带 cookie 重放同一请求即 200。实测：无 cookie 首请求 202，
    同 client 第二次 200 并拿到正文。不重试的话 CDE 一篇正文都抓不到。
    （202 不是标准缓存语义，这里当"挑战页"处理。）
    """
    h = headers or HEADERS
    for attempt in (1, 2):
        await _host_pace(url)          # 同 host 限速，别把政府站 WAF 打急
        try:
            r = await client.get(url, headers=h, follow_redirects=True)
        except Exception as e:
            print(f"  [fetch] {url} -> {type(e).__name__}")
            return None
        if r.status_code == 200:
            return r
        if r.status_code == 202 and attempt == 1:
            print(f"  [fetch] {url} -> 202 挑战页，带 cookie 重试")
            continue
        # 412 / 429：WAF 限流（NMPA 实测连发会短时 412，限速后自愈）。
        # 隔 2 秒重试一次，别在第一次失败上就判"站点封了"。
        if r.status_code in (412, 429) and attempt == 1:
            print(f"  [fetch] {url} -> {r.status_code} 限流，2 秒后重试")
            await asyncio.sleep(2.0)
            continue
        # 403/404 等 → 记日志但不抛错，让上层决定
        return None
    return None


# ============ RSS 通道 ============
async def fetch_rss(client: httpx.AsyncClient, src: dict) -> list[RawItem]:
    r = await _get(client, src["url"], _src_headers(src))
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
# 各站点的文章链接提取规则（按 access 分级）。
# base 用列表页自身的 URL——政府站大量用 "../../xxgk/..." 这类相对路径，
# 用站点根当 base 会拼错，用列表页 URL 才和浏览器语义一致。
#
# 全部选择器都是 2026-11-01 对着真实响应数出来的（命中条数见 src/probe_sources.py），
# 不是猜的。改选择器前先 probe，别把"选择器写错"当成"站点封爬"。
LIST_PATTERNS = {
    # ---- 中国监管 ----
    # NMPA 三个栏目同构：<div class="list"><ul><li><a title=标题 href=../../xxgk/...>
    #   <span>(2026-10-29)</span></li>
    "nmpa_ggtg": {"base": "https://www.nmpa.gov.cn/xxgk/ggtg/index.html",
                  "item": "div.list ul li a"},
    "nmpa_fgwj": {"base": "https://www.nmpa.gov.cn/xxgk/fgwj/index.html",
                  "item": "div.list ul li a"},
    "nmpa_zhcjd": {"base": "https://www.nmpa.gov.cn/xxgk/zhcjd/index.html",
                   "item": "div.list ul li a"},
    # CDE 首页滚动通知：<div class="news_roll_module_title"><a href="/main/news/...">
    #   <span>20261030</span>（日期是 8 位紧凑格式）
    "cde_news": {"base": "https://www.cde.org.cn/",
                 "item": "div.news_roll_module_title a"},
    # ---- 医保与集采 ----
    "nhsa_home": {"base": "https://www.nhsa.gov.cn/",
                  "item": "ul.localnews li a"},
    # 上海药事所：URL 自带 /2026/10/30/，列表另有 <span>2026-10-30</span>
    "smpaa_gggs": {"base": "https://www.smpaa.cn/xxgk/gggs/",
                   "item": "a[href*='/xxgk/gggs/']"},
    "smpaa_jc": {"base": "https://www.smpaa.cn/gjsdcg/",
                 "item": "a[href*='/gjsdcg/']"},
    # ---- 产业政策 ----
    # 中国政府网"最新政策"块；页面上还有"政策解读/图说"块，但只有这一块带日期
    "gov_zhengce": {"base": "https://www.gov.cn/zhengce/",
                    "item": "div.item03 div.list li a"},
    # 工信部消费品工业司：文件发布 + 工作动态两栏，正文走 CMS 接口（见 fetch_article）
    "miit_xfpg": {"base": "https://www.miit.gov.cn/jgsj/xfpgys/",
                  "item": "a[href*='/xfpgys/wjfb/'], a[href*='/xfpgys/gzdt/']"},
    # ---- 海外 ----
    "ema_news": {"base": "https://www.ema.europa.eu",
                 "item": "a[href*='/en/news/']"},
    # HHS 用 USWDS 的 collection 列表；日期在 <time datetime>
    "hhs_news": {"base": "https://www.hhs.gov",
                 "item": "li.usa-collection__item h2 a"},
}

# 标题里粘连的日期前缀（PharmExec 这类站点的列表页常见：
# "October 5th 2026Roundup: CSL & Alentis..."）。
# 不清理会污染标题、影响去重相似度。
#
# ⚠️ 但中文标题里的日期**是内容**："2026年10月27日中药品种保护受理公示"
# 每周一条，剥掉日期后所有条目标题完全相同，去重层会把不同批次当同一条。
# 所以只在"日期后面跟分隔符或拉丁字母"时才当列表前缀剥（英文站的写法），
# 中文日期后面直接跟汉字时保留。
LEADING_DATE = re.compile(
    r"^\s*(?:"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
    r"Oct|Nov|Dec)\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}"
    r"|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?"
    r")\s*(?:[·\-—|:：]\s*|(?=[A-Za-z]))",
    re.I,
)

# 列表页序号粘连："2AstraZeneca..." / "4AstraZeneca..."。
# 只剥 1-2 位数字紧跟大写字母的情况，避免误伤 "52周" "7家药企"。
LEADING_INDEX = re.compile(r"^\d{1,2}(?=[A-Z])")

# 列表页的方括号前缀：上海药事所是 "[置顶][12-31]标题"，
# 有些站是 "[图]" "[new]"。不剥会污染标题、拉低去重相似度。
LEADING_TAG = re.compile(r"^\s*(?:[\[【(（][^\]】)）]{1,12}[\]】)）]\s*)+")
# 某些列表页在标题前加项目符号（"· 标题"）
LEADING_BULLET = re.compile(r"^[·•\-—|]\s*")
# 列表页把日期拼在标题末尾（上海药事所："…名单 2025-12-31"）。
# 末尾的裸日期几乎总是列表排版产物，不是标题内容，剥掉。
TRAILING_DATE = re.compile(
    r"\s*[（(]?\s*20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\s*[)）]?\s*$")


# ============ 真实发布日期提取 ============
#
# 为什么需要：
# 列表页（尤其专题归档页）会把几个月前的旧文一直挂在上面。早期版本给所有
# 列表页条目一律填 now_iso()，于是旧文被当成当天新闻——BD 雷达实测把一篇
# 2026-08-18 发的 Leo Pharma 稿当成 2026-10-11 的新闻写了出来。
#
# 取日期的优先级（政策雷达的政府站大多在**列表页**就带日期，
# 先在列表页拿可以省掉一次文章页请求）：
#   1. 列表条目的 <span>2026-10-29</span> / "30 October 2026" / 20261030
#   2. 文章页 <meta name="PubDate">（NMPA/NHSA/CDE/工信部都有）
#   3. JSON-LD 的 datePublished / <time datetime>
#   4. 正文里的 "发布时间：2026-10-29" / "Published Aug. 18, 2026"
#   5. URL 里的 /2026/08/18/ 路径
# 全都取不到 → 返回空串（表示未知，绝不等于今天）。
RE_JSONLD_DATE = re.compile(
    r'"(?:datePublished|dateCreated|uploadDate)"\s*:\s*"([^"]+)"', re.I)
RE_TIME_TAG = re.compile(
    r'<time[^>]+datetime="([^"]+)"', re.I)
# 政府站/部委 CMS 的发布时间元信息：<meta name="PubDate" content="2026-10-29 17:36">
RE_META_DATE = re.compile(
    r'<meta[^>]+name="(?:PubDate|pubdate|PublishDate|publishdate|Pubdate|'
    r'DC\.date|date)"[^>]+content="([^"]+)"', re.I)
RE_PUBLISHED_TEXT = re.compile(
    r"(?:Published|Posted|Updated|发布|发表于|发布于|发布时间)\s*[:：]?\s*"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"\d{1,2},\s*\d{4}|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)", re.I)
# 中文页面常见 "发布日期：20261023"（CDE）与 "发布日期：2026-09-15"（医保局）
RE_PUBLISHED_CN8 = re.compile(
    r"(?:发布|发表于?|更新)[^0-9]{0,6}(20\d{2})(\d{2})(\d{2})")
RE_PUBLISHED_CN = re.compile(
    r"(?:发布|发表于?|更新)[^0-9]{0,6}(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
RE_URL_DATE = re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/")
# 紧凑日期目录：青海医保 /20261030/7f12…/c.html、部分省站 /20261030/t….
RE_URL_COMPACT_DATE = re.compile(
    r"/(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?=[/_])")

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

# 文本里的日期候选。四种形态都要认：
#   2026-10-30 / 2026/10/30 / 2026年10月30日 / 20261030 / 30 October 2026
#   / October 30, 2026
_DATE_CAND = re.compile(
    r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}日?"
    r"|20\d{2}年\d{1,2}月\d{1,2}日"
    r"|20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"
    r"|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+20\d{2}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+20\d{2}")


def _norm_date(s: str) -> str:
    """把各种日期写法归一成 ISO8601（UTC）。失败返回空串。"""
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s.strip())
    y = mo = d = 0
    # 2026-08-18T10:38:00 / 2026-08-18 / 2026/8/18 / 2026.8.18
    m = re.match(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        # 2026年8月18日
        m = re.match(r"(20\d{2})年(\d{1,2})月(\d{1,2})", s)
        if m:
            y, mo, d = (int(x) for x in m.groups())
        else:
            # 20261030（CDE 的 <meta name="PubDate" content="20261023">）
            m = re.match(r"(20\d{2})(\d{2})(\d{2})(?:\D|$)", s)
            if m:
                y, mo, d = (int(x) for x in m.groups())
            else:
                # 30 October 2026（EMA 列表的 metadata）
                m = re.match(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(20\d{2})", s)
                if m:
                    mon = m.group(2)[:3].lower()
                    if mon not in _MONTHS:
                        return ""
                    y, mo, d = int(m.group(3)), _MONTHS[mon], int(m.group(1))
                else:
                    # Aug. 18, 2026 / October 30, 2026
                    m = re.match(
                        r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d{2})", s)
                    if not m:
                        return ""
                    mon = m.group(1)[:3].lower()
                    if mon not in _MONTHS:
                        return ""
                    y, mo, d = int(m.group(3)), _MONTHS[mon], int(m.group(2))
    try:
        return datetime(y, mo, d, tzinfo=timezone.utc).isoformat()
    except ValueError:
        return ""


def _date_from_text(txt: str) -> str:
    """
    从一段文本里取发布日期：取**最后一个**能解析的日期。

    政府站的条目文本是"标题 + (2026-10-29)"，日期在末尾；
    标题里自带的日期（"2026年10月27日中药品种保护受理公示"）在前，
    取最后一个正好避开。
    """
    if not txt:
        return ""
    last = ""
    for m in _DATE_CAND.finditer(txt):
        iso = _norm_date(m.group(0))
        if iso:
            last = iso
    return last


def _date_from_ctx(node) -> str:
    """
    从列表条目的父节点取日期（列表页自带日期时用它，省一次文章页请求）。

    只往上找两层，且只在文本 <= 400 字的"条目级"容器里找——
    再往上就是整个列表容器，那里的日期不属于这一条。
    """
    p = getattr(node, "parent", None)
    for _ in range(2):
        if p is None:
            break
        t = re.sub(r"\s+", " ", p.text())
        if len(t) <= 400:
            d = _date_from_text(t)
            if d:
                return d
        p = p.parent
    return ""


def extract_pub_date(html: str, url: str = "") -> str:
    """从文章页提取真实发布日期。取不到返回空串（表示未知，不等于今天）。"""
    for rx in (RE_META_DATE, RE_JSONLD_DATE, RE_TIME_TAG):
        for m in rx.finditer(html):
            iso = _norm_date(m.group(1))
            if iso:
                return iso
    for m in RE_PUBLISHED_TEXT.finditer(html):
        iso = _norm_date(m.group(1))
        if iso:
            return iso
    for rx in (RE_PUBLISHED_CN8, RE_PUBLISHED_CN):
        m = rx.search(html)
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
        m = RE_URL_COMPACT_DATE.search(url)
        if m:
            iso = _norm_date("-".join(m.groups()))
            if iso:
                return iso
    return ""


def clean_title(t: str) -> str:
    """去掉标题开头的日期/方括号前缀，并压缩空白"""
    s = re.sub(r"\s+", " ", t or "").strip()
    # 先剥 [置顶][12-31] 这类前缀，再剥裸日期
    s = LEADING_TAG.sub("", s).strip()
    s = LEADING_BULLET.sub("", s).strip()
    # 最多剥两层（有些标题连写两个日期）
    for _ in range(2):
        new = LEADING_DATE.sub("", s)
        if new == s:
            break
        s = new.strip()
    s = LEADING_INDEX.sub("", s).strip()
    s = TRAILING_DATE.sub("", s).strip()
    return s or t


# 标题相关性过滤。
#
# ⚠️ 政策雷达的关键词表必须**换成政策口径**：BD 雷达那套
# （deal/licensing/授权/收购/首付款）放进政策源，条目标题会被全过滤掉——
# 政策标题长这样："国家药监局关于修订…说明书的公告""关于开展…集采的通知"。
#
# 设计原则：只收**医药/医疗领域的具体名词与监管流程**，不收"通知/公告/意见/
# 条例"这类文书类型词——否则 gov.cn 政策频道的每一条（事业单位登记、体育赛事）
# 都会通过，等于没过滤。
# 实测：gov.cn 政策频道 8 条里只留 1 条（《医疗康复护理扩容提升工程实施方案》），
# 工信部消费品司 15 条里留 2 条（医药工业"十五五"规划等）。
RELEVANT = re.compile(
    # ---- 中文：监管 / 医保 / 集采 / 产业政策 ----
    r"药品|药物|医药|医疗|医保|药监|药审|药械|器械|耗材|疫苗|中药|中成药|"
    r"饮片|生物药|生物制品|创新药|仿制药|化学药|制剂|基因|细胞治疗|"
    r"处方药|非处方药|说明书|不良反应|召回|集采|带量采购|国谈|医保目录|"
    r"挂网|招采|DRG|DIP|按病种|审评|审批|注册|备案|合规|临床|"
    r"指导原则|一致性评价|优先审评|附条件|短缺药|孤儿药|罕见病|"
    r"GMP|GSP|追溯|药品管理法|关税|出口|进口|口岸|通关|"
    # ---- 英文：监管与政策（海外源）----
    r"\b(?:drugs?|medicines?|medicinal|pharmaceuticals?|pharma|vaccines?|"
    r"therap(?:y|ies|eutic)|clinical|trial|approv\w*|authoris\w*|authoriz\w*|"
    r"recommends?|regulat\w*|guidelines?|consultation|committee|CHMP|PRAC|"
    r"safety|recalls?|labels?|labell?ing|generic|biosimilars?|orphan|"
    r"pricing|reimburs\w*|coverage|medicare|medicaid|prescription|"
    r"FDA|EMA|HHS|CMS|NMPA|CDE)\b",
    re.I,
)


async def fetch_html(client: httpx.AsyncClient, src: dict) -> list[RawItem]:
    r = await _get(client, src["url"], _src_headers(src))
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
        # 优先用 <a title="...">：政府站的锚文本常把日期/省略号/"[置顶]"粘进来
        # （上海药事所："[置顶][12-31]上海市…名单"），title 属性是干净的标题。
        attr_title = clean_text(n.attributes.get("title") or "")
        text_title = clean_title(clean_text(n.text()))
        title = clean_title(attr_title) if len(attr_title) >= 8 else text_title
        if len(title) < 12:
            continue
        if url in seen:
            continue
        seen.add(url)
        if not RELEVANT.search(title):
            continue
        # ⚠️ 日期优先级：URL 里的 /2026/10/19/ **高于**列表里的 <span>。
        # 实测国家医保局首页的"地方动态"块给整块条目统一打上"更新日"
        # （都是 2026-10-28），而文章页自己写的是"日期：2026-10-19"、
        # URL 也是 /2026/10/19/——列表日期在这里是"上架日"，不是发布日。
        # 对 NMPA / 上海药事所 / 医保局政策法规，两者本来就一致，取 URL 无副作用；
        # 对 gov.cn / 工信部 / CDE / EMA / HHS 这类 URL 不带日期的，退回列表日期。
        published = extract_pub_date("", url) or _date_from_ctx(n)
        out.append(RawItem(
            id=make_id(title, url), source=src["id"], lang=src["lang"],
            title=title, url=url,
            # ⚠️ 不再一律 now_iso()。先用 URL 日期或列表条目自带的日期，
            # 取不到就留空表示"日期未知"——由后面的日期补全去文章页取真值。
            # 用抓取时刻冒充发布日期，会把几个月前的旧文伪装成当天新闻。
            published=published,
            access=src.get("access", "headline"), mode="html",
            weight=src.get("weight", 1.0),
            topics=src.get("topics", []), fetch_time=now_iso(),
            # ⚠️ headline 级的源（正文需订阅/JS 渲染）URL 仍是完整文章页，
            #    配了 fetch_body 就可以抓（2026-10-25 的教训：不抓 = 全是空素材）。
            fetch_body=bool(src.get("fetch_body")),
        ))
    return out


# ============ Hanweb/TRS 栏目页（列表在内嵌 XML 里）============
#
# 政府站大量用汉王/TRS 的 jpage：栏目页 HTML 里的列表**不在 DOM 里**，
# 而是嵌在 <script type="text/xml"><datastore><record><![CDATA[<li>…]]></record>
# 的首屏数据中，翻页才走 dataproxy.jsp。用普通 CSS 选择器抓这个页面，
# 会得到 0 条——实测国家医保局政策法规页正是如此（32KB 页面、1 个 <a>）。
RE_HANWEB_RECORD = re.compile(
    r"<record>\s*<!\[CDATA\[(.*?)\]\]>\s*</record>", re.S)
RE_HANWEB_PARAM = re.compile(r"param_\w+\s*=\s*\{(.*?)\}\s*;", re.S)


def _hanweb_params(html: str) -> dict:
    """从页面 JS 的 param_xxx = {...} 里取 dataproxy 需要的参数。"""
    m = RE_HANWEB_PARAM.search(html)
    if not m:
        return {}
    body = m.group(1)
    out: dict[str, str] = {}
    for k, v in re.findall(r"(\w+)\s*:\s*'([^']*)'", body):
        out[k] = v
    for k, v in re.findall(r"(\w+)\s*:\s*(\d+)\b", body):
        out.setdefault(k, v)
    return out


async def fetch_hanweb(client: httpx.AsyncClient, src: dict) -> list[RawItem]:
    """
    抓 Hanweb jpage 栏目。

    实测（国家医保局 政策法规 col104）：页面 200，内嵌 45 条记录，最新在前；
    每条是一个 <li>：<span>索引号</span><span><a href="/art/…" title="标题">…
    <span>发文字号</span><span>2026-09-15</span>。
    内嵌记录为空时，POST dataproxy.jsp 兜底（参数从页面 JS 的 param_xxx 取）。
    """
    h = _src_headers(src)
    r = await _get(client, src["url"], h)
    if not r:
        return []
    html = r.text
    records = RE_HANWEB_RECORD.findall(html)
    if not records:
        params = _hanweb_params(html)
        if params:
            form = dict(params)
            form.update({"page": "1", "perpage": "15",
                         "startrecord": "1", "endrecord": "45"})
            proxy = urljoin(src["url"], "/module/web/jpage/dataproxy.jsp?page=1")
            try:
                resp = await client.post(proxy, data=form, headers=h,
                                         follow_redirects=True)
                if resp.status_code == 200:
                    records = RE_HANWEB_RECORD.findall(resp.text)
            except Exception as e:
                print(f"  [hanweb] {src['id']} dataproxy -> {type(e).__name__}")
        if not records:
            print(f"  [hanweb] {src['id']}: 页面既无内嵌记录、dataproxy 也取不到")
            return []

    return _parse_hanweb_records(records, src)


def _parse_hanweb_records(records: list[str], src: dict) -> list[RawItem]:
    """把 Hanweb <record> 里的 <li> 片段解析成 RawItem（hanweb / prov 共用）。"""
    out: list[RawItem] = []
    seen: set[str] = set()
    for rec in records[:120]:
        rt = HTMLParser(rec)
        anchor = None
        for a in rt.css("a[href]"):
            txt = clean_text(a.text())
            if len(txt) >= 8 or (a.attributes.get("title") or ""):
                anchor = a
                break
        if anchor is None:
            continue
        href = _clean_href(anchor.attributes.get("href"))
        if not href or href.startswith("javascript") or href.startswith("#"):
            continue
        url = urljoin(src["url"], href)
        attr_title = clean_text(anchor.attributes.get("title") or "")
        title = clean_title(attr_title if len(attr_title) >= 8
                            else clean_text(anchor.text()))
        if len(title) < 12 or url in seen or not RELEVANT.search(title):
            continue
        seen.add(url)
        rec_text = clean_text(rec)
        out.append(RawItem(
            id=make_id(title, url), source=src["id"], lang=src["lang"],
            title=title, url=url,
            # 记录里同时有"索引号 / 发文字号 / 发布日期"，取最后一个可解析日期
            # （发布日期），取不到再退回 URL 里的 /2026/9/15/
            published=_date_from_text(rec_text) or extract_pub_date("", url),
            access=src.get("access", "full"), mode="html",
            weight=src.get("weight", 1.0), topics=src.get("topics", []),
            fetch_time=now_iso(), fetch_body=bool(src.get("fetch_body")),
        ))
    return out


# ============ 省级政府站通用通道（prov）============
#
# 为什么单独加一个通道：省级医保局/卫健委/药监局的列表模板比国家部委碎得多，
# 同一个 CMS 在不同省会换 DOM 类和路径（有的是 li.text_list、有的是 ul.det、
# 有的是 div.news-list），一个源写死一个 CSS 选择器，改版即失效。
#
# 这里改用"政府站列表的共同特征"收口（2026-11-01 对 50 个省级栏目实测）：
#   1. 标题命中 RELEVANT（政策/医药关键词）；
#   2. 条目祖先（最多 3 层）里有日期，或 URL 自带 /20xx/ 日期段，
#      或 href 是文章路径（/art/、/content/N）——用来排除导航链接；
#   3. href 像文章页，排除栏目首页 / 站外导航。
# 需要更严的源可在 sources.yaml 里用 item: <CSS> 覆盖（值是 a 或包含 a 的 li）。
#
# 页面里嵌 Hanweb <record> 的（江苏/江西医保）先走内嵌记录解析，与 hanweb 通道一致。
RE_ARTICLE_HREF = re.compile(
    r"\.(?:s?html?|jsp)(?:\?|$)|/(?:content|art|detail)/\d|/20\d{2}[-/]|"
    r"/20\d{2}(?:0[1-9]|1[0-2])\d{2}/|/t\d{8}\.",
    re.I)
# URL 里出现"强文章特征"时，不再要求列表条目里有日期（部分省级站列表只给标题）：
#   /art/、/content/123、/2026/10/、/t20261031_、58319191.html、/2026102708.../
RE_STRONG_ARTICLE = re.compile(
    r"/art/|/content/\d+|/20\d{2}[-/]\d|/20\d{2}(?:0[1-9]|1[0-2])\d{2}/|"
    r"/t\d{6,}[_.]|/\d{5,}\.s?html?$|/\d{8,}/", re.I)


def _list_date(node) -> str:
    """列表条目日期：沿链接向上最多 3 层，取最近一个带日期的条目容器。"""
    p = node.parent
    for _ in range(3):
        if p is None:
            break
        t = re.sub(r"\s+", " ", p.text())
        if len(t) <= 350:
            d = _date_from_text(t)
            if d:
                return d
        p = p.parent
    return ""


async def fetch_prov(client: httpx.AsyncClient, src: dict) -> list[RawItem]:
    h = _src_headers(src)
    r = await _get(client, src["url"], h)
    if not r:
        return []
    html = r.text
    records = RE_HANWEB_RECORD.findall(html)
    if len(records) >= 3:                 # 栏目页是 Hanweb jpage 时直接解析内嵌记录
        return _parse_hanweb_records(records, src)

    tree = HTMLParser(html)
    cfg = LIST_PATTERNS.get(src["id"]) or {}
    sel = src.get("item") or cfg.get("item")
    nodes = tree.css(sel) if sel else tree.css("a[href]")
    base = src.get("base") or cfg.get("base") or src["url"]

    out: list[RawItem] = []
    seen: set[str] = set()
    # 省级站有些栏目页会把整站栏目树都渲染进来（广西医保一页 756 个 li），
    # 用 max_items 截断 DOM 顺序靠前的真实列表，避免把侧栏/其他栏目一起收进来。
    max_items = int(src.get("max_items") or 120)
    for n in nodes[:400]:
        if len(out) >= max_items:
            break
        if n.tag != "a":                  # item 选择器可能命中 li
            a = n.css_first("a[href]")
            if a is None:
                continue
            n = a
        href = _clean_href(n.attributes.get("href"))
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        url = urljoin(base, href)
        if not url.startswith(("http://", "https://")):
            continue
        # 优先用 <a title>：省级站锚文本常把日期/省略号粘进标题
        attr_title = clean_text(n.attributes.get("title") or "")
        text_title = clean_title(clean_text(n.text()))
        title = clean_title(attr_title) if len(attr_title) >= 8 else text_title
        if len(title) < 12 or url in seen:
            continue
        if not RELEVANT.search(title):
            continue
        if not RE_ARTICLE_HREF.search(url):
            continue
        published = extract_pub_date("", url) or _list_date(n)
        # 既没有列表日期、URL 里也没有 /20xx/ 或 /art/ 这类文章特征 → 是导航链接
        if not published and not RE_STRONG_ARTICLE.search(url):
            continue
        seen.add(url)
        out.append(RawItem(
            id=make_id(title, url), source=src["id"], lang=src["lang"],
            title=title, url=url, published=published,
            access=src.get("access", "full"), mode="html",
            weight=src.get("weight", 1.0), topics=src.get("topics", []),
            fetch_time=now_iso(), fetch_body=bool(src.get("fetch_body")),
        ))
    return out


# ============ 正文抓取（access: full，或配了 fetch_body 的源）===========
# 正文容器候选（按优先级尝试，命中第一个 > 200 字的就停）。
#
# ⚠️ 政策源的正文容器和新闻站完全不同，按 2026-11-01 的实测补：
#   国家药监局     div.text（公告正文 400-800 字）
#   CDE            div.news_detail_box
#   国家医保局     div.atricle（#c，含元数据；真实正文在 .txt 附件里，见下）
#   上海药事所     div#content（content_con_cen docContent）
#   中国政府网     div.pages_content / #UCAP-CONTENT
#   工信部         正文由 jpaas CMS 接口注入（_jpaas_body）
#   HHS            div.content
#   EMA            article
#
# 顺序即优先级：`main` 放最后——它会连导航一起吞（HHS 实测 main 9,572 字，
# 而真正的正文 div.content 5,183 字），放前面会污染正文。
#
# ⚠️ 2026-11-01 补充省级站模板（全部对着真实文章页实测命中）：
#   河北医保/药监  div.news-info
#   河南医保/药监  div.newsinfo / #mainCon
#   重庆医保      div.zwxl-article
#   贵州医保      div.Article_Con
#   云南医保      div.article-overview-wrapper
#   新疆医保      div.article_txt
#   山西卫健      div.newsTex / div.cont
#   河南卫健      div.artibody
#   重庆卫健      div.trs_editor_view（注意不是 TRS_Editor）
#   贵州卫健      div.DocTextBox
#   四川药监      td.articlecontent3（老式表格布局）
# 这些放在通用 div.content / main 之前，避免被上层容器（含导航）抢先命中。
CONTENT_SELECTORS = [
    "article",
    "div.news_detail_box",
    "div.pages_content",
    "#UCAP-CONTENT",
    "div.article-body", "div#article-body", "div.article__content",
    # ---- 省级站实测容器 ----
    "div.newsTex", "div.news-info", "div.cont",
    "div.newsinfo", "#mainCon",
    "div.zwxl-article", "div.Article_Con", "div.article-overview-wrapper",
    "div.artibody", "div.trs_editor_view", "div.DocTextBox",
    "td.articlecontent3", "div.article_txt",
    # ---- 通用容器 ----
    "div.atricle",
    "div#zoom", "div.TRS_Editor",
    # div.text 是 NMPA 的正文容器，必须排在 div.content 之前（NMPA 页面里
    # div.content 是外层大容器，会让正文混进导航/相关链接）
    "div.text",
    "div.content", "div.detail", "div.article",
    "div#content",
    "main",
]

# 省级站最后一层兜底：类名/ID 含正文语义词、链接密度低、有中文句读的容器。
# 只在上面所有选择器都 <200 字时启用（见 _generic_body），不会覆盖已知模板。
RE_BODY_ATTR = re.compile(
    r"(content|article|detail|news|text|zoom|editor|doc|con|nr|wz|body)", re.I)


def _generic_body(tree) -> str:
    """
    正文兜底提取：挑"像正文"的最大容器。

    判据（缺一不可）：
      · 文本 250-15000 字（太短是标题/摘要，太长是整页）
      · class/id 含 content/article/detail/news/... 这类语义词
      · 链接文本占全文 < 25%（排除导航/列表页）
      · 至少两处中文句读（排除表格/名单）
    """
    best, best_len = "", 0
    for n in tree.css("div,article,section,td"):
        t = clean_text(n.text())
        ln = len(t)
        if ln < 250 or ln > 15000 or ln <= best_len:
            continue
        attr = f"{n.attributes.get('class') or ''} {n.attributes.get('id') or ''}"
        if not RE_BODY_ATTR.search(attr):
            continue
        link_len = sum(len(clean_text(a.text())) for a in n.css("a"))
        if link_len / max(ln, 1) > 0.25:
            continue
        if t.count("。") < 2 and "；" not in t:
            continue
        best, best_len = t, ln
    return best

# 政策文件正文常做成附件（.txt / downfile.jsp）。医保局的政策法规文章页
# 只有元数据表 + "文件下载链接：xxx.txt"，直接抓正文只有 1,100 字的元数据，
# 成稿模型会把"索引号/发文字号"当成政策内容——必须去取附件全文。
RE_TXT_ATTACH = re.compile(
    r'<a[^>]+href="([^"]+(?:\.txt|downfile\.jsp)[^"]*)"[^>]*>', re.I)

# 政府站正文开头的 CMS 页眉，形如：
#   "视力保护色： <标题> 日期：2026-10-19 访问次数： 字号：[ 大 中 小 ] 近日，…"
# 这不是正文，留在素材里会让成稿模型把"视力保护色""字号"当成内容。
RE_CMS_HEAD = re.compile(
    r"^\s*(?:视力保护色[:：].*?)?字号[:：]\s*\[\s*大\s*中\s*小\s*\]\s*",
    re.S)


async def _attachment_text(client: httpx.AsyncClient, html: str,
                           base_url: str, headers: dict) -> str:
    """取页面里第一个 .txt / downfile.jsp 附件的正文（取不到返回空串）。"""
    seen: set[str] = set()
    for m in RE_TXT_ATTACH.finditer(html):
        url = urljoin(base_url, m.group(1))
        if url in seen:
            continue
        seen.add(url)
        r = await _get(client, url, headers)
        if not r:
            continue
        # 附件编码不定：医保局的 downfile.jsp 声明 gb2312，实际是 UTF-8+BOM。
        txt = ""
        for enc in ("utf-8-sig", "gb18030", "utf-8"):
            try:
                txt = r.content.decode(enc)
                break
            except Exception:
                txt = ""
        txt = clean_text(txt)
        if len(txt) > 200:
            return txt[:12000]
    return ""


# 工信部（jpaas CMS）：文章页 HTML 是个壳，正文由
# /api-gateway/jpaas-publish-server/front/page/build/unit 接口注入。
# 页面脚本里带的 tagId 常是空壳（实测 "文章正文2" 只返回一段脚本），
# 用 "文章正文" 才拿到真正的 602 字。
RE_JPAAS_QUERY = re.compile(r'queryData="([^"]+)"')
JPAAS_API = "/api-gateway/jpaas-publish-server/front/page/build/unit"


async def _jpaas_body(client: httpx.AsyncClient, html: str,
                      headers: dict, page_url: str) -> str:
    if "jpaas-publish-server" not in html:
        return ""
    m = RE_JPAAS_QUERY.search(html)
    if not m:
        return ""
    try:
        q = json.loads(unescape(m.group(1)))
    except Exception:
        return ""
    if not isinstance(q, dict):
        return ""
    q["tagId"] = "文章正文"
    api = urljoin(page_url, JPAAS_API)
    try:
        r = await _get(client, api + "?" + urlencode(q), headers)
        if not r:
            return ""
        unit_html = (r.json().get("data") or {}).get("html") or ""
    except Exception as e:
        print(f"  [jpaas] {type(e).__name__}: {e}")
        return ""
    if not unit_html:
        return ""
    try:
        nodes = HTMLParser(unit_html).css("body")
        txt = clean_text(nodes[0].text()) if nodes else clean_text(unit_html)
    except Exception:
        txt = clean_text(unit_html)
    return txt


async def fetch_article(client: httpx.AsyncClient, item: RawItem,
                        headers: dict | None = None) -> RawItem:
    """
    抓文章正文，顺手取发布日期。

    ⚠️ 2026-10-25 修正：原来这里第一行就 `if item.access != "full" or
    item.mode != "html": return item` —— 于是 headline 级源与 RSS 源
    **永远拿不到正文**。实测某天 67 条候选里 59 条 content 是空串（88%），
    选题层因此判定"没有可写的"，而真正的原因是**正文没抓回来**。
    现在：源配了 `fetch_body: true` 就允许抓（哪怕 access=headline 或 RSS）。

    headers 由调用方按源传入（FDA/HHS 必须用阅读器 UA，见 sources.yaml）。
    """
    if item.mode != "html" and not getattr(item, "fetch_body", False):
        return item
    h = headers or HEADERS
    r = await _get(client, item.url, h)
    if not r:
        item.access = "headline"          # 降级
        return item
    # 顺手取真实发布日期（列表页拿不到时才有用）
    if not item.published:
        d = extract_pub_date(r.text, item.url)
        if d:
            item.published = d
    tree = HTMLParser(r.text)
    best, best_len = "", 0
    for sel in CONTENT_SELECTORS:
        nodes = tree.css(sel)
        if not nodes:
            continue
        node = nodes[0]
        # EMA 之类站点的正文容器里嵌着分享配置脚本，selectolax 的 .text()
        # 会把脚本内容也带进正文（实测混进 {"service":"share",...}）。
        for bad in node.css("script, style, noscript"):
            bad.decompose()
        t = clean_text(node.text())
        if len(t) > best_len:
            best, best_len = t, len(t)
        if best_len > 200:
            break
    # 正文由 JS 注入的 CMS（工信部 jpaas）
    if best_len <= 200:
        jb = await _jpaas_body(client, r.text, h, item.url)
        if len(jb) > best_len:
            best, best_len = jb, len(jb)
    # 正文在附件里的（国家医保局政策文件）：附件比页面正文更长时用附件
    if best_len <= 3000:
        att = await _attachment_text(client, r.text, item.url, h)
        if len(att) > best_len:
            best, best_len = att, len(att)
    # 省级站模板不在候选表里时的最后一层兜底（仅在什么都没取到时启用）
    if best_len <= 200:
        gb = _generic_body(tree)
        if len(gb) > best_len:
            best, best_len = gb, len(gb)
    # 剥掉政府站正文开头的 CMS 页眉（"视力保护色…字号：[大中小]"）
    m = RE_CMS_HEAD.match(best)
    if m and m.end() <= 300:
        best = best[m.end():]
        best_len = len(best)
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


def _enrich_priority(it: RawItem) -> tuple:
    """
    正文抓取优先级（配额有限，先抓最该写的那几条）。

    政策雷达的判据和 BD 雷达不同：不看金额，看**政策层级与规则变化**
    ——规划/管理办法/征求意见/集采规则 > 单条公告 > 培训招聘类。
    另按日期新鲜度、源权重微调。
    """
    t = it.title or ""
    score = 0.0
    for kw, w in ((r"规划", 3.0),
                  (r"管理办法|管理规定|条例|暂行办法|实施细则", 3.0),
                  (r"指导意见|实施意见|若干意见|工作方案", 2.5),
                  (r"公开征求|征求意见", 2.2),
                  (r"指导原则|技术指导|指南|技术规范", 2.0),
                  (r"集采|带量采购|集中采购|挂网|招标", 2.5),
                  (r"医保目录|药品目录|支付标准|DRG|DIP|按病种", 2.5),
                  (r"审评|审批|注册|附条件|优先审评|一致性评价", 2.0),
                  (r"价格|降价|谈判", 1.5),
                  (r"公告|通告|通知", 0.5)):
        if re.search(kw, t, re.I):
            score += w
    # 越新越优先
    if it.published:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(it.published)).days
            score += max(0.0, 3.0 - age * 0.15)
        except Exception:
            pass
    score += (it.weight or 1.0) * 0.5
    # 已经没正文的优先补
    score += 1.0 - min(len(it.content or ""), 500) / 500.0
    # 事务性内容降权（公示/招聘/培训/活动），它们不是政策变化
    if re.search(r"招聘|公示|培训|报名|座谈会|活动|工作动态", t):
        score -= 3.0
    return (-score,)


# ============ 日期补全 / 陈旧过滤 ============
# 日期补全要逐篇请求，并发太高会被源站限流。
_PROBE_SEM = asyncio.Semaphore(8)


async def _probe_date(client: httpx.AsyncClient, item: RawItem,
                      headers: dict | None = None) -> bool:
    """取单条文章的真实发布日期。成功返回 True。"""
    async with _PROBE_SEM:
        r = await _get(client, item.url, headers)
    if not r:
        return False
    d = extract_pub_date(r.text, item.url)
    if d:
        item.published = d
        return True
    return False


def _drop_stale(items: list[RawItem], max_age_days: int,
                per_source: dict[str, int] | None = None) -> tuple[list, int]:
    """
    丢弃发布日期过旧的条目。

    ⚠️ 只丢【已知日期且过旧】的。日期未知的保留——
    未知不等于过旧（早期实现把未知当 999 小时，误杀大量条目）。

    per_source：按源覆盖窗口。政策类栏目更新很慢（医保局政策法规最新一条
    可能是 6 周前、工信部医药文件是 6 周甚至更久），给它们更长的窗口，
    否则天天判"0 条"——那是源的节奏，不是抓取失败。
    """
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    per_source = per_source or {}
    kept, dropped = [], 0
    for it in items:
        if not it.published:
            kept.append(it)
            continue
        limit = per_source.get(it.source, max_age_days)
        try:
            dt = _dt.fromisoformat(it.published)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            if (now - dt).days > limit:
                dropped += 1
                continue
        except Exception:
            pass
        kept.append(it)
    return kept, dropped


# ============ 主流程 ============
def noise_filter(items: list[RawItem],
                 prov_sources: set[str]) -> tuple[list, list]:
    """
    噪声过滤（省级源走单独口径）。

    ⚠️ 为什么省级源不能直接走 noise.filter_items：
    noise.py 的 LOCAL_BODY_RE 会把"地方机关 + 事务性文种"判成噪声
    （"河北省医疗保障局关于…的通知" 命中 `省` + `医疗保障局` + `通知`），
    这规则对**全国**雷达是对的——地方事务性文件确实不值得写；
    但对本轮新加的省级雷达，这些标题正是要收的一手材料。
    noise.py 不在本任务可改范围，所以在调用侧按源分流：
      · topics 含 province 的源：只过黑名单 NOISE_RE（软文/招聘/会议/人事）
      · 其他源：原样走 filter_items（行为不变）
    """
    keep, rest = [], []
    for it in items:
        if (it.source in prov_sources and len(it.title or "") >= 10
                and not NOISE_RE.search(it.title)):
            keep.append(it)
        else:
            rest.append(it)
    rest, dropped = filter_items(rest)
    return keep + rest, dropped


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
        mode = src["mode"]
        if mode == "rss":
            items = await fetch_rss(client, src)
        elif mode == "hanweb":
            items = await fetch_hanweb(client, src)
        elif mode == "prov":
            items = await fetch_prov(client, src)
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
    async with httpx.AsyncClient(timeout=TIMEOUT, http2=False) as client:
        results = await asyncio.gather(
            *[_run_one(client, s) for s in cfg["sources"]],
            return_exceptions=True
        )

        items: list[RawItem] = []
        for r in results:
            if isinstance(r, list):
                items.extend(r)

        # 噪声过滤（综合财经源收紧；省级源只过黑名单，见 noise_filter）
        prov_srcs = {s["id"] for s in cfg["sources"]
                     if "province" in (s.get("topics") or [])}
        items, dropped = noise_filter(items, prov_srcs)
        print(f"\n=== 噪声过滤：{len(items)+len(dropped)} -> {len(items)} 条"
              f"（丢弃 {len(dropped)}）===")

        # 按源准备请求头（FDA/HHS 必须用阅读器 UA，见 sources.yaml 的 ua: reader）
        hdr = {s["id"]: _src_headers(s) for s in cfg["sources"]}

        # ---- 日期补全 ----
        # 列表页拿不到发布日期时才去文章页取。不补的话，专题归档页
        # 会把几个月前的旧文当成今天的新闻（BD 雷达实测把 2026-08-18
        # 的稿写成 10-11 的头条）。政策雷达的大部分源在列表页就有日期，
        # 所以这一步通常只剩几十条。
        need_date = [i for i in items if not i.published
                     and i.mode == "html" and i.url]
        limit = cfg.get("fetch", {}).get("date_probe_limit", 120)
        if need_date:
            probe = need_date[:limit]
            print(f"\n=== 补全发布日期 {len(probe)} 条"
                  f"（共 {len(need_date)} 条缺日期）===")
            got = await asyncio.gather(
                *[_probe_date(client, i, hdr.get(i.source)) for i in probe],
                return_exceptions=True)
            n_ok = sum(1 for g in got if g is True)
            print(f"  取得日期 {n_ok}/{len(probe)}")

        # ---- 陈旧内容过滤（按源窗口）----
        # 政策栏目更新慢：医保局政策法规、NMPA 政策解读、工信部文件可能
        # 一两个月才出一条，源配置里的 max_age_days 会覆盖默认窗口。
        #
        # ⚠️ 顺序很重要：**先**过滤陈旧，**再**抓正文。
        # 反过来会把宝贵配额烧在几周前的旧稿上（实测第一次跑：75 篇正文
        # 里一大半随后被陈旧过滤丢掉，最后只剩 43 条有正文）。
        max_age = cfg.get("fetch", {}).get("max_age_days", 45)
        per_age = {s["id"]: int(s["max_age_days"]) for s in cfg["sources"]
                   if s.get("max_age_days")}
        if max_age or per_age:
            before = len(items)
            items, stale = _drop_stale(items, max_age, per_age)
            if stale:
                print(f"\n=== 陈旧过滤：丢弃 {stale} 条"
                      f"（默认窗口 {max_age} 天，政策源按源放宽）"
                      f"，{before} -> {len(items)} 条 ===")
            else:
                print(f"\n=== 陈旧过滤：无超窗口条目 ===")

        # ---- 抓正文：按源配额，保证每个源都有素材 ----
        #
        # ⚠️ 2026-10-25 修正（BD 雷达）：原来是**硬编码**的 6 源 21 篇配额，
        #   结果 9 个 full 源里有 3 个完全没有额度，headline 源一篇都抓不到。
        #   实测那天 67 条候选里 59 条 content 为空（88%），选题层因此判
        #   "没得写"——而根因是正文没抓回来，不是没有交易。
        #
        # 现在：配额从 config 读（article_quota，缺省 8），
        # 且允许抓 access=headline 但配了 fetch_body 的源。
        # 抓取顺序按【重要性】排（_enrich_priority），不按列表顺序——
        # 名额有限时必须先抓最该写的那几条。
        quota = (cfg.get("fetch", {}) or {}).get("article_quota", {}) or {}
        body_srcs = {s["id"] for s in cfg["sources"] if s.get("fetch_body")}
        need = [i for i in items
                if (i.access in ("full", "rss") and i.mode in ("html", "rss"))
                or i.source in body_srcs]
        need.sort(key=_enrich_priority)
        by_src: dict[str, int] = {}
        picked = []
        for it in need:
            cap = quota.get(it.source, 5)
            if by_src.get(it.source, 0) >= cap:
                continue
            by_src[it.source] = by_src.get(it.source, 0) + 1
            picked.append(it)
        if picked:
            print(f"\n=== 抓正文 {len(picked)} 篇（{len(need)} 条可抓）===")
            for it in picked[:5]:
                print(f"    优先: {it.title[:58]}")
            # 省级源把候选面从 ~10 条/源 拉到 ~50 条/源，一次性 gather 全部
            # 文章页就是几百个并发请求——政府站 WAF 会直接 412。
            # 全局限到 12 并发（host 级还有 0.7s 间隔）。
            sem = asyncio.Semaphore(12)

            async def _one(it):
                async with sem:
                    return await fetch_article(client, it, hdr.get(it.source))

            enriched = await asyncio.gather(
                *[_one(i) for i in picked],
                return_exceptions=True
            )
            ok = 0
            for i, e in zip(picked, enriched):
                if isinstance(e, RawItem) and e.content:
                    i.content = e.content
                    i.entity_tags = e.entity_tags
                    i.access = "full"
                    ok += 1
                elif not i.summary:
                    i.access = "headline"
            print(f"  正文成功 {ok}/{len(picked)}")
            if ok < len(picked) * 0.6:
                print(f"  ⚠️ 成功率偏低（{ok}/{len(picked)}），"
                      f"多半是源站改版或选择器失效——查 fetch_article 的正文容器候选")
        # ⚠️ 政策雷达不需要 SEC EDGAR 补全（那是 BD 雷达为并购/授权公告做的
        #    8-K Exhibit 99.1 抓取），也不需要 BioPharma Dive 的交易台账
        #    （trackers.py）。两处调用都已移除：政策源的一手材料就是
        #    政府站原文本身，正文抓取（含 .txt 附件、jpaas 接口）已覆盖。

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