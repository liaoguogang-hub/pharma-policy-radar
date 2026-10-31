#!/usr/bin/env python3
"""
去 AI 味扫描器（de-AI-flavor gate）— 零外部依赖，可直接 vendor 进任何仓库。

为什么单独一个文件：
用户 2026-10-24 指出「写出来的稿子 AI 味很重，例如『不是…而是』」。
逐词替换没用——那不是词的问题，是**造句模板 + 段落结构**的问题。
所以闸门必须同时看三样东西：

    1. AI 句式模板（跨标点匹配，0 容忍）    → 硬闸门
    2. 段落长度分布（中位数 ≤ 90 字）        → 软闸门（只提示）
    3. 句子长度分布（短句 ≥10%，长句 ≤30%）  → 软闸门（只提示）

⚠️ 历史上最大的坑：老正则 `不是[^，。；]{0,18}而是` 排除了逗号，
   而真实稿子里绝大多数是「不是 X，而是 Y」带逗号的形态 → 一条都命中不了，
   看上去"检测通过"，实际零漏检。**所有这类正则必须跨标点匹配。**

命令行：
    python check_ai_flavor.py <file.html> [more.html ...]
    python check_ai_flavor.py --text "要检查的正文"
    python check_ai_flavor.py --scope in-place <file.html>
    python check_ai_flavor.py --feedback <file.html>
    退出码 0 = 通过，1 = 有硬闸门未过

编辑范围（--scope）：
    structural  可删、并、重排（默认）
    bounded     不删整句、不并句、不重排；只句内清理
    in-place    不删句、不并句、不重排；只句内替换或删修饰
    → 受限 scope 下，需要重排/拆句的建议降级为「需授权」提示，
      但检出照常报告（句式命中和 P0 虚假归因在任何 scope 下都仍是硬闸门）。

作为库：
    from check_ai_flavor import scan_html, scan_text
    r = scan_html(html)
    if not r["passed"]: print(r["reasons"])
"""
from __future__ import annotations

import re
import statistics
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# 1. AI 句式模板
# --------------------------------------------------------------------------
# 一律跨标点匹配：用 [^。；！？\n] 排除句号级标点，但【保留逗号】。

# 硬闸门组：命中 1 处就要改（改法是拆句，不是换词）
AI_PATTERNS: dict[str, str] = {
    "不是…而是":      r"不是[^。；！？\n]{0,30}?而是",
    "不在于…而在于":  r"不在于[^。；！？\n]{0,25}?而在于",
    "不只是…更是":    r"不只是[^。；！？\n]{0,25}?(?:更是|也更是)",
    "既…又…还":       r"既[^。；！？\n]{0,12}?又[^。；！？\n]{0,12}?还",
    "问题不在":        r"问题不在",
    "关键在于":        r"关键在于",
    "更重要的是":      r"更重要的是",
    "换句话说":        r"换句话说",
    "也就是说":        r"也就是说",
    "这意味着":        r"这意味着",
    "归根结底":        r"归根结底",
    "本质(上)是":      r"本质(?:上)?是",
    "与其说":          r"与其说",
    "真正的":          r"真正的",
    "真正是":          r"真正是",
    "值得注意":        r"值得注意",
    "需要强调":        r"需要强调",
    "必须说清楚":      r"必须说清楚",
    "最该被记住":      r"最该被记住",
    "绕不开":          r"绕不开",
    "摆在眼前":        r"摆在眼前",
    "之所以":          r"之所以",
    "力求":            r"力求",
    "旨在":            r"旨在",
    # ── de-ai-flavor 规则 1：物理动作动词形容思维（翻译腔）────
    "击穿":            r"击穿",
    "拉齐":            r"拉齐",
    "深耕":            r"深耕",
    # ⚠️ 「闭环」不放这里：生物医药语境里"预测-验证-迭代的闭环"是
    #    正当术语（实测 6 处命中全是这类），硬拦会误杀。
    # ── de-ai-flavor 规则 3：抽象名词主语 + 形容词结论 ────────
    # ⚠️ 必须是「形容词 + 一套」，否则"这是一套通用的阅读顺序"这类
    #    正常表达会被误杀（实测误伤 1 处）。
    "提供了一套":      r"提供了一套(?:生产级|生产环境|完整|完善|成熟|高效|标准化|系统化|开箱即用)",
    "为.*生态带来":     r"为[^。；！？\n]{0,20}生态带来",
    # ── avoid-ai-writing-zh #8：公式化递进连接词 ──────────────
    # ⚠️ 不能用正则跨句匹配：真实稿子里「首先…。」句号「其次…。」句号「最后…」
    #    是三句，句号把正则切断了（实测漏检）。
    #    正确做法：按【段落】检查三个连接词是否共现 → 见 _firstly_clusters()
    "第一第二第三":     r"第一[^。；！？\n]{0,60}?第二[^。；！？\n]{0,60}?第三",
    # ⚠️ 「一方面…另一方面」不放硬闸门：临床/综述文体里它常用于
    #    描述真实的权衡（"一方面避免了盲法困难，另一方面可能引入偏倚"），
    #    实测 2 处命中都是正当用法。改走密度/P2 分级。
    # ── avoid-ai-writing-zh #16b：自我降格声明 ────────────────
    "自我降格":         r"(?:不是[^。；！？\n]{0,20}?，?更像是|只是一?(?:一些|点|篇)|供大家参考|抛砖引玉|不一定对)",
    # ── avoid-ai-writing-zh #31/#32：模板化开头结尾 ────────────
    "模板化开头":       r"(?:在[^。；！？\n]{0,15}?快速发展的今天|随着[^。；！？\n]{0,20}?的(?:日趋成熟|不断发展)|众所周知)",
    "模板化结尾":       r"(?:让我们拭目以待|未来已来|这只是一个开始|拭目以待)",
    # ── avoid-ai-writing-zh §六：聊天机器人残留 ───────────────
    "服务语气":         r"(?:希望这篇文章对你有帮助|如果你觉得有用|欢迎分享|让我们一起来看看|接下来[，,]?让我)",
}

