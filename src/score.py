"""
LLM 评分模块。

核心价值：把人工分析的判断规则编码进 prompt。
这些规则来自实战踩过的坑：

1. 【口径分层】潜在总额 ≠ 首付 ≠ 到账。185 亿美元里首付只有 12 亿。
   评分必须基于首付，不是总额。
2. 【关联交易检测】买卖双方有股权关系时，溢价不代表市场共识。
   迪哲×AZ 首付占比 40% 看着漂亮，但 AZ 持股 23.42%，是体系内变现。
3. 【"未找到"≠"不存在"】查不到就填 verify_todo，不要下否定结论。
   早期把"PDF 读不了"当"不存在"，导致漏掉几十项 III 期资产。
4. 【信息增量】中文媒体已充分报道、英文媒体未跟进的，降权。
5. 【排除纯融资】IPO/定增不是 BD。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

from openai import OpenAI

from common import (RawItem, SCORED_DIR, RAW_DIR, load_yaml, read_json,
                    write_json, today_cst, normalize_deal_key)

# 交易信号（用于给 LLM 一个显式提示，不做硬性拦截——
# 正则会有漏判，例如 "Ono pays Biohaven $80M for..." 没有 license 字样
# 但确实是交易。所以只作为参考字段交给 LLM 判断）
DEAL_SIGNAL = re.compile(
    r"\b(?:licens|acqui|merger|partner|collaborat|option|buyout|takeover|"
    r"stake|deal|upfront|asset\s+purchase|pays?|purchase)\w*\b"
    r"|授权|引进|收购|并购|合作|许可|首付|权益|对外授权|共同开发", re.I)


SYSTEM_PROMPT = """你是医药政策（药监 / 医保 / 卫生 / 产业）领域的资深分析师。
你的任务是给新闻条目打分，识别出真正值得深度报道的政策变化。

这里的「政策」＝ 官方发出的、对行业行为有约束力的东西：
法律 / 条例 / 部门规章 / 规范性文件 / 指导原则 / 公告 / 通知 /
征求意见稿 / 医保目录与支付规则 / 集采文件 / 执法处罚决定。

⚠️ 调用方模板里可能还沿用历史措辞（"BD 新闻""交易""deal_signal"），
那是旧口径的残留，一律忽略：本雷达只看「这是不是一条政策」。

# 先判断：这是不是一条政策（最高优先级，先判这一条再打分）

- **是政策**：机关发出的文件、公告、通知、征求意见稿、目录、集采规则、
  处罚决定；以及它们的官方解读（答记者问、政策吹风会、官方图解）。
- **不是政策**：企业融资 / 并购 / 授权交易、临床数据读出、股价波动、
  人事变动、企业软文、会议通知、纯转载通稿、科普、招聘与榜单。

⚠️ 遇到「不是政策」的条目：`score` **必须 ≤ 2.0**，`reasoning` 写明
"非政策条目"，`endpoint_verdict` 填 `none`。即使它热度很高也不改这个判断。

# 评分维度（满分 10，五项加总即 score）

## 1. 发文层级 policy_level (0-3)
- 国务院 / 多部委联合发文 = 3
- 单一部委（国家医保局、国家药监局、国家卫健委、工信部、财政部等）= 2.5
- 部委司局 / 直属机构（药审中心、医保局医药服务管理司等）= 1.5
- 地方省级（省医保局、省药监局）= 0.8
- 地方市级及以下 = 0.3
看的是**发文机关**的层级，不是转发媒体、不是解读文章的出处。

## 2. 影响范围 impact (0-3)
- 全行业规则（所有企业 / 所有品类都要改做法）= 3
- 单适应症 / 单品类 / 单环节（某个靶点、某类耗材、某个流通环节）= 2
- 单企业（只针对一家或几家企业）= 0.8
- 仅表述重申（没有新增约束）= 0.3

## 3. 效力阶段 force (0-2)
- 已生效并执行 = 2
- 正式发布未生效 = 1.5
- 征求意见稿 = 1.0
- 吹风 / 规划 / 解读 = 0.5
⚠️ 征求意见稿**不是**已发布文件。任何情况下都不得按"已生效 / 将实施"加分。

