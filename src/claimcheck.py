"""
逐条断言核验：把成稿拆成原子事实断言，要求每条都给出素材出处。

# 为什么需要它（2026-10-17 retlirafusp 事故）

BD 雷达的 `check_facts`（正则抓分子代号/靶点缩写）实测对 6 类编造**只拦住 2 类**：

    分子代号编造        ⛔ 拦下
    靶点缩写编造        ⛔ 拦下
    企业归属编造        ❌ 放行   ← retlirafusp 那篇最主要错（"君实/CytomX/Coherus"）
    交易对手错          ❌ 放行   ← "罗氏在 ALS 合作"（实为礼来/吉利德）
    商业化能力编造      ❌ 放行   ← "有北美商业化能力"
    编造缺失（把有写成没有）❌ 放行   ← "首付未披露"
    编造监管状态        ❌ 放行   ← "已获批" 实为在审
    授权区域写反        ❌ 放行   ← "全球独家" 实为中国独家

正则只能匹配"代号/缩写"形状的东西，剩下 6 类都不长那样。

# 做法：不问"这句话对不对"，而问"这句话的出处能不能逐字引出来"

两步，缺一不可：
  ① 让核查模型对每条断言给出**逐字**的素材原文片段（evidence）；
  ② **程序再验一次**这段 evidence 是否真的出现在素材里。

第 ② 步是防"核查模型自己橡皮图章"的关键——模型很容易在证据栏里
凭记忆写一段看着像原文的话。只要程序发现"引文不在素材里"，
就判定为**编造出处**，整条打回。

⚠️ 同一份代码在临床雷达里也跑（`clinical-data-radar/src/claimcheck.py`），
机制完全相同；只是 `CLAIM_SYS` 提示词和 kind 分类侧重 BD 场景。

⚠️ LLM 抽取的**运行间波动极大**（同一篇稿三次核验分别得到
"2 条无依据 / 24 条无依据 / ✅ 可信"），所以 `audit.py` 总是叠加
确定性检查 + 断言太少判"核验不充分"作兜底。本模块自身**只做核验**，
波动兜底在 `audit.py`。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# ⚠️ 复用 score 的 <think> 剥离实现（compose.py 也是这么接的）。
#    MiniMax 默认开思考，响应会混入 `<think>…`；不剥离就没法解析 JSON。
#    这是"核验返回 0 条断言"的直接成因，别再各写一份。
from score import _strip_think  # noqa: E402

# 需要出处的断言类型。general（通用医学/行业背景）免出处——
# 否则每篇都会被"这个常识没出处"打回。
KINDS_REQUIRE_EVIDENCE = {
    "attribution",   # 谁做的/谁宣布的（企业归属、合作方）
    "mechanism",     # 靶点、作用机制（交易标的中如果涉及分子）
    "number",        # 任何具体数值（首付、里程碑、样本量、营收、百分比…）
    "status",        # 监管/获批/试验/交易状态
    "company",       # 公司背景（主营、已上市产品、年收入、定位、合作伙伴）
    "deal_term",     # 交易条款（首付、里程碑、权益范围、授权区域、合作模式）
    "financial",     # 财务数据（营收、利润、估值、市值）
    "clinical",      # 标的/管线的临床数据
}

# 强调 BD 场景：合作方/授权区域/交易状态是 BD 文章最高频的胡编点
CLAIM_SYS = """你是事实核查员，专门核查**医药 BD 交易**类文章。下面给你【一篇成稿】和【它的素材】。
把成稿拆成**原子事实断言**，逐条判断它在素材里有没有依据。

# 输出（严格 JSON 数组，不要解释文字）
[
  {
    "text": "断言本身，一句话，保留具体主体/数字",
    "kind": "attribution|mechanism|number|status|company|deal_term|financial|clinical|general",
    "evidence": "素材里逐字支持这条断言的原文片段；找不到就空字符串"
  }
]

# kind 怎么分（BD 场景重点）
- attribution  ：谁做的、谁宣布的、谁是合作方（如"Coherus 与 CytomX 合作"）
- mechanism    ：靶点、作用机制（如交易标的若是已上市产品）
- number       ：任何具体数值（首付金额、里程碑、总价、试验入组数、百分比）
- status       ：监管/获批/试验/交易状态（已申报、已获批、已签约、已交割）
- company      ：公司背景（主营、已上市产品、合作伙伴、定位、pipeline）
- deal_term    ：交易条款（**首付、里程碑、权益范围、授权区域、合作模式、独家/非独家**）
- financial    ：财务（营收、利润、估值、市值）
- clinical     ：标的或对家管线的临床数据
- general      ：通用医学/行业常识（如"MNC 多采用首付+里程碑+royalty 结构"）
                —— 不需要出处

