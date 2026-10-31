"""
文章生成模块（内容层）。

架构（实测得出的分工）：
  LLM  → 只写内容和轻量标记，不写任何 HTML 标签
  render → 负责所有标签、CSS、转义

为什么这样分：
让 LLM 一次写"内容 + HTML + CSS"，成功率只有 ~1/3，
失败全在格式层（标签不配平、CSS 丢失、输出截断、返回空）。
拆开后格式错误从架构上不可能发生，模型只需专注内容。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from common import (ROOT, SCORED_DIR, RAW_DIR, DATA_DIR, load_yaml,
                    read_json, write_json, today_cst)
from render import (parse_content, render_html, body_chars,
                    structure_report, content_text)
from score import _client, _call_llm, _strip_think

POSTS_DIR = ROOT / "posts"


# ============================================================
# 素材组装
# ============================================================
def _needs_registry_lookup(title: str) -> bool:
    """
    判断这条素材是否需要查注册库。

    ⚠️ 规则来源：用户指出"凡涉及具体药物/试验的数据，
    先查 ClinicalTrials.gov 和公司公告，查不到再写'未披露'"。
    实测教训：一篇 solengepras 稿子通篇写"数据未披露"，
    而完整数据（p 值、样本量、主要终点）早已公开。

    反过来的坑也真实存在：solengepras 在注册库搜不到任何条目，
    但它的数据在公司公告和 MDS 大会披露里。
    所以注册库是【补充】通道，不是唯一通道。
    """
    t = title or ""
    if re.search(
            r"\b(?:NCT\d{8}|\d+\s*(?:期|phase)|试验|临床|III|II\b|trial|"
            r"数据读出|终点|疗效|适应症|给药|剂量|患者|入组)\w*",
            t, re.I):
        return True
    return bool(re.search(r"[A-Z][a-z]{3,}[a-z]+\b", t))   # 疑似药名


def build_material(date: str, plan_item: dict) -> str:
    scored = read_json(SCORED_DIR / f"{date}.json", [])
    raw = read_json(RAW_DIR / f"{date}.json", [])
    by_id = {s["id"]: s for s in scored}
    raw_by_id = {r["id"]: r for r in raw}

    ids = plan_item.get("member_ids") or [plan_item["lead_id"]]
    blocks: list[str] = []

    for i, sid in enumerate(ids, 1):
        s = by_id.get(sid, {})
        r = raw_by_id.get(sid, {})
        blocks.append(f"--- 素材 {i} ---")
        if s:
            blocks.append(f"标题：{s.get('title','')}")
            blocks.append(f"来源：{', '.join(s.get('sources',[]) or [])}")
            blocks.append(f"链接：{s.get('url','')}")
            # ⚠️ 新闻站自动实体标签【不是交易内容】——显式隔离，否则成稿会把
            #    标签里的靶点/公司当成交易标的（实测事故 postmortem_b4985179：
            #    标签里的 "TYK2" 被写成了"TYK2 平台授权"，还捏造了分子代号）。
            etags = (s.get("entity_tags") or []) + (r.get("entity_tags") or [])
            if etags:
                blocks.append(
                    f"⚠️ 新闻站自动标注的实体标签：{'、'.join(etags)}\n"
                    "   这些是新闻网站自己的实体识别结果，【不是公告披露的交易内容】。"
                    "除非正文正文里明确写了某个靶点/公司，否则一律不要把它们"
                    "当作本次交易的标的或对手方来写。")
            blocks.append(f"评分：{s.get('score','')}/10")
            if s.get("reasoning"):
                blocks.append(f"评分理由：{s['reasoning']}")
            if s.get("flags"):
                blocks.append(f"标记：{', '.join(s['flags'])}")
            if s.get("verify_todo"):
                blocks.append(f"待核查：{'; '.join(s['verify_todo'])}")
        content = (s.get("content") or r.get("content") or "")
        if len(content) > 200:
            blocks.append(f"正文摘录：\n{content[:7000]}")
        else:
            summ = (s.get("summary") or r.get("summary") or "")
            if summ:
                blocks.append(f"摘要：{summ[:1200]}")
        blocks.append("")

    ins = read_json(DATA_DIR / "tracker_insights.json", {})
    if ins:
        blocks.append("--- 行业背景数据 ---")
        for k, v in ins.items():
            for kk, vv in v.items():
                if isinstance(vv, list):
                    for x in vv:
                        blocks.append(f"· {x}")
                elif kk in ("jefferies", "published", "updated"):
                    blocks.append(f"· {vv}")
        blocks.append("")

    # 结构化交易库：只作【历史对比参照】，不要当成今日新闻。
    deals = read_json(DATA_DIR / "tracker_deals.json", [])
    if deals:
        dated = sorted([d for d in deals if d.get("published")],
                       key=lambda x: x["published"], reverse=True)
        if dated:
            lo = dated[-1]["published"][:7]
            hi = dated[0]["published"][:7]
            blocks.append("--- 中国对外授权交易库（历史参照）---")
            blocks.append(f"数据区间：{lo} ~ {hi}，共 {len(deals)} 笔")
            blocks.append("⚠️ 这是历史数据库，不是今日新闻。"
                          "只能用作对比参照，不要在文中说成今天发生的事。")
            for d in dated[:12]:
                # content 里已经是结构化的 授权方/受让方/首付/总额/领域/阶段
                blocks.append(f"· [{d['published'][:7]}] " +
                              " / ".join(
                                  (d.get("content") or "").split("\n")[:6]))
            blocks.append("")

    # ── 公司背景（可引用）───────────────────────────────────────────
    #
    # 用户 2026-10-17 定的规则：素材不足时首选扩写方向是"介绍相关公司"。
    # BD 文章天然是"两家公司"的故事，公司背景既是合理扩写，也是编造高发区
    # （retlirafusp 那篇的"君实/Coherus 有北美商业化能力"就是这么来的）。
    # 先把真实、可引用的公司信息取回来放进素材，成稿写得出来、核验也验得过。
    #
    # ⚠️ 取不到就不写（companyinfo 会如实说明），绝不编。
    # _blob 要提到 try 外面——下面 privco 通道也要用它判断卖方是谁。
    _parts = [plan_item.get("title", "")]
    for i in (plan_item.get("member_ids") or []):
        s = by_id.get(i, {}) or {}
        r = raw_by_id.get(i, {}) or {}
        _parts += [s.get("title", ""), (s.get("summary") or "")[:500],
                   (s.get("content") or "")[:1200],
                   r.get("title", ""), (r.get("content") or "")[:600]]
    _blob = " ".join(p for p in _parts if p)
    try:
        import companyinfo
        # BD 通常是"买方 + 卖方"，取前两家
        _cos = companyinfo.find_companies(_blob, limit=2)
        if _cos:
            brief = companyinfo.company_brief(_cos)
            if brief:
                blocks.append(brief)
                blocks.append("")
    except Exception as _e:
        blocks.append(f"（公司背景补全失败：{type(_e).__name__}，"
                      f"请勿编造公司信息）")
        blocks.append("")

    # ── 官方新闻稿（一手来源）─────────────────────────────────────
    #
    # 聚合站转述缺三样最关键的东西：
    #   1. 完整对价结构（首付 vs 总额 vs 里程碑上限）
    #   2. 买方为什么买（管理层原话、战略定位）
    #   3. 交易后的开发安排（谁主导、适应症方向）
    # 没有这三样，成稿只能把标题里的"为何"跳过。
    try:
        import pr_official
        _pr = pr_official.enrich(date, plan_item)
        if _pr:
            blocks.append(_pr)
            blocks.append("")
    except Exception as _e:
        print(f"[compose] 官方新闻稿补全失败：{type(_e).__name__}")

    # ── 私企工商信息 ──────────────────────────────────────────────
    #
    # ⚠️ 2026-10-27 实测：工商聚合站点（天眼查/企查查）会限流，
    #    同一份代码第一次抓成功、第二次返回空。素材块时有时无，
    #    会让稿子里对应的断言在核验时随机变成"无出处"——
    #    排查成本远高于收益。
    #    所以这里【不接入】privco 通道。缺的公司背景字段，
    #    要么从官方新闻稿里取（首选），要么就别写。
    #    privco.py 保留在 src/ 下，需要时可手动调用。

    return "\n".join(blocks)


async def build_material_async(date: str, plan_item: dict) -> str:
    """
    素材组装 + 临床试验注册库补查。

    规则来源：用户要求"凡涉及具体药物/试验的数据，
    先查 ClinicalTrials.gov 和公司公告，查不到再写'未披露'"。
    这里自动执行前半句——把查到的注册库条目喂给模型，
    让它手上真的有数据，而不是靠猜或者写"未披露"。
    """
    base = build_material(date, plan_item)
    title = plan_item.get("title", "")
    if not _needs_registry_lookup(title):
        return base
    try:
        from ctgov import enrich_from_trial_registry
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=30) as cl:
            txt, _srcs = await enrich_from_trial_registry(cl, title)
        if txt:
            return base + (
                "\n--- 临床试验注册库（查证补充）---\n"
                "⚠️ 查不到不等于不存在：注册库可能尚未登记，"
                "数据仍在公司公告或学术会议里。\n" + txt)
    except Exception:
        pass
    return base


def material_too_thin(date: str, plan_item: dict,
                      min_chars: int = 200) -> tuple[bool, str]:
    """
    素材太薄就不成稿（2026-10-20 事故）。

    默克/siRNA 那条正文 0 字、只有标题，成稿却凭世界知识写满 3400 字，
    可信度核查报 29 条无依据。临床雷达早就有这道闸，BD 之前漏了。

    ⚠️ 判据口径与临床雷达对齐（2026-10-21）：
      临床那边量的是【模型真正拿到的那份素材】（material_is_thin：
      剥离模板行后 ≥200 字），BD 之前量的是"正文字数"和"摘要字数"
      两栏分别求和——**量错了对象**：

        旧口径的洞：一条 content=150 / summary=100 的素材，
        两栏分别求和是 150 和 100，都 <200 → 判薄（对）；
        但两个 member 各 150+100 时，两栏求和变成 300/200 →
        判"不薄"，可模型实际收到的是两个 100 字的摘要（共 200 字）。
        也就是说旧口径会放过"模型其实没拿到什么"的簇。

      新口径跟 build_material 完全一致：正文 >200 字才用正文，
      否则用摘要。量到的就是模型看到的那份素材。
      （BD 的 material_too_thin 和临床的 material_is_thin 是两份拷贝，
      改动时两个项目要同步改。）
    """
    ids = set(plan_item.get("member_ids") or [plan_item.get("lead_id")])
    scored = read_json(SCORED_DIR / f"{date}.json", [])
    raw = read_json(RAW_DIR / f"{date}.json", [])
    s_by = {s.get("id"): s for s in scored}
    r_by = {r.get("id"): r for r in raw}
    usable = 0
    for i in ids:
        s = s_by.get(i, {}) or {}
        r = r_by.get(i, {}) or {}
        content = s.get("content") or r.get("content") or ""
        summ = s.get("summary") or r.get("summary") or ""
        # 与 build_material 的取值口径一致：正文够长才用正文，否则用摘要
        usable += len((content if len(content) > 200 else summ).strip())
    if usable < min_chars:
        return True, (f"模型可用素材仅 {usable} 字（< {min_chars}），"
                      f"相当于只有标题")
    return False, ""


# ============================================================
# Prompt —— 只要求轻量标记，不要求 HTML
# ============================================================
SYSTEM_TPL = """你是医药行业资深撰稿人，为中文读者写行业分析。**写内容，不写 HTML。**

