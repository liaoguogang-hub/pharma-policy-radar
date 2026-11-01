# -*- coding: utf-8 -*-
"""政策范围闸门（scope gate）。

用户口径（2026-11-01）：
    **只放「国家 / 各省医保局、卫健委，国家药监局、CDE 发布的、
    对国家或省份（联盟）产生影响的政策」**。

原来的问题是范围太宽：候选池里混进了公安部目录、工信部规划、化妆品/牙膏公告、
协会联合指南、以及单个药品的批准。这里做确定性闸门（不靠模型自觉）：

  1) 发文机关白名单（正则，命中任一）
  2) 发文机关排除表（命中即出局，优先级高于白名单）
  3) 主题排除（化妆品/牙膏/保健食品等非医药政策）
  4) 影响范围：national / province / alliance 三者之一
  5) 海外：只认国家级监管机构（FDA/EMA/HHS/CMS/EC）的**政策类文件**
     （guidance/rule/regulation/framework…），排除单个药品批准

用法：
    import scope
    ok, why = scope.in_scope(title, content, flags, channel)
"""
from __future__ import annotations

import re

# ---------- 默认规则（可在 scoring.yaml 的 policy_scope 里覆盖）----------
DEFAULTS = {
    "allowed_authorities": [
        r"国家医疗保障局|国家医保局|医疗保障局|医保局",
        r"国家卫生健康委员会|国家卫健委|卫生健康委员会|卫健委|卫生和计划生育委员会|卫计委",
        r"国家药品监督管理局|国家药监局|药品监督管理局|药监局|药品监督",
        r"药品审评中心|药审中心|CDE",
        r"联合采购办公室|联采办",
    ],
    "excluded_authorities": [
        r"公安部|公安厅|公安局",
        r"工业和信息化部|工信部|工业和信息化厅",
        r"财政部|商务部和?|商务部|市场监管总局|市场监督管理总局|市场监督管理局|知识产权局",
        r"国家中医药管理局|中医药管理局",
        r"协会|学会|研究会|基金会|联盟(?!采购)|论坛|商会|"
        r"AMA|American Medical Association|American Academy",
        r"医院|医科大学|医学院|研究院|大学",
        r"海关总署|税务总局|统计局|发展改革委",
    ],
    "excluded_topics": [
        r"化妆品|牙膏|保健食品|食品添加剂|普通食品|消毒产品|涉水产品",
    ],
    # 事务性文件：不是政策（用户口径：要对国家或省份/联盟产生影响）
    "excluded_routine": [
        r"参比制剂目录", r"转换为非处方药", r"说明书修订", r"中药品种保护",
        r"受理公示", r"批准证明文件", r"注册证书", r"挂网价格公示",
    ],
    "province_pattern": r"省|自治区|直辖市|北京市|天津市|上海市|重庆市",
    "alliance_pattern": r"省际联盟|跨省联盟|联盟采购|区域联盟|集采联盟",
    "overseas_authorities": [
        r"FDA|Food and Drug Administration",
        r"EMA|European Medicines Agency|European Commission",
        r"HHS|Department of Health and Human Services",
        r"CMS|Centers for Medicare",
    ],
    "overseas_policy_types": (
        r"guidance|guideline|final rule|proposed rule|rule\b|regulation|"
        r"framework|policy|notice|directive|announcement|strategy|program"
    ),
    "overseas_exclude": (
        r"approves|approval of|grants approval|authoriz(?:es|ation) of|"
        r"clearance of|launches? .{0,40}\b(?:drug|therapy|treatment)\b"
    ),
}


def _cfg(raw: dict | None) -> dict:
    cfg = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if v:
            cfg[k] = v
    return cfg


def _any_pos(patterns, text: str):
    """返回 (位置, 命中词)；无命中返回 None。"""
    best = None
    for p in patterns or []:
        m = re.search(p, text or "", re.I)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.group(0))
    return best


def _any(patterns, text: str) -> str:
    for p in patterns or []:
        m = re.search(p, text, re.I)
        if m:
            return m.group(0)
    return ""


def impact_level(title: str, content: str, flags, channel: str) -> str:
    """national / province / alliance / "" """
    t = f"{title} {content[:1200]}"
    flags = set(flags or [])
    if re.search(r"省际联盟|跨省联盟|联盟采购|区域联盟|集采联盟", t):
        return "alliance"
    if "national" in flags or re.search(r"国务院|国家组织|全国|国家医保局|国家药监局|国家卫健委", t):
        return "national"
    if re.search(r"(?:省|自治区|直辖市)的?(?:医疗保障局|卫健委|卫生健康委|药品监督管理局)", t) \
            or re.search(r"(?:省|自治区|直辖市)(?:医保|卫健|药监)", t):
        return "province"
    if "local" in flags:
        return "province"
    return ""


def in_scope(title: str, content: str, flags=None, channel: str = "china",
             raw_cfg: dict | None = None) -> tuple[bool, str]:
    """返回 (是否在范围内, 原因)。原因只在 False 时有意义。"""
    cfg = _cfg(raw_cfg)
    title = title or ""
    text = f"{title} {content or ''}"

    # 1) 主题排除（非医药政策）
    hit = _any(cfg["excluded_topics"], title)
    if hit:
        return False, f"主题不属于医药政策（{hit}）"

    # 2) 发文机关：按【出现位置】判断谁是牵头机关。
    #
    #   联合发文很常见（医保局+财政部、卫健委+公安部…）。不能一看到财政部/公安部就毙掉：
    #     · "公安部、国家卫生健康委、国家药监局关于…"  → 公安部牵头，出局
    #     · "国家医保局办公室 财政部办公厅关于…"      → 医保局牵头，应保留
    #   规则：白名单机关在标题里的位置 <= 排除机关的位置 → 保留，否则出局。
    scope_cfg = cfg
    _ex = _any_pos(scope_cfg["excluded_authorities"], title)
    _al = _any_pos(scope_cfg["allowed_authorities"], title)
    if _ex and (_al is None or _ex[0] < _al[0]):
        return False, f"牵头机关不在白名单（{_ex[1]}）"

    # 2b) 事务性文件（不是你口径里的"政策"）
    hit = _any(cfg["excluded_routine"], title)
    if hit:
        return False, f"事务性文件，非政策（{hit}）"

    # 3) 海外：只认国家级监管机构的政策类文件
    if channel == "global" or "overseas" in set(flags or []):
        auth = _any(cfg["overseas_authorities"], text)
        if not auth:
            return False, "海外但不是国家级监管机构"
        if not re.search(cfg["overseas_policy_types"], text, re.I):
            return False, f"海外非政策类文件（{auth}）"
        if re.search(cfg["overseas_exclude"], title, re.I):
            return False, "海外单个产品批准，不是政策"
        return True, f"海外国家级政策（{auth}）"

    # 4) 国内：必须命中发文机关白名单
    auth = _any(cfg["allowed_authorities"], text)
    if not auth:
        return False, "发文机关不在白名单（医保局/卫健委/药监局/CDE）"

    # 5) 影响范围：国家 / 省 / 省际联盟
    lvl = impact_level(title, content, flags, channel)
    if lvl not in ("national", "province", "alliance"):
        return False, "无法判定影响范围（需国家/省/省际联盟）"
    return True, f"{auth} / 影响范围={lvl}"
