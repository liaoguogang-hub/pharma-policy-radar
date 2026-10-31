"""
从公司投资者关系页面拉取官方新闻稿原文，作为素材的一手补充通道。

为什么需要（2026-10-27 实测）：
  雷达的素材库只有聚合站转述（如摩熵医药），缺三样最关键的东西——
    1. 交易的完整对价结构（首付 vs 总额 vs 里程碑上限）
    2. 买方为什么买（管理层原话、战略定位）
    3. 交易后的开发安排（谁主导、适应症方向）
  没有这三样，成稿只能把标题里"为何"这个问题跳过——
  答案在官方新闻稿里，但素材里没有，核验也会把有依据的句子打成"无出处"。

为什么放在素材层而不是让 LLM 自己找：
  LLM 编造公司公告内容的概率远高于引用它。核验器要求逐字出处，
  官方新闻稿原文进素材，才能既写得出、又验得过。

用法：
  python pr_official.py <date>          # 打印当天素材里能对上的官方稿
  from pr_official import enrich        # enrich(date, plan_item) -> str
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx

# 常见公司 IR 页的搜索入口。命中即抓正文。
# ⚠️ investors.gilead.com 对脚本请求返 403，必须用 www.gilead.com 主站。
#
# 为什么要抓【不止一篇】：
#   单看收购公告，只能写清"买了什么、多少钱"。
#   把买方自己的临床管线新闻稿一起抓进来，才能回答"为什么现在买"——
#   联用伙伴 emvistegrast 的机制、适应症、数据、进度，
#   都只在它自己的新闻稿里（2026-10-27 实测）。
IR_SEARCH = [
    "https://www.gilead.com/news/news-details/2026/"
    "gilead-to-acquire-investigational-oral-il-23r-inhibitor-hcxt-2001-"
    "strengthening-its-inflammation-pipeline-in-ibd",
    "https://investors.gilead.com/news/news-details/2026/"
    "Gilead-to-Acquire-Investigational-Oral-IL-23R-Inhibitor-HCXT-2001-"
    "Strengthening-Its-Inflammation-Pipeline-in-IBD/default.aspx",
]

# 联用伙伴的管线新闻稿：提供 emvistegrast 的机制与数据出处
PIPELINE_SEARCH = [
    "https://www.gilead.com/company/company-statements/2026/"
    "phase-2-swift-study-of-emvistegrast-a-potential-differentiated-"
    "once-daily-oral-treatment-meets-primary-endpoint-in-ulcerative-colitis",
]

# 竞品（已获批的口服 IL-23R 药物）的官方新闻稿：
# 提供 icotrokinra 的获批状态与适应症出处
COMPETITOR_SEARCH = [
    "https://www.jnj.com/media-center/press-releases/"
    "fda-approval-of-icotyde-icotrokinra-ushers-in-new-era-for-first-line-"
    "systemic-treatment-of-plaque-psoriasis-with-a-targeted-oral-peptide",
]

# ── 按交易主题匹配的官方公告（2026-10-25 新增）─────────────────
#
# 为什么要有这层：
#   雷达选题经常是「多笔交易混写」的 roundup（stale + multi_deal_roundup），
#   聚合站转述会漏掉关键条款——开发责任怎么分、利润怎么切、区域权益边界。
#   这些只有买方/卖方的官方公告里才有。
#
# 匹配方式：关键词命中 → 抓对应公告 → 整段进素材。
# 抓不到就跳过，绝不编。
DEAL_PR = [
    {
        "keys": ["lixudebart", "alenti", "claudin"],
        "label": "CSL与Alentis全球合作公告",
        "urls": [
            "https://newsroom.csl.com/2026-10-04-CSL-and-Alentis-announce-"
            "global-partnership-to-develop-and-commercialise-lixudebart-"
            "for-rare-kidney-and-liver-diseases",
            "https://alentis.ch/csl-and-alentis-announce-global-partnership-"
            "to-develop-and-commercialise-lixudebart-for-rare-kidney-and-"
            "liver-diseases/",
        ],
    },
    {
        "keys": ["somnivera", "nhwa", "恩华"],
        "label": "恩华药业与Somnivera许可公告",
        "urls": [
            "https://www.nhwa-group.com/news/515946.htm",
        ],
    },
]

# 抽取正文：去掉脚本/样式/导航，保留段落文字
_STRIP = re.compile(
    r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def _clean(html: str) -> str:
    html = _STRIP.sub(" ", html)
    text = _TAG.sub("\n", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&quot;", '"').replace("&#39;", "'")
                .replace("&rsquo;", "'").replace("&ldquo;", '"')
                .replace("&rdquo;", '"').replace("&mdash;", "—")
                .replace("&ndash;", "–").replace("&lt;", "<")
                .replace("&gt;", ">"))
    # ⚠️ 希腊字母实体不解码会破坏机制描述的检索与逐字比对：
    #    "&alpha;4&beta;7" → "α4β7"（2026-10-27 实测，
    #    漏解导致含 emvistegrast 机制的新闻稿被当成"抓不到"而丢弃）。
    for ent, ch in (("&alpha;", "α"), ("&beta;", "β"), ("&gamma;", "γ"),
                     ("&delta;", "δ"), ("&mu;", "μ"), ("&sigma;", "σ"),
                     ("&trade;", "™"), ("&reg;", "®"), ("&copy;", "©")):
        text = text.replace(ent, ch)
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def fetch(url: str, timeout: float = 45.0) -> str:
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/147.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers=headers) as cl:
        r = cl.get(url)
        r.raise_for_status()
        return _clean(r.text)


def enrich(date: str, plan_item: dict) -> str:
    """返回官方新闻稿素材块；抓不到就返回空串（绝不编）。"""
    blob = " ".join(str(plan_item.get(k, "")) for k in ("title", "reasoning"))
    for i in (plan_item.get("member_ids") or []):
        blob += " " + str(i)
    # ⚠️ members 里是【原始素材的标题】（多笔交易混写的 roundup 时，
    #    plan_item["title"] 只是其中一笔的标题）。
    #    漏了它，"CSL & Alentis" 这类英文标题就匹配不上主题规则。
    for m in (plan_item.get("members") or []):
        blob += " " + str(m.get("title", "")) + " " + str(m.get("reasoning", ""))
    low = blob.lower()

    out = []

    # ── 通用：按交易主题抓双方官方公告 ────────────────────────
    # 多笔交易混写的 roundup 选题，聚合站转述会漏掉关键条款，
    # 官方公告里才有开发责任、利润分配、区域权益这些硬信息。
    for spec in DEAL_PR:
        if not any(k.lower() in low for k in spec["keys"]):
            continue
        for url in spec["urls"]:
            try:
                body = fetch(url)
            except Exception as e:
                print(f"[pr_official] 交易公告抓取失败 {url[:60]}: "
                      f"{type(e).__name__}")
                continue
            if len(body) < 500:
                continue
            out.append(
                f"--- 官方公告（一手来源，可逐字引用）：{spec['label']} ---\n"
                f"链接：{url}\n"
                "以下为公司官方公告原文，引用其中事实时请与原文一致：\n\n"
                + body[:12000])
            break

    # ── 吉利德专线（口服 IL-23R）────────────────────────────
    if ("gilead" in low or "吉利德" in blob) and ("il-23" in low
                                                  or "hcxt" in low):
        for url in IR_SEARCH:
            try:
                body = fetch(url)
            except Exception as e:
                print(f"[pr_official] 抓取失败 {url[:70]}: {type(e).__name__}")
                continue
            if "HCXT-2001" not in body:
                continue
            out.append(
                "--- 官方新闻稿（一手来源，可逐字引用）---\n"
                f"来源：Gilead Sciences 官方新闻稿\n链接：{url}\n"
                "以下为公司官方新闻稿原文，引用其中事实时请与原文一致：\n\n"
                + body[:12000])
            break

        # 买方自家管线新闻稿：提供联用伙伴的机制、适应症与数据出处
        for url in PIPELINE_SEARCH:
            try:
                body = fetch(url)
            except Exception as e:
                print(f"[pr_official] 管线稿抓取失败：{type(e).__name__}")
                continue
            if "emvistegrast" not in body:
                continue
            out.append(
                "--- 买方管线新闻稿（一手来源，可逐字引用）---\n"
                f"来源：Gilead Sciences 官方新闻稿\n链接：{url}\n"
                "以下为公司官方新闻稿原文，引用其中事实时请与原文一致：\n\n"
                + body[:12000])
            break

        # 竞品官方新闻稿：提供已获批口服 IL-23R 药物的获批状态与适应症出处
        for url in COMPETITOR_SEARCH:
            try:
                body = fetch(url)
            except Exception as e:
                print(f"[pr_official] 竞品稿抓取失败：{type(e).__name__}")
                continue
            if "icotrokinra" not in body:
                continue
            out.append(
                "--- 竞品官方新闻稿（一手来源，可逐字引用）---\n"
                f"来源：Johnson & Johnson 官方新闻稿\n链接：{url}\n"
                "以下为公司官方新闻稿原文，引用其中事实时请与原文一致：\n\n"
                + body[:12000])
            break

    return "\n".join(out)


if __name__ == "__main__":
    print(fetch(IR_SEARCH[0])[:6000])
