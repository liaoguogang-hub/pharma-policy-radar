"""
去重模块。

两个层级：
1. 完全重复 —— 同 URL 或同 hash
2. 语义重复 —— 不同媒体报同一件事（标题相似度）

用 rapidfuzz 做标题近似，因为同一条交易在不同媒体的标题
往往只差几个词（"Novo to pay Hengrui up to $2.6B for a
once-weekly obesity pill" vs "Novo Nordisk licenses Hengrui
oral GLP-1 for $2.6 billion"）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from rapidfuzz import fuzz
from rapidfuzz import process

from common import (DATA_DIR, RawItem, RAW_DIR, SCORED_DIR, SEEN_FILE,
                    load_yaml, normalize_deal_key, read_json, write_json,
                    today_cst)

DEALS_FILE = DATA_DIR / "published_deals.json"


def normalize_title(t: str) -> str:
    """去掉标点、公司后缀等干扰，让相似度更准"""
    t = t.lower()
    t = re.sub(r"\s+[—\-|·].*$", "", t)          # 去掉破折号后的副标题
    t = re.sub(r"\(.*?\)", "", t)                  # 去括号
    t = re.sub(r"[^\w\s一-鿿]", " ", t)       # 去标点（保留中文）
    t = re.sub(r"\b(inc|corp|ltd|plc|co|company|holdings|therapeutics|"
               r"biosciences|biopharma|pharmaceuticals?)\b", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def entity_hint(t: str) -> str:
    """抽取公司名片段，作为辅助判据"""
    caps = re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", t)
    stop = {"The", "This", "That", "With", "From", "After", "What", "Why",
            "How", "Its", "His", "Her", "They", "Their", "Both", "More",
            "Here", "Now", "New", "First", "Two", "Three", "Four", "Five"}
    caps = [c for c in caps if c not in stop]
    return " ".join(caps[:4]).lower()


def dedupe(items: list[RawItem], threshold: int = 82) -> list[RawItem]:
    """
    返回去重后的列表，被合并的来源会记到 keep.sources 里（用 extra_sources 字段）。
    """
    # 按发布时间倒序，保留最新的
    items = sorted(items, key=lambda x: x.published, reverse=True)

    kept: list[RawItem] = []
    norms: list[str] = []
    url_seen: set[str] = set()
    hash_seen: set[str] = set()
    merged: dict[str, list[str]] = {}

    for it in items:
        base = it.url.split("?")[0]
        if it.id in hash_seen or base in url_seen:
            # 完全重复，只记录来源
            for k in kept:
                if k.id == it.id or k.url.split("?")[0] == base:
                    merged.setdefault(k.id, []).append(it.source)
                    break
            continue

        n = normalize_title(it.title)
        dup_of = None
        if n:
            m = process.extractOne(n, norms, scorer=fuzz.token_set_ratio,
                                   score_cutoff=threshold)
            if m:
                dup_of = m[0]

        if dup_of is not None:
            for k in kept:
                if normalize_title(k.title) == dup_of:
                    merged.setdefault(k.id, []).append(it.source)
                    break
        else:
            kept.append(it)
            norms.append(n)
            url_seen.add(base)
            hash_seen.add(it.id)

    # 把合并的来源写回去
    for it in kept:
        extra = merged.get(it.id, [])
        if extra:
            it.topics = list(set(it.topics + [f"also:{s}" for s in extra]))

    return kept


def filter_history(items: list[RawItem], today: str | None = None) -> list[RawItem]:
    """
    跨天去重：只丢弃"已经写成稿"的条目。

    ⚠️ 两个踩过的坑，都在这里：

    【坑一】seen.json 早期不带日期，导致同一天的重跑把当天所有新闻
    都当成"已报道"。工作流每天跑两次，第二次永远产出 0 篇。
    实测：139 条被丢 110 条，只剩 2 条，最终 0 篇成稿。
    修法：条目带日期。

    【坑二】更隐蔽——去重基准用的是"抓过的"而不是"写过的"。
    一天抓 100+ 条但只写 2-3 篇，剩下 97 条明明没写过，
    却被永久标记为"已报道"。次日它们仍在源站首页，直接被过滤，
    于是天天产出 0 篇。
    实测：次日采集 143 条 → 丢弃 105 条 → 只剩 8 条 → 选题 0 篇。
    修法：seen.json 只记录【已发表】的选题（见 record_published），
    其余条目每次照常进入评分，让没入选的高分条目有机会第二天补上。
    """
    today = today or today_cst()
    data = read_json(SEEN_FILE, {})
    entries = _load_entries(data)

    # 已发表选题：不过滤同日的（同日重跑靠 compose 的文件幂等兜住）
    old = [e for e in entries if e.get("date", "") < today]
    # ⚠️ 按 8 位前缀比对，不按全量 id。
    # seen.json 里的 id 有两种来源：
    #   - compose 写入的是完整 hash（16 位）
    #   - 从 posts 文件名反查重建时只能拿到 8 位前缀
    # 早先按全量比对，导致 8 位的历史条目永远匹配不上，
    # 同一笔交易在 10-10 和 10-11 各发了一遍（诺诚健华×礼来）。
    seen_ids = {str(e["id"])[:8] for e in old if e.get("id")}
    seen_titles = [normalize_title(e["title"]) for e in old if e.get("title")]

    out, dropped = [], 0
    for it in items:
        if it.id[:8] in seen_ids:
            dropped += 1
            continue
        if seen_titles:
            m = process.extractOne(normalize_title(it.title), seen_titles,
                                   scorer=fuzz.token_set_ratio, score_cutoff=90)
            if m:
                dropped += 1
                continue
        out.append(it)

    if dropped:
        print(f"  历史去重：丢弃 {dropped} 条（往日已发表 {len(old)} 篇为基准）")
    else:
        print(f"  历史去重：基准 {len(old)} 篇往日已发表，本轮无丢弃")
    return out


def _load_entries(data: dict) -> list[dict]:
    """
    读 seen 条目。只接受 v3。

    版本历史（每次都是因为语义或格式错了才升版）：
      v1  {"ids":[], "titles":[]}
          无日期。同日重跑会把当天所有新闻当成"已报道"。
      v2  {"entries":[{id,title,date}]}
          有日期了，但记录的是【抓取到的全部条目】，
          导致没写过的高分条目被永久埋掉。
      v3  {"entries":[{id,title,date}]}
          记录【已发表选题】。格式与 v2 相同，语义不同，
          所以按 version 字段严格区分。

    旧版一律丢弃：宁可让最近一轮多评几条，也不能让错误的基准
    继续把好条目挡在外面。
    """
    if not isinstance(data, dict):
        return []
    ver = data.get("version")
    if ver == 3:
        return [e for e in data.get("entries", []) if isinstance(e, dict)]
    if data.get("entries") or data.get("ids") or data.get("titles"):
        print(f"  [seen] 检测到旧版格式 v{ver}，已丢弃"
              f"（语义已变更：只记录已发表选题）")
    return []


def record_published(topics: list[dict], today: str | None = None,
                     cap: int = 2000) -> None:
    """
    记录【已发表】的选题，作为跨天去重的基准。

    ⚠️ 只记录真正写成稿的，不要记录抓取到的全部条目。
    记录全部会把"没写过的高分条目"永久埋掉——见 filter_history 的坑二。

    topics: [{"lead_id":..., "title":...}, ...]
    """
    today = today or today_cst()
    data = read_json(SEEN_FILE, {})
    entries = _load_entries(data)

    known = {e.get("id") for e in entries}
    added = 0
    for t in topics:
        tid = t.get("lead_id") or t.get("id")
        title = t.get("title") or ""
        if not tid or tid in known:
            continue
        entries.append({"id": tid, "title": title, "date": today})
        known.add(tid)
        added += 1

    entries = entries[-cap:]
    write_json(SEEN_FILE, {"version": 3, "entries": entries})
    print(f"  [seen] 记录已发表 {added} 篇（库中共 {len(entries)} 篇）")


# ============================================================
# 交易级去重
#
# seen.json 按 raw id 记，同一笔交易换个来源/语言就是新 id，
# 拦不住。2026-10-19 实测：10-17 已发 AZ-insitro，10-19 又从
# 另一个来源抓进来，拿到 7.2 分，差点再发一遍。
#
# 这里用评分阶段产出的 deal_key（买卖双方 + 资产，双方按字母排序）
# 做指纹，发布成功后写入独立台账。
# ============================================================
def load_published_deals() -> list[dict]:
    data = read_json(DEALS_FILE, {})
    if isinstance(data, dict):
        return list(data.get("entries") or [])
    if isinstance(data, list):
        return data
    return []


# ============================================================
# 交易级去重的比对工具（2026-10-21）
#
# deal_key 是模型自由写的，槽位里的写法千差万别：
#   merck-kgaa / merck / merck-&-co    —— 同一个公司的不同写法
#   sirna-target / sirna-pipeline      —— 同一个资产的不同写法
# 而且模型偶尔把"资产"写进"乙方"槽（10-20 的
# `merck|sirna-target|acquisition` 就是这样）。
# 所以比对必须落到【token】层，不能逐字比整段。
# ============================================================

# 公司法律形式/通用词：比对公司名时去掉
_CORP_NOISE = {
    "kgaa", "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "plc", "gmbh", "ag", "sa", "nv", "llc", "lp", "the",
    "and", "of", "de", "la", "group", "holding", "holdings",
}


def _slot_tokens(slot: str) -> set[str]:
    """槽位字符串 → token 集合（只按非字母数字切分，不做语义处理）。"""
    return {t for t in re.split(r"[^a-z0-9]+", (slot or "").lower()) if t}


def _party_tokens(slot: str) -> set[str]:
    """
    公司名槽位 → token 集合（去掉法律形式后缀）。

    merck-kgaa → {merck}，于是能和 `merck` 对上。
    ⚠️ 只去**法律形式/通用词**，不去 pharma/bio/therapeutics 这类
    品牌词（"Silence Therapeutics" 和 "Silence" 是一家公司，
    但 "OncoBio" 和 "Onco" 未必是，去掉 brand 词容易误合并）。
    """
    toks = _slot_tokens(slot)
    stripped = {t for t in toks if t not in _CORP_NOISE}
    return stripped or toks


def _parties_match(pa: list[set[str]], pb: list[set[str]]) -> bool:
    """
    交易双方是否对得上。

    槽位 0/1 是模型写的"甲方/乙方"，但 normalize_deal_key 会把前两段
    按字母排序——两边如果用了不同的名字，排序结果可能对调，
    所以两种配对都试。
    """
    a0, a1 = pa
    b0, b1 = pb
    return bool((a0 & b0 and a1 & b1) or (a0 & b1 and a1 & b0))


def _asset_match(ta: set[str], tb: set[str]) -> bool:
    """
    资产是否对得上。

    资产未披露（空 / unknown）时按"双方相同即同一笔"处理——
    这跟旧实现一致：拿不到资产信息时不该因为资产对不上而放行重复。
    """
    if not ta or not tb:
        return True
    if "unknown" in ta or "unknown" in tb:
        return True
    return bool(ta & tb)


def same_deal(a: str, b: str) -> bool:
    """
    两个 deal_key 是否指向同一笔交易。

    deal_key 形如 `<甲方>|<乙方>|<资产>`（前两段按字母排序）。
    ⚠️ 这个判断跨天跨源跨语种，键里的写法千差万别：
      - 公司名带法律形式后缀：merck-kgaa / merck / merck-&-co
      - 同一个东西被模型放进了不同的槽位：10-20 的
        `merck|sirna-target|acquisition`（资产被塞进了"乙方"槽）
        vs 10-21 的 `merck-kgaa|silence-therapeutics|sirna-pipeline`
      - 资产名本身不同：sirna-target vs sirna-pipeline

    2026-10-21 实测事故：上面这两个键就是同一笔交易
    （默克 13 亿美元收购 siRNA 公司），但旧实现判成"不是同一笔"
    ——因为旧实现要求前两段【逐字相等】，merck ≠ merck-kgaa 直接出局。
    结果 10-21 把 10-20 已经发过的稿子又选了一遍，
    白烧 4 次 LLM 调用（约 10 分钟）差点重复发布。

    现在按【槽位 token】比对，两条规则：

    规则 1（同槽）：交易双方 token 对得上（允许法律形式后缀差异，
      且允许双方顺序互换），且资产 token 有交集（或一侧资产未披露）。
    规则 2（角色交叉）：同一个 token 在一个键里是"乙方"、在另一个键里
      是"资产"——这是模型把资产写错槽位造成的，必须能认出来。
      同时要求买方 token 对得上，避免"同资产不同交易"被误合并。
    """
    na, nb = normalize_deal_key(a), normalize_deal_key(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    sa, sb = na.split("|"), nb.split("|")
    if len(sa) < 2 or len(sb) < 2:
        return False

    # 槽位 → token 集合（去掉公司法律形式后缀）
    pa = [_party_tokens(sa[0]), _party_tokens(sa[1])]
    pb = [_party_tokens(sb[0]), _party_tokens(sb[1])]
    ta = _slot_tokens("|".join(sa[2:]))
    tb = _slot_tokens("|".join(sb[2:]))

    # ---- 规则 1：交易双方相同（同 token，允许顺序互换）----
    if _parties_match(pa, pb) and _asset_match(ta, tb):
        return True

    # ---- 规则 2：资产写错了槽位（同一个 token 在一边是"乙方"、
    #      在另一边是"资产"），且买方对得上 ----
    any_party_hit = bool(
        (pa[0] & pb[0]) or (pa[0] & pb[1]) or (pa[1] & pb[0]) or (pa[1] & pb[1]))
    crossing = bool((pa[1] & tb) or (pb[1] & ta))
    if any_party_hit and crossing:
        return True

    return False


def record_published_deals(topics: list[dict], today: str | None = None,
                           cap: int = 5000) -> None:
    """
    topics: [{"lead_id":..., "title":..., "deal_keys": [...]}, ...]
    """
    today = today or today_cst()
    entries = load_published_deals()
    by_key = {normalize_deal_key(e.get("deal_key", "")): e
              for e in entries}
    added = 0
    for t in topics:
        keys = t.get("deal_keys") or (
            [t["deal_key"]] if t.get("deal_key") else [])
        for k in keys:
            nk = normalize_deal_key(k)
            if not nk:
                continue
            item = {
                "date": today,
                "deal_key": nk,
                "lead_id": t.get("lead_id") or t.get("id") or "",
                "title": (t.get("title") or "")[:160],
            }
            hit = by_key.get(nk) or next(
                (e for k, e in by_key.items() if same_deal(nk, k)), None)
            if hit is not None:
                # 同一笔交易再次发表：刷新日期，保证 60 天窗口从最近一次算起
                old = normalize_deal_key(hit.get("deal_key", ""))
                hit.update(item)
                by_key.pop(old, None)
                by_key[nk] = hit
            else:
                entries.append(item)
                by_key[nk] = item
                added += 1
    entries = entries[-cap:]
    write_json(DEALS_FILE, {"version": 1, "entries": entries})
    print(f"  [deals] 记录已发表交易 {added} 笔"
          f"（库中共 {len(entries)} 笔）")


def run(date: str | None = None) -> list[RawItem]:
    date = date or today_cst()
    cfg = load_yaml("sources.yaml")
    thr = cfg.get("dedupe", {}).get("threshold", 82)

    raw = read_json(RAW_DIR / f"{date}.json", [])
    if not raw:
        print(f"[dedupe] 没有原始数据: {RAW_DIR}/{date}.json")
        return []

    items = [RawItem.from_dict(d) for d in raw]
    print(f"[dedupe] 输入 {len(items)} 条")

    items = filter_history(items, today=date)
    before = len(items)
    items = dedupe(items, threshold=thr)
    print(f"[dedupe] 去重后 {len(items)} 条（合并 {before - len(items)} 条）")

    # 注意：这里不写 seen.json。
    # seen 记录的是【已发表】选题，由 compose 成功后调用 record_published。
    write_json(RAW_DIR / f"{date}.deduped.json", [i.to_dict() for i in items])
    return items


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出特殊符号会崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    run(sys.argv[1] if len(sys.argv) > 1 else None)