## 4. 增量 novelty (0-1.5)
- 首次明确 / 推翻既有规则 / 超预期收紧 = 1.5
- 增量收紧（在旧规则上再加一层）= 1.0
- 例行发布（年度目录、常规调整）= 0.4
- 纯重申（和旧文件一个意思）= 0

## 5. 可核验性 verifiability (0-0.5)
- 官方原文 + 文号 + 生效日期 = 0.5
- 只有官方通知摘要（官网短讯、答记者问）= 0.3
- 只有媒体转述 = 0.1

# 输出字段（一个都不能多、不能少、不能改名）

- `id`：原样回填条目的 id
- `score`：0-10，上面五项加总
- `channel`：`"china"` | `"global"`。
  境内机关发布 = china；境外机构（FDA / EMA / WHO / 他国卫生部）发布 = global
- `cluster_key`：同一政策事件的归并键，格式 `policy:<主题短横线>`，
  例 `policy:nmpa-xxx`、`policy:集采-第11批`、`policy:drg-2.0`。
  ⚠️ 判断依据是**政策主题，不是发文机关**：同一件事的征求意见稿与正式稿、
  中央文件与地方落地版、中英文两条报道，都必须用**同一个** cluster_key。
  不是政策的条目输出空字符串。
- `flags`：从下面这套里取，可多选，不确定就不选：
  `consultation`(征求意见稿) / `formal`(正式发布) / `enforcement`(执法处罚) /
  `effective`(已生效) / `national`(全国性) / `local`(地方) / `overseas` /
  `price`(价格/支付) / `procurement`(集采) / `approval`(审评审批) /
  `reimbursement`(医保目录/支付) / `industry`(产业扶持/监管改革) /
  `stale`(过期) / `duplicate`(与其他条目重复) / `minor`(地方小文件/事务性通知)
- `endpoint_verdict`：政策所处阶段，六选一：
  `draft`(征求意见/草案) / `issued`(正式发布) / `effective`(已生效) /
  `enforced`(已执行、处罚落地) / `repealed`(废止/被替代) / `none`(判断不了)
- `verdict_basis`：结论依据，四选一：
  `official_text`(官方原文) / `official_notice`(官方通知摘要) /
  `media`(媒体转述) / `none`
- `confidence`：0-1，你对本条判断的把握
- `reasoning`：**只写"为什么值得报道"，不下事实结论**（见下方 ⓐ）
- `verify_todo`：字符串数组，成稿前必须核对的具体事项，**至少 1 条**
- `article_type`：四选一
  `policy_readout`(单一政策解读) / `consultation`(征求意见稿解读) /
  `enforcement`(执法处罚复盘) / `policy_review`(多文件/时间线综述)

# 必须执行的核查（历史教训，务必遵守）

**ⓐ reasoning 只写"为什么值得报道"，不写事实结论（踩过事故的规则）**
`reasoning` 会被原样喂给成稿阶段当"选题理由"。你在这里下的任何一个事实判断，
下游都会当既成事实继续扩写：机制、金额、品种数、执行日期、适用对象、
谁负责执法——**一律进 `verify_todo`，不要在 reasoning 里替原文下结论**。

- ❌ "新规把某某品种的报量规则改成按厂牌报量"（素材没写清就下结论）
- ❌ "文件要求所有药店在 12 月 1 日前完成整改"（时间点/执行细节是推断）
- ✅ "第十一批集采文件已正式发布，涉及报量规则的调整，属年度性全行业事项，
     值得把规则变化和影响面写清"
- 若你怀疑某细节（文号、生效日期、适用对象、具体条款），放进 `verify_todo`。

**ⓑ 征求意见稿不得当已发布写**
征求意见稿：`flags` 必须含 `consultation`，`endpoint_verdict` 填 `draft`，
`force` 只能给 1.0。**不得**写"已发布""将实施""新规来了"，
也不得因为媒体标题写成"新规"就跟着抬分。

**ⓒ "未找到" ≠ "不存在"**
某项信息无法从素材确认（文号、生效日期、是否废止旧文件），
放进 `verify_todo`，**不要下否定结论**。

**ⓓ 时效性（容易踩坑）**
每条都带 `published` 和 `age_days`：
- `age_days` 超过 7 天：不是新闻，降 `impact`，reasoning 里说明"非近日"
- 超过 14 天：直接降 2 分以上，或标 `stale`
- 为「未知」：不要假设它是新闻，也不要假设它旧，按内容本身评分，
  并在 reasoning 里标注"发布日期未知"
