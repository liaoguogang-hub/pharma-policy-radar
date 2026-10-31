"""
公司背景补全：给成稿素材补一段**可引用的**公司与产品信息。

# 为什么 BD 雷达更需要它（2026-10-17）

用户定的规则：素材不足时要扩写，首选方向是「介绍相关公司」。
BD 文章天然是**两家公司的故事**（买方/卖方、授权方/被授权方），
公司背景既是合理的扩写方向，也是**编造的高发区**：

    "Coherus 在北美拥有成熟商业化团队"     ← retlirafusp 那篇的编造
    "此前已与罗氏在 ALS 领域合作"          ← 编造合作史
    "该公司年收入 320 亿元"                ← 编造财务

这些断言 `check_facts`（正则抓分子代号）**一个都拦不住**。
所以必须先把真实、可引用的公司信息取回来放进素材，
成稿写得出来、`claimcheck` 也验得过。

# 数据源与各自的边界（务必如实告知用户）

| 源 | 给什么 | 边界 |
|---|---|---|
| **openFDA Drugs@FDA** | 已上市产品（美国批准） | 只有美国批准记录。实测 BEIGENE 有、HENGRUI/JUNSHI 都 404 |
| **ClinicalTrials.gov** | 在研管线（试验数/期别/适应症） | 只覆盖注册在 CT.gov 的试验；中国试验常只登 CDE |
| **公司年报** | 营业收入 / 归母净利润 | 美股走 SEC XBRL；A 股走巨潮年报 PDF |
| **公司官网** | 主营/定位自述 | 需在 `config/company_sites.yaml` 配 URL，**人工维护** |

⚠️ **不提供**：中国 NMPA 批文、市场份额、产品定价。
素材里没有 → 断言核验会拦下相关表述。**这是刻意的：宁可不写，不要编。**

⚠️ 与临床雷达 `clinical-data-radar/src/companyinfo.py` 是一对镜像，
机制相同；差别在于本模块的 `company_brief` 额外支持**一次取两方**
（BD 文章常同时涉及买方和卖方）。
"""
from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path

import httpx
import pypdf

from common import ROOT, load_yaml

UA = {"User-Agent": "Mozilla/5.0 (compatible; pharma-bd-radar/1.0)"}
OPENFDA = "https://api.fda.gov/drug/drugsfda.json"
CT_BASE = "https://clinicaltrials.gov/api/v2/studies"
CNINFO_QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC = "http://static.cninfo.com.cn/"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS = ("https://data.sec.gov/api/xbrl/companyconcept/"
             "CIK{cik}/{tax}/{tag}.json")
SEC_HEADERS = {"User-Agent": "pharma-bd-radar/1.0 "
                             "(research; contact: radar@example.com)"}

# ⚠️ 必须同时支持 **us-gaap 和 ifrs-full** 两套分类（2026-10-17 实测）：
#    美国本土公司（ABBV/LLY/PFE）走 us-gaap；
#    但**外国私人发行人（20-F）常用 IFRS**，其营收不在 us-gaap 下——
#    实测 AstraZeneca（AZN）us-gaap 全部 404，而
#    ifrs-full/Revenue 有 FY2025 = 587.39 亿美元。
#    只查 us-gaap 会让整个欧洲药企群体都"取不到"。
_REV_TAGS = (
    ("us-gaap", "Revenues"),
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    ("us-gaap", "SalesRevenueNet"),
    ("ifrs-full", "Revenue"),
    ("ifrs-full", "RevenueFromContractsWithCustomers"),
)

# ⚠️ 币种不能一律当美元：IFRS 申报主体可能用 EUR/GBP 报告。
#    把欧元数字标成"亿美元"是**新的编造**，必须按实际币种标注。
_CUR_ZH = {"USD": "美元", "EUR": "欧元", "GBP": "英镑",
           "CNY": "人民币", "JPY": "日元"}