# ⚠️ 轻微项：报出来让人改，但不拦。
#
# 分级的理由：de-ai-flavor 规则 2「形容词预判 + 冒号」是真实的 AI 模式，
# 但它改起来只要删一个词（"实操上第一道分水线很简单：" → "实操上第一道分水线："），
# 属于打磨，不是结构问题。硬拦会让闸门对"基本干净"的稿子也亮红灯，
# 时间久了就没人看这个灯了。
ADVISORY_PATTERNS: dict[str, str] = {
    "评价+冒号":       r"(?:很简单|有几个亮点|最让人印象深刻的是|核心理念是|清晰分层|值得一看|值得一提的是|最关键的是)[：:]",
}

# 真正的 AI 味【词】——可以少量豁免（词能替换，模板改不掉）
# Tier 1：见到就标记（avoid-ai-writing-zh #17）
AI_WORDS_TIER1: tuple[str, ...] = (
    "值得注意的是", "需要注意的是", "综上所述", "总而言之",
    "不言而喻", "至关重要", "具有里程碑意义", "赋能",
    "底层逻辑", "抓手", "护城河", "本文将", "本系列将", "让我们",
    "全方位", "颗粒度", "降本增效", "范式转移", "破圈",
    "行稳致远", "未来可期", "相辅相成", "应运而生", "与时俱进",
)

# Tier 2：聚集时标记（avoid-ai-writing-zh #18）——同一段 2+ 个才报
AI_WORDS_TIER2: tuple[str, ...] = (
    "本质上", "某种意义上", "真正的", "核心", "关键在于",
    "换句话说", "从某种角度来看", "不可忽视", "毋庸置疑",
    "事实上", "总的来说", "不难发现", "显而易见", "可以说",
    "本身", "恰恰", "往往", "其实", "某种程度上",
)

# Tier 3：humanizer 第 12 条词表的中文对应（blader/humanizer v3.0.0）
#
# 背景：humanizer 的 25 条模式里，只有第 12 条是【纯词表】（约 30 个英文高频词）：
#   actually / additionally / crucial / delve / enhance / fostering /
#   pivotal / robust(比喻义) / showcase / tapestry / testament /
#   underscore / vibrant
# 这些词的共同特征是——**AI 味主要来自词本身，而不是句式**。
# 它们正好补上「0 句式命中」这个闸门覆盖不到的盲区：
# 一段话可以句式全干净，但满篇「深入探讨 / 至关重要 / 彰显」。
#
# ⚠️ 为什么单独成 Tier 3 而不是并入 Tier 1：
#   humanizer 明确说「词表之外的正式用词单独出现不算问题」，
#   反过来说，词表内的词**单独出现也未必是问题**——
#   「至关重要」在报道一个临床终点时就是准确的。
#   所以这批词按【密度】判定，不按次数硬拦。
#   （Tier 1 是"见到就标记但可豁免 2 次"，Tier 3 是"密度超标才提示"，
#   两者的宽松度不同，不能混。）
#
# 对照表（英文 → 中文）：
#   delve → 深入探讨 / 深入剖析    crucial → 至关重要 / 关键性
#   pivotal → 关键性的 / 举足轻重  enhance → 增强 / 赋能
#   fostering → 培育 / 促进        showcase → 展示 / 呈现
#   tapestry → 错综复杂 / 多元     testament → 印证 / 见证
#   underscore → 凸显 / 强调      vibrant → 充满活力的 / 蓬勃
#   additionally → 此外 / 另外     actually → 实际上 / 其实
AI_WORDS_TIER3: tuple[str, ...] = (
    # delve
    "深入探讨", "深入剖析", "深度剖析", "深入分析",
    # crucial / pivotal
    "至关重要", "举足轻重", "关键性的",
    # enhance
    "赋能", "增强了", "极大提升了",
    # fostering
    "培育了", "培育出", "促进了", "助推了",
    # showcase
    "展示出", "展示了", "呈现了",
    # tapestry
    "错综复杂", "多元",
    # testament / underscore
    "印证了", "见证了", "凸显了", "凸显出",
    # vibrant
    "充满活力的", "蓬勃", "活力四射",
    # additionally / actually
    "此外还", "实际上也", "值得一提的是", "值得一提的是",
)