⚠️ 媒体转载旧文件是常见现象。讲"去年那份文件"的文章讲的是历史，不要当新政。

**ⓔ 素材量不足要标注**
`material_chars` 是可用正文字数：
- 为 0 或很小（只有标题）：`verify_todo` 写明"仅有标题，无正文素材，
  成稿前需补官方原文"，并把 `confidence` 压到 0.5 以下。
- 只有媒体转述（`verdict_basis` = media）同样压低 `confidence`。

**ⓕ 噪声兜底（本地已有前置过滤，这里再判一道）**
企业软文 / 产品推广、会议通知、人事任免、纯转载通稿、科普问答、
地方事务性小文件（无全国影响）、招聘 / 榜单：
`score` ≤ 2.0，事务性地方文件加 `minor`，过期加 `stale`。

**ⓖ 同一政策事件的多条要归并**
一天里同一件事的多条（征求意见稿 + 媒体解读 + 官方答记者问）：
`cluster_key` 相同，`score` 按其中**最权威的那条**给；
组里超过 2 条时，其余条目 `article_type` 用 `policy_review`，
让 select 阶段合并成一篇综述。

**ⓗ 不要因为"媒体报得多"加分**
这个雷达选的是**文件**，不是热度。多渠道转载但拿不到原文的，
`novelty` 和 `verifiability` 都要压；官方原文一句话没说清的，宁可低分。

**ⓘ endpoint_verdict 与 flags 会决定"部委级正式文件保底"是否触发**
（当天没有达标条目时，部委级正式文件允许保底 1 篇。）
所以这两个字段必须按原文如实填，判断不了用 `none` / 不选，**不要猜**。

# 输出格式（严格 JSON，不要任何解释文字）

⚠️ **只输出一个 JSON 数组**。不要输出思考过程、不要解释、不要 markdown 代码块包裹。
第一个字符必须是 `[`，最后一个必须是 `]`。

[{
  "id": "条目id",
  "score": 7.8,
  "channel": "china",
  "cluster_key": "policy:nmpa-中药注册管理专门规定",
  "flags": ["formal", "national", "approval", "effective"],
  "endpoint_verdict": "effective",
  "verdict_basis": "official_text",
  "confidence": 0.85,
  "reasoning": "NMPA 正式发布中药注册管理专门规定并已生效，属部委级全行业规则，条文改动直接影响中药新药申报路径，值得逐条解读",
  "verify_todo": ["核对原文文号与生效日期", "确认是否另有配套申报资料要求文件"],
  "article_type": "policy_readout"
}]

