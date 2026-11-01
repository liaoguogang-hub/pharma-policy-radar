"""
源健康探针（医药政策雷达版）。

为什么需要它：
调试中多次把"选择器写错"误判成"站点封爬"。典型三例——
  · PharmExec 文章路径写成 /news/（实际 /view/），local 5 条、CI 0 条；
  · fda.gov 用 Chrome UA 返回 404（反爬页）、换阅读器 UA 就 200；
  · 国家医保局政策法规页的列表根本不在 DOM 里（Hanweb jpage 的内嵌 XML）。
判据永远要落在"真实响应长什么样"，而不是"站点封了我"。

⚠️ 设计要点：探针**直接调用生产用的 fetch_html / fetch_rss / fetch_hanweb /
fetch_article**，不重新实现过滤与正文提取。
早期版本自己写了一遍过滤流程，结果因为去重与长度检查的顺序不同，
把能用的源误报成"关键词全过滤"——工具自身的 bug 比被检测的 bug 更危险。

本版在原有"列表是否可解析"之上，加了**正文与日期**两项检查：
政策源最容易出的问题是"列表有 20 条、正文 0 条"（正文在附件/由 JS 渲染），
只看列表条数会漏掉。

用法：
    python probe_sources.py              # 全部源
    python probe_sources.py nmpa         # 只测 id 含该串的源
    python probe_sources.py --body 5     # 每个源多抓几篇正文（默认 3）
"""
from __future__ import annotations

import asyncio
import re
import sys

import httpx

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:  # pragma: no cover
    from selectolax.parser import HTMLParser

from common import load_yaml
from fetch import (LIST_PATTERNS, TIMEOUT, _enrich_priority, _run_one,
                   _src_headers, fetch_article, noise_filter)


def _link_diag(html: str, src: dict) -> dict:
    """选择器命中情况（仅在抓取结果为 0 时用于定位）"""
    try:
        tree = HTMLParser(html)
    except Exception:
        return {}
    cfg = LIST_PATTERNS.get(src["id"])
    # mode: prov 的源不写死选择器，这里报告通用通道的判据命中情况：
    # "带日期的条目容器"有多少、文章型链接有多少（见 fetch.fetch_prov）
    if src.get("mode") == "prov" and not cfg:
        sel = src.get("item") or "a[href]"
        nodes = tree.css(sel)
        date_li = 0
        for li in tree.css("li,tr,dd,p"):
            if li.css_first("a[href]") and re.search(
                    r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}", li.text() or ""):
                date_li += 1
        texts = [re.sub(r"\s+", " ", n.text()).strip() for n in nodes]
        return {
            "selector": sel,
            "raw_links": len(re.findall(r'href="([^"]+)"', html)),
            "selected": len(nodes),
            "with_title": len([t for t in texts if len(t) >= 12]),
            "date_items": date_li,
            "samples": [t for t in texts if len(t) >= 12][:3],
            "records": len(re.findall(r"<record>", html)),
        }
    sel = cfg["item"] if cfg else "h2 a, h3 a"
    nodes = tree.css(sel)
    texts = [re.sub(r"\s+", " ", n.text()).strip() for n in nodes]
    nonempty = [t for t in texts if len(t) >= 12]
    return {
        "selector": sel,
        "raw_links": len(re.findall(r'href="([^"]+)"', html)),
        "selected": len(nodes),
        "with_title": len(nonempty),
        "samples": nonempty[:3],
        "records": len(re.findall(r"<record>", html)),   # hanweb 内嵌记录数
    }


