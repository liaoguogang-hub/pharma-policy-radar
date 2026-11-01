"""
噪声过滤器（政策口径）。

问题：政策雷达的候选里混着大量"看着像政策、其实不是政策"的东西——
企业软文 / 产品推广、会议通知、人事任免、纯转载通稿、科普问答、
地方事务性小文件（无全国影响）、招聘与榜单。这些标题里也带着"药""医保"
"监管"这些词，命中法拦不住，只能按"它是不是一份文件"来判断。

策略（两级）：
1. 噪声黑名单 —— 标题命中即丢（软文 / 会议 / 人事 / 转载 / 科普 / 招聘 / 榜单）
2. 源分级 —— 官方源与垂直医药源放宽，综合财经源收紧
   （综合财经源必须同时带"政策强信号"和"医药属性"才留）

刻意 fail-open：来源不明的源默认保留，交给评分层判断。
误杀一条政策原文的代价，比多花一次 LLM 打分大得多。

保留的公开接口（签名与调用方式不变）：
    is_noise(title, source) -> bool
    is_weak(title) -> bool
    filter_items(items) -> (kept, dropped)
"""
from __future__ import annotations

import re

# ============ 噪声黑名单（标题命中即丢弃）============
NOISE_PATTERNS = [
    # 企业软文 / 产品推广 / 招商加盟
    r"新品上市|重磅上市|正式上市|全球首发|首发上市|招商加盟|代理招募|"
    r"火热招商|限时优惠|促销|周年庆|品牌焕新|荣获|斩获|"
    r"\b(?:launch(?:es|ed)? (?:its|new)|now available|order now|"
    r"sponsored|advertorial|webinar)\b",
    # 会议 / 活动（通知本身不是政策）
    r"会议通知|论坛|峰会|研讨会|沙龙|培训班|研修班|宣讲会|直播预告|"
    r"邀请函|参会报名|展位|博览会",
    # 人事任免
    r"任免|任命|免去|辞去|辞职|离任|履新|人事变动|领导班子调整|拟任|"
    r"(?:CEO|CFO|CMO|总经理|董事长|院长).{0,6}(?:离职|辞职|上任|履新)",
    # 纯转载 / 通稿（二手到没有新增信息）
    r"^\s*转载|转自|全文转载|原文链接|综合报道|通稿",
    # 科普 / 问答 / 养生
    r"科普|十问十答|一问一答|问答|小知识|医生提醒|"
    r"如何(?:看|选|用|预防)|能(?:治|吃)吗|吃什么",
    # 招聘 / 榜单
    r"招聘|诚聘|招贤|纳贤|简历投递|岗位职责|榜单|排行榜",
]
NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.I)

# ============ 地方事务性小文件（无全国影响）============
# 地方机关 + 事务性文种 + 没有任何全国性信号 → 噪声。
# 三个条件同时成立才动手；省级政策文件本身也在这个管辖范围内
# （policy_level 0.8 打不到 keep 6.0，留下来只是白占池子）。
LOCAL_BODY_RE = re.compile(
    r"(?:省|市|县|区|盟|旗)(?:医保局|医疗保障局|药监局|药品监督管理局|"
    r"卫生健康委|卫健委|市场监管局|人民政府|人社厅|财政厅|中医药局)")
MUNICIPAL_TXN_RE = re.compile(
    r"通知|公告|工作动态|工作简报|公示|名单|答复|复函|检查计划|"
    r"抽检|实施方案|考核|通报|会议")
NATIONAL_MARKERS = re.compile(
    r"国家|全国|国务院|部委|总局|海关总署|全行业")

# ============ 政策强信号（命中即算政策话题）============
STRONG_POLICY = re.compile(
    r"国务院|国家发展改革委|国家发改委|国家医保局|国家医疗保障局|"
    r"国家药监局|国家药品监督管理局|国家卫生健康委|国家卫健委|"
    r"国家中医药局|工业和信息化部|工信部|财政部|商务部|市场监管总局|"
    r"国家疾控局|NMPA|NHSA|FDA|EMA|WHO|CDE|药审中心|"
    r"征求意见|管理办法|管理规定|实施细则|指导意见|实施意见|"
    r"条例|规定|办法|方案|公告|通知|规范性文件|指导原则|"
    r"医保目录|支付标准|集采|带量采购|DRG|DIP|"
    r"审评审批|注册|监管|处罚|执行|落地|新规|政策",
    re.I)

# 医药属性（辅助判断这是医药内容，不是稀土/航运那种"通知"）
PHARMA_TOPIC = re.compile(
    r"\b(?:drug|medicine|pharma|biotech|vaccine|device|IVD|generic|"
    r"antibody|ADC|cell therapy|gene therapy|prescription|hospital|"
    r"reimbursement|procurement|regulator)\b"
    r"|医药|药品|医疗机构|器械|耗材|疫苗|中药|创新药|仿制药|处方|药店|"
    r"医院|医保|集采|药监|卫健委|临床|注册|审评|支付|价格|患者",
    re.I)

# ============ 源分级 ============
# 官方源 / 垂直医药源：只过黑名单，其余交给评分层
OFFICIAL_SOURCE_RE = re.compile(
    r"nmpa|nhsa|nhc|gov|gov_|cde|fda|ema|who|official|policy|regulat",
    re.I)

# 综合财经源：收紧——必须带政策强信号 + 医药属性，否则丢
GENERAL_SOURCES = {"stcn_medicine", "nbdd_cn", "cnstock_health"}

# 弱相关（保留但降权）：二手解读/评论（不是原文，也不是软文）
SECONDHAND_RE = re.compile(
    r"解读|图解|一图读懂|一文读懂|速览|划重点|答记者问|专访|观察|"
    r"评论|述评|前瞻|盘点|影响分析|如何理解")


def is_noise(title: str, source: str) -> bool:
    """
    判断是否噪声。
    返回 True = 噪声，丢弃。
    """
    t = (title or "").strip()
    if len(t) < 10:
        return True

    # 黑名单命中 → 噪声
    if NOISE_RE.search(t):
        return True

    # 地方事务性小文件（地方机关 + 事务性文种 + 无全国信号）→ 噪声
    if (LOCAL_BODY_RE.search(t) and MUNICIPAL_TXN_RE.search(t)
            and not NATIONAL_MARKERS.search(t)):
        return True

    src = (source or "").lower()

    # 综合财经源：既要有政策强信号，也要有医药属性
    if src in GENERAL_SOURCES:
        if not STRONG_POLICY.search(t):
            return True
        if not PHARMA_TOPIC.search(t):
            return True
        return False

    # 官方源 / 垂直医药源 / 未知源：只过黑名单（fail-open）
    return False


def is_weak(title: str) -> bool:
    """
    弱相关内容：是政策话题，但属于二手解读/评论（原文不在手上），
    保留但降权——成稿前必须补官方原文。
    """
    t = (title or "").strip()
    if not t:
        return True
    if SECONDHAND_RE.search(t) and not STRONG_POLICY.search(t):
        return True
    # 只有"解读"没有文件要素（文号/日期/条款），说明手上的只是别人的转述
    if SECONDHAND_RE.search(t) and not re.search(
            r"第\d+号|文号|〔\d{4}〕|\[\d{4}\]|生效|施行|发布|印发", t):
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
