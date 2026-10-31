"""
SEC EDGAR 一手材料查询（免费，无需 API key）。

为什么需要：
RSS 摘要只有 200 字符，写出来的稿子缺关键事实——比如
"Viatris 收购 Pacira" 一篇里没有每股收购价（实际 $36.50/股），
也没有交易日期。读者看不出这是新闻，更看不出值不值得看。

一手材料在 SEC 备案里：
  8-K 的 Exhibit 99.1 就是完整的并购公告，含每股价格、
  交易结构、标的财务、交割条件、顾问名单。

EDGAR 全文检索接口（免费公开，无需 key）：
  https://efts.sec.gov/LATEST/search-index?q=<词>&forms=8-K&startdt=&enddt=

⚠️ SEC 要求 User-Agent 带联系方式，且限速 10 请求/秒。
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:  # pragma: no cover
    from selectolax.parser import HTMLParser

# SEC 要求可识别的 UA
UA = "pharma-bd-radar/1.0 (research; contact: radar@example.com)"
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/html"}

SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"

# 从标题里抽公司名（用于 EDGAR 检索）
STOP = {"the", "a", "an", "to", "for", "of", "in", "on", "with", "and",
        "inc", "corp", "co", "ltd", "plc", "pharma", "pharmaceutical",
        "pharmaceuticals", "therapeutics", "biosciences", "bio"}


def guess_companies(title: str, limit: int = 2) -> list[str]:
    """从标题里猜可能的美国上市公司名。EDGAR 只收美国注册主体。"""
    caps = re.findall(r"\b([A-Z][A-Za-z&.\-]{2,}(?:\s+[A-Z][A-Za-z&.\-]{2,}){0,2})\b",
                      title or "")
    out, seen = [], set()
    for c in caps:
        base = c.split()[0].lower()
        if base in STOP or c.lower() in STOP:
            continue
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
        if len(out) >= limit:
            break
    return out


async def search_filings(client: httpx.AsyncClient, query: str,
                         form: str = "8-K", days: int = 30) -> list[dict]:
    """
    全文检索 EDGAR，返回最近的相关备案。
    每条含 {company, form, filed, url, doc}
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    params = {
        "q": f'"{query}"',
        "forms": form,
        "startdt": start.isoformat(),
        "enddt": end.isoformat(),
    }
    try:
        r = await client.get(SEARCH_URL, params=params, headers=HEADERS,
                             timeout=25)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []

    out: list[dict] = []
    for h in (data.get("hits", {}) or {}).get("hits", [])[:8]:
        src = h.get("_source", {}) or {}
        acc = (h.get("_id") or "").split(":")[0]
        doc = (h.get("_id") or "").split(":")[-1]
        cik = str(src.get("ciks", [""])[0]).lstrip("0") if src.get("ciks") else ""
        if not (acc and doc and cik):
            continue
        out.append({
            "company": (src.get("display_names") or [""])[0],
            "form": src.get("file_type") or form,
            "filed": src.get("file_date") or "",
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-','')}/{doc}",
            "accession": acc,
        })
    return out


async def fetch_filing_text(client: httpx.AsyncClient, url: str,
                            max_chars: int = 9000) -> str:
    """抓备案正文并抽成纯文本"""
    try:
        r = await client.get(url, headers={"User-Agent": UA}, timeout=30,
                             follow_redirects=True)
    except Exception:
        return ""
    if r.status_code != 200 or len(r.text) < 500:
        return ""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", r.text)
    try:
        tree = HTMLParser(html)
    except Exception:
        return ""
    # 优先取正文容器
    best = ""
    for sel in ("div#contentDiv", "div.content", "body"):
        nodes = tree.css(sel)[:1]
        if nodes:
            t = re.sub(r"\s+", " ", nodes[0].text()).strip()
            if len(t) > len(best):
                best = t
    return best[:max_chars]


