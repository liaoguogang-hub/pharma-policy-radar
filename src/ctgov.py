"""
ClinicalTrials.gov 一手材料查询（免费，无需 API key）。

为什么需要：
"未找到公开数据"这句话本身可能就是错的。

实测案例：一篇讲 Cerevance solengepras 3期 ARISE 试验的稿子，
通篇写"数据未披露""无法确认主要终点""p值/CI/样本量均未找到"。
实际查证后，完整数据早就公开：
  主要终点 OFF 时间减少 0.61 小时（p=0.0350）
  次要终点 ON time +0.60h、UPDRS-II +1.91 分（p=0.0008）
  安全性 150mg 组无严重不良事件
数据在【公司公告 + MDS 2026 大会披露】里，不在注册库。

⚠️ 反过来的坑也存在：ARISE 试验 2024年11月就开始给药，
但 ClinicalTrials.gov 用 solengepras 搜不到任何条目。
**注册库查不到 ≠ 数据不存在**，所以本模块的设计是
"注册库 + 公司新闻室"双通道，缺一不可。

接口（免费公开，无需 key）：
  https://clinicaltrials.gov/api/v2/studies?query.term=...&pageSize=N
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

UA = "pharma-bd-radar/1.0 (research; contact: radar@example.com)"
API = "https://clinicaltrials.gov/api/v2/studies"
HEADERS = {"User-Agent": UA}

# 公司新闻室常见路径（用于第二通道兜底）
NEWSROOM_HINTS = [
    "newsroom", "press-release", "press-releases", "news",
    "media", "investors", "ir",
]


def _first(x, *keys, default=None):
    """安全取多层字典字段"""
    cur = x
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _fmt_date(s: str) -> str:
    """2024-11-19 -> 2024年11月"""
    m = re.match(r"(\d{4})-(\d{2})", s or "")
    return f"{m.group(1)}年{int(m.group(2))}月" if m else (s or "")


def extract_digest(study: dict) -> dict:
    """把一条 study 抽成紧凑摘要"""
    ps = study.get("protocolSection", {}) or {}
    ident = ps.get("identificationModule", {}) or {}
    status = ps.get("statusModule", {}) or {}
    design = ps.get("designModule", {}) or {}
    sponsor = ps.get("sponsorCollaboratorsModule", {}) or {}
    arms = ps.get("armsInterventionsModule", {}) or {}
    outc = ps.get("outcomesModule", {}) or {}
    cond = ps.get("conditionsModule", {}) or {}

    prim = None
    for o in (outc.get("primaryOutcomes") or []):
        prim = (o.get("measure") or "").strip()
        break

    drugs = []
    for it in (arms.get("interventions") or []):
        n = (it.get("name") or "").strip()
        if n and n not in drugs:
            drugs.append(n)

    return {
        "nct": ident.get("nctId"),
        "title": (ident.get("briefTitle") or "").strip(),
        "official": (ident.get("officialTitle") or "").strip(),
        "sponsor": (sponsor.get("leadSponsor") or {}).get("name", ""),
        "phase": (design.get("phases") or []),
        "status": status.get("overallStatus"),
        "enroll": _first(design, "enrollmentInfo", "count"),
        "start": status.get("startDateStruct", {}).get("date"),
        "primary_completion": status.get("primaryCompletionDateStruct", {}).get("date"),
        "completion": status.get("completionDateStruct", {}).get("date"),
        "conditions": (cond.get("conditions") or [])[:3],
        "interventions": drugs[:6],
        "primary_outcome": prim,
        "url": f"https://clinicaltrials.gov/study/{ident.get('nctId')}"
               if ident.get("nctId") else "",
        "has_results": "resultsSection" in study,
    }


def digest_to_text(d: dict) -> str:
    """渲染成可直接喂给模型的文本"""
    bits = [f"NCT {d['nct']}"]
    if d["phase"]:
        bits.append("/".join(d["phase"]) + "期")
    if d["status"]:
        bits.append(d["status"])
    lines = ["· " + " ".join(bits) + f" — {d['title'][:90]}"]
    if d["sponsor"]:
        lines.append(f"    申办方：{d['sponsor']}")
    if d["enroll"]:
        lines.append(f"    样本量：{d['enroll']}")
    if d["start"]:
        lines.append(f"    起始：{_fmt_date(d['start'])}"
                     + (f"；主要完成：{_fmt_date(d['primary_completion'])}"
                        if d["primary_completion"] else ""))
    if d["interventions"]:
        lines.append(f"    干预：{', '.join(d['interventions'])}")
    if d["primary_outcome"]:
        lines.append(f"    主要终点：{d['primary_outcome'][:110]}")
    if d["has_results"]:
        lines.append("    ⚠ 该试验已上传结果（可在注册库查到统计量）")
    if d["url"]:
        lines.append(f"    {d['url']}")
    return "\n".join(lines)


async def search_trials(client: httpx.AsyncClient, query: str,
                        page_size: int = 5) -> list[dict]:
    """
    检索注册库。query 建议用药物代号或疾病名。
    返回结构化摘要列表。

    ⚠️ 不要用 ?fields= 参数：实测加上后 designModule 不返回，
    导致 phase / enrollment 全部变成 NA/空。不加则完整返回。
    """
    if not query or len(query) < 3:
        return []
    params = {"query.term": query, "pageSize": min(page_size, 20)}
    try:
        r = await client.get(API, params=params, headers=HEADERS, timeout=25)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    out = []
    for s in (data.get("studies") or [])[:page_size]:
        d = extract_digest(s)
        # 太不相关的过滤掉：标题/条件里要出现查询词的一部分
        blob = " ".join([d["title"], d["official"],
                         " ".join(d["conditions"]),
                         " ".join(d["interventions"])]).lower()
        key = query.strip().lower()
        if key and key not in blob:
            words = [w for w in re.split(r"\W+", key) if len(w) > 2]
            if not words or not any(w in blob for w in words):
                continue
        out.append(d)
    return out


async def enrich_from_trial_registry(client: httpx.AsyncClient,
                                     title: str,
                                     keywords: list[str] | None = None,
                                     max_hits: int = 3) -> tuple[str, list[str]]:
    """
    给一条标题查注册库补充素材。

    返回 (素材文本, 来源说明列表)。

    ⚠️ 重要：查不到不等于不存在。
    实测 solengepras 在注册库里搜不到任何条目，
    但该药物的3期数据已在公司公告和 MDS 大会完整披露。
    所以调用方必须同时准备公司公告通道。
    """
    queries: list[str] = []
    if keywords:
        queries.extend(k for k in keywords if k and len(k) >= 3)
    if not queries and title:
        # 从标题里抽可能的药物代号（大写字母+数字）
        queries.extend(re.findall(r"\b([A-Z][a-z]{3,}[a-z]+\b|\d+[A-Z]{2,})\b",
                                  title)[:2])

    seen_nct: set[str] = set()
    blocks: list[str] = []
    sources: list[str] = []

    for q in queries[:3]:
        hits = await search_trials(client, q, page_size=4)
        if not hits:
            continue
        blocks.append(f"【ClinicalTrials.gov 检索：{q}】")
        for d in hits[:max_hits]:
            if d["nct"] in seen_nct:
                continue
            seen_nct.add(d["nct"])
            blocks.append(digest_to_text(d))
            sources.append(f"ClinicalTrials.gov {d['nct']} {d['url']}")
        if len(seen_nct) >= max_hits:
            break
        await asyncio.sleep(0.3)

    if blocks:
        return "\n".join(blocks), sources
    return "", []


# ============================================================
# 自测
# ============================================================
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    async def _main():
        async with httpx.AsyncClient(timeout=30) as c:
            for q in sys.argv[1:] or ["semaglutide", "GPR6", "Parkinson Disease OR"]:
                hits = await search_trials(c, q, page_size=3)
                print(f"\n{'='*64}\n检索「{q}」-> {len(hits)} 条")
                for d in hits:
                    print(f"\n{digest_to_text(d)}")
    asyncio.run(_main())