# ⚠️ 跨国药企的**中文简称**必须收录，否则「艾伯维」「礼来」「诺华」
#    这类写法抽不出来（BD 文章里 MNC 是高频主体）。
MNC_CN_EN = {
    "礼来": "Lilly", "诺华": "Novartis", "罗氏": "Roche",
    "默沙东": "Merck Sharp", "默克": "Merck",
    "阿斯利康": "AstraZeneca", "辉瑞": "Pfizer",
    "艾伯维": "AbbVie", "强生": "Johnson", "杨森": "Janssen",
    "赛诺菲": "Sanofi", "葛兰素史克": "GlaxoSmithKline",
    "百时美施贵宝": "Bristol", "施贵宝": "Bristol",
    "武田": "Takeda", "安进": "Amgen",
    "勃林格殷格翰": "Boehringer", "拜耳": "Bayer",
    "诺和诺德": "Novo Nordisk", "卫材": "Eisai",
    "第一三共": "Daiichi", "大冢": "Otsuka", "优时比": "UCB",
    "吉利德": "Gilead", "渤健": "Biogen", "再生元": "Regeneron",
    "安斯泰来": "Astellas", "盐野义": "Shionogi", "灵北": "Lundbeck",
    "恒瑞医药": "Hengrui", "恒瑞": "Hengrui",
    "百济神州": "BeiGene", "百济": "BeiGene",
    "信达生物": "Innovent", "君实生物": "Junshi", "石药集团": "CSPC",
    "中国生物制药": "Sino Biopharm", "复星医药": "Fosun",
    "再鼎医药": "Zai Lab", "康方生物": "Akeso",
    "诺诚健华": "InnoCare", "翰森制药": "Hansoh",
    "齐鲁制药": "Qilu", "正大天晴": "Chia Tai Tianqing",
    "科伦药业": "Kelun", "先声药业": "Simcere",
    "和黄医药": "Hutchmed", "传奇生物": "Legend Biotech",
    "荣昌生物": "RemeGen", "贝达药业": "Betta", "三生制药": "3SBio",
    "天境生物": "I-Mab", "基石药业": "CStone", "亚盛医药": "Ascentage",
    "德琪医药": "Antengene", "加科思": "Jacobio",
    "康宁杰瑞": "Alphamab", "泽璟制药": "Zelgen",
    "微芯生物": "Chipscreen", "艾力斯": "Allist", "迪哲医药": "Dizal",
    "益方生物": "InventisBio", "海思科": "Haisco", "恩华药业": "Nhwa",
    "人福医药": "Humanwell", "丽珠集团": "Livzon", "华海药业": "Huahai",
    "药明生物": "WuXi Biologics", "药明康德": "WuXi AppTec",
    "凯莱英": "Asymchem", "齐鲁": "Qilu",
}

RE_EN_CO = re.compile(
    r"\b([A-Z][A-Za-z0-9&\-]{2,}(?:\s+[A-Z][A-Za-z0-9&\-]{2,}){0,2})"
    r"\s+(?:Pharma|Pharmaceuticals|Biosciences|Biotech|Therapeutics|"
    r"Oncology|Biotherapeutics|Inc|Corp|Ltd|plc|AG|SA|NV)\b")