# 输出格式（严格遵守）

按下面的标记逐行输出。除了这些标记，**不要输出任何 HTML 标签**，
不要 markdown 代码块包裹，不要解释文字。

```
KICKER: 栏目名，例如 Event Report｜创新药授权
TITLE: 主标题（吸引人但不要标题党）
SUB: 一句话副标题
HOOK: 钩子，一段话。放最反常识的数字或矛盾对比
## 01 小节标题
正文段落，可以有多段
### 子标题（可选）
更多段落
TABLE: 列1 | 列2 | 列3
行1列1 | 行1列2 | 行1列3
行2列1 | 行2列2 | 行2列3
ENDTABLE
BOX w 标签： | 提示框内容（w=琥珀 g=绿 r=红）
## 02 下一节
……
ENDCARD 结语标题 | 结语正文
FN 1. 来源说明，注明来源性质
FN 2. 来源说明
```

规则：
- `## ` 小节必须有编号，如 `## 01 交易拆解`
- 表格第一行是表头，`|` 分隔，**每行列数要一致**
- `BOX` 三个变体：`w` 琥珀（口径提示）/ `g` 绿（正面）/ `r` 红（风险）
- `ENDCARD` 和 `FN` 各只能出现一次/在末尾，FN 可以多行
- 正文里可用 `**加粗**` 强调关键数字

# 内容规范

- **正文 2800-3200 字**（不含表格和脚注）。这是硬指标，宁可写满
- **HOOK 第一句必须交代交易时间**（年-月-日，如"2026年9月24日"或
  "Oct. 8, 2026"）。读者第一眼就要知道这是什么时候的事——
  没有时间的稿子读起来不像新闻。同时给出反常识数字或矛盾对比。

  反例（实测被用户指出）：
    "礼来2026年单笔首付均值已达2.17亿美元，而这一次它只掏出1亿美元
     '零头'，就绑定了诺诚健华最多5个创新靶点的研发管线。"
    —— 通篇没有交易日期，读者无从判断时效性。

  正例：
    "2026年9月24日，礼来只掏出1亿美元首付，就锁定了诺诚健华
     最多5个创新靶点的研发管线。"

  如果素材里查不到确切日期，写"未找到公开数据"并说明，
  但**不要略过不提**。
- **并购与授权同等对待**：两者都是 BD 交易，都要写。
  不要因为"并购没有每股价格可写"就回避并购类选题。
- **金额必须分层**：首付 / 里程碑上限 / 总额，三者不可混为一谈。
  如果只披露了合并口径，要写明"首付款与近期里程碑合计"，不能简写成"首付"
- 查不到的数据写"未找到公开数据"，**绝对禁止编造**
- **已知信息要写足**：不要把"未披露"当成段落主体。
  有的信息就展开分析；确实没有的，一句话交代后转到能分析的部分
- 每个小节至少 2-3 段，表格后要有解读，不要甩个表就走
- 结尾 ENDCARD 给判断，不要复述前文

## 禁止脑补（实测踩过的坑）

素材里**没有**的交易方、金额、条款、情节，一律不得出现：

- ❌ 素材只说"临床三期成功"，却写"这款中国资产凭什么让跨国药企出手"
  —— 素材里根本没有跨国药企，也没有交易
