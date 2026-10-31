"""
选题模块：从评分结果里挑出当天要写的 2-3 篇。

规则（来自实战判断）：
1. >= must_publish 必选
2. 同一 cluster_key（同靶点）超过 N 条 → 合并成一篇综述，不重复写
3. 每天至少 1 篇非纯新闻（综述或历史对比），否则全是快报太单薄
4. 分数接近时，优先选有正文素材的（full > rss > headline）
5. 置信度过低的不选，宁缺毋滥
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date as _date

from common import (ROOT, SCORED_DIR, load_yaml, read_json, write_json,
                    today_cst)
from dedupe import load_published_deals, same_deal

POSTS_DIR = ROOT / "posts"


def _has_body(s: dict) -> bool:
    return len(s.get("content") or "") > 500


def _big_deal(s: dict, cfg: dict, min_conf: float) -> bool:
    """
    大额交易保底：总分不到 keep 也放行。

    例：2026-10-19 Roche/Hansoh 首付 4 亿美元、总额 150 亿美元，
    因处于早期且非首创只拿 6.3 分，被 6.5 硬门槛挡掉。这种交易不该漏。
    要求 deal_key 非空（有明确买卖双方），避免把"未披露金额的传闻"放进来。
    """
    if "not_bd" in (s.get("flags") or []):
        return False
    if not s.get("deal_key"):
        return False
    if s.get("confidence", 0) < min_conf:
        return False
    up = float(s.get("upfront_usd") or 0)
    total = float(s.get("total_usd") or 0)
    return (up >= cfg.get("big_deal_upfront_usd", 300_000_000)
            or total >= cfg.get("big_deal_total_usd", 5_000_000_000))


def _published_deal_keys(date: str, days: int) -> list[str]:
    """最近 N 天已发表交易的 deal_key，用于跨来源/跨语种去重。"""
    out: list[str] = []
    try:
        today = _date.fromisoformat(date)
    except ValueError:
        return out
    for e in load_published_deals():
        try:
            d = _date.fromisoformat(str(e.get("date") or ""))
        except ValueError:
            continue
        if abs((today - d).days) <= days and e.get("deal_key"):
            out.append(e["deal_key"])
    return out


def _published_leads(date: str) -> set[str]:
    """
    已经成稿过的 lead_id 前 8 位——**扫全部日期**，不只是当天。

    ⚠️ 2026-10-19 修正：原来只扫 `posts/<date>/`，于是同一件事隔天再被抓到
    （raw id 相同）时拦不住，会白占名额。实测 10-19 又把 10-17 那笔
    AZ-insitro 交易选进候选（只是分数 5.5 没过阈值才没发）。
    跨天扫描是必要的兜底。

    ⚠️ 但它只能拦"同一个 raw id"，拦不住"同一件事来自不同源"——
    后者 id 和标题都不同（10-17 发的中文标题 vs 10-19 的英文原标题），
    跨语种模糊匹配也失效。那种重复要靠 compose 的
    `any(lead in p.name ...)` 之外的手段，暂未实现。
    """
    import re
    out: set[str] = set()
    if not POSTS_DIR.exists():
        return out
    for p in POSTS_DIR.rglob("*.html"):
        m = re.search(r"-([0-9a-f]{8})\.html$", p.name)
        if m:
            out.add(m.group(1))
    return out


def _evidence(s: dict) -> int:
    """素材完整度打分，用于同分时的 tie-break"""
    n = 0
    if _has_body(s):
        n += 3
    if len(s.get("sources") or []) > 1:
        n += 2          # 多家媒体交叉印证
    if s.get("access") == "full":
        n += 1
    return n


# 保底选取时要排除的 flags（2026-10-25 用户要求"评分不达标也推 1-2 篇最高分"，
# 但 10-19 教训证明不能无条件降分——必须排掉写不出/是栏目摘要/是过期的）
#
# ⚠️ `insufficient_material` / `thin_material` **不在排除名单里**：
#    它们只说明"素材不够厚"，不等于"没有素材"。只要 content 非空
#    （>500 字有正文，或带 NCT 号），成稿阶段就能写；真正的空素材
#    由下面的 `_has_body` 判据拦掉。
_FALLBACK_EXCLUDE_FLAGS = {
    "not_bd",              # 不是 BD 交易
    "news_roundup",        # 每日新闻摘要栏目（10-19 实测补出来过）
    "headline_only",       # 只有标题没正文 → 写不出稿
    "pure_financing",      # 纯融资，不是授权交易
    "stale",               # 过期
    "stale_news",
    "no_content",
    "unknown_date",
}


def _fallback_candidates(scored: list[dict], keep_thr: float,
                          min_conf: float, posted: set[str],
                          deal_keys: list[str]) -> list[dict]:
    """评分不达标时的受限保底候选（2026-10-25 用户要求）。

    只从"写得出来、没发过、不是栏目摘要"的候选里挑，按分数降序。
    刻意**不降 min_confidence**：置信度低说明素材本身可疑，
    那种稿子推出去是给用户添麻烦，不是帮忙。
    """
    out: list[dict] = []
    for s in scored:
        flags = set(s.get("flags") or [])
        if flags & _FALLBACK_EXCLUDE_FLAGS:
            continue
        if float(s.get("score") or 0) <= 0:
            continue
        if float(s.get("confidence") or 0) < min_conf:
            continue
        # 必须有实质素材，否则成稿阶段必然写不出来
        if not _has_body(s) and not s.get("registry_nct"):
            continue
        # 三重去重：lead_id（跨天）/ deal_key 台账（跨源跨语种）/ cluster_key
        if s.get("id", "")[:8] in posted:
            continue
        if s.get("deal_key") and any(
                same_deal(s["deal_key"], k) for k in deal_keys):
            continue
        if s.get("cluster_key") and s["cluster_key"] in posted:
            continue
        out.append(s)
    out.sort(key=lambda x: (float(x.get("score") or 0), _evidence(x)),
              reverse=True)
    return out


def run(date: str | None = None) -> dict:
    date = date or today_cst()
    cfg = load_yaml("scoring.yaml")["score"]

    scored = read_json(SCORED_DIR / f"{date}.json", [])
    if not scored:
        print(f"[select] 没有评分数据: {SCORED_DIR}/{date}.json")
        return {}

    keep_thr = cfg.get("keep", 7.5)
    pub_thr = cfg.get("must_publish", 8.5)
    min_conf = cfg.get("min_confidence", 0.5)
    dmax = cfg.get("daily_max", 3)
    cluster_max = cfg.get("same_target_cluster_max", 3)

    pool = [s for s in scored
            if (s.get("score", 0) >= keep_thr
                and s.get("confidence", 0) >= min_conf)
            or _big_deal(s, cfg, min_conf)]
    big = [s for s in pool
           if not (s.get("score", 0) >= keep_thr
                   and s.get("confidence", 0) >= min_conf)]
    if big:
        print(f"[select] 大额交易保底放行 {len(big)} 条")
        for s in big[:3]:
            print(f"         · {s.get('score')} 分  "
                  f"首付 {int(s.get('upfront_usd') or 0)/1e8:.2f} 亿美元  "
                  f"{s.get('title','')[:52]}")

    # 排除"非交易"条目（评分层已判过，这里再拦一道）。
    # 这是 BD 雷达，临床数据读出/监管获批/股价波动都不是 BD 新闻。
    # 放进去会生成一篇"交易要素"全是"未找到公开数据"的空壳文章。
    not_bd = [s for s in pool if "not_bd" in (s.get("flags") or [])]
    if not_bd:
        pool = [s for s in pool if "not_bd" not in (s.get("flags") or [])]
        print(f"[select] 剔除非交易条目 {len(not_bd)} 条")
        for s in not_bd[:3]:
            print(f"         · {s.get('title','')[:56]}")

    # 排除【今天已经成稿】的条目。
    # 否则它们会白占 daily_max 的名额：compose 会跳过（文件已存在），
    # 结果本该出 3 篇的一天只出 2 篇。daily_max 的含义是"3 篇新稿"，
    # 不是"3 个名额（含重复）"。
    posted = _published_leads(date)
    if posted:
        before = len(pool)
        pool = [s for s in pool if s.get("id", "")[:8] not in posted]
        if before != len(pool):
            print(f"[select] 剔除已成稿（含历史日期）{before - len(pool)} 条")

    # 交易级去重：同一笔交易换个来源/标题/语言就是新 raw id，
    # 上面按 id 的检查拦不住。2026-10-19 实测 AZ-insitro 重复进池。
    deal_keys = _published_deal_keys(
        date, cfg.get("deal_lookback_days", 60))
    if deal_keys:
        before = len(pool)
        pool = [s for s in pool
                if not any(same_deal(s.get("deal_key", ""), k)
                           for k in deal_keys)]
        if before != len(pool):
            print(f"[select] 剔除已发表过的交易 {before - len(pool)} 条")

    print(f"[select] 候选池 {len(pool)} 条（>= {keep_thr} 分且置信度 >= {min_conf}，"
          f"另有 {len(big)} 条大额保底）")

    must = [s for s in pool if s.get("score", 0) >= pub_thr]

    # ---- 按 cluster 分组 ----
    clusters: dict[str, list[dict]] = defaultdict(list)
    singles: list[dict] = []
    for s in pool:
        ck = (s.get("cluster_key") or "").strip()
        if ck:
            clusters[ck].append(s)
        else:
            singles.append(s)

    plan: list[dict] = []

    # 1) 多条同 cluster → 合并成综述；单条 cluster 按单篇处理
    consumed_singles: set[str] = set()
    for ck, group in clusters.items():
        if len(group) >= 2:
            group.sort(key=lambda x: x.get("score", 0), reverse=True)
            lead = group[0]
            plan.append({
                "type": "landscape_review",
                "cluster_key": ck,
                "lead_id": lead["id"],
                "title": lead.get("title", ""),
                "score": lead.get("score", 0),
                "member_ids": [g["id"] for g in group[:cluster_max]],
                "deal_keys": [g["deal_key"] for g in group[:cluster_max]
                              if g.get("deal_key")],
                "members": [
                    {"title": g.get("title"), "url": g.get("url"),
                     "score": g.get("score"), "reasoning": g.get("reasoning")}
                    for g in group[:cluster_max]
                ],
                "reasoning": lead.get("reasoning", ""),
            })
        else:
            # 单条簇：进单篇池，但保留 cluster_key 供后续参考
            s = group[0]
            consumed_singles.add(s["id"])
            singles.append(s)

    # 2) 单条高分（含无簇与单簇）
    singles.sort(key=lambda x: (x.get("score", 0), _evidence(x)), reverse=True)
    for s in singles:
        if len(plan) >= dmax:
            break
        plan.append({
            "type": s.get("article_type", "event_report"),
            "cluster_key": None,
            "lead_id": s["id"],
            "title": s.get("title", ""),
            "score": s.get("score", 0),
            "member_ids": [s["id"]],
            "deal_keys": [s["deal_key"]] if s.get("deal_key") else [],
            "members": [{"title": s.get("title"), "url": s.get("url"),
                         "score": s.get("score"), "reasoning": s.get("reasoning")}],
            "reasoning": s.get("reasoning", ""),
            "verify_todo": s.get("verify_todo", []),
            "flags": s.get("flags", []),
        })

    # 3) 保证至少 1 篇非纯新闻
    if plan and not any(p["type"] != "event_report" for p in plan):
        # 从备选池里找一个历史对比型的
        alt = next((s for s in pool
                    if s.get("article_type") == "historical_compare"), None)
        if alt and len(plan) < dmax:
            plan.append({
                "type": "historical_compare",
                "cluster_key": None,
                "lead_id": alt["id"],
                "title": alt.get("title", ""),
                "score": alt.get("score", 0),
                "member_ids": [alt["id"]],
                "deal_keys": [alt["deal_key"]] if alt.get("deal_key") else [],
                "members": [{"title": alt.get("title"), "url": alt.get("url"),
                             "score": alt.get("score")}],
                "reasoning": alt.get("reasoning", ""),
            })

    # 4) 关于 daily_min：**降阈值保底改为"受限保底"**（2026-10-25 用户要求）
    #
    # ⚠️ 背景：10-19 曾做过一版"放宽到 keep-1.0 分补够 2 篇"，实测补出来的是
    #    ① 10-17 已发过的同一笔 AZ-insitro 交易（跨源重复，lead_id 不同没被拦住）
    #    ② `STAT Plus: Pharmalittle...` 每日新闻摘要栏目，不是单笔交易
    #    所以"降分凑数"被撤回。
    #
    # ⚠️ 但用户 2026-10-25 明确要求：**评分不达标时仍推送 1-2 篇分数最高的，
    #    并在飞书提示"评分不达标"**。这不是"把低分内容当高分混进去"，
    #    而是"照常成稿、照常核查、照常推草稿箱，但明确标注评分未达标"，
    #    由人工复核决定要不要发。
    #
    # 因此保底必须**严格过滤**，只从"写得出、没发过、不是栏目摘要"里挑：
    #   · 有实质素材（正文 >500 字或带 NCT 号）——否则成稿会写不出来
    #   · 排除 headline_only / thin_material / insufficient_material
    #   · 排除 news_roundup（每日摘要栏目，不是单笔交易）
    #   · 排除 not_bd（非交易）、pure_financing（纯融资）
    #   · 排除 stale / stale_news（过期）
    #   · 排除已发表：lead_id + deal_key 台账 + cluster_key 三重
    #   · 排除 no_content
    #
    # 保底篇数上限 2 篇，标记 below_threshold 让下游显示"评分不达标"。

    if not plan:
        fb = _fallback_candidates(scored, keep_thr, min_conf, posted, deal_keys)
        for s in fb[:2]:
            plan.append({
                "type": s.get("article_type", "event_report"),
                "cluster_key": s.get("cluster_key") or None,
                "lead_id": s["id"],
                "title": s.get("title", ""),
                "score": s.get("score", 0),
                "member_ids": [s["id"]],
                "deal_keys": [s["deal_key"]] if s.get("deal_key") else [],
                "members": [{"title": s.get("title"), "url": s.get("url"),
                             "score": s.get("score"),
                             "reasoning": s.get("reasoning")}],
                "reasoning": s.get("reasoning", ""),
                "verify_todo": s.get("verify_todo", []),
                "flags": s.get("flags", []),
                # ↓ 评分不达标标记：下游（飞书/草稿箱）据此提示
                "below_threshold": True,
                "keep_threshold": keep_thr,
            })
        if plan:
            print(f"[select] ⚠️ 无达标选题 → 受限保底 {len(plan)} 篇"
                  f"（最高 {plan[0]['score']} 分 < 门槛 {keep_thr}）")
            for p in plan:
                print(f"         · {p['score']}分  {p['title'][:52]}")
                print(f"           flags: {', '.join(p.get('flags') or []) or '无'}")
        else:
            # ⚠️ 保底池空 = **采集侧空素材**，不是"今天没交易"（2026-10-25 实测）。
            #   那天 67 条候选 content 全是空串——正文没抓回来。
            #   这种情况下任何保底都会让模型拿世界知识编，所以必须报清楚根因，
            #   而不是笼统说"0 篇"。
            empty = sum(1 for s in scored
                        if not (s.get("content") or "").strip())
            empty_pct = 100.0 * empty / len(scored) if scored else 0
            print(f"[select] 无达标选题，且无可保底候选（写不出/重复/摘要栏目）")
            print(f"         ⚠️ 空素材 {empty}/{len(scored)} 条（{empty_pct:.0f}%）"
                  f" —— 若占比很高，是【采集侧正文抓取】失效，"
                  f"不是当天没有交易。查 fetch.py 的选择器/源状态。")
            print(f"         最高分候选："
                  + (f"{max((float(s.get('score') or 0) for s in scored), default=0):.1f} 分"
                     f"（门槛 {keep_thr}）" if scored else "无数据"))

    plan = plan[:dmax]

    out = {"date": date, "count": len(plan), "plan": plan}
    # 诊断信息：本地 stage 读它来向用户解释"0 篇"的真实原因
    if not plan:
        empty = sum(1 for s in scored if not (s.get("content") or "").strip())
        top = max((float(s.get("score") or 0) for s in scored), default=0)
        out["diagnosis"] = {
            "keep_threshold": keep_thr,
            "candidates": len(scored),
            "empty_material": empty,
            "empty_material_pct": (100.0 * empty / len(scored)
                                   if scored else 0),
            "top_score": top,
            "reason": ("no_fallback_candidate" if empty else "below_threshold"),
        }
    elif plan and all(p.get("below_threshold") for p in plan):
        out["below_threshold"] = True
        out["keep_threshold"] = keep_thr
    write_json(SCORED_DIR / f"{date}.plan.json", out)

    print(f"\n[select] 选定 {len(plan)} 篇")
    for i, p in enumerate(plan, 1):
        print(f"  {i}. [{p['type']}] {p['score']}分  {p['title'][:56]}")
        if len(p["member_ids"]) > 1:
            print(f"     └ 合并 {len(p['member_ids'])} 条同簇交易")
    return out


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出特殊符号会崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    run(sys.argv[1] if len(sys.argv) > 1 else None)