# ⚠️ RE_EN_CO 要求公司名后面带行业/公司后缀（"… Pharma" / "… Inc"）。
#    但新闻标题里公司名常常**裸写**（"AstraZeneca does AI deal with insitro"），
#    没有后缀就抽不出来——实测这条公告因此完全识别不到公司，
#    公司背景对该条目失效。这里补一张**去后缀的英文名**清单。
#
# 维护说明：只收本 beat 高频出现的公司；新公司出现得频繁就往这里加。
# 收录裸名有误匹配风险，所以只收"不像普通英文单词"的名字。
EN_CO_BARE = {
    "AstraZeneca", "insitro", "BioNTech", "Moderna", "Vertex", "Regeneron",
    "Gilead", "Biogen", "Incyte", "Seagen", "Argenx", "AbbVie", "Amgen",
    "Lilly", "Roche", "Novartis", "Pfizer", "Sanofi", "Bayer", "Takeda",
    "Eisai", "Otsuka", "Astellas", "Daiichi", "UCB", "Lundbeck", "Jazz",
    "Sarepta", "Alnylam", "Ionis", "BridgeBio", "Arcutis", "Vanda",
    "Syndax", "Madrigal", "Intercept", "Denali", "Annexon", "Apellis",
    "Karuna", "Cerevel", "Relay", "Kymera", "Nimbus", "Terns",
    "Structure", "Aligos", "Arbutus", "Arrowhead", "Dicerna", "Silence",
    "Immunocore", "Adaptimmune", "Autolus", "Gracell", "Gilead Sciences",
    "Hutchmed", "BeiGene", "Zai Lab", "Innovent", "Junshi", "Hengrui",
    "Akeso", "InnoCare", "Hansoh", "Legend Biotech", "RemeGen",
    "Boehringer", "Merck", "Novo Nordisk", "Johnson", "Janssen",
}


def _cfg() -> dict:
    p = ROOT / "config" / "company_sites.yaml"
    if not p.exists():
        return {}
    try:
        return load_yaml("company_sites.yaml") or {}
    except Exception:
        return {}


def find_companies(text: str, limit: int = 2) -> list[tuple[str, str]]:
    """
    从文本里找出可能的公司，返回 [(中文名, 英文名)]。

    BD 文章通常是"买方 vs 卖方"，所以默认要 2 家。
    按**出现位置**排序，靠前的优先（标题/开头的通常是主角）。

    ⚠️ 只做"识别"，不做"确认"——识别错了不会造成错误事实，
    因为下游取不到材料时会如实说"未取到"，绝不编。
    """
    hits: list[tuple[int, str, str]] = []
    for zh, en in MNC_CN_EN.items():
        i = text.find(zh)
        if i >= 0:
            hits.append((i, zh, en))
    for m in RE_EN_CO.finditer(text):
        hits.append((m.start(), m.group(1), m.group(1)))
    # 裸英文名（无 Pharma/Inc 后缀）——见 EN_CO_BARE 的说明
    for name in EN_CO_BARE:
        i = text.find(name)
        if i >= 0:
            hits.append((i, name, name))
    hits.sort(key=lambda x: x[0])
    out, seen = [], set()
    for _, zh, en in hits:
        if en.lower() in seen:
            continue
        seen.add(en.lower())
        out.append((zh, en))
        if len(out) >= limit:
            break
    return out


# ---------- 源 A：药品注册库（美国已上市产品）----------
def us_approved_products(client: httpx.Client, sponsor_en: str) -> list[dict]:
    """
    ⚠️ openFDA 的 sponsor_name 是**大写**存的，查询也必须大写，
    否则一律 404（实测 "Hengrui" 404 / "BEIGENE" 3 条）。
    404 表示"没有记录"，是正常结果。
    """
    q = (sponsor_en or "").strip().upper()
    if len(q) < 3:
        return []
    try:
        r = client.get(OPENFDA, params={
            "search": f'sponsor_name:"{q}"', "limit": 20}, timeout=25)
        if r.status_code != 200:
            return []
        d = r.json()
    except Exception:
        return []
    out, seen = [], set()
    for it in (d.get("results") or []):
        prods = it.get("products") or []
        brands = [p.get("brand_name") for p in prods if p.get("brand_name")]
        if not brands:
            continue
        b = brands[0]
        if b in seen:
            continue
        seen.add(b)
        subs = it.get("submissions") or []
        dates = sorted(s.get("submission_status_date", "") for s in subs
                       if s.get("submission_status_date"))
        out.append({"brand": b,
                    "appno": it.get("application_number", ""),
                    "date": (dates[-1] if dates else "")[:8]})
    return out


