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


SYSTEM_PROMPT = """你是医药 BD（商务拓展/授权交易）领域的资深分析师。
你的任务是给新闻条目打分，识别出真正值得深度报道的 BD 热点。

# 评分维度（满分 10 分）

## 1. 金额量级 (0-2.5)
- 首付 >5 亿美元 = 2.5
- 首付 >1 亿美元 = 2.0
- 首付 >1000 万美元 = 1.2
- 首付 <500 万美元 = 0.3
⚠️ **必须基于【首付】评分，不是潜在总额。** 185 亿美元的总额里首付往往只有 12 亿。
   里程碑和销售提成是附带的，不是买方的即时承诺。

## 2. 交易阶段 (0-2.0)
- III 期数据读出 / NDA 受理 = 2.0
- II 期阳性数据 = 1.5
- I 期 = 0.8
- 临床前 = 0.5

## 3. 首创性 (0-2.0)
- 全球首个靶点或机制 = 2.0
- 全球首个适应症 = 1.6
- 国产首个 = 1.2
- me-better / 跟随 = 0.4

## 4. 公司层级 (0-1.5)
- MNC 主导 = 1.5
- 大型药企对大型药企 = 1.2
- 二线 biotech = 0.7

## 5. 外溢影响 (0-2.0)
- 改变赛道竞争格局 = 2.0
- 单一公司重大进展 = 1.3
- 常规交易 = 0.6

# 必须执行的核查（历史教训，务必遵守）

**⓪ 并购与授权同等重要**
BD 交易包含两大类，**评分时同等对待，不要偏袒任何一类**：
- **并购/收购**（M&A）：买方收购标的企业股权或资产
- **授权/许可**（licensing）：一方授权另一方开发商业化权益

两者都是这个雷达的核心内容。不要因为"授权类更容易写"就给授权类
加分，也不要因为"并购类缺少每股价格等细节"就给并购类减分。
按同样的五维标准打分。

**① 关联交易检测**
若买卖双方存在股权关系（一方持有另一方股份，或有共同大股东），
必须在 flags 里标 "related_party"，并在 reasoning 里说明。
这类交易的溢价不代表市场共识，不能按普通交易打分。

**② 口径分层**
涉及金额时，reasoning 里必须写明是"首付""里程碑上限""总额"还是"传闻"。

**③ "未找到" ≠ "不存在"**
如果某项信息（如 NDA 状态、临床阶段）无法从素材确认，
放进 verify_todo，**不要下否定结论**。

**④ 信息增量**
若该交易中文媒体已充分报道而英文媒体未跟进，降低外溢影响分并说明。
反之亦然——英文媒体先报而中文未跟进的，适度加权。

**⑤ 排除纯融资**
IPO、定增、Series A/B 不是 BD，直接给 0 分。

**⑥ 识别综述型条目**
若一天内同靶点/同领域有多笔交易，标记 cluster_key 相同的条目，
score 略降但保留——这类应该合并成一篇 landscape_review 综述。

**⑦ 时效性（重要，容易踩坑）**
每条都带 `published` 和 `age_days` 字段：
- `age_days` 超过 7 天：这不是新闻，降外溢影响分，并在 reasoning 中说明"非近日"
- `age_days` 超过 14 天：直接降 2 分以上
- `age_days` 为「未知」：**不要假设它是新闻，也不要假设它旧**。
  按内容本身评分，但在 reasoning 中标注"发布日期未知"
- ⚠️ 媒体转载旧闻是常见现象。一篇讲「去年那笔交易」的文章，
  讲的是历史，不是今天发生的事。不要把旧交易当成新交易打分。

**⑧ 必须是"交易"才算 BD 新闻（最高优先级）**
这是 BD 雷达，不是临床进展雷达。判断条目是不是一笔交易：

- **是交易**：存在授权/收购/合作/选择权关系，有买方卖方（或授权方受让方），
  有 license / acquire / option / collaboration / 授权 / 引进 / 收购 / 合作 等关系词
- **不是交易**：单纯的临床数据读出、监管获批、学术发表、股价波动、
  人事变动、行业综述。这类**没有交易对手方**。

⚠️ 遇到"不是交易"的条目：
- `score` **必须 ≤ 3.0**，并在 flags 中加 `"not_bd"`
- 即使它有 III 期成功、全球首个、大公司背书——都不改变它不是交易这个事实
- reasoning 里写明"非交易条目"

反例（实测踩过的坑）：
  "全球首个双周给药 GLP-1R/GIPR 双受体激动剂临床三期成功"
  → 只有临床数据，没有任何交易双方，不是 BD 新闻。
  当时给了 6.5 分并成稿，结果文章的"交易要素"表全是"未找到公开数据"，
  副标题还编出了"凭什么让跨国药企出手"这种素材里不存在的情节。

**⑨ 素材量不足要标注**
`material_chars` 是可用正文字数：
- 为 0 或很小（只有标题）：说明没有正文素材。
  这类即使分数够，也要在 `verify_todo` 里写明"仅有标题，无正文素材，
  成稿前需补一手材料"，并把 `confidence` 压到 0.5 以下。

**⑩ reasoning 只写"为什么值得报道"，不写事实结论（重要）**
`reasoning` 会被原样喂给成稿阶段当"选题理由"。如果你在这里下了
一个事实判断（例如"自有 TYK2 平台授权"），成稿会把它当既成事实继续扩写，
下游所有环节都在错误前提上加法——**评分只是选材，不是定稿**。

- ❌ "中国 biotech 自有 TYK2 平台授权给 MNC 礼来"（把某靶点说成交易标的）
- ❌ "礼来首次布局 BTK"（素材没说的动机/历史）
- ✅ "诺诚健华与礼来达成多靶点研发授权，首付+近期里程碑最高 1 亿美元，
     潜在总额 33.5 亿美元，属中等量级平台型交易"
- 若你怀疑某细节（如具体靶点、对手方、临床阶段）但素材没写清，
  **放进 `verify_todo`，不要在 reasoning 里替它下结论**。

反例（实测事故 postmortem_b4985179）：
  公告只说"依托专有药物发现平台…至多5项创新靶点"，新闻站自动实体标签里
  带着 "TYK2"。评分 reasoning 写成"自有 TYK2 平台授权"，
  成稿据此把整篇写成"TYK2 平台"，还捏造了一个公告里没有的分子代号。

# 交易要素抽取（代码会用这些字段算金额分、做跨天去重）

除打分外，每条还要输出三个结构化字段：

- `deal_key`：这笔交易的稳定指纹，格式 `"<party_a>|<party_b>|<asset>"`。
  全小写；公司用英文常用名；asset 用靶点/分子/技术平台的英文短横线写法。
  **两个交易方按字母顺序排列**，避免同一笔交易因书写顺序不同而指纹不一致。
  不是交易（not_bd）的条目输出空字符串。
  例：AstraZeneca 授权 insitro 做 AI+MASH
      → "astrazeneca|insitro|ai-mash"
      Roche 引进 Rona Therapeutics 的 ALK7 RNAi
      → "roche|rona|alk7-rnai"

- `upfront_usd`：首付款，美元整数。**未披露填 0**。
  只填首付/预付款；"up to $1.5B"、"总额最高15亿美元" 是潜在总额，不是首付。

- `total_usd`：潜在总额（含里程碑），美元整数。未披露填 0。

金额只从素材里原样抄，不要估算，不要用未披露的数字凑。
如果素材只给人民币/欧元，按素材里的数字换算成美元并在 reasoning 里注明汇率来源。

# 输出格式（严格 JSON，不要任何解释文字）

⚠️ **只输出一个 JSON 数组**。不要输出思考过程、不要解释、不要 markdown 代码块包裹。
第一个字符必须是 `[`，最后一个必须是 `]`。

[{
  "id": "条目id",
  "score": 8.7,
  "dims": {"amount": 2.0, "stage": 2.0, "first_mover": 1.6, "tier": 1.5, "impact": 1.6},
  "flags": ["related_party"],
  "cluster_key": "b7h3-adc",
  "confidence": 0.85,
  "deal_key": "hengrui|novo|glp-1",
  "upfront_usd": 200000000,
  "total_usd": 3350000000,
  "reasoning": "首付2亿美元、全球首个口服GLP-1、诺和诺德亲自下场，60字以内",
  "article_type": "event_report",
  "verify_todo": ["需查港交所公告确认首付到账金额"]
}]

article_type 取值：
- event_report：单笔重大交易深挖
- landscape_review：同靶点/同领域多笔，适合做综述
- historical_compare：有明确历史参照系（如"继XX之后"）

score <7.5 的条目也要返回，但会被 select 阶段丢弃——
这样做是为了让 LLM 看到全量信息，避免它误判某条低分条目其实重要。
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
    batch = cfg.get("batch_size", 30)
    retries = cfg.get("max_retries", 2)
    results: list[dict] = []

    pre = _prefilter(items, cfg)
    print(f"[score] 预筛后 {len(pre)} 条（原始 {len(items)}）")

    for i in range(0, len(pre), batch):
        chunk = pre[i:i + batch]
        user = f"请为以下 {len(chunk)} 条医药 BD 新闻打分。\n" \
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