# 缓冲词（avoid-ai-writing-zh #20c）——全文密度上限
BUFFER_WORDS: tuple[str, ...] = (
    "这件事", "这个事情", "本身", "没有那么", "并没有那么",
    "恰恰", "真正", "往往", "其实", "某种程度上",
)

# 假归因（avoid-ai-writing-zh #20）——P0，必须改
FAKE_ATTRIBUTION: tuple[str, ...] = (
    "业界普遍认为", "众所周知", "很多人都在讨论", "业内公认",
    "据我观察", "我个人认为",
)

# 物理动作动词形容抽象思维（de-ai-flavor 规则 1）
PHYSICAL_VERBS: tuple[str, ...] = (
    "接住", "击穿", "拆解", "锋利", "打通", "抓住", "落地",
    "深耕", "赋能", "抓手", "沉淀", "闭环", "拉齐", "对齐",
    "链路", "痛点", "颗粒度", "掀掉", "炸出来", "照亮",
    "扛不住", "吃掉了", "撑不起", "追不上",
)

# AI_WORDS 保留为旧接口别名
AI_WORDS = AI_WORDS_TIER1

# --------------------------------------------------------------------------
# 2. 阈值（用 69 篇真稿校准过，不是拍脑袋）
#    语料：雷达已发布 65 篇 + 用户已接受 4 篇
#    实测：段中位中位数 85（雷达）/ 58（重写稿）；短句中位数 12.5% / 22.7%
#
#    密度类阈值来自 avoid-ai-writing-zh 的明文上限。
# --------------------------------------------------------------------------

THRESHOLDS = {
    "para_median_max": 90,     # 段落长度中位数（字）
    "short_ratio_min": 0.10,   # 短句（≤12 字）占比下限
    "long_ratio_max": 0.30,    # 长句（>45 字）占比上限
    "short_max_len": 12,
    "long_min_len": 45,
    "word_tolerate": 2,        # AI 味词豁免次数
    # ── 密度类（单位：每 1000 字）─────────────────────────────
    # 上限来自 avoid-ai-writing-zh 的明文规定，再按 67 篇真稿校准：
    #   破折号 1/千字（参考值）→ 1.5（1.0 会误伤用户已接受的重写稿）
    #   缓冲词 2/千字（参考值 2）→ 超标 10% 篇数
    #   物理动词 1/千字（参考值约 1）→ 超标 9% 篇数
    "buffer_per_1k_max": 2,    # 缓冲词密度（avoid-ai-writing-zh #20c）
    "physical_per_1k_max": 1,  # 物理动词密度（de-ai-flavor 规则 1）
    "dash_per_1k_max": 1.5,    # 破折号密度（avoid-ai-writing-zh #24）
    "notbut_max": 0,          # 「不是…而是」：逐处硬拦，此处仅留计数
    "firstly_max": 0,         # 「首先其次最后」全文上限（#8）
    "fake_attribution_max": 0,  # 虚假归因（P0）
    "tier2_per_para_max": 2,  # Tier 2 词单段聚集上限（#18）
    # Tier 3（humanizer §12 中文词表）密度上限，单位：每 1000 字。
    # 比 Tier 2 宽，因为单个词可能完全正当（"至关重要"描述临床终点）。
    # 5/千字 ≈ 300 字里出现 1.5 个，意思是「通篇都在用这类渲染词」。
    "tier3_per_1k_max": 5,
    "bold_max": 5,            # 加粗处数上限（#21）
    "bullets_max": 4,         # 列表组数上限（#22）
    "metaphor_per_500_max": 2,  # 比喻密度（#33）
}

# --------------------------------------------------------------------------
# 3. 文本抽取
# --------------------------------------------------------------------------

_TAG_SPLIT = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(
    r"</(?:p|div|section|h1|h2|h3|h4|li|tr|blockquote)>|<br\s*/?>", re.I)
_TABLE = re.compile(r"(?is)<table.*?</table>")
_STYLE = re.compile(r"(?is)<style.*?</style>")
_SCRIPT = re.compile(r"(?is)<script.*?</script>")
_HEAD = re.compile(r"(?is)<head.*?</head>")
_FOOTNOTE = re.compile(
    r"(?is)<(?:p|section)[^>]*class=\"[^\"]*\bfn\b[^\"]*\"[^>]*>.*?</(?:p|section)>")
