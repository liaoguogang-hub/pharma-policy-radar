"""
象信 AI 前置闸门（Xiangxin pre-filter）——医药政策雷达。

作用：在昂贵的 LLM 打分之前，用象信先判断「这条到底是不是一条政策」，
把明显不是政策的内容（融资并购、临床读出、股价、人事、科普、招聘榜单）
拦掉，省 LLM 调用。

⚠️ 与另外两个雷达的闸门【判据完全不同】，不要照搬：
  · BD 雷达问「有没有交易对手方」
  · 临床雷达问「有没有具体的临床试验结果或关键节点」
  · 本雷达问「是不是官方机关发出的、对行业有约束力的文件」
  用错判据会大量误杀。

为什么需要这一层：
  本雷达的 _prefilter 是【确定性范围预筛】，只丢「明显出局」的
  （排除主题 / 牵头机关不在白名单 / 事务性文件 / 海外单个产品批准），
  其余全部留给 LLM——单日条目可达 500+ 条。
  象信能读正文，判断「这是不是政策」比关键词表准。

边界（重要）：
  - 象信【不】替代 LLM 打分。发文层级、影响范围、支付规则细节这些
    仍然只有 LLM 能评。
  - 闸门只拦「高置信度的非政策」，低置信度一律放行给 LLM。
  - ⚠️ 本雷达的判据只回答「是不是政策」这一个原子问题，
    【不】判断发文机关在不在白名单、影响范围够不够大——
    那是 scope.py 和 LLM 的活。闸门管得太宽会误杀省级文件。
  - ⚠️ 「单个药品的批准」在海外口径里【不算】政策（见 SYSTEM_PROMPT
    §B），但在国内口径里「批准证明文件」是事务性文件、也不算。
    这一类要拦。
  - 象信服务不可用时，闸门整体旁路，绝不阻断生产。
"""
from __future__ import annotations

import os
import sys
from typing import Any

# 置信度门限：只有象信非常确定「不是政策」时才拦。
# 宁可多花钱给 LLM，也不要漏杀一条该成稿的政策。
BLOCK_CONFIDENCE = 0.80

# 送给象信的摘要最大字符数。象信对长 state 效果下降（SKILL.md §6）。
MAX_SUMMARY_CHARS = 800

# 题目定义（口径对齐 score.py 的 SYSTEM_PROMPT 开头的「先判断」段）
#
# ⚠️ 2026-11-25 调过一轮。第一版把「纯转载通稿、新闻报道（非官方解读）」
#    和「企业产品介绍、推广、招商」笼统写进「非政策」，
#    结果象信把**官方自己发的政策解读文章**也判成非政策。
#    实测漏杀 13 条，全是真政策：
#      「各地医保持续调整优化医疗服务价格」(score 6.7, flags=[national, price])
#      「"医保钱包2.0"上线 广东职工医保个人账户可以跨省共济使用了」
#      「新疆推行DRG/DIP半年清算机制 释放医保基金使用综合效能」
#      「国家医保局公开征集立项指南落地执行具体问题」
#      「口腔医用耗材集采接续启动，预计年底前完成」
#      「省医疗保障事务中心关于公布新增省本级医疗保障定点医药机构的通告」
#
#    教训：判据里**不能靠"像不像通稿"来判断是不是政策**，
#    要看**发布主体是不是官方机关 + 内容是不是政策规则**。
#    官方发的解读/进展/落地报道本身就是政策的一部分。
Q_POLICY = {
    "type": "choice",
    "instructions": (
        "`标题` 和 `摘要` 描述的是否是一条由官方机关（医保局、卫健委、药监局等）"
        "发布或解读的、对医药行业有约束力或指导意义的政策内容？"
    ),
    "criteria": {
        "是政策": (
            "以下任一均算「是政策」："
            "① 机关发布的文件、公告、通知、征求意见稿、目录、集采规则、"
            "处罚决定（法律/条例/部门规章/规范性文件/指导原则/医保目录与支付规则）；"
            "② 上述文件的官方解读、官方答记者问、政策吹风会、官方图解；"
            "③ 各级医保局、卫健委、药监局、CDE、国家组织药品联合采购办公室、"
            "FDA、EMA/欧盟委员会、美国 HHS、CMS 发出的政策类文件"
            "（final guidance / proposed rule / final rule / regulation / "
            "framework / strategy / policy notice）；"
            "④ 面向行业征求意见、公布实施日期/过渡期安排的政策内容；"
            "⑤ 官方对医保待遇、药品定价、集采规则、注册审评流程的说明性文件；"
            "⑥ 官方发布的【政策落地进展/实施动态/价格调整/清算机制/支付方式改革/"
            "集中采购接续/定点机构公布】等内容——这类内容本身就是政策的组成部分；"
            "⑦ 官方对医保钱包等便民服务的上线说明、对药品目录立项指南落地执行"
            "问题的公开征求意见"
        ),
        "非政策": (
            "以下均算「非政策」："
            "企业融资（IPO/定增/Series A/B）、并购、授权交易、BD 合作；"
            "临床试验数据读出、疗效结果；"
            "监管对【单个药品或器械】的批准（FDA Approves…、NMPA 批件公示）；"
            "股价波动、市值、财报、营收、融资新闻；人事变动、组织调整；"
            "会议通知、培训班、征稿、讲座、奖项、招聘、榜单；"
            "企业产品介绍、推广、招商；"
            "不涉及政策规则的行业评论、市场分析、观点文章、软文、科普"
        ),
    },
}


