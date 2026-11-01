"""
选题模块：从评分结果里挑出当天要写的 1-3 篇政策稿。

规则（政策口径）：
1. >= must_publish 必发
2. 同一 cluster_key（同一政策事件）>= 2 条 → 合并成一篇 policy_review，不重复写
3. 每天最多 max_articles 篇（默认 3）；china / global 两个口径轮流取，
   防止一边把名额占满
4. 分数接近时，优先选有正文素材的（full > rss > headline）
5. 置信度 < min_confidence 不选，宁缺毋滥
6. 政策保底：当天没有 >= keep 的条目时，若存在"部委级正式文件"
   （policy_level >= 2.5 且 force >= 1.5），允许保底 1 篇，
   plan 里标 fallback: true + reason

已发表去重走两条线：
  · raw id（跨天同一个条目）
  · cluster_key（同一政策事件换来源/换语种/换标题再来一次）
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import date as _date

import scope
from common import (ROOT, SCORED_DIR, load_yaml, read_json, write_json,
                    today_cst)

POSTS_DIR = ROOT / "posts"

# 进池前就排除的 flags：
#   stale     过期（媒体转载旧文件，评分层已判）
#   duplicate 与别的条目重复（同源转载）
#   minor     地方小文件/事务性通知（无全国影响，写不出政策稿）
#   out_of_scope 发文机关不在白名单 / 影响范围不到省级或联盟 / 海外非政策类
_POOL_EXCLUDE_FLAGS = {"stale", "duplicate", "minor", "out_of_scope"}

# endpoint_verdict → force 分（与 config/scoring.yaml 的 force 档位对应）
_FORCE_BY_VERDICT = {
    "enforced": 2.0,     # 已执行 / 处罚落地
    "effective": 2.0,    # 已生效并执行
    "issued": 1.5,       # 正式发布未生效
    "draft": 1.0,        # 征求意见稿
    "repealed": 0.0,     # 废止 / 被替代
    "none": 0.0,
}

# 国务院 / 中央层面（policy_level 3：国务院或多部委联合）
_CENTRAL_RE = re.compile(
    r"国务院|中共中央|中办|国办|多部委|联合发文|等部门")
# 部委级（policy_level 2.5）
_MINISTRY_RE = re.compile(
    r"国家发展改革委|国家发改委|国家医保局|国家医疗保障局|国家药监局|"
    r"国家药品监督管理局|国家卫生健康委|国家卫健委|国家中医药局|"
    r"国家中医药管理局|科技部|工业和信息化部|工信部|财政部|商务部|"
    r"国家市场监管总局|市场监管总局|国家疾控局|人力资源社会保障部|"
    r"人社部|国家数据局|国家局|NMPA|NHSA|FDA|EMA|WHO")
# 部委司局 / 直属机构（policy_level 1.5）：出现即降级
# ⚠️ `[^公]司` 是为了放过"公司"这种词（"管理司"命中，"有限公司"不命中）
_SUB_AGENCY_RE = re.compile(
    r"(?:[^公]司|办公室|中心|直属机构|直属单位|分局|派出机构|学会|协会)")
# 地方省级（policy_level 0.8）
_PROVINCE_RE = re.compile(
    r"(?:省|自治区|直辖市)(?:医保局|医疗保障局|药监局|药品监督管理局|"
    r"卫健委|卫生健康委|中医药局)")

# 四种文章类型；老口径的类型名做一次映射，防止模型回退到旧词表
_ARTICLE_TYPES = {"policy_readout", "consultation", "enforcement",
                  "policy_review"}
_LEGACY_TYPE_MAP = {
    "event_report": "policy_readout",
    "landscape_review": "policy_review",
    "historical_compare": "policy_review",
}


def _has_body(s: dict) -> bool:
    # 政策稿正文天然短：CDE 均长 368、NMPA 公告 400-800、EMA 约 500。
    # 原来用 500 会把 CDE 的征求意见稿全部判成「无正文」（2026-11-01 实测）。
    return len(s.get("content") or "") > 250


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


def _channel(s: dict) -> str:
    ch = str(s.get("channel") or "").strip().lower()
    return ch if ch in ("china", "global") else "china"


def _article_type(s: dict) -> str:
    t = str(s.get("article_type") or "").strip()
    if t in _ARTICLE_TYPES:
        return t
    return _LEGACY_TYPE_MAP.get(t, "policy_readout")


def _member(s: dict) -> dict:
    return {"title": s.get("title", ""), "url": s.get("url", ""),
            "score": s.get("score", 0), "reasoning": s.get("reasoning", "")}


def _published_leads() -> set[str]:
    """
    已经成稿过的 lead_id 前 8 位——**扫全部日期**，不只是当天。

    只能拦"同一个 raw id"，拦不住"同一件事来自不同源"——
    后者 id 和标题都不同，要靠下面的 _published_clusters()。
    """
    out: set[str] = set()
    if not POSTS_DIR.exists():
        return out
    for p in POSTS_DIR.rglob("*.html"):
        m = re.search(r"-([0-9a-f]{8})\.html$", p.name)
        if m:
            out.add(m.group(1))
    return out


def _published_clusters(date: str, days: int, posted: set[str]) -> set[str]:
    """
    最近 N 天**真的成过稿**的政策事件（cluster_key），用于跨来源/跨语种去重。

    ⚠️ 政策稿的重复形态和 BD 不一样：同一份文件会被十几家媒体报，
    raw id 不同、标题不同，按 id 拦不住；cluster_key 才是政策事件的
    稳定指纹（定义见 score.SYSTEM_PROMPT）。

    ⚠️ 只认「plan 里选过 **且** 真的出了稿」的条目（lead_id 在 posts 里）：
      · 只看 plan.json 会把"选中了但成稿失败"的事件永久埋掉；
      · 只看当天的 plan 又会让同一天重跑 pick 时把自己上一次的选择排除掉。
    用 posts 里的 lead_id 做闸门，两个问题一起解决。
    """
    out: set[str] = set()
    try:
        today = _date.fromisoformat(date)
    except ValueError:
        return out
    for p in SCORED_DIR.glob("*.plan.json"):
        stem = p.name.split(".", 1)[0]
        try:
            d = _date.fromisoformat(stem)
        except ValueError:
            continue
        if abs((today - d).days) > days:
            continue
        for it in (read_json(p, {}) or {}).get("plan") or []:
            ck = str(it.get("cluster_key") or "").strip()
            lead = str(it.get("lead_id") or "")[:8]
            if ck and lead in posted:
                out.add(ck)
    return out


# ============================================================
# 政策保底：把 scoring.yaml 的维度档位映射回"部委级正式文件"
# ============================================================
# ⚠️ 评分 JSON 的 schema 被下游锁死，模型不回传 policy_level / force
#    这两个数值，所以这里用【等价映射】：
#      force        ← endpoint_verdict（draft 1.0 / issued 1.5 /
#                     effective,enforced 2.0），与 scoring.yaml 的档位一一对应
#      policy_level ← flags(national/local/minor/overseas) + 发文机关正则
#    映射只在保底这一处使用，不参与正常选题排序。
def _force(s: dict) -> float:
    v = str(s.get("endpoint_verdict") or "").strip().lower()
    if v in _FORCE_BY_VERDICT and v != "none":
        return _FORCE_BY_VERDICT[v]
    # 模型没填或填 none 时，用 flags 兜底（保证不自相矛盾的条目能被认出来）
    flags = {str(x).lower() for x in (s.get("flags") or [])}
    if "effective" in flags:
        return 2.0
    if "formal" in flags:
        return 1.5
    if "consultation" in flags:
        return 1.0
    return 0.0


def _policy_level(s: dict) -> float:
    flags = {str(x).lower() for x in (s.get("flags") or [])}
    t = str(s.get("title") or "").strip()
    if "local" in flags or "minor" in flags:
        return 0.8 if _PROVINCE_RE.search(t) else 0.3
    if "overseas" in flags:
        return 2.5          # FDA/EMA/WHO 这类境外国家级监管机构 ≈ 单一部委
    if not ("national" in flags or _MINISTRY_RE.search(t)):
        return 1.5          # 层级判不清 → 按司局/直属机构保守处理，不给保底
    if _CENTRAL_RE.search(t):
        return 3.0
    if _SUB_AGENCY_RE.search(t):
        return 1.5
    return 2.5


def _is_ministry_formal(s: dict, fb: dict, min_conf: float) -> bool:
    """部委级正式文件？政策保底只从这里放行。"""
    if not fb.get("enabled", True):
        return False
    flags = {str(x).lower() for x in (s.get("flags") or [])}
    # 征求意见稿 / 过期 / 重复 / 地方事务性 / 已标注地方：都不保底
    if flags & {"consultation", "stale", "duplicate", "minor", "local"}:
        return False
    if float(s.get("confidence") or 0) < min_conf:
        return False
    if _force(s) < float(fb.get("min_force", 1.5)):
        return False
    if _policy_level(s) < float(fb.get("min_policy_level", 2.5)):
        return False
    # 成稿至少要有素材：正文或摘要不能全空
    if not _has_body(s) and not str(s.get("summary") or "").strip():
        return False
    return True


def _single_entry(s: dict) -> dict:
    return {
        "type": _article_type(s),
        "cluster_key": (str(s.get("cluster_key") or "").strip() or None),
        "channel": _channel(s),
        "lead_id": s["id"],
        "title": s.get("title", ""),
        "score": s.get("score", 0),
        "member_ids": [s["id"]],
        "members": [_member(s)],
        "reasoning": s.get("reasoning", ""),
        "verify_todo": s.get("verify_todo", []),
        "flags": s.get("flags", []),
    }


def _review_entry(ck: str, group: list[dict], cluster_max: int) -> dict:
    lead = group[0]
    members = group[:cluster_max]
    todos: list[str] = []
    for g in members:
        for t in (g.get("verify_todo") or []):
            if t not in todos:
                todos.append(t)
    return {
        "type": "policy_review",
        "cluster_key": ck,
        "channel": _channel(lead),
        "lead_id": lead["id"],
        "title": lead.get("title", ""),
        "score": lead.get("score", 0),
        "member_ids": [g["id"] for g in members],
        "members": [_member(g) for g in members],
        "reasoning": lead.get("reasoning", ""),
        "verify_todo": todos[:8],
        "flags": sorted({f for g in members for f in (g.get("flags") or [])}),
    }


def run(date: str | None = None) -> dict:
    date = date or today_cst()
    cfg = load_yaml("scoring.yaml")["score"]

    scored = read_json(SCORED_DIR / f"{date}.json", [])
    if not scored:
        print(f"[select] 没有评分数据: {SCORED_DIR}/{date}.json")
        return {}

    keep_thr = float(cfg.get("keep", 6.0))
    pub_thr = float(cfg.get("must_publish", 7.5))
    min_conf = float(cfg.get("min_confidence", 0.5))
    dmax = int(cfg.get("max_articles", cfg.get("daily_max", 3)))
    cluster_max = int(cfg.get("same_cluster_max",
                              cfg.get("same_target_cluster_max", 3)))
    lookback = int(cfg.get("cluster_lookback_days", 45))
    fb_cfg = cfg.get("policy_fallback") or {}

    # ---- 0) 政策范围闸门（2026-11-01 用户口径）----
    #
    # 只放「国家/各省医保局、卫健委，国家药监局、CDE 发布的、
    # 对国家或省份（省际联盟）产生影响的政策」。
    # 这是确定性闸门，不依赖模型打分——范围外的条目即使 9 分也不进候选池。
    scope_cfg = load_yaml("scoring.yaml").get("policy_scope") or {}
    in_scope, out_scope = [], []
    for s in scored:
        ok, why = scope.in_scope(
            s.get("title", ""), s.get("content") or s.get("summary") or "",
            s.get("flags"), s.get("channel", "china"), scope_cfg,
            source=((s.get("sources") or [""])[0] or s.get("source", "")))
        (in_scope if ok else out_scope).append((s, why))
    if out_scope:
        print(f"[select] 范围闸门剔除 {len(out_scope)} 条（不在目标范围）")
        for s, why in sorted(out_scope, key=lambda x: -float(x[0].get("score") or 0))[:8]:
            print(f"    ✗ {float(s.get('score') or 0):>4} | {s.get('title','')[:44]:46} | {why[:36]}")
    scored = [s for s, _why in in_scope]

    # ---- 1) 达标池：分数 + 置信度 + flag 排除 ----
    pool: list[dict] = []
    dropped_flags = 0
    for s in scored:
        flags = {str(x).lower() for x in (s.get("flags") or [])}
        if flags & _POOL_EXCLUDE_FLAGS:
            dropped_flags += 1
            continue
        if (float(s.get("score") or 0) >= keep_thr
                and float(s.get("confidence") or 0) >= min_conf):
            pool.append(s)
    if dropped_flags:
        print(f"[select] 剔除 stale/duplicate/minor {dropped_flags} 条")

    # ---- 2) 已发表去重（跨天）：raw id + cluster_key ----
    posted = _published_leads()
    if posted:
        before = len(pool)
        pool = [s for s in pool if (s.get("id") or "")[:8] not in posted]
        if before != len(pool):
            print(f"[select] 剔除已成稿（raw id，含历史日期）"
                  f"{before - len(pool)} 条")

    pub_clusters = _published_clusters(date, lookback, posted)
    if pub_clusters:
        before = len(pool)
        pool = [s for s in pool
                if (str(s.get("cluster_key") or "").strip() not in pub_clusters)]
        if before != len(pool):
            print(f"[select] 剔除 {lookback} 天内写过的政策事件 "
                  f"{before - len(pool)} 条")

    print(f"[select] 候选池 {len(pool)} 条"
          f"（>= {keep_thr} 分且置信度 >= {min_conf}）")

    # ---- 3) 按 cluster_key 归并 ----
    clusters: dict[str, list[dict]] = defaultdict(list)
    singles: list[dict] = []
    for s in pool:
        ck = str(s.get("cluster_key") or "").strip()
        if ck:
            clusters[ck].append(s)
        else:
            singles.append(s)

    reviews: list[dict] = []
    for ck, group in clusters.items():
        if len(group) >= 2:
            group.sort(key=lambda x: (float(x.get("score") or 0), _evidence(x)),
                       reverse=True)
            reviews.append(_review_entry(ck, group, cluster_max))
        else:
            # 单条簇：进单篇池，cluster_key 保留在条目里
            singles.append(group[0])
    # 同簇综述按分数降序，保证高分事件先占名额
    reviews.sort(key=lambda x: (float(x.get("score") or 0),
                                len(x.get("member_ids") or [])), reverse=True)

    plan: list[dict] = reviews[:dmax]
    if reviews:
        print(f"[select] 同簇合并 {len(reviews)} 篇综述"
              f"（占 {len(plan)} 个名额，同簇最多 {cluster_max} 条）")

    # ---- 4) 单篇：china / global 轮流取，避免一边占满 ----
    singles.sort(key=lambda x: (float(x.get("score") or 0), _evidence(x)),
                 reverse=True)
    by_ch: dict[str, list[dict]] = defaultdict(list)
    for s in singles:
        by_ch[_channel(s)].append(s)
    # ⚠️ 2026-11-01 改：原来是"china/global 死板轮流"，结果 7.8 分的国内条例
    #    输给了 7.5 分的海外规则。用户口径是「中国为主」，所以改成：
    #    每轮取当前分数更高的那个渠道；只有两边差距 < 0.3 才按 china 优先轮流，
    #    避免一边独吞名额。
    last_ch = None
    while len(plan) < dmax and any(by_ch.values()):
        cands = [(ch, by_ch[ch][0]) for ch in ("china", "global") if by_ch.get(ch)]
        if not cands:
            break
        if len(cands) == 1:
            ch, _ = cands[0]
        else:
            cands.sort(key=lambda x: -float(x[1].get("score") or 0))
            (ch1, s1), (ch2, s2) = cands[0], cands[1]
            gap = float(s1.get("score") or 0) - float(s2.get("score") or 0)
            if gap >= 0.3:
                ch = ch1
            else:
                ch = "china" if last_ch != "china" else "global"
        if by_ch.get(ch):
            plan.append(_single_entry(by_ch[ch].pop(0)))
            last_ch = ch

    # ---- 5) 政策保底：一条都没达标时，部委级正式文件放行 1 篇 ----
    if not plan and fb_cfg.get("enabled", True):
        cands: list[dict] = []
        for s in scored:
            if (s.get("id") or "")[:8] in posted:
                continue
            if str(s.get("cluster_key") or "").strip() in pub_clusters:
                continue
            if _is_ministry_formal(s, fb_cfg, min_conf):
                cands.append(s)
        cands.sort(key=lambda x: (float(x.get("score") or 0), _evidence(x)),
                   reverse=True)
        fb_max = int(fb_cfg.get("max_articles", 1))
        reason = str(fb_cfg.get("reason") or "部委级正式文件保底")
        for s in cands[:fb_max]:
            e = _single_entry(s)
            e.update({
                "fallback": True,
                "reason": reason,
                # 分数确实没过 keep：下游（飞书/草稿箱）据此提示"评分未达标"
                "below_threshold": True,
                "keep_threshold": keep_thr,
            })
            plan.append(e)
        if plan:
            print(f"[select] ⚠️ 无达标选题 → {reason} {len(plan)} 篇"
                  f"（最高 {plan[0]['score']} 分 < 门槛 {keep_thr}）")
            for p in plan:
                print(f"         · {p['score']}分 [{p['channel']}] "
                      f"{p['title'][:52]}")
                print(f"           flags: "
                      f"{', '.join(p.get('flags') or []) or '无'}")
        else:
            _print_empty_diagnosis(scored, keep_thr, fb_cfg)

    plan = plan[:dmax]

    out = {"date": date, "count": len(plan), "plan": plan}
    # 诊断信息：本地 stage 读它来向用户解释"0 篇"的真实原因
    # ⚠️ reason 的两个取值是 publish._plan_diagnosis() 的契约，不要改名
    if not plan:
        empty = sum(1 for s in scored if not (s.get("content") or "").strip())
        empty_pct = 100.0 * empty / len(scored) if scored else 0
        top = max((float(s.get("score") or 0) for s in scored), default=0)
        out["diagnosis"] = {
            "keep_threshold": keep_thr,
            "candidates": len(scored),
            "empty_material": empty,
            "empty_material_pct": empty_pct,
            "top_score": top,
            "reason": ("no_fallback_candidate" if empty_pct >= 80
                       else "below_threshold"),
        }
    elif plan and all(p.get("fallback") for p in plan):
        out["below_threshold"] = True
        out["keep_threshold"] = keep_thr
    write_json(SCORED_DIR / f"{date}.plan.json", out)

    print(f"\n[select] 选定 {len(plan)} 篇")
    for i, p in enumerate(plan, 1):
        tag = "⚠️保底" if p.get("fallback") else ""
        print(f"  {i}. [{p['type']}][{p.get('channel', 'china')}] "
              f"{p['score']}分 {tag} {p['title'][:52]}")
        if len(p["member_ids"]) > 1:
            print(f"     └ 合并 {len(p['member_ids'])} 条同簇条目")
    return out


def _print_empty_diagnosis(scored: list[dict], keep_thr: float,
                           fb_cfg: dict) -> None:
    """0 篇时把根因说清楚，而不是笼统报"今天没政策"。"""
    empty = sum(1 for s in scored if not (s.get("content") or "").strip())
    empty_pct = 100.0 * empty / len(scored) if scored else 0
    top = max((float(s.get("score") or 0) for s in scored), default=0)
    print("[select] 无达标选题，且没有可保底的部委级正式文件")
    print(f"         ⚠️ 空素材 {empty}/{len(scored)} 条（{empty_pct:.0f}%）"
          f" —— 若占比很高，是【采集侧正文抓取】失效，不是当天没有政策。"
          f"查 fetch.py 的选择器/源状态。")
    print(f"         最高分候选：{top:.1f} 分（门槛 {keep_thr}）")
    print(f"         保底判据：policy_level >= "
          f"{fb_cfg.get('min_policy_level', 2.5)} 且 force >= "
          f"{fb_cfg.get('min_force', 1.5)}（见 scoring.yaml policy_fallback）")


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出特殊符号会崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    run(sys.argv[1] if len(sys.argv) > 1 else None)