_HTML_ENT = {
    "&nbsp;": " ", "&amp;": "&", "&quot;": '"', "&#39;": "'",
    "&lt;": "<", "&gt;": ">", "&mdash;": "—", "&ldquo;": "“",
    "&rdquo;": "”", "&hellip;": "…",
}


def _unescape(t: str) -> str:
    for a, b in _HTML_ENT.items():
        t = t.replace(a, b)
    return t


def html_to_prose(html: str) -> str:
    """把文章 HTML 抽成「散文正文」。

    表格和脚注要剔除——表格单元格不是句子，会污染句长分布；
    脚注是来源列表，也不是散文。
    """
    h = _STYLE.sub(" ", html)
    h = _SCRIPT.sub(" ", h)
    h = _HEAD.sub(" ", h)
    h = _FOOTNOTE.sub(" ", h)
    h = _TABLE.sub(" ", h)
    h = _BLOCK_END.sub("\n", h)
    h = _TAG_SPLIT.sub(" ", h)
    h = _unescape(h)
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in h.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def plain_to_prose(text: str) -> str:
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


# --------------------------------------------------------------------------
# 4. 编辑范围（scope）
# --------------------------------------------------------------------------
#
# 来源：MrGeDiao/shuorenhua 的三档编辑范围模型。
#
# 为什么扫描器必须知道 scope：
# 「去 AI 味」这个请求本身是**歧义**的——用户可能只想清掉套话，
# 也可能允许你重排段落。如果用户只授权句内修改（in-place），
# 扫描器却要求"段落中位数 ≤90 字"（需要拆段），那条建议就超出了授权范围。
# 反过来，用户授权了 structural 却不拆段，就是漏掉了本可以做的工作。
#
# 三档：
#   structural —— 可删、并、重排。全部建议都提。
#   bounded    —— 不删整句、不并句、不重排；只句内清理。
#                 需要拆段/拆句的建议降级为提示，不进软闸门。
#   in-place   —— 不删句、不并句、不重排；只句内替换或删修饰。
#                 结构性建议全部降级为提示。
#
# ⚠️ 注意：scope 只影响【建议的分级】，不影响【检出本身】。
#    命中还是要报出来（否则用户不知道有这些问题），只是不再当作必须改的项。
#    唯一例外：P0 虚假归因在任何 scope 下都是硬闸门——
#    它是可信度问题，不是风格问题，跟编辑范围无关。

SCOPES = ("structural", "bounded", "in-place")
DEFAULT_SCOPE = "structural"

# 需要改动句/段结构的建议 → 在受限 scope 下降级
_STRUCTURAL_ADVISORY_MARKERS = ("段落长度中位数", "长句占比")