- ❌ 素材只说"某公司获批"，却推断"某某可能成为潜在买家"
- ❌ 素材只有一行标题，却编出交易结构和双方角色

规则：
1. **交易对手方必须来自素材**。素材没提买方/卖方，就写"未找到公开数据"，
   不要让文章暗示存在一笔交易。
2. **不要给素材里没有的主体安角色**。没有买方就不要写"买方动机"章节。
3. **素材只有标题时**：坦率说明信息有限，聚焦标题本身能支撑的分析，
   不要用推测填满 2800 字。
4. 如果素材根本不含交易（纯临床数据/监管获批），
   写"本条为临床进展，非 BD 交易"并聚焦数据本身，**不要套交易结构表**。

## ⚠️ 素材很短怎么办：写公司背景，别编交易细节（2026-10-16 用户定）

素材常常只有一段公告转述（几百字），但正文要求 2800-3200 字。
**扩写的正确方向是介绍交易双方的公司背景**，因为这是公开常识、写它不产生事实风险：

- ✅ 双方各自的主营业务、核心产品线、在售产品、研发方向与管线规模、
  在该赛道的地位、商业化能力（国内外）、出海历史
- ✅ "这家公司靠什么立足""为什么它有资格做这笔交易"

**但交易本身的细节仍然只能来自素材**，素材没披露就写"未披露"，绝不脑补：

- ❌ 交易标的的具体靶点（素材只说"至多5项创新靶点"，就写"至多5个创新靶点"，
   不要从公司管线里挑一个当标的）
- ❌ 具体分子代号（ICP-488、DC-806 这类，公告没写就一个字都不能编）
- ❌ 临床阶段、数据读出、疗效指标
- ❌ 金额、里程碑节奏、权益区域

⚠️ **实测事故（postmortem_b4985179）就是这么来的**：素材是诺诚健华与礼来
的一则平台合作公告，"TYK2"只出现在新闻站的自动实体标签里。成稿为了凑字数和
"显得有料"，把它当成交易标的写成"TYK2 平台授权"，又编了一个公告里根本没有的
分子代号 ICP-488，还加了 Dice 收购、竞对格局等一堆无源内容。
**"素材短"从来不是编造交易细节的理由。**

## 查证规则（写"未披露"之前必须做）

**5. 涉及具体药物/试验的数据，先查注册库和公司公告。**
- 素材区若给了「临床试验注册库（查证补充）」，一律以它为准
- 它给了什么就写什么：NCT 编号、期别、样本量、起始时间、主要终点
- ⚠️ **查不到 ≠ 不存在**。注册库可能尚未登记，数据仍在公司公告或学术会议里。
  实测 Cerevance solengepras：ClinicalTrials.gov 搜不到任何条目，
  但3期 ARISE 数据（OFF 时间减少 0.61 小时、p=0.0350、n=341）
  已在公司公告和 MDS 2026 大会完整披露。
  **所以绝不能因为"注册库没有"就写"数据未披露"。**
- 只有公告和注册库都查不到，才写"未找到公开数据"

**6. 出处只能写真实的一手来源。**
- ❌ 不写"用户提供""用户提供材料""据用户提供的文章"
- ✅ 写实际查到的：公司公告 / SEC 备案 / 交易所披露 / 注册库 /
  学术会议 / 媒体转述
- 素材来自转述时，出处写你查到的那一家

# 文风（大白话，去 AI 味）
- 短句。不要长定语堆叠
- 禁止词：赋能、底层逻辑、抓手、值得注意的是、需要注意的是、
  综上所述、总而言之、不言而喻、至关重要、具有里程碑意义
- 禁止句式："不是…而是…"排比
- 禁止："本文将""本系列将""让我们"
- 直接抛数字或场景，不要铺垫

# 文章结构
{structure}

只输出上面的标记文本，第一个字符应该是 KICKER 或 TITLE。"""


def build_prompt(plan_item: dict, material: str, style_cfg: dict) -> tuple[str, str]:
    atype = plan_item.get("type", "event_report")
    struct = style_cfg.get("article_types", {}).get(atype, {})
    system = SYSTEM_TPL.replace("{structure}", struct.get("structure", ""))
    target = style_cfg.get("generation", {}).get("target_words", 3000)

    user = f"""# 今日选题
类型：{atype}（{struct.get('name','')}）
标题参考：{plan_item.get('title','')}
评分：{plan_item.get('score','')}/10
选题理由：{plan_item.get('reasoning','')}

# 可用素材
{material}

# 要求
标题要吸引人点击，但不要标题党。
正文 {target} 字左右（不含表格和脚注）。
素材里没有的数字不要写。素材里标了"待核查"的点，要么核实，
要么在文中写明"未找到公开数据"。