# ---------- 源 B：在研管线 ----------
def pipeline_summary(client: httpx.Client, sponsor_en: str) -> dict:
    if not sponsor_en or len(sponsor_en) < 3:
        return {}
    try:
        r = client.get(CT_BASE, params={
            "query.spons": sponsor_en, "pageSize": 40}, timeout=30)
        d = r.json() if r.status_code == 200 else {}
    except Exception:
        return {}
    trials = d.get("studies") or []
    if not trials:
        return {}
    from collections import Counter
    phases, conds = Counter(), Counter()
    for t in trials:
        ps = t.get("protocolSection") or {}
        for ph in ((ps.get("designModule") or {}).get("phases") or []):
            phases[ph] += 1
        for c in ((ps.get("conditionsModule") or {}).get("conditions") or []):
            conds[c] += 1
    return {"n": len(trials),
            "phases": phases.most_common(5),
            "conditions": conds.most_common(6)}


# ---------- 源 C：年报 ----------
def _sec_cik(client: httpx.Client, ticker: str) -> str:
    try:
        d = client.get(SEC_TICKERS, headers=SEC_HEADERS, timeout=30).json()
    except Exception:
        return ""
    t = (ticker or "").strip().upper()
    for v in (d.values() if isinstance(d, dict) else []):
        if str(v.get("ticker", "")).upper() == t:
            return f"{int(v['cik_str']):010d}"
    return ""


def annual_report_us(client: httpx.Client, ticker: str) -> dict:
    cik = _sec_cik(client, ticker)
    if not cik:
        return {}
    for tax, tag in _REV_TAGS:
        try:
            r = client.get(SEC_FACTS.format(cik=cik, tax=tax, tag=tag),
                           headers=SEC_HEADERS, timeout=30)
            if r.status_code != 200:
                continue
            units_all = (r.json().get("units") or {})
        except Exception:
            continue
        # 币种按实际申报取（IFRS 主体可能是 EUR/GBP）
        cur = next((k for k in ("USD", "EUR", "GBP", "CNY", "JPY")
                    if units_all.get(k)), "")
        if not cur:
            continue
        units = units_all.get(cur) or []
        ann = [x for x in units
               if x.get("form") in ("20-F", "10-K", "40-F")
               and x.get("fp") == "FY" and x.get("end")]
        if not ann:
            continue
        # ⚠️ 同一个 end 日期会有**多条**：全年 + 第四季度（2026-10-17 实测）
        #    实测 AbbVie Revenues：
        #      frame=CY2025    → 611.60 亿美元（全年，对）
        #      frame=CY2025Q4  → 166.18 亿美元（仅 Q4，错）
        #    按 end 排序取最后一条会拿到 Q4 当全年。
        #    这个错比"取不到"危险得多：错数字进了素材，
        #    claimcheck 会把它当"有出处"放行。
        # 先只留【纯年度】frame（CY + 4 位年），没有再按 end 取最大值兜底。
        pure = [x for x in ann
                if not x.get("frame")
                or re.fullmatch(r"CY\d{4}", str(x["frame"]))]
        if pure:
            ann = pure
        else:
            best: dict = {}
            for x in ann:      # 同一 end 只留最大值（全年 > 任一单季）
                if x["end"] not in best or x["val"] > best[x["end"]]["val"]:
                    best[x["end"]] = x
            ann = list(best.values())
        ann.sort(key=lambda x: x["end"])
        last = ann[-1]
        # ⚠️ 新鲜度闸门（2026-10-17 实测）：AMGN 用 Revenues 标签时
        #    "纯年度"条目只剩到 2020-12-31，不设闸门就会把**6 年前**的
        #    营收当成当前年报数字写进素材。过期数据同样是有害错误，
        #    不只是"取不到"。超过 2 年判过期，继续试下一个标签。
        if int(last["end"][:4]) < date.today().year - 2:
            continue
        return {"source": f"SEC XBRL {tax}/{tag}", "period": last["end"],
                "value": last["val"], "currency": cur,
                "ticker": ticker.upper()}
    return {}