# ⚠️ evidence 的唯一合法填法：从素材里**逐字复制粘贴**
- 不许改写、不许概括、不许翻译、不许凭记忆写、不许把两处拼起来
- ⚠️ **语言必须与素材一致。素材是英文就贴英文原文，不要译成中文。**
  （实测：成稿写"阿斯利康"、素材写 "AstraZeneca"，若 evidence 填中文
   就与素材匹配不上，真断言被判成无依据——这是本模块最常见的误报来源。）
- ⚠️ **不许写"素材中提到""素材标题显示""素材里说"这类说明文字**，
  那只说明"你以为有"，不是证据。直接贴素材的原文片段。
- 素材里**没有**这条断言 → evidence 留空字符串
- **留空不会被罚；编一个出处会被程序发现并判整条不通过**

# ⚠️ 拆细
一句里含两个事实就拆两条。数值、企业名、合作方、授权区域、监管状态
各算独立的断言。
标题、HOOK、正文、表格、结语里的断言**都要拆**——不要只查正文。

只输出 JSON 数组。"""


# 引文的最短有效长度（归一化后）。中文里 "首付 1 亿美元" 只有 8 个字，
# 但是一条很好的出处。门槛太高会把"简短但真实"的引文误杀。
MIN_EVIDENCE_LEN = 5

# 引文与素材比对判据：**最长公共子串**。
#
# ⚠️ 试过两种更"高级"的算法，都不如这个（2026-10-17 实测）：
#   ① 严格"整段是子串" —— 核查模型常改写/截断/拼接引文，
#      把大量真出处判成编造，误报淹掉报告。
#   ② 字符 n-gram 重合率 —— 素材本身很短时，任何改写都把重合率稀释到极低
#      （实测一条真出处的重合率只有 10%，因为引文里混了改写词）。
# 最长公共子串只问一件事：**引文里有没有一段 ≥MIN_EVIDENCE_LEN 字的连续真原文**。
# 纯凭记忆编的引文与素材几乎没有连续重合，会被抓住。
_GRAM_N = 6  # 保留常量名以免外部引用报错；当前用最长公共子串


def _norm(s: str) -> str:
    """归一化，用于"引文是否真的在素材里"的比对。"""
    s = (s or "").lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    return re.sub(r"[，。；：、！？（）()\[\]【】\"'“”‘’·,.;:!?\-—_/]", "", s)


def _longest_common_run(ev_n: str, mat_n: str) -> int:
    """
    引文与素材的最长公共**连续**子串长度（滚动 DP）。
    引文几百字、素材几千字，开销可忽略。
    """
    if not ev_n or not mat_n:
        return 0
    prev = [0] * (len(mat_n) + 1)
    best = 0
    for i in range(1, len(ev_n) + 1):
        cur = [0] * (len(mat_n) + 1)
        ei = ev_n[i - 1]
        for j in range(1, len(mat_n) + 1):
            if ei == mat_n[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


# 解析失败时重试几次。单次坏响应曾让整篇的核验归零（2026-10-17）。
_EXTRACT_ATTEMPTS = 2


def claims_from_raw(raw: str) -> list[dict] | None:
    """
    从模型响应里解析断言数组。**解析失败返回 None**，由调用方决定重试/报错。

    ⚠️ 三个真实踩过的坑（2026-10-17，一次"上午 50 条、下午 0 条"的事故）：

    ① **MiniMax 默认开思考**，响应以 `<think>…` 开头（有时不闭合）。
       不剥离就去找 `[...]`，会把思考正文里的方括号当成数组起点，
       `json.loads` 必然失败 → 原来静默 `return []`
       → 报告里显示"未能拆出任何断言"，与"模型真没拆出"完全同形，
       排查时分不清是解析坏了还是模型没做事。
       （实测：思考里出现一个 `[` 就复现 0 条；一个都没有就正常。）
    ② 原来的 `re.search(r"\\[.*\\]", raw, re.S)` 是**贪婪**匹配，
       从第一个 `[` 抓到最后一个 `]`，把思考正文整个吞进候选串。
    ③ 所以：先剥 think，再**扫描**出最大的一组合法 JSON 数组。
    """
    text = _strip_think(raw or "")
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    if not text:
        return None
    dec = json.JSONDecoder()
    best: list = []
    for i, ch in enumerate(text):
        if ch != "[":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except Exception:
            continue
        if isinstance(obj, list) and len(obj) > len(best):
            best = obj
    if not best:
        # 模型明确返回空数组 `[]`：算解析成功（确实没有断言），
        # 用 None 区分开"根本没解析出来"。
        return [] if re.search(r"\[\s*\]", text) else None
    return [c for c in best if isinstance(c, dict)]


def extract_claims(client, eff: dict, article_text: str,
                   material: str) -> list[dict]:
    """让 LLM 把成稿拆成原子断言，并尽量给出逐字出处。"""
    user = (f"# 成稿\n{article_text}\n\n"
            f"# 素材\n{material}\n\n"
            f"现在按 JSON 数组输出。")
    data: list[dict] = []
    last = ""
    # 2026-11-01：走 provider 降级链（MiniMax 主用、DeepSeek 备用）。
    # 主用 429/鉴权失败时不要直接判"核验失败"，切备用再试。
    chain = [(client, eff)] + list(eff.get("_fallback_chain") or [])
    for _ in range(_EXTRACT_ATTEMPTS):
        for _ci, (_c, _e) in enumerate(chain):
            try:
                r = _c.chat.completions.create(
                    model=_e["model"],
                    messages=[{"role": "system", "content": CLAIM_SYS},
                              {"role": "user", "content": user}],
                    temperature=0,
                    max_tokens=_e.get("max_tokens", 4000),
                )
            except Exception as _ex:
                if _ci + 1 < len(chain):
                    print(f"[claimcheck] {_e.get('provider')} 调用失败"
                          f"（{type(_ex).__name__}: {str(_ex)[:80]}）→ 切备用")
                else:
                    print(f"[claimcheck] {_e.get('provider')} 调用失败"
                          f"（{type(_ex).__name__}: {str(_ex)[:80]}）")
                last = ""
                continue
            last = (r.choices[0].message.content or "").strip()
            data = claims_from_raw(last)
            if data:
                break
        if data:
            break
    if not data:
        # ⚠️ **必须出声**：解析失败以前是静默 return []，与"模型没拆出断言"
        #    长得一样，导致"核验没生效"可能被读成"文章没问题"。
        print(f"[claimcheck] ⚠️ 未解析出断言（响应 {len(last)} 字符，"
              f"已试 {_EXTRACT_ATTEMPTS} 次）：{last[:120]!r}")
        return []
    out = []
    for c in data:
        txt = str(c.get("text") or "").strip()
        if not txt:
            continue
        out.append({"text": txt,
                    "kind": str(c.get("kind") or "general").strip().lower(),
                    "evidence": str(c.get("evidence") or "").strip()})
    return out


def _cjk_ratio(s: str) -> float:
    """中文汉字占字母类的比例，用来判断引文/素材是否同一书写系统。"""
    if not s:
        return 0.0
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in s if ch.isalpha())
    return cjk / letters if letters else 0.0


def verify_claims(claims: list[dict], material: str) -> tuple[list[str], dict]:
    """
    程序化核验。返回 (问题列表, 统计)。

    一类问题 + 一类"无法判定"：
      1. 需要出处却没给出处            → no_evidence
      2. 给出了处但引文不在素材里       → fake_evidence（编造出处）
      3. 引文与素材**书写系统不同**     → cross_lang（无法判定，不罚）

    ⚠️ 第 3 类必须单独算（2026-10-19 临床雷达事故根因）：
       素材是英文（公告/CT.gov），成稿是中文；核查模型被要求"逐字引文"时
       常给中文译文 → 与英文素材子串重合 ≈0 → 被判**编造出处** →
       闸门把整天的稿子全拦掉，产出归零。
       跨语种时子串比对没有意义：既不能证明有出处，也不能证明编造。
    """
    mat_n = _norm(material)
    mat_cjk = _cjk_ratio(material)
    errs: list[str] = []
    stat = {"total": 0, "ok": 0, "no_evidence": 0,
            "fake_evidence": 0, "cross_lang": 0}

    for c in claims:
        kind = c.get("kind", "general")
        if kind not in KINDS_REQUIRE_EVIDENCE:
            continue                       # general：免出处
        stat["total"] += 1
        ev = c.get("evidence") or ""
        if not ev:
            stat["no_evidence"] += 1
            errs.append(f"[{kind}] 断言「{c['text'][:60]}」在素材里找不到出处。"
                        f"把它删掉，或改成素材支持的表述。")
            continue
        # 跨语种：一边倒中文 / 一边倒英文 → 无法用子串判定
        ev_cjk = _cjk_ratio(ev)
        if (ev_cjk > 0.5 and mat_cjk < 0.1) or (ev_cjk < 0.1 and mat_cjk > 0.5):
            stat["cross_lang"] += 1
            continue
        ev_n = _norm(ev)
        run = _longest_common_run(ev_n, mat_n)
        if len(ev_n) >= MIN_EVIDENCE_LEN and run >= MIN_EVIDENCE_LEN:
            stat["ok"] += 1
            continue
        stat["fake_evidence"] += 1
        errs.append(
            f"[{kind}] 断言「{c['text'][:50]}」给出的出处**不在素材里**"
            f"（引文：{ev[:40]}…，与素材最长连续重合仅 {run} 字）"
            f"——疑似编造出处。出处必须从素材逐字复制；素材里没有就删掉这条断言。")
    return errs, stat


def check_article(client, eff: dict, article_text: str,
                  material: str) -> tuple[bool, list[str], dict]:
    """成稿后跑一次断言核验。返回 (是否通过, 问题, 统计)。"""
    claims = extract_claims(client, eff, article_text, material)
    if not claims:
        return False, ["断言核验未能拆出任何断言（核查模型返回为空）"], {}
    errs, stat = verify_claims(claims, material)
    return (not errs), errs, stat


# 无出处断言**不**作为拦截依据（2026-10-19 事后修正，见 should_block）
NO_EVIDENCE_BLOCK_RATIO = None


def should_block(stat: dict, errs: list[str]) -> tuple[bool, str]:
    """
    决定这条稿子该不该【硬拦】。

    ⚠️ 与临床雷达 `clinical-data-radar/src/claimcheck.py` 同实现（两份镜像）。

    演进（前两版都不对）：
      v1「有错就拦」→ 2026-10-19 临床雷达 3 篇 × 4 次重试全被拦，产出归零。
         根因：跨语种素材（英文公告 + 中文成稿）下核查模型把 evidence 写成
         中文，与英文素材匹配不上，**真断言**被判无出处。
      v2「无出处占比 > 50% 才拦」→ 仍然拦得住，因为跨语种下占比轻易过半。
      v3（现在）**无出处一律不拦**，只有"编造出处"硬拦。

    理由：`fake_evidence` 是核查模型自己编造引文，无歧义；`no_evidence`
    是"没找到出处"，跨语种下大量是误报，不能用来决定今天能不能发稿。
    真相交给 `audit.py` 事后报告 + 人工复核。

    仍硬拦：未返回统计 / 未拆出断言（核验没生效）、fake_evidence > 0。
    """
    total = int((stat or {}).get("total", 0) or 0)
    fake = int((stat or {}).get("fake_evidence", 0) or 0)

    if not stat:
        return True, "断言核验未返回统计（核验未生效）"
    if total == 0:
        return True, "未能拆出任何断言（核验未生效）"
    if fake > 0:
        return True, f"{fake} 条断言的出处是编造的（引文不在素材里）"
    return False, ""


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # 离线自测：喂"已拆好的断言"验证第二步（引文核验）
    # 素材模拟一条 AZ-insitro 类交易公告
    MAT = ("标题：AstraZeneca 与 insitro 达成战略药物发现与授权合作\n"
           "阿斯利康将向 insitro 支付 4000 万美元首付，并就 MASH 与心肌病两个"
           "适应症获得其 Virtual Human AI 平台的开发权益；insitro 另有最高约 14.6 亿"
           "美元的研发与商业化里程碑，AZ 接手临床开发并持全球独家权利。\n"
           "合作双方首次披露于 2026 年 10 月 15 日。")
    CASES = [
        # 真实出处
        ("首付有出处",  "deal_term",   "4000 万美元首付",                    True),
        # 没给出处
        ("没给出处",    "attribution", "",                                    False),
        # 编造合作方
        ("编造合作方",  "attribution", "此前已与罗氏在 ALS 领域合作",         False),
        # 编造商业化能力
        ("编造能力",    "company",     "Coherus 在北美拥有成熟商业化团队",     False),
        # 编造缺失
        ("编造缺失",    "deal_term",   "首付金额未在公告中披露",               False),
        # 编造监管状态
        ("编造状态",    "status",      "该合作已获 NMPA 批准",                 False),
        # 改写但真出处
        ("改写真出处",  "deal_term",   "最高约 14.6 亿美元的研发与商业化里程碑", True),
        # 通用常识免出处
        ("通用常识",    "general",     "",                                    True),
        # 授权区域反转（编造）
        ("授权反转",    "deal_term",   "AZ 持有该产品在中国大陆的独家权利",     False),
    ]
    print("=" * 70)
    print("BD 雷达：第二步（引文核验）离线自测")
    print("=" * 70)
    for name, kind, ev, expect_ok in CASES:
        errs, _ = verify_claims(
            [{"text": "测试断言", "kind": kind, "evidence": ev}], MAT)
        ok = not errs
        mark = "✓" if ok == expect_ok else "✗"
        print(f"   {mark} {name:10} kind={kind:12} 通过={ok}")
        if errs:
            print(f"        {errs[0][:100]}")
