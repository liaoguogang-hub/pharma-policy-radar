"""
噪声过滤器。

问题：证券时报/每经这类综合财经媒体，首页混了大量与BD 无关的内容
（退市、稀土价格、苹果订单、航运海峡、电视剧）。
实测 99 条里，证券时报 10 条有 7 条是噪声。

策略：两级过滤
1. 噪声黑名单 —— 明确的非医药/非交易话题
2. 医药源加权 —— 医药垂直源放宽，综合财经源收紧
"""
from __future__ import annotations

import re

# ============ 噪声黑名单（标题命中即丢弃）============
NOISE_PATTERNS = [
    # 资本市场/股价
    r"锁定退市|退市风险|股价连续|涨停潮|拉高|抄底",
    r"\bdelist|delisting|stock price|shares? (?:rise|fall|jump|plunge)\b",
    # 宏观/产业无关
    r"稀土|苹果|iPhone|手机订单|油价|航运|海峡|干散货|煤业|矿价",
    r"霍尔木兹|OPEC|关税|汇率|降息|加息",
    # 娱乐/社会
    r"综艺|真人剧|芒果|会员拉新|电视剧",
    # 人事/行政
    r"general counsel.{0,20}(?:left|resign)|总经理辞职|CEO离任",
    # 注意：科学家/高管加盟要保留（如"两位前莫德纳核心科学家加盟"是实质行业动态）
    # 反垄断/法务（非BD）
    r"反垄断审查通过|诉.{0,10}政府|集体诉讼",
    # 招聘/学术会议广告
    r"媒体招募|大会.{0,6}开幕|论坛|研讨会.{0,4}举办|欢迎报名",
]

NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.I)

# ============ 例外：即使命中黑名单也保留（确认过的实质内容）============
KEEP_ANYWAY = re.compile(
    r"核心科学家.{0,6}加盟|科学家.{0,4}加盟|"
    r"科学家|研发.{0,4}总监|医学总监.{0,6}(?:加盟|履新)",
    re.I,
)


# ============ BD 强相关词（命中即保留）============
STRONG_BD = re.compile(
    r"\b(license[- ]?(?:out|in)|licensing deal|acquire|acquisition|merger|"
    r"collaboration|partnership|option agreement|upfront payment|milestone|"
    r"royalty|asset purchase|spin[- ]?off|takeover|buyout|stake)\b"
    r"|授权|收购|合作|许可|首付款|里程碑|分成|权益|引进|对外授权|共同开发",
    re.I,
)

# 医药/科学强相关（辅助判断这是医药内容）
PHARMA_TOPIC = re.compile(
    r"\b(drug|molecule|antibody|ADC|bispecific|cell therapy|CAR[- ]?T|"
    r"phase [123]|trial|FDA|NMPA|pipeline|therapeutic|oncology|"
    r"immunology|inflammation|obesity|diabetes|cardio|rare disease)\b"
    r"|新药|创新药|临床|获批|适应症|靶点|管线|单抗|双抗|细胞治疗|肿瘤|"
    r"免疫|自免|减重|糖尿病|心血管|罕见病|医保",
    re.I,
)

# 非 BD 的"合作/协议"语境（综合财经常见，需排除）
FAKE_DEAL = re.compile(
    r"战略合作框架协议|签订.{0,6}(?:框架)?协议|举行.{0,8}启动会|"
    r"共筑|共建.{0,10}平台|战略合作.{0,6}(?:仪式|启幕)",
)


# ============ 垂直医药源（放宽）============
PHARMA_SOURCES = {
    "phirda_industry", "pharnexcloud", "pharmcube",
    "biopharmadive_deals", "biopharmadive_home", "stat_biotech",
    "stat_pharma", "endpoints_rss", "bioworld_deals", "bioworld_asia",
    "pharmexec_partnership", "pharmexec_irlp",
}

# 综合财经源（收紧）
GENERAL_SOURCES = {"stcn_medicine", "nbdd_cn", "cnstock_health"}


def is_noise(title: str, source: str) -> bool:
    """
    判断是否噪声。
    返回 True = 噪声，丢弃。
    """
    t = title.strip()
    if len(t) < 10:
        return True

    # 例外白名单优先
    if KEEP_ANYWAY.search(t):
        return False

    # 黑名单命中 → 噪声
    if NOISE_RE.search(t):
        return True

    # 综合财经源：必须有 BD 强信号，且不能是假合作
    if source in GENERAL_SOURCES:
        if FAKE_DEAL.search(t):
            return True
        if not STRONG_BD.search(t):
            return True
        # 还需带医药属性，避免"稀土合作框架协议"这类
        if not PHARMA_TOPIC.search(t):
            return True
        return False

    # 垂直医药源：排除黑名单即可，其余交给后续 RELEVANT 过滤
    return False


def is_weak(title: str) -> bool:
    """弱相关内容：可能是 BD 但信息量低，保留但降权"""
    t = title.lower()
    # 只有泛泛的"合作"没有具体交易细节
    if re.search(r"合作", t) and not re.search(
            r"亿美元|万美元|首付|里程碑|授权|收购|license|\$\d", t):
        return True
    return False


def filter_items(items: list) -> tuple[list, list]:
    """
    返回 (保留, 丢弃)。
    items 需有 .title 和 .source 属性，或为 dict。
    """
    kept, dropped = [], []

    def get(x, k):
        return x.get(k) if isinstance(x, dict) else getattr(x, k, "")

    for it in items:
        if is_noise(get(it, "title"), get(it, "source")):
            dropped.append(it)
        else:
            kept.append(it)

    return kept, dropped