`score` 不到 6 的条目也要返回（select 阶段会丢弃低分，但要让模型看到全量）。
"""


def _provider_eff(cfg: dict, provs: dict, name: str):
    """构造某个 provider 的 (client, eff)。取不到 key 直接抛。"""
    p = provs.get(name)
    if p:
        base_url = p.get("base_url")
        model = p.get("model")
        max_tokens = p.get("max_tokens", cfg.get("max_tokens", 8000))
        json_mode = p.get("json_mode", False)
        env_names = [p.get("env_key", ""), "LLM_API_KEY", "OPENAI_API_KEY"]
    elif name == cfg.get("provider"):
        # 没有 providers 表时的老路径（cfg 里直填 base_url/model）
        base_url = cfg.get("base_url")
        model = cfg.get("model")
        max_tokens = cfg.get("max_tokens", 8000)
        json_mode = cfg.get("json_mode", False)
        env_names = ["LLM_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY",
                     "OPENAI_API_KEY"]
    else:
        raise RuntimeError(f"未配置的 provider: {name}（providers 表里没有）")

    api_key = None
    for env in env_names:
        if env and os.getenv(env):
            api_key = os.getenv(env)
            break
    if not api_key:
        raise RuntimeError(f"缺少 API key（provider={name}）")

    eff = {"model": model, "max_tokens": max_tokens, "json_mode": json_mode,
           "temperature": cfg.get("temperature", 0.3),
           "base_url": base_url, "provider": name}
    return OpenAI(api_key=api_key, base_url=base_url,
                  timeout=180.0, max_retries=2), eff


def _client(cfg: dict, all_providers: dict) -> tuple[OpenAI, dict]:
    """
    按配置创建客户端，支持【provider 降级链】。

    cfg.provider 主用；cfg.fallback_providers 按顺序备用。

    ⚠️ 2026-11-01 加。当天 MiniMax 的 M Plan 额度耗尽（文本 429、图片 429），
    两个雷达 + 公众号 publisher 全断，只能人工改配置切 DeepSeek。
    现在的口径是：**MiniMax 主用、DeepSeek Flash 备用**，主用挂掉时自动切，
    不要再等人来改配置。
    """
    provs = all_providers or {}
    names = [cfg.get("provider", "deepseek")]
    for n in (cfg.get("fallback_providers") or []):
        if n and n not in names:
            names.append(n)

    chain, errs = [], []
    for name in names:
        try:
            c, e = _provider_eff(cfg, provs, name)
            chain.append((c, e))
        except Exception as ex:
            errs.append(f"{name}: {type(ex).__name__}: {str(ex)[:80]}")
            print(f"[llm] provider={name} 不可用（{type(ex).__name__}: {str(ex)[:60]}），跳过")
    if not chain:
        raise RuntimeError("所有 provider 都不可用：" + "；".join(errs))

    client, eff = chain[0]
    print(f"[llm] provider={eff['provider']} model={eff['model']}")
    print(f"     base_url={eff['base_url']} json_mode={eff['json_mode']}"
          + (f" | 备用: {[e['provider'] for _, e in chain[1:]]}" if len(chain) > 1 else ""))
    eff["_fallback_chain"] = chain[1:]
    return client, eff


def _strip_think(text: str) -> str:
    """
    MiniMax 等模型默认开启思考，内容里会混入 <think>...</think>。
    必须剥掉，否则 JSON 解析失败。
    """
    if not text:
        return ""
    # 情况1：完整的 <think>...</think> 前缀
    text = re.sub(r"^\s*<think>.*?</think>\s*", "", text, flags=re.S)
    # 情况2：未闭合的截断式思考（max_tokens 用尽时常见）
    if "</think>" not in text and "<think>" in text:
        text = text.split("<think>", 1)[1]
        if "</think>" in text:
            text = text.split("</think>", 1)[1]
    # 情况3：残留的空标签
    text = text.replace("<think>", "").replace("</think>", "")
    return text.strip()


def _build_items(items: list[RawItem]) -> list[dict[str, Any]]:
    out = []
    for it in items:
        out.append({
            "id": it.id,
            "title": it.title,
            "source": it.source,
            "lang": it.lang,
            "url": it.url,
            "published": it.published or "未知",
            "age_days": (round(it.age_hours / 24, 1)
                         if it.age_hours >= 0 else "未知"),
            "access": it.access,
            # 素材量：为 0 表示只有标题、没有正文，写作时极易脑补
            "material_chars": len(it.content or it.summary or ""),
            # 交易信号：仅作提示，最终由你判断
            "deal_signal": bool(DEAL_SIGNAL.search(it.title)),
            "summary": (it.summary or it.content)[:600],
            "multi_source": [t for t in it.topics if t.startswith("also:")],
        })
    return out


def _parse(text: str) -> list[dict]:
    """LLM 返回可能带 markdown 包裹或前后废话，做健壮解析"""
    if not text:
        return []
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # 优先整段 JSON 数组
    i, j = text.find("["), text.rfind("]")
    if i >= 0 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            pass

    # 退化：逐个捞对象（支持嵌套括号）
    objs, depth, start, in_str, esc = [], 0, -1, False, False
    for k, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = k
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    o = json.loads(text[start:k + 1])
                    if isinstance(o, dict) and "id" in o:
                        objs.append(o)
                except Exception:
                    pass
                start = -1
    return objs


def _amount_dim(upfront: float, total: float) -> float:
    """按 rubric 把首付映射成金额分；首付未披露但有总额时给 0.5。"""
    if upfront >= 500_000_000:
        return 2.5
    if upfront >= 100_000_000:
        return 2.0
    if upfront >= 10_000_000:
        return 1.2
    if upfront > 0:
        return 0.3
    if total > 0:
        return 0.5
    return 0.0


def _to_float(v: Any) -> float:
    if isinstance(v, str):
        v = re.sub(r"[^0-9.\-]", "", v)
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return 0.0


def _dim_value(v: Any) -> float:
    """维度分允许为负（例如 stale 条目的 impact 会被扣分）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _apply_deal_fields(s: dict) -> None:
    """
    用代码覆盖两个最不稳定的字段：
    - amount 维度：模型经常把 "up to $1.5B" 当成首付，同一篇稿子三天
      给出 3.0 / 5.5 / 7.2。金额分必须由 upfront_usd 决定。
    - score：始终等于五个维度之和，避免模型自报分数与维度不一致。
    """
    dims = s.get("dims") or {}
    upfront = _to_float(s.get("upfront_usd"))
    total = _to_float(s.get("total_usd"))

    if upfront > 0 or total > 0:
        dims["amount"] = _amount_dim(upfront, total)

    s["deal_key"] = normalize_deal_key(s.get("deal_key") or "")
    s["upfront_usd"] = int(upfront)
    s["total_usd"] = int(total)

    keys = ("amount", "stage", "first_mover", "tier", "impact")
    if all(k in dims for k in keys):
        s["dims"] = dims
        s["score"] = round(sum(_dim_value(dims[k]) for k in keys), 2)


