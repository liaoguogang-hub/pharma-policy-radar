"""
质量闸门。

为什么必须做在生成侧：
实战中栽过两次 —
1. <p> 写了 29 个只闭合 4 个，无效 HTML，浏览器靠推断补，排版在不同环境下崩
2. 修复标签时把 <style> 整块替换成空格，CSS 全丢，文章变纯文本

所以每次生成后立即校验，不合格就带错误重写。
"""
from __future__ import annotations

import re
import sys

# 允许的标签（用于正文文本提取）
_TEXT_TAGS = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)

# 必须配平的块级标签
BALANCE_TAGS = ["p", "div", "td", "tr", "th", "li", "h1", "h2", "h3",
                "table", "thead", "tbody", "section", "span", "strong", "em"]

# CSS 必须包含的类
REQUIRED_CSS = [".hook", ".box", "table", "th", "td", ".m", ".hl",
                 ".foot", ".fn", ".end", "h1", "h2", "body", ".wrap"]


def _cn_count(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", s))


# AI 味词的等价替换（保守：只换，不改语义）
AI_REPLACEMENTS = {
    "值得注意的是": "",
    "需要注意的是": "",
    "综上所述": "",
    "总而言之": "",
    "不言而喻": "",
    "至关重要": "",
    "具有里程碑意义": "是一个重要节点",
    "本文将": "",
    "本系列将": "",
    "让我们": "",
    "赋能": "支撑",
    "底层逻辑": "基本逻辑",
    "抓手": "着力点",
}


def _deai(body: str, banned: list[str]) -> dict:
    """找出需要替换的 AI 味词，返回 {旧: 新}"""
    out = {}
    for w in banned:
        if w in body:
            out[w] = AI_REPLACEMENTS.get(w, "")
    return out


def _apply_replacements(html: str, repl: dict[str, str]) -> str:
    """
    把 AI 味词替换掉。

    ⚠️ 这里刻意做得很保守：只在标签外的纯文本节点上替换，
    不碰任何 HTML 结构。早先版本用正则匹配元素块，
    会把 </html>、跨标签的 <td> 一并吞掉，产出垃圾嵌套。
    宁可漏替换，也不能破坏结构。
    """
    if not repl:
        return html

    def _clean(txt: str) -> str:
        for a, b in repl.items():
            txt = txt.replace(a, b) if b else txt.replace(a, "")
        txt = re.sub(r"^[，、。；：\s]+", "", txt)
        txt = re.sub(r"[，、]{2,}", "，", txt)
        return txt

    # 拆分：标签片段 vs 文本片段
    parts = re.split(r"(<[^>]+>)", html)
    out = []
    for p in parts:
        if p.startswith("<"):
            out.append(p)          # 标签原样保留
        elif p.strip():
            out.append(_clean(p))  # 文本才替换
        else:
            out.append(p)
    return "".join(out)


def extract_css(html: str) -> str:
    m = re.search(r"(?is)<style[^>]*>(.*?)</style>", html)
    return m.group(1) if m else ""


def extract_body_text(html: str, strip: bool = True) -> str:
    """提取正文纯文本"""
    h = html
    if strip:
        h = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", h)
        h = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", h)
        h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = (h.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&quot;", '"').replace("&#39;", "'")
          .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", h).strip()


def body_chars(html: str) -> int:
    """正文字数：剥离 style / 脚注 / 表格"""
    h = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html)
    h = re.sub(r'(?is)<p class="fn">.*?</p>', " ", h, flags=re.S)
    h = re.sub(r"(?is)<table.*?</table>", " ", h, flags=re.S)
    h = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return _cn_count(h)


def validate(html: str, cfg: dict | None = None) -> list[str]:
    """
    返回错误列表，空列表 = 通过。
    """
    errors: list[str] = []

    if not html or len(html) < 500:
        return ["输出为空或过短"]

    # 0. 基础结构
    if not html.lstrip().lower().startswith("<!doctype html"):
        errors.append("缺少 <!DOCTYPE html> 开头")
    if "</html>" not in html.lower():
        errors.append("缺少 </html> 收尾")

    # 1. 标签配平（最关键）
    unbalanced = []
    for tag in BALANCE_TAGS:
        o = len(re.findall(rf"<{tag}(?=[ >/])", html))
        c = len(re.findall(rf"</{tag}>", html))
        if o != c:
            unbalanced.append(f"{tag}({o}开/{c}闭)")
    if unbalanced:
        errors.append("标签未配平: " + ", ".join(unbalanced))

    # 2. CSS 完整
    css = extract_css(html)
    if len(css) < 1500:
        errors.append(f"CSS 缺失或过短（{len(css)} 字符）")
    else:
        missing = [c for c in REQUIRED_CSS if c not in css]
        if missing:
            errors.append("CSS 缺少类: " + ", ".join(missing))

    # 3. 字数
    cfg = cfg or {}
    target = cfg.get("target_words", 3000)
    tol = cfg.get("word_tolerance", 0.75)
    wmax = cfg.get("max_words", int(target * 1.3))
    n = body_chars(html)
    lo, hi = int(target * tol), int(wmax)
    if not (lo <= n <= hi):
        errors.append(f"正文字数 {n} 超出范围（目标 {lo}-{hi}）")

    # 4. 脚注
    if 'class="fn"' not in html:
        errors.append("缺少脚注 <p class=\"fn\">")

    # 5. 钩子
    if 'class="hook"' not in html:
        errors.append("缺少钩子块 <div class=\"hook\">")

    # 6. AI 味检查（分档：少量出现放过，成堆出现才报错）
    #    实测：单次"值得注意的是"就重写整篇，代价远大于收益，
    #    且重写后常引入新的字数/标签问题。
    body = extract_body_text(html)
    gen = cfg.get("generation", {})
    tolerate = gen.get("ai_word_tolerate", 0)
    hard = gen.get("ai_word_hard_limit", 5)
    counts = {w: body.count(w) for w in cfg.get("banned_words", [])}
    total = sum(counts.values())
    if total > hard:
        top = sorted(counts.items(), key=lambda x: -x[1])[:5]
        errors.append(f"AI味词过多({total}处): "
                      + ", ".join(f"{w}x{c}" for w, c in top))
    elif total > tolerate:
        top = sorted(counts.items(), key=lambda x: -x[1])[:3]
        errors.append("AI味词: " + ", ".join(f"{w}x{c}" for w, c in top))

    pats = cfg.get("banned_patterns", [])
    hit_pats = []
    for p in pats:
        if re.search(p, body):
            hit_pats.append(p)
    if hit_pats:
        errors.append("AI味句式: " + ", ".join(hit_pats[:3]))

    # 7. HTML 转义检查（中文正文里不该有裸 < >）
    if re.search(r"[\u4e00-\u9fff]\s*<\s*[\u4e00-\u9fff]", body):
        errors.append("正文含未转义的尖括号")

    return errors


def auto_fix(html: str) -> str:
    """
    只做"零风险"修补：剥 markdown 包裹、补 DOCTYPE、补 </html>。

    ⚠️ 刻意不做标签自动配平。
    实测：尝试用正则补 </p></td></li> 时，反复产生
    「冗余闭合」和「跨标签垃圾嵌套」——
    修复引入的错比原错更难发现。
    标签不配平交给 LLM 重写更可靠（它能看到上下文）。
    """
    h = html.strip()
    h = re.sub(r"^```(?:html)?\s*", "", h)
    h = re.sub(r"\s*```\s*$", "", h)
    h = re.sub(r"^\s*<html[^>]*>\s*", "", h, flags=re.I)
    if not h.lstrip().lower().startswith("<!doctype"):
        h = "<!DOCTYPE html>\n" + h
    if "</html>" not in h.lower():
        h = h.rstrip() + "\n</body>\n</html>"
    return h


def auto_fix_deep(html: str) -> str:
    """
    深度修复版：额外做标签配平和 AI 味替换。

    ⚠️ 当前默认关闭，因为实测会引入新错误。
    保留代码供将来 debug 对比用。启用方式：
        html = auto_fix_deep(html)   # 替换 compose.py 里的 auto_fix
    """
    h = auto_fix(html)
    for tag in ("strong", "em", "span"):
        o = len(re.findall(rf"<{tag}(?=[ >])", h))
        c = len(re.findall(rf"</{tag}>", h))
        if c > o:                      # 多余闭合：直接删掉全部
            for _ in range(c - o):
                m = list(re.finditer(rf"</{tag}>", h))
                if m:
                    h = h[:m[-1].start()] + h[m[-1].end():]
    return _apply_replacements(h, AI_REPLACEMENTS)


def summarize(html: str) -> dict:
    return {
        "body_chars": body_chars(html),
        "css_len": len(extract_css(html)),
        "has_footnote": 'class="fn"' in html,
        "has_hook": 'class="hook"' in html,
        "tables": len(re.findall(r"<table", html)),
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        h = f.read()
    import json
    errs = validate(h)
    print(json.dumps(summarize(h), ensure_ascii=False, indent=1))
    print("\n错误:" if errs else "\n✅ 全部通过")
    for e in errs:
        print("  -", e)