def extract_entities(title: str) -> list[str]:
    """
    从标题里抽"可验证实体"：公司名、产品代号、金额。

    用于判断备案是否真的讲同一件事。
    实测教训：第一版只检查"搜到的公司名出现在备案里"——这永远为真
    （因为就是搜它搜出来的），于是
      "FDA review docs spark Revolution sell-off" 匹配到了 AEON Biopharma、
      "Pfizer's eczema drug" 匹配到了 C4 Therapeutics。
    往文章里注入错误的一手材料比没有更糟。
    """
    ents: list[str] = []
    # 公司名（跳过通用词）
    for m in re.finditer(
            r"\b([A-Z][A-Za-z&.\-]{2,}(?:\s+[A-Z][A-Za-z&.\-]{2,}){0,2})\b",
            title or ""):
        c = m.group(1)
        if c.split()[0].lower() in STOP:
            continue
        ents.append(c)
    # 产品代号（PCRX-201、BHV-1300、HRS-1596…）
    ents += re.findall(r"\b([A-Z]{2,6}-\d{2,4})\b", title or "")
    # 金额（$1.65B / $36.50）
    ents += re.findall(r"\$[\d.,]+\s*(?:billion|bn|million|m)?", title or "",
                       re.I)
    # 去重保序
    seen, out = set(), []
    for e in ents:
        k = e.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def _relevance(title: str, body: str, searched: str) -> int:
    """
    备案与标题的相关度 = 正文里命中的标题实体数。

    要求 >= 2：既要搜到的公司，也要另一个独立证据
    （另一家公司名 / 产品代号 / 金额）。
    """
    low = body.lower()
    n = 0
    for e in extract_entities(title):
        if e.lower() in low:
            n += 1
    return n


async def enrich_with_filing(client: httpx.AsyncClient, title: str,
                             days: int = 30,
                             min_relevance: int = 2) -> tuple[str, str]:
    """
    给一条标题找 SEC 一手材料。
    返回 (正文, 来源说明)，找不到或相关度不足返回 ("", "")。

    三层一致性检查（缺一不可）：
      1. 标题必须是并购/授权类——8-K 对纯临床读出、股价评论没有对应文件
      2. **备案主体本身必须出现在标题里**。
         实测：`Roundup: Novartis & Abogen Enter Licensing...` 搜 "Novartis"
         会命中 Quince Therapeutics 的备案（它正文里提了 Novartis）。
         备案主体不在标题里，就不是这笔交易的当事方，直接拒绝。
      3. 正文命中的标题实体数 >= 2（排除"只沾了一个名字"的情况）
    """
    t = title or ""
    # 1. 类型门槛
    if not re.search(r"acqui|merger|buyout|takeover|licens|收购|并购|授权",
                     t, re.I):
        return "", ""
    # 综述类标题聚合多笔交易，单份 8-K 代表不了它
    if re.search(r"\broundup\b|综述|一周|本周|daily:", t, re.I):
        return "", ""

    companies = guess_companies(title)
    if not companies:
        return "", ""
    title_low = t.lower()
    for comp in companies:
        hits = await search_filings(client, comp, form="8-K", days=days)
        for h in hits[:3]:
            # 2. 备案主体必须在标题里
            filer = re.sub(r"\s*\(.*$", "", h.get("company") or "").strip()
            filer_key = filer.split()[0].lower() if filer else ""
            if not filer_key or filer_key not in title_low:
                continue
            body = await fetch_filing_text(client, h["url"])
            if len(body) < 400:
                continue
            # 3. 实体重合度
            score = _relevance(t, body, comp)
            if score < min_relevance:
                continue
            note = (f"SEC 8-K 备案（{filer}，{h.get('filed','')}，"
                    f"相关度{score}）{h['url']}")
            return body, note
        await asyncio.sleep(0.2)      # SEC 限速
    return "", ""


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    async def _main():
        title = sys.argv[1] if len(sys.argv) > 1 else \
            "Viatris to make $1.65B Pacira acquisition, adding to momentum in pain"
        async with httpx.AsyncClient(timeout=30) as c:
            body, note = await enrich_with_filing(c, title)
            print("来源:", note or "(未找到)")
            print("正文字数:", len(body))
            if body:
                import re as _re
                # 找关键数字
                for pat in [r"\$[\d.,]+\s*per\s+share", r"\$[\d.,]+\s+per\s+share",
                            r"per share of \$[\d.,]+",
                            r"(?:January|February|March|April|May|June|July|August|"
                            r"September|October|November|December)\s+\d{1,2},\s+20\d\d"]:
                    m = _re.findall(pat, body, _re.I)
                    if m:
                        print(f"  匹配 {pat[:34]:36} -> {m[:3]}")
                print("\n前 600 字:")
                print(body[:600])
    asyncio.run(_main())