def apply_scope(res: dict, scope: str) -> dict:
    """按编辑范围给结果重新分级。

    - 把需要结构性改动的软提示，在 bounded/in-place 下移出软提示，
      改放进 scope_notes（单独一栏，明确说明"这些要改，但需要额外授权"）。
    - P0 虚假归因、句式命中**不受 scope 影响**。
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")
    res["scope"] = scope
    if scope == "structural":
        res.setdefault("scope_notes", [])
        return res

    kept: list[str] = []
    notes: list[str] = []
    for r in res.get("soft_reasons", []):
        if any(m in r for m in _STRUCTURAL_ADVISORY_MARKERS):
            notes.append(
                f"[需授权] {r}\n"
                f"  当前 scope={scope}，需要重排/拆句才能改。\n"
                f"  改法：用户说「可以重写」或切到 structural 后再处理。")
        else:
            kept.append(r)
    res["soft_reasons"] = kept
    res["scope_notes"] = notes
    return res


# --------------------------------------------------------------------------
# 5. 指标计算
# --------------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"[。！？!?]+")
_CN = re.compile(r"[\u4e00-\u9fff]")


def _cn_len(s: str) -> int:
    return len(_CN.findall(s))


def _sentences(prose: str) -> list[str]:
    out: list[str] = []
    for para in prose.split("\n"):
        for s in _SENT_SPLIT.split(para):
            s = s.strip()
            if _cn_len(s) >= 4:      # 丢掉「好。」这类太碎的碎片
                out.append(s)
    return out


def _paragraphs(prose: str) -> list[str]:
    out = []
    for p in prose.split("\n"):
        if _cn_len(p) >= 20:        # 太短的行是标题/标签，不算段落
            out.append(p)
    return out


# 公式化递进的连接词组（三者同时出现才算报幕式）
_FIRSTLY_SETS: tuple[tuple[str, ...], ...] = (
    ("首先", "其次", "最后"),
    ("第一", "第二", "第三"),
    ("一方面", "另一方面", "此外"),
)


def _firstly_clusters(prose: str) -> int:
    """统计【公式化递进】的段落数。

    为什么不能写成一条正则：
    真实稿子里是「首先…。」句号「其次…。」句号「最后…」三句，
    句号把 [^。；！？] 切断了，跨句正则一条都匹配不到（实测漏检）。
    正确做法是按段检查三个连接词是否共现。
    """
    n = 0
    for p in prose.split("\n"):
        for trio in _FIRSTLY_SETS:
            if all(w in p for w in trio):
                n += 1
                break
    return n


def compute_metrics(prose: str) -> dict:
    para_lens = [_cn_len(p) for p in _paragraphs(prose)]
    sent_lens = [_cn_len(s) for s in _sentences(prose)]
    short_n = sum(1 for n in sent_lens if n <= THRESHOLDS["short_max_len"])
    long_n = sum(1 for n in sent_lens if n > THRESHOLDS["long_min_len"])
    total = len(sent_lens) or 1
    chars = _cn_len(prose)
    per_1k = max(chars / 1000.0, 0.001)
    # ⚠️ 密度指标在短文本上会失真：
    #    200 字里出现 1 个缓冲词 = 5/千字，远超 2 的上限——
    #    但"200 字里 1 个缓冲词"完全正常。
    #    所以低于 MIN_CHARS_FOR_DENSITY 的文本不做密度判定。
    MIN_CHARS_FOR_DENSITY = 500
    density_reliable = chars >= MIN_CHARS_FOR_DENSITY

    # ── 密度类指标（avoid-ai-writing-zh / de-ai-flavor 的明文上限）──
    def _count(words) -> int:
        return sum(prose.count(w) for w in words)

    # ⚠️ 破折号必须正确计数：中文破折号是「——」（两个 em dash），
    #    但 HTML 里常以 &mdash; 单个出现。用 count("——") + count("—")
    #    会把「——」算成 3 次（1 + 2），严重虚高。
    #    正确做法：正则匹配「——」或孤立的「—」。
    dash_n = len(re.findall(r"——|(?<!—)—(?!—)", prose))
    buf_n = _count(BUFFER_WORDS)
    phys_n = _count(PHYSICAL_VERBS)
    fake_n = _count(FAKE_ATTRIBUTION)
    notbut_n = len(re.findall(AI_PATTERNS["不是…而是"], prose))
    firstly_n = _firstly_clusters(prose)
    tier3_n = _count(AI_WORDS_TIER3)

    # Tier 2 词单段聚集
    tier2_clusters = 0
    for p in _paragraphs(prose):
        c = sum(p.count(w) for w in AI_WORDS_TIER2)
        if c > THRESHOLDS["tier2_per_para_max"]:
            tier2_clusters += 1

    return {
        "chars": chars,
        "para_count": len(para_lens),
        "para_median": int(statistics.median(para_lens)) if para_lens else 0,
        "para_max": max(para_lens) if para_lens else 0,
        "sent_count": len(sent_lens),
        "short_ratio": round(short_n / total, 3),
        "long_ratio": round(long_n / total, 3),
        "short_count": short_n,
        "long_count": long_n,
        # 密度类
        "dash_per_1k": round(dash_n / per_1k, 2),
        "buffer_per_1k": round(buf_n / per_1k, 2),
        "physical_per_1k": round(phys_n / per_1k, 2),
        "fake_attribution": fake_n,
        "notbut_total": notbut_n,
        "firstly_total": firstly_n,
        "tier2_clusters": tier2_clusters,
        "tier3_total": tier3_n,
        "tier3_per_1k": round(tier3_n / per_1k, 2),
        "density_reliable": density_reliable,
    }


def find_pattern_hits(prose: str) -> list[dict]:
    hits: list[dict] = []
    for name, pat in AI_PATTERNS.items():
        for m in re.finditer(pat, prose):
            hits.append({
                "name": name,
                "text": prose[max(0, m.start() - 25): m.end() + 25].replace("\n", " "),
            })
    return hits


def find_advisory_hits(prose: str) -> list[dict]:
    """轻微项：报出来让人改，但不拦。"""
    hits: list[dict] = []
    for name, pat in ADVISORY_PATTERNS.items():
        for m in re.finditer(pat, prose):
            hits.append({
                "name": name,
                "text": prose[max(0, m.start() - 25): m.end() + 25].replace("\n", " "),
            })
    return hits


def find_word_hits(prose: str) -> list[dict]:
    """Tier 1 词：见到就标。Tier 2 词：只在单段聚集时标。"""
    hits: list[dict] = []
    for w in AI_WORDS_TIER1:
        for m in re.finditer(re.escape(w), prose):
            hits.append({
                "name": w,
                "tier": 1,
                "text": prose[max(0, m.start() - 25): m.end() + 25].replace("\n", " "),
            })
    # Tier 2：按段统计，单段超过上限才报
    for p in _paragraphs(prose):
        c = sum(p.count(w) for w in AI_WORDS_TIER2)
        if c > THRESHOLDS["tier2_per_para_max"]:
            found = [w for w in AI_WORDS_TIER2 if w in p]
            hits.append({
                "name": "Tier2聚集: " + "、".join(found),
                "tier": 2,
                "text": p[:90].replace("\n", " "),
            })
    return hits


def find_tier3_hits(prose: str) -> list[dict]:
    """Tier 3（humanizer §12 中文词表）：逐处标出位置，但由密度决定是否报。

    为什么要单独一个函数而不是复用 find_word_hits：
    Tier 1 是"见到就标"，豁免次数 2 次；Tier 3 是"密度超标才提示"，
    判定逻辑不同。分开写可以避免以后有人把两者的宽松度搞混。
    """
    hits: list[dict] = []
    for w in AI_WORDS_TIER3:
        for m in re.finditer(re.escape(w), prose):
            hits.append({
                "name": w,
                "tier": 3,
                "text": prose[max(0, m.start() - 25): m.end() + 25].replace("\n", " "),
            })
    return hits


def find_density_hits(prose: str, metrics: dict) -> list[dict]:
    """密度类命中：只报超标项，给出具体位置。

    严重程度分级（沿用 avoid-ai-writing-zh 的 P0/P1/P2 思路）：
      P0 = 可信度问题，必须改（虚假归因）
      P1 = 明显 AI 味，发布前必须改（公式化递进）
      P2 = 风格打磨，改了更好（缓冲词/物理动词/破折号密度）
    """
    out: list[dict] = []
    t = THRESHOLDS

    # ── P0：虚假归因 ───────────────────────────────────────────
    # ⚠️ 这条是【绝对计数】不是密度，短文本也必须查——
    #    "业界普遍认为"在 200 字里出现一次和 1 万字里出现一次同样严重。
    if metrics["fake_attribution"] > t["fake_attribution_max"]:
        for m in re.finditer("|".join(re.escape(w) for w in FAKE_ATTRIBUTION), prose):
            out.append({
                "name": f"虚假归因: {m.group(0)}",
                "severity": "P0",
                "text": prose[max(0, m.start() - 25): m.end() + 25].replace("\n", " "),
            })

    # ── P1：公式化递进（绝对计数，短文本也查）──────────────────
    if metrics["firstly_total"] > t["firstly_max"]:
        out.append({
            "name": f"公式化递进 {metrics['firstly_total']} 处（上限 {t['firstly_max']}）",
            "severity": "P1",
            "text": "「首先/其次/最后」是报幕式递进。让内容的递进关系自己呈现。",
        })

    # ── P1：Tier 2 词单段聚集（按段计数，短文本也查）───────────
    if metrics["tier2_clusters"] > 0:
        out.append({
            "name": f"Tier2 词单段聚集 {metrics['tier2_clusters']} 段"
                    f"（上限 {t['tier2_per_para_max']} 个/段）",
            "severity": "P1",
            "text": "同一段出现 2 个以上缓冲/填充类词，就是 AI 在回避表态。",
        })

    # ── P2：以下三项是【每千字密度】────────────────────────────
    # ⚠️ 密度指标在短文本上会失真：
    #    200 字里出现 1 个缓冲词 = 5/千字，远超 2 的上限，
    #    但"200 字里 1 个缓冲词"完全正常。密度只在 ≥500 字时才有意义。
    if not metrics.get("density_reliable", True):
        return out

    if metrics["buffer_per_1k"] > t["buffer_per_1k_max"]:
        out.append({
            "name": f"缓冲词密度 {metrics['buffer_per_1k']}/千字（上限 {t['buffer_per_1k_max']}）",
            "severity": "P2",
            "text": "缓冲词是回避表态的信号。删掉后句意若不变，就是缓冲词。",
        })
    if metrics["physical_per_1k"] > t["physical_per_1k_max"]:
        out.append({
            "name": f"物理动词密度 {metrics['physical_per_1k']}/千字（上限 {t['physical_per_1k_max']}）",
            "severity": "P2",
            "text": "用物理动作描述抽象思维，是翻译腔。换成本来就有的中文说法。",
        })
    if metrics["dash_per_1k"] > t["dash_per_1k_max"]:
        out.append({
            "name": f"破折号密度 {metrics['dash_per_1k']}/千字（上限 {t['dash_per_1k_max']}）",
            "severity": "P2",
            "text": "破折号用多了像 AI。改用逗号、句号或拆成两句。",
        })
    if metrics["tier3_per_1k"] > t["tier3_per_1k_max"]:
        out.append({
            "name": f"渲染词密度 {metrics['tier3_per_1k']}/千字"
                    f"（上限 {t['tier3_per_1k_max']}，共 {metrics['tier3_total']} 处）",
            "severity": "P2",
            "text": "「至关重要 / 深入探讨 / 赋能 / 凸显」这类渲染词密度过高。"
                    "换成平实说法，或直接删掉修饰只留事实。",
        })
    return out


# --------------------------------------------------------------------------
# 6. 对外接口
# --------------------------------------------------------------------------

def scan_text(prose: str, scope: str = DEFAULT_SCOPE) -> dict:
    """对纯文本正文做扫描。prose 应按段落用 \\n 分隔。

    scope: 编辑范围，见 apply_scope()。
    """
    metrics = compute_metrics(prose)
    pat_hits = find_pattern_hits(prose)
    word_hits = find_word_hits(prose)
    tier3_hits = find_tier3_hits(prose)
    density_hits = find_density_hits(prose, metrics)
    advisory_hits = find_advisory_hits(prose)

    # 硬闸门：命中就必须改（改法是拆句，不是换词）
    hard: list[str] = []
    if pat_hits:
        names = sorted({h["name"] for h in pat_hits})
        hard.append(f"AI 句式命中 {len(pat_hits)} 处（{', '.join(names)}）")
    if len(word_hits) > THRESHOLDS["word_tolerate"]:
        names = sorted({h["name"] for h in word_hits})
        hard.append(
            f"AI 味词 {len(word_hits)} 处（超过豁免 {THRESHOLDS['word_tolerate']}）："
            f"{', '.join(names)}")

    # 软闸门：只提示，不拦
    # ⚠️ 段落/句长指标不作为硬失败原因：生成侧的重试循环可能把 3000 字好稿
    #    换成 500 字残稿（2026-10-24 Gilead 事故）。结构问题交给人工改稿。
    soft: list[str] = []
    if metrics["para_median"] > THRESHOLDS["para_median_max"]:
        soft.append(
            f"段落长度中位数 {metrics['para_median']} 字 > {THRESHOLDS['para_median_max']}"
            f"（段落焊成一大坨）")
    if metrics["short_ratio"] < THRESHOLDS["short_ratio_min"]:
        soft.append(
            f"短句占比 {metrics['short_ratio']:.1%} < {THRESHOLDS['short_ratio_min']:.0%}"
            f"（没有呼吸感）")
    if metrics["long_ratio"] > THRESHOLDS["long_ratio_max"]:
        soft.append(
            f"长句占比 {metrics['long_ratio']:.1%} > {THRESHOLDS['long_ratio_max']:.0%}")

    # 密度类按严重程度分流：P0/P1 进硬闸门，P2 只提示。
    #
    # 为什么 P2 不拦：破折号/缓冲词/物理动词密度属于"打磨项"，
    # 一篇稿子偶尔密一点很正常。拦它会误杀。
    # 但 P0（虚假归因）和 P1（公式化递进、句式依赖）必须拦——
    # 那些是可信度和结构问题。
    p01 = [h for h in density_hits if h.get("severity") in ("P0", "P1")]
    p2 = [h for h in density_hits if h.get("severity") == "P2"]
    if p01:
        names = sorted({h["name"] for h in p01})
        hard.append(f"密度/结构超标 {len(p01)} 项（{'; '.join(names)}）")
    for h in p2:
        soft.append(f"[P2] {h['name']}：{h['text']}")
    for h in advisory_hits:
        soft.append(f"[打磨] {h['name']}：…{h['text']}…（删掉评价词即可）")

    return {
        "passed": not hard,
        "reasons": hard,
        "soft_reasons": soft,
        "scope_notes": [],
        "metrics": metrics,
        "pattern_hits": pat_hits,
        "word_hits": word_hits,
        "tier3_hits": tier3_hits,
        "density_hits": density_hits,
        "advisory_hits": advisory_hits,
    }


def scan_html(html: str, scope: str = DEFAULT_SCOPE) -> dict:
    res = scan_text(html_to_prose(html), scope=scope)
    return apply_scope(res, scope)


def feedback_lines(res: dict) -> list[str]:
    """把扫描结果转成【可执行的】纠正指令。

    为什么不能只把报错丢给模型：「命中 3 处 AI 句式」模型不知道改哪。
    必须给出位置和改法，否则重试只是随机换一个说法再犯。
    """
    out: list[str] = []
    for h in res.get("pattern_hits", []):
        out.append(
            f"- 删掉「{h['name']}」这个句式模板：…{h['text']}…\n"
            f"  改法：把解释层整个删掉，只留事实。"
            f"「不是 A，而是 B」→ 直接写 B；"
            f"「这意味着/也就是说」→ 直接给下一句事实。")
    for h in res.get("word_hits", []):
        out.append(f"- 删掉 AI 味词「{h['name']}」：…{h['text']}…")
    for h in res.get("tier3_hits", []):
        out.append(
            f"- 换掉渲染词「{h['name']}」：…{h['text']}…\n"
            f"  改法：换平实说法，或删掉修饰只留事实。"
            f"（humanizer §12 词表的中文对应）")
    for h in res.get("density_hits", []):
        out.append(f"- {h['name']}：{h['text']}")
    for r in res.get("soft_reasons", []):
        out.append(f"- {r}\n"
                   f"  改法：把长句用句号断开，超过 45 字的句子拆成两句；"
                   f"一段只说一件事，控制在 90 字内。")
    for r in res.get("scope_notes", []):
        out.append(f"- {r}")
    return out


def scan_file(path: str | Path, scope: str = DEFAULT_SCOPE) -> dict:
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() in (".html", ".htm"):
        return scan_html(raw, scope=scope)
    res = scan_text(plain_to_prose(raw))
    return apply_scope(res, scope)


def format_report(path: str, res: dict) -> str:
    m = res["metrics"]
    scope = res.get("scope", DEFAULT_SCOPE)
    lines = [
        f"=== {path} ===",
        f"  结论：{'✅ 通过' if res['passed'] else '❌ 未通过'}"
        f"｜编辑范围 {scope}",
        f"  正文字数 {m['chars']}｜段落 {m['para_count']}"
        f"（中位 {m['para_median']}，最长 {m['para_max']}）",
        f"  句子 {m['sent_count']}｜短句 {m['short_ratio']:.1%}（{m['short_count']}）"
        f"｜长句 {m['long_ratio']:.1%}（{m['long_count']}）",
        f"  密度：破折号 {m['dash_per_1k']}/千字｜缓冲词 {m['buffer_per_1k']}/千字"
        f"｜物理动词 {m['physical_per_1k']}/千字｜渲染词 {m.get('tier3_per_1k', 0)}/千字"
        f"｜「不是…而是」{m['notbut_total']} 处",
    ]
    if res["pattern_hits"]:
        lines.append(f"  AI 句式 {len(res['pattern_hits'])} 处：")
        for h in res["pattern_hits"]:
            lines.append(f"    · [{h['name']}] …{h['text']}…")
    if res["word_hits"]:
        lines.append(f"  AI 味词 {len(res['word_hits'])} 处：")
        for h in res["word_hits"]:
            lines.append(f"    · [{h['name']}] …{h['text']}…")
    if res.get("tier3_hits"):
        lines.append(f"  渲染词（humanizer §12 中译）{len(res['tier3_hits'])} 处：")
        for h in res["tier3_hits"]:
            lines.append(f"    · [{h['name']}] …{h['text']}…")
    if res.get("density_hits"):
        lines.append(f"  密度超标 {len(res['density_hits'])} 项：")
        for h in res["density_hits"]:
            lines.append(f"    · [{h.get('severity','?')}] {h['name']} — {h['text']}")
    if res.get("advisory_hits"):
        lines.append(f"  打磨项 {len(res['advisory_hits'])} 处：")
        for h in res["advisory_hits"]:
            lines.append(f"    · [{h['name']}] …{h['text']}…")
    for r in res["reasons"]:
        lines.append(f"  !! {r}")
    for r in res.get("soft_reasons", []):
        lines.append(f"  ~  {r}")
    for r in res.get("scope_notes", []):
        lines.append(f"  ?  {r}")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not argv:
        print(__doc__)
        return 0

    # ── 解析全局参数 ──────────────────────────────────────────
    scope = DEFAULT_SCOPE
    feedback = False
    files: list[str] = []
    text_mode = False

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--scope":
            if i + 1 >= len(argv):
                print(f"!! --scope 需要一个参数，取值：{' / '.join(SCOPES)}")
                return 2
            scope = argv[i + 1]
            if scope not in SCOPES:
                print(f"!! 未知 scope：{scope!r}，取值：{' / '.join(SCOPES)}")
                return 2
            i += 2
            continue
        if a == "--feedback":
            feedback = True
            i += 1
            continue
        if a == "--text":
            text_mode = True
            i += 1
            continue
        files.append(a)
        i += 1

    if text_mode:
        body = " ".join(files)
        if not body.strip():
            print(__doc__)
            return 0
        res = scan_text(plain_to_prose(body))
        res = apply_scope(res, scope)
        if feedback:
            for line in feedback_lines(res):
                print(line)
        else:
            print(format_report("(text)", res))
        return 0 if res["passed"] else 1

    if not files:
        print(__doc__)
        return 0

    all_passed = True
    for path in files:
        try:
            res = scan_file(path, scope=scope)
        except Exception as e:
            print(f"=== {path} ===\n  !! 读取失败：{type(e).__name__}: {e}")
            all_passed = False
            continue
        if feedback:
            print(f"=== {path} ===")
            for line in feedback_lines(res):
                print(line)
        else:
            print(format_report(path, res))
        print()
        all_passed = all_passed and res["passed"]
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
