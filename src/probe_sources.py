"""
源健康探针。

为什么需要它：
调试中多次把"选择器写错"误判成"站点封爬"。PharmExec 是一例——
我把文章路径写成 /news/（实际是 /view/），local 有 5 条、CI 有 0 条，
当时结论成了"数据中心 IP 被拦"，实际是代码 bug。

⚠️ 设计要点：探针**直接调用真实的 fetch_html / fetch_rss**，
不重新实现抓取逻辑。
早期版本自己写了一遍过滤流程，结果因为去重与长度检查的顺序不同，
把能用的源误报成"关键词全过滤"——工具自身的 bug 比被检测的 bug 更危险。

用法：
    python probe_sources.py              # 全部源
    python probe_sources.py pharmexec    # 只测名字含该串的源
"""
from __future__ import annotations

import asyncio
import re
import sys
from collections import Counter

import httpx

try:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:  # pragma: no cover
    from selectolax.parser import HTMLParser

from common import load_yaml
from fetch import (HEADERS, LIST_PATTERNS, TIMEOUT, fetch_html, fetch_rss)
from noise import filter_items


def _link_diag(html: str, src: dict) -> dict:
    """选择器命中情况（仅在抓取结果为 0 时用于定位）"""
    try:
        tree = HTMLParser(html)
    except Exception:
        return {}
    cfg = LIST_PATTERNS.get(src["id"])
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
    }


async def probe_one(client: httpx.AsyncClient, src: dict) -> dict:
    out = {"id": src["id"], "status": None, "items": 0, "kept": 0,
           "stage": "", "note": "", "diag": {}, "samples": []}

    if src.get("access") in ("blocked", "disabled"):
        out["stage"] = src["access"]
        return out
    if src.get("mode") == "search" or not src.get("url"):
        out["stage"] = "search"
        return out

    # ---- 用真实抓取函数 ----
    try:
        if src.get("mode") == "rss":
            items = await fetch_rss(client, src)
        else:
            items = await fetch_html(client, src)
    except Exception as e:
        out["stage"] = f"异常 {type(e).__name__}"
        return out

    # 顺带记录 HTTP 状态（用于区分 403 和"结构变了"）
    try:
        r = await client.get(src["url"], headers=HEADERS,
                             follow_redirects=True)
        out["status"] = r.status_code
        html = r.text
    except Exception:
        html = ""

    out["items"] = len(items)
    if not items:
        if out["status"] and out["status"] != 200:
            out["stage"] = f"HTTP {out['status']}"
        elif html:
            d = _link_diag(html, src)
            out["diag"] = d
            if d.get("selected", 0) == 0:
                out["stage"] = "选择器未命中"
                out["note"] = (f"原始链接 {d['raw_links']} 个，"
                               f"选择器 `{d['selector']}` 命中 0")
            elif d.get("with_title", 0) == 0:
                out["stage"] = "命中但无标题"
                out["note"] = (f"命中 {d['selected']} 个，"
                               f"但都取不到标题文本")
            else:
                out["stage"] = "关键词过滤"
                out["note"] = (f"命中 {d['selected']} 个 / "
                               f"有标题 {d['with_title']} 个，"
                               f"均被 RELEVANT 或噪声过滤挡下")
        else:
            out["stage"] = "抓取为空"
        for s in out["diag"].get("samples", []):
            out["samples"].append(s)
        return out

    # 噪声过滤
    kept, dropped = filter_items([i.to_dict() for i in items])
    out["kept"] = len(kept)
    if kept:
        out["stage"] = "ok"
        out["samples"] = [i["title"][:66] for i in kept[:3]]
    else:
        out["stage"] = "噪声全过滤"
        out["note"] = f"{len(items)} 条全部被噪声过滤器挡下"
        out["samples"] = [i["title"][:60] for i in items[:3]]
    return out


async def run(pattern: str = "") -> list[dict]:
    cfg = load_yaml("sources.yaml")
    srcs = [s for s in cfg["sources"] if pattern.lower() in s["id"].lower()]
    print(f"探测 {len(srcs)} 个源\n")

    async with httpx.AsyncClient(timeout=TIMEOUT, http2=False) as c:
        results = await asyncio.gather(
            *[probe_one(c, s) for s in srcs], return_exceptions=True)

    ok, bad, skip = [], [], []
    for r in results:
        if not isinstance(r, dict):
            continue
        if r["stage"] in ("blocked", "disabled", "search"):
            skip.append(r)
            print(f"SKIP {r['id']:26} {r['stage']:12} (已在配置中停用/需外部检索)")
            continue
        good = r["stage"] == "ok"
        (ok if good else bad).append(r)
        print(f"{'OK  ' if good else 'FAIL'} {r['id']:26} "
              f"{str(r['stage']):12} HTTP={r['status']} "
              f"抓取={r['items']:>4} 保留={r['kept']:>4}")
        if r["note"]:
            print(f"       └ {r['note']}")
        for s in r["samples"]:
            print(f"       · {s}")

    active = len(ok) + len(bad)
    print(f"\n在用源可用率 {len(ok)}/{active}"
          f"（另有 {len(skip)} 个已停用/需外部检索）")
    if bad:
        print("\n仍在失败的源:")
        for r in bad:
            print(f"  {r['id']:26} {r['stage']:12} {r['note'][:70]}")
    return results


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else ""))