def annual_report_a(client: httpx.Client, zh_name: str,
                    max_pages: int = 25) -> dict:
    """
    巨潮年报 PDF 抽「营业收入/归母净利润」。

    ⚠️ 只抽年报正本（排除摘要/半年度/季度）；只解前 25 页
    （「主要会计数据」在开头）。抽不到返回 {}。
    """
    try:
        r = client.post(CNINFO_QUERY, data={
            "pageNum": 1, "pageSize": 30, "column": "szse",
            "tabName": "fulltext",
            "searchkey": f"{zh_name} 年度报告", "isHLtitle": "true",
        }, headers={"Referer": "http://www.cninfo.com.cn/",
                    "Content-Type":
                    "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=35)
        anns = (r.json() or {}).get("announcements") or []
    except Exception:
        return {}
    cand = []
    for a in anns:
        t = re.sub(r"<[^>]+>", "", a.get("announcementTitle") or "")
        if "年度报告" not in t:
            continue
        if any(k in t for k in ("摘要", "半年度", "季度", "英文")):
            continue
        if a.get("adjunctUrl"):
            cand.append((t, a["adjunctUrl"]))
    if not cand:
        return {}
    title, url = cand[0]
    try:
        pr = client.get(CNINFO_STATIC + url, timeout=90)
        if pr.status_code != 200 or len(pr.content) < 10000:
            return {}
        rd = pypdf.PdfReader(io.BytesIO(pr.content))
        text = ""
        for i in range(min(max_pages, len(rd.pages))):
            try:
                text += (rd.pages[i].extract_text() or "")
            except Exception:
                continue
    except Exception:
        return {}
    if len(text) < 500:
        return {}

    def _pick(kw: str) -> int | None:
        """
        ⚠️ 取**第一个**匹配，不取最大值（2026-10-17 实测）：
           「归属于上市公司股东的净利润」后面紧跟「…净资产」（612 亿），
           再才是净利润（77 亿）——按"取最大"会把净资产当净利润。
        """
        m = re.search(kw + r"[^\d]{0,40}?([\d,]{7,}\.\d{2})", text)
        if not m:
            return None
        try:
            return int(float(m.group(1).replace(",", "")))
        except Exception:
            return None

    rev, np_ = _pick("营业收入"), _pick("归属于上市公司股东的净利润")
    if rev is None and np_ is None:
        return {}
    return {"source": f"巨潮资讯《{title}》", "revenue_cny": rev,
            "net_profit_cny": np_, "url": CNINFO_STATIC + url}


# ---------- 源 D：官网 ----------
def _site_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|footer|header|svg)[^>]*>.*?</\1>",
                  " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", html)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", txt).strip()


def official_site(client: httpx.Client, zh: str, en: str,
                  limit: int = 2500) -> dict:
    """
    ⚠️ **必须验证页面确实属于这家公司**：配错 URL 会抓到别家站点，
    然后把别人的产品/营收当成本公司的——比没有材料严重得多。
    验证方式：正文里出现公司中文名或英文名，否则整段丢弃。
    """
    sites = _cfg().get("sites") or {}
    url = sites.get(zh) or sites.get(en)
    if not url:
        return {}
    try:
        r = client.get(url, timeout=25, follow_redirects=True)
        if r.status_code != 200 or len(r.text) < 500:
            return {}
        txt = _site_text(r.text)
    except Exception:
        return {}
    if len(txt) < 200:
        return {}
    if not any(n and n in txt for n in (zh, en)):
        return {"rejected": True,
                "why": "页面正文未出现该公司名，疑似 URL 配错，已丢弃"}
    return {"url": url, "text": txt[:limit]}


def _resolve_ticker(client: httpx.Client, en: str) -> str:
    if not en:
        return ""
    up = en.strip().upper()
    try:
        d = client.get(SEC_TICKERS, headers=SEC_HEADERS, timeout=30).json()
    except Exception:
        return ""
    rows = list(d.values()) if isinstance(d, dict) else []
    for v in rows:
        if str(v.get("ticker", "")).upper() == up:
            return up
    key = re.sub(r"[^A-Z]", "", up)
    if len(key) >= 4:
        for v in rows:
            if key in re.sub(r"[^A-Z]", "", str(v.get("title", "")).upper()):
                return str(v.get("ticker", "")).upper()
    return ""


