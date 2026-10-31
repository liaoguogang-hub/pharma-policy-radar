"""
当天推文的可信度核查报告（并推送飞书）。

# 为什么要有这个（2026-10-17 retlirafusp 事故的延伸）

成稿阶段的断言核验（`compose.check_facts` + `claimcheck`）只拦
**生成时**跑的稿子。两类情况漏在外面：
  · 早期生成的旧稿（核验上线前发的，比如 retlirafusp 那篇）
  · 人写成稿 / 手工重写进去的稿

所以再提供一个**事后独立审计**：对某天已落地的每一篇，
重建它的素材，重新拆断言、重新核验，出一份可读报告，并推到飞书。

⚠️ 这是**审计**不是**发布闸门**——它只对**已经入库的稿件**核验，
不阻拦当天的发布。要拦在发布前，看 `compose.py` 的接入。

用法：
    python audit.py                        # 今天
    python audit.py 2026-10-17             # 指定日期
    python audit.py 2026-10-17 --no-notify
    python audit.py 2026-10-17 --report-only    # 只出表，不调 LLM 核验

⚠️ 与临床雷达 `clinical-data-radar/src/audit.py` 是一对镜像：
机制完全相同（确定性补检 + 核验不充分兜底 + 飞书卡片），只是核验
目标换成了 BD 文章（`pharma-bd-radar/src/claimcheck.py`）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import claimcheck  # noqa: E402
import compose  # noqa: E402
import score  # noqa: E402
from common import SCORED_DIR, load_yaml, today_cst  # noqa: E402
from compose import POSTS_DIR  # noqa: E402
from render import parse_content, content_text  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "data" / "audit"


def _plan_items(date: str) -> dict:
    """lead_id(前 8) -> 选题条目"""
    plan_file = SCORED_DIR / f"{date}.plan.json"
    if not plan_file.exists():
        return {}
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    out = {}
    for it in (plan.get("plan") or plan.get("items") or []):
        out[(it.get("lead_id") or "")[:8]] = it
    return out


def _day_articles(date: str) -> list[Path]:
    d = POSTS_DIR / date[:4] / date[5:7] / date[8:10]
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.txt") if not p.name.startswith("_"))


def audit(date: str, do_notify: bool = True,
          report_only: bool = False) -> dict:
    items = _day_articles(date)
    plans = _plan_items(date)
    cfg = load_yaml("scoring.yaml")

    client = eff = None
    if not report_only:
        gcfg = cfg.get("gen_llm") or cfg["llm"]
        client, eff = score._client(gcfg, cfg.get("providers", {}))

    print(f"\n=== BD 雷达可信度核查 {date}（{len(items)} 篇）===")
    rows = []
    for p in items:
        lead_m = re.search(r"-([0-9a-f]{8})\.txt$", p.name)
        lead = lead_m.group(1) if lead_m else p.stem
        txt = p.read_text(encoding="utf-8")
        title = ""
        m = re.search(r"^TITLE:\s*(.+)$", txt, re.M)
        if m:
            title = m.group(1).strip()

        plan = plans.get(lead)
        # ⚠️ 必须用【async 版】重建素材（2026-10-17 实测踩到）：
        #    compose.run() 写稿时用的是 build_material_async
        #    （= build_material + CT.gov 注册库补查）。
        #    审计若用同步版 build_material，重建出来的素材**比作者手上的少一截**，
        #    于是大量真实存在的断言被判"素材里找不到"——误报把报告淹掉。
        #    审计要和作者看到**同一份素材**，否则核验没有意义。
        material = ""
        if plan:
            try:
                material = asyncio.run(
                    compose.build_material_async(date, plan))
            except Exception as e:
                print(f"  [{lead}] 素材重建异常 {type(e).__name__}: {e}")
                material = compose.build_material(date, plan)
        if not material:
            rows.append({"lead": lead, "title": title,
                         "verdict": "无素材可核",
                         "detail": "选题计划里找不到该条，无法重建素材",
                         "stat": {}, "bad": []})
            print(f"  [{lead}] ⚠️  无素材可核")
            continue

        if report_only:
            rows.append({"lead": lead, "title": title,
                         "verdict": "未核验", "detail": "report-only 模式",
                         "stat": {}, "bad": []})
            continue

        doc = parse_content(txt)
        body = content_text(doc) + " " + (doc.get("title") or "")
        try:
            claims = claimcheck.extract_claims(client, eff, body, material)
            errs, stat = claimcheck.verify_claims(claims, material)
        except Exception as e:
            claims, errs, stat = [], [f"核验异常 {type(e).__name__}: {e}"], {}

        # ── 补两道确定性/充分性防线（与临床雷达同形，详见 clinical audit.py）──
        #   ① 叠加 BD 事实闸门（正则抓分子代号/靶点缩写）——不依赖 LLM
        #   ② 断言太少判"核验不充分"，**不给"可信"结论**
        from compose import check_facts as _check_facts
        det = _check_facts(doc, body, material)
        if det:
            errs = list(errs) + [f"[确定性检查] {e}" for e in det]

        n_claims = len(claims)
        min_claims = max(5, len(body) // 300)
        insufficient = n_claims < min_claims

        if not claims:
            errs = list(errs) + ["未能拆出任何断言（核查模型返回为空）——"
                                 "核验未生效，不能据此判为可信"]
        elif insufficient:
            errs = list(errs) + [
                f"核验不充分：全文 {len(body)} 字只拆出 {n_claims} 条断言"
                f"（至少应有 {min_claims} 条）。核查未真正展开，"
                f"**不能判为可信**，需人工复核"]

        if not errs:
            verdict = "✅ 可信"
        elif insufficient or not claims:
            verdict = f"⚠️ 核验不充分（{n_claims} 条断言）"
        else:
            verdict = f"⚠️ {len(errs)} 条无依据"
        rows.append({"lead": lead, "title": title, "verdict": verdict,
                     "detail": "", "stat": stat, "bad": errs[:8]})
        print(f"  [{lead}] {verdict}  "
              f"断言 {stat.get('ok',0)}/{stat.get('total',0)} 有出处  {title[:34]}")
        for e in errs[:3]:
            print(f"        · {e[:100]}")

    result = {"date": date, "rows": rows}
    _write_report(result)
    if do_notify:
        _notify(result)
    return result


def _write_report(res: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date = res["date"]
    lines = [f"# 可信度核查报告 · {date}（BD 雷达）", "",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}",
             "",
             "对当天每篇成稿重建素材，把文章拆成原子断言，逐条要求"
             "**素材逐字出处**；引文经程序比对，不在素材里的判为编造出处。",
             "",
             "| 稿件 | 结论 | 有出处/总断言 |", "|---|---|---|"]
    for r in res["rows"]:
        st = r.get("stat") or {}
        ratio = (f"{st.get('ok',0)}/{st.get('total',0)}" if st else "—")
        lines.append(f"| {r['title'][:42]} | {r['verdict']} | {ratio} |")
    lines.append("")
    for r in res["rows"]:
        if not r.get("bad"):
            continue
        lines += [f"## {r['title'][:60]}", f"`{r['lead']}`", ""]
        for e in r["bad"]:
            lines.append(f"- {e}")
        lines.append("")
    p = REPORT_DIR / f"credibility_{date}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[audit] 报告已写 -> {p}")
    return p


def _notify(res: dict) -> bool:
    """推飞书卡片。复用 publish 的签名实现。

    ⚠️ 2026-10-25 用户澄清：云端雷达要发飞书（"做的稿件要发飞书"），
    核查报告卡属于"做的稿件"的配套信息，一并发出。
    临时关掉：RADAR_FEISHU_NOTIFY=0。
    """
    if os.getenv("RADAR_FEISHU_NOTIFY", "1").lower() in ("0", "false", "no"):
        print("[audit] 飞书推送已跳过（RADAR_FEISHU_NOTIFY=0）")
        return False
    url = os.getenv("FEISHU_WEBHOOK")
    if not url:
        print("[audit] 未配置 FEISHU_WEBHOOK，跳过推送")
        return False
    from publish import _feishu_sign  # 复用已验证的签名实现

    bad = [r for r in res["rows"] if r.get("bad")]
    n = len(res["rows"])
    body = [f"**{res['date']} BD 雷达**　共 {n} 篇，"
            f"{'全部通过 ✅' if not bad else f'{len(bad)} 篇有问题 ⚠️'}", ""]
    for r in res["rows"]:
        mark = "⚠️" if r.get("bad") else "✅"
        st = r.get("stat") or {}
        extra = (f"（{st.get('ok',0)}/{st.get('total',0)} 有出处）"
                 if st else "")
        body.append(f"{mark} {r['title'][:42]}　{extra}")
    if bad:
        body += ["", "**有问题的稿件（最多 3 条）**"]
        for r in bad[:3]:
            for e in (r.get("bad") or [])[:2]:
                body.append(f"- {e[:100]}")
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain", "content":
                          f"BD 雷达·可信度核查 {res['date']}"},
                "template": ("red" if bad else "green"),
            },
            "elements": [{
                "tag": "markdown",
                "content": "\n".join(body),
            }],
        },
    }
    # 飞书签名模式：_feishu_sign(timestamp, secret)，参数名别改
    secret = os.getenv("FEISHU_SECRET")
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _feishu_sign(ts, secret)
    try:
        import httpx
        r = httpx.post(url, json=payload, timeout=15)
        try:
            d = r.json() if r.text else {}
        except Exception:
            d = {}
        ok = r.status_code == 200 and d.get("code") == 0
        code = d.get("code")
        msg = d.get("msg", "")
        suffix = f" code={code} {msg}" if msg else ""
        print(f"[audit] 飞书推送{'成功' if ok else '失败'}"
              f"（HTTP {r.status_code}{suffix}）")
        return ok
    except Exception as e:
        print(f"[audit] 飞书推送异常 {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    # ⚠️ 必须用北京日期。GitHub runner 是 UTC，06:50 北京 = 前一天 22:50 UTC，
    # 用 time.strftime 会去核查"昨天"的稿件，而当天稿件一条都没审到。
    date = sys.argv[1] if len(sys.argv) > 1 else today_cst()
    no_notify = "--no-notify" in sys.argv
    report_only = "--report-only" in sys.argv
    audit(date, do_notify=not no_notify, report_only=report_only)