def _prefilter(items: list[RawItem], cfg: dict) -> list[RawItem]:
    """规则层预筛：纯融资直接丢弃，过期的降权"""
    excl = cfg.get("exclude_keywords", {}) or {}
    bad = [w.lower() for w in excl.get("pure_financing", [])]
    out = []
    for it in items:
        t = it.title.lower()
        if any(w in t for w in bad):
            continue
        out.append(it)
    return out


_NO_RETRY_HINTS = ("用量上限", "token plan", "insufficient", "balance",
                   "429", "401", "invalid api key", "authentication")


def _no_retry(msg: str) -> bool:
    """额度用尽 / 鉴权失败：原地重试没有意义，立刻交给降级链。"""
    m = (msg or "").lower()
    return any(k in m for k in _NO_RETRY_HINTS)


def _one_provider_call(client: OpenAI, eff: dict, system: str, user: str,
                       retries: int = 2) -> str:
    """单个 provider 内的调用：json_mode 不支持时自动降级。"""
    kwargs = {
        "model": eff["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": eff["temperature"],
        "max_tokens": eff["max_tokens"],
    }
    if eff.get("json_mode"):
        kwargs["response_format"] = {"type": "json_object"}

    attempt = 0
    while True:
        try:
            r = client.chat.completions.create(**kwargs)
            content = r.choices[0].message.content or ""
            if not content.strip() and eff.get("json_mode"):
                kwargs.pop("response_format", None)
                eff["json_mode"] = False
                continue
            return _strip_think(content)
        except Exception as e:
            msg = str(e)
            if "response_format" in msg or "json_object" in msg.lower():
                kwargs.pop("response_format", None)
                eff["json_mode"] = False
                continue
            if _no_retry(msg) or attempt >= retries:
                raise
            attempt += 1
            time.sleep(2 ** (attempt - 1))


def _call_llm(client: OpenAI, eff: dict, system: str, user: str,
              retries: int = 2) -> str:
    """
    统一调用入口：provider 降级链 + json_mode 降级 + think 剥离。

    主 provider 失败（含额度用尽）→ 立刻切 fallback_providers 里的下一个；
    全部失败才抛错。
    """
    chain = [(client, eff)] + list(eff.get("_fallback_chain") or [])
    last = None
    for i, (c, e) in enumerate(chain):
        try:
            return _one_provider_call(c, e, system, user, retries=retries)
        except Exception as ex:
            last = ex
            if i + 1 < len(chain):
                nxt = chain[i + 1][1]
                print(f"[llm] {e['provider']} 调用失败（{type(ex).__name__}: "
                      f"{str(ex)[:90]}）→ 切备用 provider={nxt['provider']}")
    raise last


def score_items(items: list[RawItem], cfg: dict, client: OpenAI,
                eff: dict) -> list[dict]:
    # ⚠️ 2026-11-01 修：batch_size / max_retries 写在 scoring.yaml 的 llm 块里，
    #    而 run() 传进来的是整个 cfg —— 原来直接 cfg.get("batch_size", 30)
    #    永远拿默认值 30（政策评分输出更长，30 条有 JSON 截断风险）。
    _llm_cfg = cfg.get("llm") or cfg
    batch = _llm_cfg.get("batch_size", 30)
    retries = _llm_cfg.get("max_retries", 2)
    results: list[dict] = []

    pre = _prefilter(items, cfg)
    print(f"[score] 预筛后 {len(pre)} 条（原始 {len(items)}）")

    for i in range(0, len(pre), batch):
        chunk = pre[i:i + batch]
        user = f"请为以下 {len(chunk)} 条医药政策/监管条目打分。\n" \
               f"只输出 JSON 数组，不要任何其他文字。\n\n" + \
               json.dumps(_build_items(chunk), ensure_ascii=False, indent=1)
        try:
            txt = _call_llm(client, eff, SYSTEM_PROMPT, user, retries)
            parsed = _parse(txt)
            for s in parsed:
                if isinstance(s, dict) and "id" in s:
                    _apply_deal_fields(s)
            results.extend(parsed)
            print(f"  [batch {i//batch + 1}] 返回 {len(txt)} 字符，"
                  f"解析 {len(parsed)}/{len(chunk)}")
            time.sleep(1.0)
        except Exception as e:
            print(f"  [batch {i//batch + 1}] 失败: {type(e).__name__}: {e}")
    return results


def run(date: str | None = None) -> list[dict]:
    date = date or today_cst()
    cfg = load_yaml("scoring.yaml")

    path = RAW_DIR / f"{date}.deduped.json"
    raw_items = read_json(path, [])
    if not raw_items:
        raw_items = read_json(RAW_DIR / f"{date}.json", [])
    if not raw_items:
        print(f"[score] 没有待评分数据: {path}")
        return []

    items = [RawItem.from_dict(d) for d in raw_items]
    client, eff = _client(cfg["llm"], cfg.get("providers", {}))
    scored = score_items(items, cfg, client, eff)

    # 回填原始信息
    by_id = {i.id: i for i in items}
    for s in scored:
        src = by_id.get(s.get("id", ""))
        if src:
            s["title"] = src.title
            s["url"] = src.url
            s["sources"] = [src.source] + [
                t.split(":", 1)[1] for t in src.topics if t.startswith("also:")]
            s["content"] = src.content
            # ⚠️ summary / access 必须落盘（2026-10-21 修正）：
            #    之前只存 content，于是"有摘要、没正文"的条目
            #    （付费墙/只给 RSS 描述）在 scored.json 里看起来像"零素材"，
            #    事后复盘分不清它到底有没有料、素材太薄那道闸为什么放行——
            #    2026-10-21 那笔重复交易就是这样白烧了 4 次 LLM 调用。
            #    access 同样要落：pick._evidence() 用它做同分 tie-break
            #    （full 加 1 分），不落盘等于这个 tie-break 一直是失效的。
            s["summary"] = src.summary
            s["access"] = src.access
            s["entity_tags"] = list(src.entity_tags)
            s["lang"] = src.lang
            s["scored_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            # 只有标题/摘要极短的条目，模型仍可能给高置信度
            # （2026-10-20：默克/siRNA 正文 0 字，置信度 0.7，
            #   最终写出 29 条无依据）。置信度必须由素材量兜底。
            mat_chars = len((src.content or src.summary or "").strip())
            if mat_chars < 200:
                s["confidence"] = min(float(s.get("confidence") or 0), 0.4)
                flags = list(s.get("flags") or [])
                if "thin_material" not in flags:
                    flags.append("thin_material")
                s["flags"] = flags

    scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    write_json(SCORED_DIR / f"{date}.json", scored)

    print(f"\n[score] 完成 {len(scored)} 条 -> data/scored/{date}.json")
    if scored:
        thr = cfg["score"]["must_publish"]
        hot = [s for s in scored if s.get("score", 0) >= thr]
        print(f"  >= {thr} 分：{len(hot)} 条")
        for s in scored[:8]:
            print(f"    {s.get('score', 0):>4} | {s.get('title', '')[:62]}")
    return scored


if __name__ == "__main__":
    # Windows 控制台默认 GBK，输出特殊符号会崩
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    run(sys.argv[1] if len(sys.argv) > 1 else None)