# ---------- 汇总 ----------
def _one_company(client: httpx.Client, zh: str, en: str) -> list[str]:
    lines: list[str] = []
    if en:
        prods = us_approved_products(client, en)
        if prods:
            lines.append(f"  · 已上市产品（美国 FDA，来源 openFDA，"
                         f"sponsor_name={en.upper()}）："
                         + "、".join(f"{p['brand']}（{p['appno']}）"
                                     for p in prods[:6]))
        else:
            lines.append(f"  · 已上市产品：openFDA 未检索到"
                         f"（{en.upper()}）美国批准记录")
        pipe = pipeline_summary(client, en)
        if pipe:
            lines.append(f"  · 在研管线（ClinicalTrials.gov，{pipe['n']} 项）："
                         + ("期别 " + "、".join(f"{k} {v}项"
                                                for k, v in pipe["phases"])
                            if pipe["phases"] else "")
                         + ("；适应症 " + "、".join(
                                f"{k} {v}项" for k, v in pipe["conditions"])
                            if pipe["conditions"] else ""))
    # 年报
    ar = None
    if en:
        tk = _resolve_ticker(client, en)
        if tk:
            ar = annual_report_us(client, tk)
            if ar:
                unit = _CUR_ZH.get(ar.get("currency", "USD"), "美元")
                ar["_txt"] = (f"  · 年报营收（{ar['source']}，"
                              f"截至 {ar['period']} 财年）：约 "
                              f"{ar['value']/1e8:,.2f} 亿{unit}")
    if not ar and zh:
        a = annual_report_a(client, zh)
        if a:
            seg = []
            if a.get("revenue_cny"):
                seg.append(f"营业收入约 {a['revenue_cny']/1e8:,.2f} 亿元")
            if a.get("net_profit_cny"):
                seg.append(f"归母净利润约 {a['net_profit_cny']/1e8:,.2f} 亿元")
            if seg:
                ar = {"_txt": f"  · 年报（{a['source']}）：" + "；".join(seg)}
    lines.append(ar["_txt"] if ar else
                 "  · 年报：**未取到**（SEC XBRL 与巨潮均未读到）"
                 "→ 不要写任何营收/利润数字")
    # 官网
    site = official_site(client, zh, en)
    if site.get("text"):
        lines.append(f"  · 官网自述（{site['url']}）：{site['text'][:800]}")
    elif site.get("rejected"):
        lines.append(f"  · 官网：{site['why']}")
    return lines


def company_brief(companies: list[tuple[str, str]]) -> str:
    """
    返回可放进成稿素材的公司背景（带来源标注）。

    `companies` 是 [(中文名, 英文名), ...]，BD 文章通常传两方。
    任何一方取不到就如实说明，**绝不编**。

    ⚠️ 刻意做成**同步**：`compose.build_material()` 是同步函数。
    """
    companies = [(z, e) for z, e in (companies or []) if (z or e)][:2]
    if not companies:
        return ""
    lines = ["【公司背景（可引用，来自公开注册库/年报/官网）】"]
    try:
        with httpx.Client(headers=UA, follow_redirects=True) as c:
            for zh, en in companies:
                lines.append(f"- {zh or en}：")
                lines.extend(_one_company(c, zh, en))
    except Exception as e:
        lines.append(f"  · 取公司背景时异常：{type(e).__name__}")
    lines.append("⚠️ 以上是本段提供的**全部**公司信息。没有列出的"
                 "（中国 NMPA 批文、市场份额、合作伙伴历史等）一律不要写"
                 "——素材里没有出处，断言核验会拦。")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for cos in ([("艾伯维", "AbbVie"), ("再鼎医药", "Zai Lab")],
                [("恒瑞医药", "Hengrui"), ("insitro", "insitro")]):
        print("=" * 72)
        print(company_brief(cos))