现在按标记格式输出。"""
    return system, user


# ============================================================
# 内容层校验
# ============================================================
# 中文金额："5亿美元" "50亿" "1.25亿美元"
MONEY_CN = re.compile(r"(\d+(?:\.\d+)?)\s*亿(?:美元|美金|元)?")
# 金额限定词
MONEY_QUAL = ("首付款", "首付", "总对价", "总额", "里程碑")


def _title_money_pairs(text: str) -> list[tuple[str, str]]:
    """提取「金额 + 限定词」组合。限定词取金额后 8 字内出现的那个。"""
    out = []
    for m in MONEY_CN.finditer(text):
        tail = text[m.end():m.end() + 8]
        qual = next((q for q in MONEY_QUAL if q in tail), "")
        out.append((m.group(1), qual))
    return out


def _check_title_numbers(doc: dict, body: str) -> list[str]:
    """
    标题里的金额与正文是否自洽。
    只检查带限定词的金额——不带限定词的（如"60亿押注"）噪声太大，
    误报成本高于收益，不做强制。
    """
    errs: list[str] = []
    title = doc.get("title", "")
    if not title:
        return errs

    body_pairs = _title_money_pairs(body)
    body_nums = {n for n, _ in body_pairs}
    body_qual_nums = {(q, n) for n, q in body_pairs if q}

    for num, qual in _title_money_pairs(title):
        if not qual:
            continue
        # 正文里必须有同限定词同数字的组合（限定的就近匹配）
        if (qual, num) in body_qual_nums:
            continue
        # 该限定词在正文中出现的所有数字
        same_qual = sorted({n for q, n in body_qual_nums
                            if q == qual or q.startswith(qual[:2])})
        if same_qual:
            errs.append(
                f"标题金额与正文不符：标题称 {num}亿{qual}，"
                f"正文中{qual}为 {'/'.join(same_qual)}亿")
        elif num not in body_nums:
            errs.append(f"标题金额 {num}亿{qual} 未在正文出现")
    return errs


# ============================================================
# 事实闸门：成稿里出现但素材里没有的"高风险实体"
#
# 为什么需要这道闸门（实测事故 postmortem_b4985179）：
#   素材是一段公告转述，"TYK2"只作为新闻站实体标签出现。成稿把它当成
#   交易标的，整篇写成"TYK2 平台授权"，还从世界知识捏造了一个公告里
#   根本不存在的分子代号 ICP-488。结构校验（字数/表格/金额口径）全过——
#   因为金额分层恰好写对了——没有任何一条把实体拿去和素材比对。
#
# 做法：从成稿正文里抽取"高风险专有名词"（药物代号、靶点缩写、具体年份+金额），
#      凡是素材里找不到的，列为待核。刻意只抓高风险类型，避免误杀正常分析里
#      合法的常识性表述（公司名、赛道通用词等）——skill §6：规则不能比问题更伤人。

# ⚠️ 这里的边界【不能用 \b】——Python 的 \b 基于 \w，而中文字符在 Unicode 里
#    属于 \w，所以 "的ICP-488抑" 在 ICP 前面【没有】词边界，正则会整段匹配不到。
#    实测踩过：闸门写了 \b，结果 ICP-488 和 TYK2 一个都没抓到，闸门形同虚设。
#    正确写法是用否定环视，只把 ASCII 字母数字算作"词内字符"。
# 药物/分子代号：ICP-488、DC-806、ICP-332、XYZ-9 这类 "字母+数字(+连字符)" 组合
_DRUG_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z]{1,6}[-]?\d{2,5})(?![A-Za-z0-9])")
# 靶点/通路缩写：TYK2、ALK7、GLP-1、JAK1、CD19…
_TARGET = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{2,7})(?![A-Za-z0-9])")

# 常见无需核实的"非交易事实"词库：这些即使素材没提也通常是常识/科普，
# 放行以免误杀。但**刻意不含**分子代号（ICP-488 这类必须逐一对素材）。
_TARGET_ALLOW = {
    "TYK2", "JAK", "GLP", "ALK", "BTK", "SHP2", "SOS1", "KRAS", "EGFR",
    "PD", "PD1", "HER2", "CD19", "CD20", "BCMA", "IL", "TNF", "MCL1",
    "BRAF", "MEK", "FGFR", "MET", "ROS1", "NTRK", "CDK", "MDM2",
    "A", "I", "II", "III", "IV", "FDA", "NMPA", "EMA", "US", "EU", "MNC",
    "BD", "CEO", "IPO", "IND", "NDA", "PCC", "GLP1", "GLP2", "SIK2",
    "RNAi", "SIRNA", "ASO", "ADC", "CAR", "T", "Q", "R", "H1", "H2",
    "PCR", "ELISA", "DNA", "RNA", "P", "L", "D",
}
_TARGET_STOP = _TARGET_ALLOW  # 同义


def _norm(s: str) -> str:
    return re.sub(r"[\s\-_/]", "", s).upper()


# 细胞因子/通路族名：IL-17、IL-23、TNF-α、IFN-γ… 这些是生物学分类，
# 不是某家药企的专利分子代号，正文里出现是正常的（"靶向 IL-23 通路"），
# 不该被当成捏造的药物代号拦下。
_CYTOKINE = re.compile(r"^(IL|TNF|IFN|IL23|IL17|IL6|TGF|CHEMOKINE)")


def _material_keys(material: str) -> str:
    """把素材归一化成便于子串匹配的紧凑大写串。"""
    return _norm(material or "")


def check_facts(doc: dict, text: str, material: str) -> list[str]:
    """
    事实闸门：抓出成稿里"素材中没有"的高风险实体。
    返回问题列表（空 = 通过）。
    """
    material_n = _material_keys(material)
    if not material_n:
        return []              # 没有素材可比对，放行（不能凭空判失败）

    body = content_text(doc, include_tables=True) + " " + (doc.get("title") or "")
    errs: list[str] = []
    seen: set[str] = set()

    # 1) 药物/分子代号 —— 最强信号。ICP-488、DC-806 之类必须能在素材里找到。
    for m in _DRUG_CODE.finditer(body):
        code = m.group(1)
        key = _norm(code)
        if key in seen:
            continue
        seen.add(key)
        # 细胞因子族名（IL-17/IL-23/TNF-a…）是生物学分类，不是捏造的分子代号
        if _CYTOKINE.match(code):
            continue
        # 常见量词/编号噪音排除（纯数字、日期、百分比已被正则结构排除大部分）
        if key in material_n:
            continue
        # 形如 "2026"/"III"/"GLP" 的不会匹配到这里（字母+数字要求字母开头且≥2）
        errs.append(
            f"分子代号「{code}」在素材中找不到——疑似凭空捏造。"
            f"公告里没有出现的药物代号一律不能写。")

    # 2) 靶点/通路缩写 —— 中风险。允许常见通路词（JAK/GLP1…），
    #    但像 TYK2 这种具体靶点若素材没提正文却出现，需要提示核实。
    #    注意：素材的 entity_tags 可能已含这些，但那是标签不是披露——
    #    这里比对的是【正文素材】，所以标签里的靶点也会被标出来，符合预期。
    for m in _TARGET.finditer(body):
        tok = m.group(1)
        key = _norm(tok)
        if key in seen or key in _TARGET_STOP:
            continue
        seen.add(key)
        if key in material_n:
            continue
        # 只报"像靶点/药"的（长度合理且不是普通英文缩写堆），降低噪音
        if len(key) >= 3 and any(c.isdigit() for c in key):
            errs.append(
                f"靶点/代号「{tok}」在素材正文中找不到——请核实是否公告披露。"
                f"新闻站自动标签不等于交易披露。")

    return errs


def validate_content(doc: dict, text: str, style_cfg: dict,
                     src_title: str = "", material: str = "") -> list[str]:
    """
    校验结构化内容（不涉及 HTML，因为 HTML 由代码保证）。

    src_title: 源新闻标题。判断"这是不是并购类"必须用它，不能用生成的文章标题
    ——文章标题可能写成"18亿美元买一个止痛贴"，不含"收购"二字；
    而正文的对比表格里又可能出现别的公司的"收购"，用正文判断会误报。

    material:  可用素材。用来判断"该事实是否适用且可得"。
    """
    errs: list[str] = []
    gen = style_cfg.get("generation", {})
    target = gen.get("target_words", 3000)
    tol = gen.get("word_tolerance", 0.75)
    wmax = gen.get("max_words", 3900)

    # 1. 必需元素
    if not doc.get("title"):
        errs.append("缺少 TITLE")
    if not doc.get("hook"):
        errs.append("缺少 HOOK")
    if len(doc.get("sections") or []) < 3:
        errs.append(f"小节太少（{len(doc.get('sections') or [])} 个，至少 3 个）")
    if not doc.get("footnotes"):
        errs.append("缺少 FN 来源脚注")

    # 2. 字数
    n = body_chars(doc)
    lo, hi = int(target * tol), int(wmax)
    if not (lo <= n <= hi):
        errs.append(f"正文字数 {n} 超出范围（目标 {lo}-{hi}）")

    # 2.5 空壳表格检测（仅在【整张表全空】时才判失败）
    #
    # 实测案例：一篇讲"临床三期成功"的稿子（素材里没有任何交易），
    # 却套了「交易要素」表，各行全是"未找到公开数据"——根本没有这笔交易。
    #
    # ⚠️ 第一版用"≥50% 且 ≥3 行未找到"就判失败，结果【误杀了诚实稿件】：
    #   · 综述里的多笔交易对比表，部分交易金额未披露是如实报告
    #   · 条款表里确实没披露的项目，写"未找到公开数据"是对的
    #   实测一轮里 3 篇有 1 篇因这条规则连续 4 次重写全部失败。
    #
    # 而且根因已由别处解决：评分层第 ⑧ 条会把"非交易"标 not_bd，
    # 选题层再拦一道，非交易条目根本到不了成稿。所以这里只保留
    # 最保守的兜底——整张表没有任何一行有实质信息，才算空壳。
    for sec in doc.get("sections") or []:
        for b in sec.get("blocks") or []:
            if b.get("type") != "table":
                continue
            rows = b.get("rows") or []
            if len(rows) < 3:
                continue
            blank = sum(1 for r in rows
                        if any("未找到公开数据" in c for c in r))
            if blank == len(rows):
                head = " / ".join(b.get("head") or [])[:40]
                errs.append(
                    f"表格「{head}」{len(rows)} 行全部是「未找到公开数据」，"
                    f"是一张没有任何信息的空壳表。请删掉它——"
                    f"没有内容支撑的表格不如不写。")

    # 2.7 并购类稿件的关键事实
    #
    # 实测案例：一篇"Viatris 18亿美元收购 Pacira"的稿子里
    # 既没有每股收购价（实际 $36.50/股），也没有交易日期。
    # 只写总额的并购报道，读者看不出这是新闻，也判断不了贵不贵
    # ——每股价格才是估值锚点（对比市价能看出多少溢价）。
    #
    # 只对并购类做要求；授权类交易没有"每股价格"这个概念。
    # 判据用源标题（见函数说明），不用正文——正文对比表里常有
    # 别家公司的"收购"字样，会误报。
    subj = src_title or doc.get("title", "")
    facts = doc.get("title", "") + " " + content_text(doc)
    if re.search(r"收购|并购|买下|acqui|buyout|takeover|merger", subj, re.I):
        # 每股收购价只对【上市公司整体收购】适用。
        # 判据：素材或标题里出现交易所代码，如 (Nasdaq: PCRX)。
        # 反例："Merck Acquires SPR2015" 是资产收购（SPR2015 是药物代号），
        # 根本没有"每股价格"这个概念，强制要求会把好稿误拦。
        is_listed = bool(re.search(
            r"\((?:NASDAQ|NYSE|Nasdaq|NYSE|HKEX|SEHK)[:：]\s*[A-Z0-9]{2,6}",
            (material or "") + " " + subj))
        if is_listed and not re.search(
                r"(?:每股|per\s+share)[^。；]{0,24}[\d$]|"
                r"\$\s?\d[\d.,]*\s*(?:per\s+share|/\s*股)|"
                r"[\d.]+\s*美元\s*/\s*股", facts, re.I):
            errs.append(
                "上市公司整体收购，稿件缺少【每股收购价】——"
                "这是估值锚点，能看出溢价多少。"
                "可从 SEC 8-K / 公司公告取。")
        # 交易日期对所有并购类都适用
        if not re.search(
                r"20\d\d\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|"
                r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
                r"\d{1,2},?\s+20\d\d|"
                r"\d{1,2}\s+(?:January|February|March|April|May|June|July|"
                r"August|September|October|November|December)\s+20\d\d",
                facts, re.I):
            errs.append(
                "并购类稿件缺少【交易/公告日期】——读者据此判断是不是新闻")

    # 2.6 HOOK 必须交代交易时间
    #
    # 实测被用户指出：那篇「礼来1亿美元首付"敲门"」的开头写
    #   "礼来2026年单笔首付均值已达2.17亿美元，而这一次它只掏出
    #    1亿美元'零头'，就绑定了诺诚健华最多5个创新靶点的研发管线。"
    # 通篇没有交易日期，读者无从判断时效性——不像新闻。
    #
    # ⚠️ 只看 HOOK 段落，不查全文：日期写在文末或表格里，
    # 读者第一眼看不到，等于没写。
    hook = doc.get("hook", "") or ""
    if hook:
        DATE_RE = (
            r"20\d\d\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?|"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
            r"[a-z]*\.?\s+\d{1,2},?\s+20\d\d|"
            r"\d{1,2}\s+(?:January|February|March|April|May|June|July|"
            r"August|September|October|November|December)\s+20\d\d|"
            r"20\d\d-\d{1,2}-\d{1,2}"
        )
        if not re.search(DATE_RE, hook, re.I):
            errs.append(
                "HOOK 里没有交易时间。读者第一眼就要知道这是什么时候的事"
                "——请在开头写明具体年月日（如「2026年9月24日」或"
                "「Oct. 8, 2026」）。查不到就写「未找到公开数据」，"
                "但不要略过不提。")

    # 3. AI 味（分档）
    body = content_text(doc)
    tolerate = gen.get("ai_word_tolerate", 2)
    hard = gen.get("ai_word_hard_limit", 5)
    counts = {w: body.count(w) for w in style_cfg.get("banned_words", [])}
    total = sum(counts.values())
    if total > hard:
        top = sorted(counts.items(), key=lambda x: -x[1])[:5]
        errs.append(f"AI味词过多({total}处): "
                    + ", ".join(f"{w}x{c}" for w, c in top))
    elif total > tolerate:
        top = sorted(counts.items(), key=lambda x: -x[1])[:3]
        errs.append("AI味词: " + ", ".join(f"{w}x{c}" for w, c in top))

    for p in style_cfg.get("banned_patterns", []):
        if re.search(p, body):
            errs.append(f"AI味句式: {p}")

    # 3.5 去 AI 味硬闸门（2026-10-24 用户反馈后加）
    #
    # 背景：用户指出「你写的还是有很强的 AI 味，例如『不是…而是』」。
    # 根因是上面那条 banned_patterns 正则**排除了逗号**——
    #   '不是[^，。]{0,15}而是' 只匹配「不是A而是B」，
    #   而真实稿子绝大多数是「不是 X，而是 Y」带逗号的形态 → 一条都命中不了。
    # 后果：65 篇已发布稿里有 59 篇带着这类句式，而质检从来没报过警。
    #
    # 所以这里改用跨标点匹配的正则，并且：
    #   · 句式命中 = 硬失败（命中 1 处也要改）
    #   · 段落/句长 = 软提示（不判失败，见下）
    #
    # ⚠️ 为什么段落/句长只提示不拦：生成侧的重试循环会把 3000 字好稿
    #    换成 500 字残稿（2026-10-24 Gilead 事故）。
    #    句式是「改 1 个句子」，成本低、风险小；结构是「重排全文」，成本高。
    #    两者不能同一个力度。
    try:
        import deai as _deai
        _dres = _deai.scan_text(body)
        if _dres["pattern_hits"]:
            _names = sorted({h["name"] for h in _dres["pattern_hits"]})
            errs.append(
                f"AI味句式 {len(_dres['pattern_hits'])} 处"
                f"（{', '.join(_names)}）——"
                f"必须改掉，不是换词而是拆句重写")
        if _dres["word_hits"]:
            _wnames = sorted({h["name"] for h in _dres["word_hits"]})
            _wtot = len(_dres["word_hits"])
            if _wtot > _deai.THRESHOLDS["word_tolerate"]:
                errs.append(f"AI味词 {_wtot} 处: {', '.join(_wnames)}")
        # P0/P1 密度/结构违规 = 硬失败
        # P0 = 虚假归因等可信度问题；P1 = 公式化递进、句式依赖等结构问题。
        # P2（破折号/缓冲词/物理动词密度）只提示不拦——打磨项偶尔密一点很正常。
        for _h in _dres.get("density_hits", []):
            if _h.get("severity") in ("P0", "P1"):
                errs.append(f"[{_h.get('severity')}] {_h['name']}：{_h['text']}")
        # 软提示：只打印，不进 errs（进 errs 就变成硬失败了）
        for _sr in _dres.get("soft_reasons", []):
            print(f"      [提示] {_sr}")
    except Exception as _e:      # 闸门本身坏了不能连累成稿
        print(f"      [提示] 去AI味扫描器异常 {type(_e).__name__}: {_e}")

    # 4. 标题金额 vs 正文金额（实测抓到的真错误）    #
    # 案例：CI 生成的稿件标题写「GSK豪掷50亿美元首付」，
    # 正文却写「GSK向恒瑞支付了5亿美元首付」——差 10 倍。
    # 正文是对的，标题错了。而标题是读者第一眼看到的数字，必须拦。
    #
    # 规则：标题里「数字+限定词」（首付/总额/里程碑）的组合，
    #       必须在正文中找到同一组合。限定词不去管的话，
    #       「50亿」会误配到表格里康方的「50亿美元总额」而放行。
    errs.extend(_check_title_numbers(doc, body))

    # 4. 表格列数一致
    for sec in doc.get("sections") or []:
        for b in sec.get("blocks") or []:
            if b.get("type") == "table":
                head = len(b.get("head") or [])
                for r in b.get("rows") or []:
                    if head and len(r) != head:
                        errs.append(
                            f"表格列数不一致：表头 {head} 列，"
                            f"有一行 {len(r)} 列")
                        break

    # 5. 标记残留（模型误把标记写进正文）
    if re.search(r"^\s*(TABLE|BOX|FN|KICKER|TITLE|SUB|HOOK)\s*[:：]",
                 body, re.M):
        pass  # 已被解析器消费，不算错
    if "<p>" in text or "<div" in text:
        errs.append("正文里混入了 HTML 标签（应使用标记格式）")

    # 6. 事实闸门（最后一道，也是最贵的一道）——
    #    抓出成稿里"素材中没有"的高风险实体（分子代号、具体靶点）。
    #    放在结构校验之后：先确认这是个体完整的稿子，再去质疑它的真伪，
    #    避免对着半成品反复报错烧掉重试次数。
    errs.extend(check_facts(doc, text, material))

    return errs


# ============================================================
# 生成
# ============================================================
LENGTH_HINT = """