async def probe_one(client: httpx.AsyncClient, src: dict, body_n: int = 3) -> dict:
    out = {"id": src["id"], "stage": "", "items": 0, "kept": 0,
           "dated": 0, "body_ok": 0, "body_n": 0, "avg_len": 0,
           "samples": [], "note": "", "diag": {}, "status": None}

    if src.get("access") in ("blocked", "disabled"):
        out["stage"] = src["access"]
        return out
    if src.get("mode") == "search" or not src.get("url"):
        out["stage"] = "search"
        return out

    # ---- 用真实抓取函数（含 hanweb 分派、UA 覆盖、202 重试）----
    try:
        items = await _run_one(client, src)
    except Exception as e:
        out["stage"] = f"异常 {type(e).__name__}"
        return out

    out["items"] = len(items)
    out["dated"] = sum(1 for i in items if i.published)
    if items:
        # 与生产一致：省级源只过噪声黑名单（noise_filter 的分流口径）
        prov = ("province" in (src.get("topics") or []))
        kept, _ = noise_filter(items, {src["id"]} if prov else set())
        out["kept"] = len(kept)
        if kept:
            out["stage"] = "ok"
        else:
            out["stage"] = "噪声全过滤"
            out["note"] = f"{len(items)} 条全部被噪声过滤器挡下"

        # ---- 正文检查：按生产优先级取前 N 条，走真实的 fetch_article ----
        hdr = _src_headers(src)
        pick = sorted(items, key=_enrich_priority)[:body_n]
        out["body_n"] = len(pick)
        lens = []
        for it in pick:
            got = await fetch_article(client, it, hdr)
            n = len(got.content or "")
            lens.append(n)
            if n > 200:
                out["body_ok"] += 1
        out["avg_len"] = int(sum(lens) / len(lens)) if lens else 0
        out["samples"] = [f"[{(i.published or '日期未知')[:10]}] {i.title[:52]}"
                          for i in (kept or items)[:2]]
        return out

    # ---- 0 条：分辨 HTTP 拒绝 / 选择器未命中 / 关键词过滤 ----
    try:
        r = await client.get(src["url"], headers=_src_headers(src),
                             follow_redirects=True)
        out["status"] = r.status_code
        html = r.text
    except Exception:
        html = ""
    if out["status"] and out["status"] != 200:
        out["stage"] = f"HTTP {out['status']}"
    elif html:
        d = _link_diag(html, src)
        out["diag"] = d
        if d.get("records"):
            out["stage"] = "内嵌记录但未解析"
            out["note"] = f"页面有 {d['records']} 条 <record>，检查 hanweb 解析"
        elif d.get("selected", 0) == 0:
            out["stage"] = "选择器未命中"
            out["note"] = (f"原始链接 {d['raw_links']} 个，"
                           f"选择器 `{d['selector']}` 命中 0")
        elif d.get("with_title", 0) == 0:
            out["stage"] = "命中但无标题"
            out["note"] = f"命中 {d['selected']} 个，但都取不到标题文本"
        elif src.get("mode") == "prov":
            out["stage"] = "通用提取未命中"
            out["note"] = (f"链接 {d.get('raw_links')} / 选中 {d.get('selected')} / "
                           f"带日期条目 {d.get('date_items')}；"
                           f"检查 RELEVANT、href 文章特征，或改用 item: 指定列表容器")
        else:
            out["stage"] = "关键词过滤"
            out["note"] = (f"命中 {d['selected']} 个 / 有标题 {d['with_title']} 个，"
                           f"均被 RELEVANT 或噪声过滤挡下")
    else:
        out["stage"] = "抓取为空"
    out["samples"] = out["diag"].get("samples", [])[:3]
    return out


async def run(pattern: str = "", body_n: int = 3) -> list[dict]:
    cfg = load_yaml("sources.yaml")
    srcs = [s for s in cfg["sources"] if pattern.lower() in s["id"].lower()]
    print(f"探测 {len(srcs)} 个源（每个源抓 {body_n} 篇正文）\n")

    async with httpx.AsyncClient(timeout=TIMEOUT, http2=False) as c:
        results = await asyncio.gather(
            *[probe_one(c, s, body_n) for s in srcs], return_exceptions=True)

    ok, bad, skip = [], [], []
    for r in results:
        if not isinstance(r, dict):
            continue
        if r["stage"] in ("blocked", "disabled", "search"):
            skip.append(r)
            print(f"SKIP {r['id']:16} {r['stage']}")
            continue
        good = r["stage"] == "ok" and r["body_ok"] > 0
        (ok if good else bad).append(r)
        line = (f"{'OK  ' if good else 'FAIL'} {r['id']:16} "
                f"条={r['items']:>3} 有日期={r['dated']:>3} "
                f"正文={r['body_ok']}/{r['body_n']} 均长={r['avg_len']:>5} "
                f"[{r['stage']}]")
        print(line)
        if r["note"]:
            print(f"       └ {r['note']}")
        for s in r["samples"]:
            print(f"       · {s}")

    active = len(ok) + len(bad)
    print(f"\n在用源可用率 {len(ok)}/{active}"
          f"（另有 {len(skip)} 个已停用/需外部检索）")
    if bad:
        print("\n仍需处理:")
        for r in bad:
            print(f"  {r['id']:16} {r['stage']:14} {r['note'][:70]}")

    # 汇总：正文率低的源是"列表能抓、素材抓不到"的信号
    low = [r for r in ok + bad if r["body_n"] and r["body_ok"] / r["body_n"] < 0.6]
    if low:
        print("\n⚠️ 正文成功率 < 60% 的源（源站改版或正文在附件/JS 里）:")
        for r in low:
            print(f"  {r['id']:16} {r['body_ok']}/{r['body_n']}")
    return results


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n = 3
    for a in sys.argv[1:]:
        if a.startswith("--body"):
            try:
                n = int(a.split("=", 1)[1])
            except Exception:
                pass
    asyncio.run(run(args[0] if args else "", n))