def _state(item: Any) -> dict[str, str]:
    """构造象信 state。只放判断需要的内容。"""
    if isinstance(item, dict):
        title = item.get("title") or ""
        body = item.get("summary") or item.get("content") or ""
    else:  # RawItem
        title = getattr(item, "title", "") or ""
        body = (getattr(item, "summary", "") or
                getattr(item, "content", "") or "")
    return {
        "标题": title,
        "摘要": (body or "")[:MAX_SUMMARY_CHARS],
    }


def _has_key() -> bool:
    return bool(os.getenv("XIANGXIN_API_KEY"))


def _has_text(item: Any) -> bool:
    """条目是否至少有标题或摘要可判。

    ⚠️ 这条不能省。实测踩过的坑（2026-11-25 临床雷达回归时发现）：
    历史 scored 数据里存在 title / summary / content 【全空】的条目
    ——正文抓取失败时只剩 id。这类条目送进象信时 state 是空的，
    象信只能凭两个空字符串猜，结果一律猜成「非政策」，
    于是一条条真实政策被误杀。

    实测（临床雷达 43 天历史数据）：
      拦掉 1176 条（28.8%），其中 10 条是 score>=6.0 的真实读出，
      全部是空 state 的条目。

    规则：**没有文本就不判**。空条目直接放行给 LLM——
    反正 LLM 也拿不到素材（compose 阶段 material_too_thin 会拦），
    但至少不会在闸门这层被误杀。
    """
    st = _state(item)
    return bool((st["标题"] or "").strip() or (st["摘要"] or "").strip())


def filter_items(items: list[Any]) -> tuple[list[Any], dict[str, Any]]:
    """
    象信前置闸门。

    返回 (放行的条目, 统计信息)。
    任何异常都会退化为「全部放行」，绝不阻断生产。
    """
    stats: dict[str, Any] = {
        "enabled": False,
        "total": len(items),
        "blocked": 0,
        "passed": len(items),
        "error": None,
        "blocked_ids": [],
    }

    if not _has_key():
        stats["error"] = "XIANGXIN_API_KEY 未设置，闸门旁路"
        return items, stats

    try:
        from xiangxin import Choice, XiangxinClient
    except ImportError:
        stats["error"] = "xiangxin-sdk 未安装，闸门旁路"
        return items, stats

    question = Choice(
        instructions=Q_POLICY["instructions"],
        criteria=Q_POLICY["criteria"],
    )

    def _item_id(it: Any) -> str:
        if isinstance(it, dict):
            return it.get("id") or ""
        return getattr(it, "id", "") or ""

    try:
        with XiangxinClient() as client:
            passed = []
            for it in items:
                iid = _item_id(it)
                # 空 state 不判：象信只能凭空字符串猜，一定猜成「非政策」，
                # 会把真实政策误杀。详见 _has_text 的注释。
                if not _has_text(it):
                    stats["no_text"] = stats.get("no_text", 0) + 1
                    passed.append(it)
                    continue
                try:
                    resp = client.system_one(
                        state=_state(it),
                        questions={"policy": question},
                    )
                    ans = resp.answers["policy"]
                    if ans.choice == "非政策" and ans.confidence >= BLOCK_CONFIDENCE:
                        stats["blocked"] += 1
                        stats["blocked_ids"].append(iid)
                        continue
                except Exception as e:  # 单条失败不阻断整批
                    # 鉴权失败是全局性的，继续打只会白费 401 —— 整体旁路
                    if type(e).__name__ == "AuthenticationError":
                        stats["error"] = f"AuthenticationError: {e}"
                        print(f"[xiangxin-gate] 鉴权失败，全部放行: {stats['error']}")
                        return items, stats
                    print(f"[xiangxin-gate] 单条失败，放行: {type(e).__name__}: {e}")
                passed.append(it)
    except Exception as e:
        stats["error"] = f"{type(e).__name__}: {e}"
        print(f"[xiangxin-gate] 初始化失败，全部放行: {stats['error']}")
        return items, stats

    stats["enabled"] = True
    stats["passed"] = len(passed)
    return passed, stats


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    demo = [
        {"id": "a", "title": "国家医疗保障局关于印发《全民医疗保障“十五五”规划》的通知",
         "summary": "经国家医保局党组会议审议通过，现印发给你们，请认真组织实施。"
                    "规划明确了参保率、基金支付比例、长期护理保险等指标。"},
        {"id": "b", "title": "某省医疗保障局关于印发《XX省职工医保门诊共济实施办法》的通知",
         "summary": "自2026年起实施，扩大门诊统筹范围，明确个人账户共济规则。"},
        {"id": "c", "title": "国家药监局关于发布《化学药品创新药上市申请前会议药学共性问题指导原则》的通告",
         "summary": "指导原则对创新药注册申报的药学研究提出技术要求。"},
        {"id": "d", "title": "礼来以 2 亿美元首付款获得诺诚健华 BTK 降解剂全球权益",
         "summary": "双方达成授权许可交易，礼来支付首付款。"},
        {"id": "e", "title": "默沙东 TROP2 ADC 胃食管腺癌三期总生存达标",
         "summary": "III 期研究达到主要终点总生存，HR 0.62，473 例三线患者。"},
        {"id": "f", "title": "某医药公司招聘销售代表若干名",
         "summary": "任职要求：本科以上学历，有医院渠道资源者优先。"},
    ]
    passed, st = filter_items(demo)
    print(f"总 {st['total']} → 放行 {st['passed']}，拦掉 {st['blocked']}")
    print(f"error: {st['error']}")
    for it in passed:
        print("  放行:", it.get("id"), (it.get("title") or "")[:60])
    for iid in st["blocked_ids"]:
        t = next((x.get("title") for x in demo if x.get("id") == iid), "")
        print("  拦掉:", iid, (t or "")[:60])