# 篇幅不足，必须加长

上一版正文只有 {n} 字，目标是 {target} 字。请按下面的方式扩写：

1. 每个 `## ` 小节至少 3 段（当前普遍只有 1-2 段）
2. 交易拆解加一张"条款逐项对照"表格（首付/里程碑/总额/权益范围/开发责任）
3. **介绍交易双方的公司背景**——这是最推荐的扩写方向：各自的主营业务、
   核心产品线/在售产品、研发方向、在该赛道的地位、商业化能力。
   这类公司概况是公开常识，写它不会造成事实风险。
4. 增加"同类交易历史对照"段落，用素材里的数据库数据做纵向比较
5. 增加"买方动机"和"卖方视角"两段独立分析
6. 行业背景数据（授权支出占比、领域分布、阶段分布）展开成表格并解读
7. 结尾加"未来 12-18 个月时间表"

**注意：已知信息要展开写透，不要把多个"未找到公开数据"堆成段落。**

⚠️ **扩写公司背景 ≠ 可以编交易细节。** 公司主营业务和产品线可以展开，
但交易本身的标的靶点、分子代号、临床阶段、金额条款，仍只能来自素材；
素材没披露的就写"未披露"，绝不脑补。
"""

STYLE_HINT = """

# 文风有问题

## ⚠️ 首要问题：AI 味句式（这是结构问题，不是换词能解决的）

下面列出了具体的句子和位置。**必须逐句改写，不能只换一个词。**

核心手法：**把「解释」改成「陈述」**。
「不是…而是」「这意味着」「也就是说」都是在解释一个读者已经看懂的东西。
读者没看懂就别解释，直接给下一个事实。

```
❌ 真正可以横向看的不是 16.4 与 13.2，而是 0.29 与 0.62 这两个风险比；
   而风险比能不能横着看，又取决于两组人群的可比性。
✅ 能横着看的只有风险比：0.29 对 0.62。
   风险比能不能横着看，取决于两组人群的可比性。
```

**拆句规则**：
1. 删掉「不是 A，而是 B」里的 A，直接写 B
2. 删掉「这意味着/也就是说/换句话说」后面的整句，前半句通常已经够
3. 超过 45 字的句子用句号断开成两句
4. 一段只说一件事，控制在 90 字内

## 结构类问题（同样是硬失败）

- **公式化递进**：「首先…其次…最后…」是报幕式递进。
  让三点之间的递进关系自己呈现，不要用连接词假装有层次。
- **虚假归因**：「业界普遍认为」「很多人都在讨论」「我个人认为」——
  换成具名引用，或者删掉归因直接陈述。
- **模板化开头/结尾**：「在……快速发展的今天」「让我们拭目以待」「未来已来」——删掉。
- **自我降格**：「不是成熟方案，更像是……」「只是一点初步思考」——直接删。
- **翻译腔**（物理动作形容抽象思维）：
  拆解→分析、打通→完成、沉淀→积累、接住→承接、闭环→循环。
  换成中文里本来就有的说法。
- **抽象名词主语 + 形容词结论**：「提供了一套生产级的……」
  → 说清楚到底提供了什么。

## 次要问题：AI 味词

- "值得注意的是" → 直接给结论
- "综上所述"/"总而言之" → 直接给结论
- "不言而喻"/"至关重要" → 删掉或换成具体描述
- "赋能" → "支撑""帮上"
- "底层逻辑" → "基本逻辑""实际原因"
- "本文将"/"让我们" → 直接开始讲

**篇幅保持不变，只改写句子结构。**
"""

STRUCT_HINT = """

# 结构有问题

请检查并修正：

1. 必须有 `TITLE:`、`HOOK:`、至少 3 个 `## ` 小节、以及 `FN ` 脚注
2. `TABLE:` 之后每行的 `|` 数量必须与表头一致
3. 不要输出任何 HTML 标签，只用标记
4. 结尾用 `ENDCARD 标题 | 正文`
"""


async def generate_one(client, eff: dict, plan_item: dict, material: str,
                       style_cfg: dict, out_dir: Path) -> dict:
    gen_cfg = style_cfg.get("generation", {})
    retries = gen_cfg.get("max_retries", 3)
    target = gen_cfg.get("target_words", 3000)

    system, user = build_prompt(plan_item, material, style_cfg)
    feedback = ""
    last_doc = None

    # ── 「先跑出来，再人工核查」（2026-10-24 加）─────────────────
    # 背景：10-24 那篇 Gilead 交易，尝试2 已经 3168 字/6小节/4脚注，
    # 只差 1 个问题（标题金额与正文不符）。但重试循环把 last_doc 一路覆盖，
    # 最后落盘的是尝试4 的 184 字，稿子白丢，还触发「产出丢失」告警。
    #
    # 现在的策略：
    #   · 每轮记录【问题数最少】的那一版
    #   · 全部轮次都没完全通过时，只要这一版过了【最低可用线】就照样写出去，
    #     并把剩余问题写进 <name>.review.txt，由飞书提示「需核查」
    #   · 过了最低线才写，是为了不把 184 字那种残稿推给人看
    # 事实性错误（金额对不上等）由本地核查+改稿环节收口，不在这一步硬拦。
    best = None          # {"nerr","errs","doc","txt","st"}
    MIN_BODY = 1200      # 低于这字数不值得给人看
    MIN_SECTIONS = 2     # 至少两个小节

    def _floor_ok(st_: dict) -> bool:
        return (st_.get("body_chars", 0) >= MIN_BODY
                and st_.get("sections", 0) >= MIN_SECTIONS)

    def _write_out(doc_, txt_, st_, review_notes: list[str] | None,
                   claim_block: bool = False):
        """写 HTML + 标记原文；needs_review 时同时写 .review.txt"""
        # ⚠️ 评分不达标的稿件：把标记带进 doc，render 时挂到 kicker 上
        #    （2026-10-25 用户要求"提示评分不达标"）。标记随稿件走，
        #    本机 publisher 从 HTML 就能读到，不用对齐 plan.json。
        if plan_item.get("below_threshold"):
            doc_ = dict(doc_)
            doc_["below_threshold"] = True
        html = render_html(doc_)
        idx = plan_item.get("_idx", 1)
        atype = plan_item.get("type", "event_report")
        fname = f"{idx:02d}-{atype}-{plan_item.get('lead_id','')[:8]}.html"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / fname).write_text(html, encoding="utf-8")
        (out_dir / (fname.replace(".html", ".txt"))).write_text(
            txt_, encoding="utf-8")
        if review_notes:
            if claim_block:
                head = ("⚠️ 事实断言存疑：断言核验这一步判定「有断言在素材里找不到出处」。\n"
                        "这篇【不能直接发布】，必须逐条核对下面列出的断言，"
                        "删掉或改写成素材支持的表述。\n\n")
            else:
                head = ("此稿自动生成时未通过全部质检，已按「先产出、后核查」策略落盘。\n"
                        "下面是要人工确认的问题：\n\n")
            (out_dir / (fname.replace(".html", ".review.txt"))).write_text(
                head + "\n".join(f"- {e}" for e in review_notes) + "\n",
                encoding="utf-8")
        return fname

    for attempt in range(retries + 1):
        msg = user
        if feedback:
            msg += f"\n\n# 上次的问题（必须修正）\n{feedback}\n"
            if "字数" in feedback:
                n = body_chars(last_doc) if last_doc else 0
                msg += LENGTH_HINT.replace("{n}", str(n)).replace(
                    "{target}", str(target))
            if "AI味" in feedback:
                msg += STYLE_HINT
            if any(k in feedback for k in ("缺少", "小节太少", "列数", "HTML")):
                msg += STRUCT_HINT

        try:
            txt = _strip_think(_call_llm(client, eff, system, msg, retries=1))
        except Exception as e:
            print(f"    尝试{attempt+1}: LLM 异常 {type(e).__name__}")
            continue

        if not txt or len(txt) < 200:
            print(f"    尝试{attempt+1}: 返回为空/过短（{len(txt or '')} 字符）")
            # 存下原始输出，便于诊断"为什么模型输出骤降"
            dbg = DATA_DIR / "debug"
            dbg.mkdir(parents=True, exist_ok=True)
            (dbg / f"short_{plan_item.get('lead_id','')[:8]}_{attempt+1}.txt"
             ).write_text(txt or "", encoding="utf-8")
            feedback = ("上次返回为空或过短。请输出完整内容："
                        "TITLE、HOOK、至少 3 个 ## 小节、表格、FN 脚注，"
                        "正文 2800-3200 字。")
            continue

        doc = parse_content(txt)
        last_doc = doc
        errs = validate_content(
                doc, txt, style_cfg,
                src_title=plan_item.get('title', ''),
                material=material)
        st = structure_report(doc)

        # 去 AI 味的具体命中位置（喂给模型，它才知道改哪句）
        deai_hits: list[str] = []
        try:
            import deai as _deai
            _dres = _deai.scan_text(content_text(doc))
            deai_hits = _deai.feedback_lines(_dres)
        except Exception:
            pass

        print(f"    尝试{attempt+1}: 正文{st['body_chars']}字 "
              f"小节{st['sections']} 表{st['tables']} "
              f"框{st['boxes']} 注{st['footnotes']} "
              f"{'✅通过' if not errs else '❌' + str(len(errs)) + '个问题'}")

        # 记录问题最少的那一版（同问题数取更长的，避免留下短残稿）
        if errs and (best is None
                     or len(errs) < best["nerr"]
                     or (len(errs) == best["nerr"]
                         and st["body_chars"] > best["st"].get("body_chars", 0))):
            best = {"nerr": len(errs), "errs": list(errs), "doc": doc,
                    "txt": txt, "st": st}

        if not errs:
            # ── 断言级核验（事实闸门拦不住的那几类）───────────────
            # check_facts 只抓分子代号/靶点缩写；编造合作方、编造商业化能力、
            # 编造监管状态、编造缺失等它抓不住，由 claimcheck 用
            # "逐字引文必须真在素材里"的判据补上。
            #
            # ⚠️ 判据是 claimcheck.should_block()，**不是"有错就拦"**：
            #    2026-10-19 临床雷达按"有错就拦"接线，跨语种素材的误报
            #    把 3 篇稿子全拦掉、重试 12 次产出归零。只拦强信号。
            try:
                import claimcheck
                body = content_text(doc) + " " + (doc.get("title") or "")
                _ok, _cerrs, _cstat = claimcheck.check_article(
                    client, eff, body, material)
                _block, _why = claimcheck.should_block(_cstat, _cerrs)
            except Exception as _e:
                _cerrs = [f"断言核验异常 {type(_e).__name__}: {_e}"]
                _cstat, _block, _why = {}, True, "断言核验异常（不能当通过）"
            print(f"      → 断言 {_cstat.get('ok', 0)}/{_cstat.get('total', 0)} 有出处"
                  f"（无出处 {_cstat.get('no_evidence', 0)}，"
                  f"编造出处 {_cstat.get('fake_evidence', 0)}，"
                  f"跨语种无法判定 {_cstat.get('cross_lang', 0)}）"
                  + (f"  ❌拦：{_why}" if _block else "  ✅放行"))
            if not _block:
                fname = _write_out(doc, txt, st, None)
                return {"ok": True, "file": fname, "stats": st,
                        "html_len": len(render_html(doc)),
                        "claimcheck": {"ok": not _cerrs, "why": _why,
                                       "errs": _cerrs, "stat": _cstat}}
            # ── 被拦：先把草稿存下来，否则"为什么被拦"事后无据可查 ──
            dbg = DATA_DIR / "debug"
            dbg.mkdir(parents=True, exist_ok=True)
            (dbg / f"rejected_{plan_item.get('lead_id','')[:8]}"
                   f"_{attempt+1}.txt").write_text(
                f"# 拦因：{_why}\n# 统计：{_cstat}\n# 问题：\n"
                + "\n".join(f"- {e}" for e in _cerrs[:40])
                + f"\n\n{txt}", encoding="utf-8")
            errs = _cerrs
            # 断言被拦的稿子也是"候选最优"，只是问题更多。
            # 标记 claim_block：这类是【事实存疑】，落盘时要用更重的警告措辞。
            if best is None or len(errs) < best["nerr"]:
                best = {"nerr": len(errs), "errs": list(errs), "doc": doc,
                        "txt": txt, "st": st, "claim_block": True}

        # 事实类问题需要【可执行的】纠正指令，光把报错丢回去模型往往改不掉
        fact_errs = [e for e in errs if "找不到" in e or "捏造" in e]
        feedback = "\n".join(f"- {e}" for e in errs)
        if deai_hits:
            # 具体到句子的改写指令——只说"有 AI 味"模型不知道改哪
            feedback += ("\n\n# ⚠️ AI 味句式（逐句改写，位置已标出）\n"
                         + "\n".join(deai_hits))
        if fact_errs:
            feedback += (
                "\n\n# ⚠️ 事实错误（必须删掉，不能换个说法保留）\n"
                "上面这些代号/靶点在本次素材里根本不存在——公告没有披露。"
                "正确做法是【删掉具体代号】，改用素材里有的表述：\n"
                "  · 素材说“至多5项创新靶点”就写“至多5项创新靶点”，"
                "不要从公司管线里挑一个具体靶点当标的\n"
                "  · 素材没给分子代号，就不要出现任何分子代号\n"
                "  · 篇幅不够就写【双方公司背景】（主营业务、产品线、研发方向），"
                "但绝不能靠编交易细节凑字数\n"
                "不要解释这些代号是否真实——素材里没有就是没有。"
            )
        print(f"      问题: {feedback[:180]}")

    # ── 全部轮次都没通过：退而求其次，把【最好的那一版】写出去 ────────
    # 这是「先跑出来、后人工核查」策略的落点。不写的话，
    # 一篇 3000 字的成稿会因为一个金额口径问题被整篇丢弃（2026-10-24 事故）。
    #
    # ⚠️ 但「素材太薄」这一类绝对不放行：那种稿子是靠世界知识凑出来的，
    #    整篇都不可信，不是"改几个数字"能救的。必须仍然跳过。
    _mat_thin = any("素材太薄" in e for e in (best["errs"] if best else []))
    if _mat_thin and best:
        print("    ✗ 最优版本的问题是「素材太薄」——整篇不可信，"
              "不落盘（按原规则跳过）")
    elif best and _floor_ok(best["st"]):
        _cb = bool(best.get("claim_block"))
        fname = _write_out(best["doc"], best["txt"], best["st"], best["errs"],
                           claim_block=_cb)
        print(f"    ⚠ 未通过全部质检，已按最优版本落盘（{fname}）"
              f"，剩余 {best['nerr']} 个问题待人工核查：")
        for e in best["errs"][:6]:
            print(f"       · {e[:110]}")
        return {"ok": True, "needs_review": True, "claim_block": _cb,
                "file": fname, "stats": best["st"], "problems": best["errs"],
                "html_len": len(render_html(best["doc"])),
                "note": ("事实断言存疑，禁止直接发布，必须逐条核对"
                         if _cb else
                         "未通过全部质检，已按最优版本落盘，需人工核查")}

    if best:
        print(f"    ✗ 最好的一版也没过最低可用线"
              f"（正文{best['st'].get('body_chars',0)}字/"
              f"小节{best['st'].get('sections',0)}个，"
              f"要求≥{MIN_BODY}字/≥{MIN_SECTIONS}节）→ 不落盘")
    return {"ok": False, "error": feedback[:400],
            "stats": structure_report(last_doc) if last_doc else {}}


# ============================================================
# 主流程
# ============================================================
def run(date: str | None = None) -> list[dict]:
    date = date or today_cst()
    plan = read_json(SCORED_DIR / f"{date}.plan.json", {})
    if not plan or not plan.get("plan"):
        print("[compose] 没有选题计划")
        return []

    style_cfg = load_yaml("style_guide.yaml")
    cfg = load_yaml("scoring.yaml")
    out_dir = POSTS_DIR / date[:4] / date[5:7] / date[8:10]

    print(f"\n=== 生成 {len(plan['plan'])} 篇 -> {out_dir} ===")
    results: list[dict] = []

    async def _all():
        gcfg = cfg.get("gen_llm") or cfg["llm"]
        client, eff = _client(gcfg, cfg.get("providers", {}))
        print(f"       model={eff['model']} max_tokens={eff['max_tokens']}")
        for i, item in enumerate(plan["plan"], 1):
            item["_idx"] = i
            # 幂等：同一 lead_id 只要【任何日期】成过稿就不再生成。
            # ⚠️ 早先只查今天的目录，导致同一笔交易跨天重复发布
            # （诺诚健华×礼来在 10-10 和 10-11 各发了一遍）。
            # 根因是 seen.json 里存了 8 位短 id、比对时按 16 位全量比，
            # 历史去重没拦住；这里再加一道跨日期检查兜底。
            lead = (item.get("lead_id") or "")[:8]
            if lead and POSTS_DIR.exists() and any(
                    lead in p.name
                    for p in POSTS_DIR.rglob("*.html")):
                print(f"\n  [{i}] 已存在成稿（任意日期），跳过 "
                      f"{item.get('title','')[:50]}")
                results.append({"ok": True, "skipped": True,
                                "title": item.get("title", ""),
                                "score": item.get("score", 0),
                                "type": item.get("type", ""),
                                "stats": {}})
                continue
            thin, why = material_too_thin(date, item)
            if thin:
                print(f"\n  [{i}] ⏭ 素材太薄，跳过：{why}")
                print(f"      {item.get('title','')[:60]}")
                results.append({"ok": False, "skipped": True,
                                "reason": f"素材太薄：{why}",
                                "title": item.get("title", ""),
                                "score": item.get("score", 0),
                                "type": item.get("type", ""),
                                "stats": {}})
                continue
            material = await build_material_async(date, item)
            print(f"\n  [{i}] {item.get('type')} | {item.get('score')}分")
            if "临床试验注册库" in material:
                print(f"      已补充注册库查证素材")
            print(f"      {item.get('title','')[:60]}")
            r = await generate_one(client, eff, item, material,
                                    style_cfg, out_dir)
            r["title"] = item.get("title", "")
            r["score"] = item.get("score", 0)
            r["type"] = item.get("type", "")
            results.append(r)

    asyncio.run(_all())

    ok = sum(1 for r in results if r.get("ok"))
    new = sum(1 for r in results if r.get("ok") and not r.get("skipped"))
    review = sum(1 for r in results if r.get("needs_review"))
    print(f"\n=== 完成 {ok}/{len(results)} 篇"
          f"（新生成 {new}，跳过已存在 {ok - new}，需人工核查 {review}）===")
    for r in results:
        mark = "⚠" if r.get("needs_review") else ("✅" if r.get("ok") else "❌")
        st = r.get("stats") or {}
        print(f"  {mark} {r.get('type','')[:16]:16} "
              f"{st.get('body_chars',0):>5}字  {r.get('title','')[:36]}")
        if r.get("needs_review"):
            if r.get("claim_block"):
                print(f"      ‼️ 事实断言存疑：禁止直接发布，需逐条核对")
            for e in (r.get("problems") or [])[:4]:
                print(f"      · 待核查：{e[:120]}")
        elif not r.get("ok"):
            print(f"      {r.get('error','')[:140]}")

    # 把【已发表】的选题写进 seen.json，作为跨天去重基准。
    # 只记写成的，不记抓到的——否则没入选的高分条目会被永久埋掉。
    published = [
        {"lead_id": it.get("lead_id"), "title": it.get("title", ""),
         "deal_keys": it.get("deal_keys") or (
             [it["deal_key"]] if it.get("deal_key") else [])}
        for it, r in zip(plan["plan"], results)
        if r.get("ok")
    ]
    if published:
        try:
            from dedupe import record_published, record_published_deals
            record_published(published, today=date)
            record_published_deals(published, today=date)
        except Exception as e:
            print(f"  [seen] 记录失败（不影响成稿）: {type(e).__name__}")

    write_json(DATA_DIR / f"compose_{date}.json", results)
    return results


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    run(sys.argv[1] if len(sys.argv) > 1 else None)