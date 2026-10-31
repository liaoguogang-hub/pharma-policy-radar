"""
发布层：生成站点首页 + 飞书通知。

产出：
  site/            静态站点（GitHub Pages 直接发布）
  site/index.html  当日简报汇总页
  site/archive.html 历史归档
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html as _html
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

from common import (ROOT, DATA_DIR, RAW_DIR, SCORED_DIR, load_yaml, read_json,
                    write_json, today_cst)
from render import CSS, body_chars

POSTS_DIR = ROOT / "posts"
SITE_DIR = ROOT / "site"


def _iter_posts() -> list[dict]:
    """扫描 posts/ 目录，返回按日期倒序的文章列表"""
    out: list[dict] = []
    for p in sorted(POSTS_DIR.rglob("*.html"), reverse=True):
        # posts/YYYY/MM/DD/NN-type-id.html
        parts = p.relative_to(POSTS_DIR).parts
        if len(parts) < 4:
            continue
        date = f"{parts[0]}-{parts[1]}-{parts[2]}"
        body = p.read_text(encoding="utf-8")
        m = re.search(r"(?is)<title>(.*?)</title>", body)
        h1 = re.search(r"(?is)<h1>(.*?)</h1>", body)
        kicker = re.search(r'(?is)<div class="kicker">(.*?)</div>', body)
        sub = re.search(r'(?is)<p class="sub">(.*?)</p>', body)
        hook = re.search(r'(?is)<div class="hook">(.*?)</div>', body)
        cn = len(re.findall(r"[\u4e00-\u9fff]",
                            re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", body)))
        # 「先产出、后核查」：compose 未通过全部质检但已落盘的稿子，
        # 会在旁边留一个 .review.txt。飞书必须显式标出来，否则
        # 这种稿子和干净稿长得一模一样，人工会误当合格稿处理。
        rv = p.with_suffix(".review.txt")
        problems = []
        if rv.exists():
            try:
                for ln in rv.read_text(encoding="utf-8").splitlines():
                    ln = ln.strip()
                    if ln.startswith("- "):
                        problems.append(ln[2:])
            except Exception:
                problems = ["（review 文件读取失败）"]
        out.append({
            "path": p,
            "rel": str(p.relative_to(POSTS_DIR)).replace("\\", "/"),
            "date": date,
            "title": (h1.group(1) if h1 else (m.group(1) if m else p.stem)),
            "kicker": kicker.group(1) if kicker else "",
            "sub": _strip_tags(sub.group(1)) if sub else "",
            "hook": _strip_tags(hook.group(1)) if hook else "",
            "chars": cn,
            "site_rel": str(p.relative_to(POSTS_DIR.parent)).replace("\\", "/"),
            "needs_review": rv.exists(),
            "problems": problems,
            # ⚠️ 评分不达标（2026-10-25 用户要求）：保底稿的 kicker 带
            #    「· 评分不达标」标记，云端产出卡片要显式标出来。
            "below_threshold": "评分不达标" in (kicker.group(1) if kicker else ""),
        })
    return out


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return _html.unescape(s).strip()


def build_index(posts: list[dict], date: str) -> str:
    today = [p for p in posts if p["date"] == date]
    rows = []
    for p in today:
        rows.append(f"""
    <article class="card">
      <div class="meta">{_html.escape(p['kicker'] or 'Analysis')} · {p['chars']} 字</div>
      <h2><a href="{_html.escape(p['rel'])}">{_html.escape(p['title'])}</a></h2>
      <p class="sub">{_html.escape(p['sub'][:160])}</p>
      <div class="hook">{_html.escape(p['hook'][:220])}</div>
      <a class="more" href="{_html.escape(p['rel'])}">阅读全文 →</a>
    </article>""")

    by_date: dict[str, list] = {}
    for p in posts:
        by_date.setdefault(p["date"], []).append(p)
    archive = "\n".join(
        f'<li><span class="d">{d}</span>'
        f'<a href="{_html.escape(v[0]["rel"])}">{_html.escape(v[0]["title"][:70])}</a>'
        f'<span class="c">{len(v)} 篇</span></li>'
        for d, v in sorted(by_date.items(), reverse=True)[:60]
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>医药 BD 雷达 · {date}</title>
<style>
{CSS}
.index{{max-width:900px;margin:0 auto;padding:56px 24px 90px}}
.index h1{{font-size:clamp(26px,4vw,36px);color:var(--red);margin-bottom:8px}}
.tagline{{color:var(--ink2);font-size:15px;margin-bottom:40px}}
.card{{border:1px solid var(--rule);border-left:3px solid var(--red);padding:22px 26px;margin-bottom:22px;background:#fff}}
.card .meta{{font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--bronze);font-weight:700;margin-bottom:9px}}
.card h2{{font-size:20px;margin:0 0 8px;border:0;padding:0;line-height:1.45}}
.card h2 a{{color:var(--deep);text-decoration:none}}
.card h2 a:hover{{color:var(--red)}}
.card .sub{{font-size:13.5px;color:var(--ink2);margin:0 0 12px;font-style:normal}}
.card .hook{{background:var(--soft);color:var(--ink2);border-left:3px solid var(--red);padding:12px 15px;margin:0 0 13px;font-size:13.5px}}
.more{{font-size:13px;color:var(--red);text-decoration:none;font-weight:700}}
.ar{{margin-top:56px;padding-top:26px;border-top:2px solid var(--rule)}}
.ar h3{{font-size:17px;color:var(--deep);margin-bottom:14px}}
.ar ul{{list-style:none;margin:0}}
.ar li{{display:flex;gap:14px;align-items:baseline;padding:9px 0;border-bottom:1px solid var(--rule);font-size:14px}}
.ar .d{{font-family:var(--mono);font-size:12px;color:var(--faint);flex:0 0 88px}}
.ar li a{{color:var(--ink);text-decoration:none;flex:1}}
.ar li a:hover{{color:var(--red)}}
.ar .c{{font-size:11.5px;color:var(--faint)}}
.empty{{color:var(--ink2);padding:40px 0}}
</style>
</head>
<body>
<div class="index">
  <h1>医药 BD 雷达</h1>
  <p class="tagline">每日自动抓取全球医药 BD 热点 · 评分 · 成稿 · {date}</p>
  {''.join(rows) if rows else '<p class="empty">今日无稿件。</p>'}
  <div class="ar">
    <h3>历史归档</h3>
    <ul>{archive}</ul>
  </div>
</div>
</body>
</html>
"""


def build_site(date: str | None = None) -> Path:
    date = date or today_cst()
    posts = _iter_posts()
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    # 文章复制到 site（保持相对路径）
    for p in posts:
        dest = SITE_DIR / p["rel"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(p["path"].read_text(encoding="utf-8"), encoding="utf-8")

    # 首页
    (SITE_DIR / "index.html").write_text(build_index(posts, date), encoding="utf-8")

    # 当日简报 JSON（供外部消费）
    today = [p for p in posts if p["date"] == date]
    write_json(SITE_DIR / "feed.json", {
        "date": date,
        "count": len(today),
        "posts": [{k: v for k, v in p.items() if k not in ("path",)} for p in today],
        "all_count": len(posts),
    })

    print(f"[publish] 站点 -> {SITE_DIR}（{len(posts)} 篇，今日 {len(today)} 篇）")
    if not today:
        st = _run_stats(date)
        print(f"[publish] 今日 0 篇：采集 {st['raw']} 条，评分 {st['scored']} 条，"
              f"最高 {st['top']:.1f} 分（门槛 6.5）")
    return SITE_DIR


# ============================================================
# 通知
# ============================================================
def _feishu_sign(timestamp: str, secret: str) -> str:
    """
    飞书自定义机器人的签名。

    ⚠️ 算法与常见 HMAC 用法相反，别想当然：
    飞书把 "{timestamp}\\n{secret}" 当作【密钥】，
    消息体是【空】。即 hmac.new(key=string_to_sign, msg=b"")，
    不是 hmac.new(key=secret, msg=timestamp)。写反了会一直被拒。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"),
                      digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _run_stats(date: str) -> dict:
    """当天采集/评分计数，用于 0 篇时说明"跑过了，只是没有合格交易"。"""
    raw = read_json(RAW_DIR / f"{date}.json", []) or []
    scored = read_json(SCORED_DIR / f"{date}.json", []) or []
    top = max((float(s.get("score") or 0) for s in scored), default=0)
    return {"raw": len(raw), "scored": len(scored), "top": top}


def _plan_diagnosis(date: str) -> dict | None:
    """读 plan.json 的 diagnosis 字段（0 篇时说明根因）。

    2026-10-25 新增：pick.py 在 0 篇时会把根因写进 plan.json：
      · no_fallback_candidate → 采集侧空素材（**不是"当天没交易"**）
      · below_threshold       → 有交易但都低于门槛
    """
    plan = read_json(SCORED_DIR / f"{date}.plan.json", {}) or {}
    diag = plan.get("diagnosis")
    return diag if isinstance(diag, dict) else None


def build_card(posts: list[dict], date: str, base_url: str = "") -> dict:
    """飞书交互卡片"""
    review = [p for p in posts if p.get("needs_review")]
    low = [p for p in posts if p.get("below_threshold")]
    lines = []
    if review:
        lines.append(f"**⚠️ 今日 {len(review)} 篇未通过全部质检，"
                     f"需人工核查后再推草稿箱**")
        lines.append("")
    if low:
        lines.append(f"**⚠️ 今日 {len(low)} 篇评分未达标入选门槛**"
                     f"（保底选取，稿件已照常成稿，请重点复核）")
        lines.append("")
    for p in posts:
        t = _html.escape(p["title"][:60])
        flag = "⚠️ " if p.get("needs_review") else ""
        if p.get("below_threshold"):
            flag += "🔸 "      # 评分不达标
        if base_url:
            lines.append(f"{flag}**[{t}]({base_url}/{p['rel']})**")
        else:
            lines.append(f"{flag}**{t}**")
        if p["sub"]:
            lines.append(p["sub"][:90])
        if p.get("below_threshold"):
            lines.append("　🔸 评分不达标（保底选取）")
        if p.get("needs_review") and p.get("problems"):
            for pr in p["problems"][:5]:
                lines.append(f"　· {_html.escape(pr[:110])}")
        lines.append("")
    if lines:
        body = "\n".join(lines)
    else:
        st = _run_stats(date)
        # ⚠️ 0 篇要说清根因（2026-10-25 用户要求）：区分
        #    "当天没有够格的交易"和"采集侧正文抓取失效"。
        body = (f"**✓ 雷达正常运行，今日 0 篇** —— 跑了采集和评分，"
                f"但没有交易过入选门槛。不是故障。\n\n")
        diag = _plan_diagnosis(date)
        if diag:
            thr = diag.get("keep_threshold")
            cand = diag.get("candidates")
            empty = diag.get("empty_material")
            pct = diag.get("empty_material_pct")
            top = diag.get("top_score")
            if diag.get("reason") == "no_fallback_candidate" and empty:
                body += (f"**根因：采集侧正文抓取失效**\n"
                         f"候选 {cand} 条中 {empty} 条（{pct:.0f}%）抓不到正文，"
                         f"写不出稿也不能保底。\n"
                         f"最高分候选 {top} 分（门槛 {thr}）。"
                         f"这不是「当天没有交易」，是采集侧出了问题。")
            elif diag.get("reason") == "below_threshold":
                body += (f"候选 {cand} 条全部低于入选门槛 {thr} 分"
                         f"（最高 {top} 分），且没有可保底的候选。")
        else:
            body += (f"评分 {st['scored']} 条 · 最高 {st['top']:.1f} 分"
                     f"（入选门槛 6.5）")

    # 0 篇=grey（跑过、没得写）；有待核查=orange；有评分不达标=yellow；正常=red
    if not posts:
        template = "grey"
    elif review:
        template = "orange"
    elif low:
        template = "yellow"
    else:
        template = "red"

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": (f"医药 BD 雷达 · {date}"
                                      + (f" · {len(review)} 篇待核查" if review else
                                         (f" · {len(low)} 篇评分不达标" if low else
                                          (" · 0 篇" if not posts else ""))))},
                "template": template,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": (f"共 {len(posts)} 篇 · 自动生成"
                                 + (f" · 其中 {len(review)} 篇需人工核查"
                                    if review else "")
                                 + (f" · {len(low)} 篇评分不达标"
                                    if low else ""))},
                ]},
            ],
        },
    }


def notify_feishu(posts: list[dict], date: str,
                  webhook: str | None = None,
                  base_url: str = "") -> bool:
    """
    推送到飞书群自定义机器人。

    环境变量：
      FEISHU_WEBHOOK  机器人 Webhook 地址（必填）
      FEISHU_SECRET   签名密钥（开启了"签名校验"时必填）

    安全模式说明：
      关键词模式   —— 消息里含你设的关键词即可。
                     本卡片标题含"医药 BD 雷达"，可把关键词设为 医药 / BD / 雷达
      签名模式     —— 需要 FEISHU_SECRET，见 _feishu_sign
      IP 白名单    —— GitHub Actions 出口 IP 不固定，无法使用
    """
    url = webhook or os.getenv("FEISHU_WEBHOOK")
    if not url:
        print("[notify] 未配置 FEISHU_WEBHOOK，跳过")
        return False
    # 0 篇也推送（2026-10-25 用户要求）：雷达正常跑过但当天无合格交易时，
    # 必须让用户在飞书里看到"跑了、0 篇"的明确信号——否则无法区分
    # "真没有值得写的"和"故障没跑"。统计卡片由 build_card 的空列表分支生成。
    if not posts:
        print("[notify] 今日 0 篇 —— 推送 0 产出统计卡片")

    payload = build_card(posts, date, base_url)

    # 签名校验模式
    secret = os.getenv("FEISHU_SECRET")
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _feishu_sign(ts, secret)

    try:
        r = httpx.post(url, json=payload, timeout=20)
    except Exception as e:
        print(f"[notify] 飞书请求异常: {type(e).__name__}: {str(e)[:100]}")
        return False

    # 飞书成功返回 {"code":0,"msg":"success"}
    try:
        d = r.json()
        code = d.get("code")
        if code == 0:
            print(f"[notify] 飞书推送成功（{len(posts)} 篇）")
            return True
        # 常见错误给出可操作的提示
        hint = {
            19021: "sign 校验失败：检查 FEISHU_SECRET 是否正确",
            9499: "消息内容为空或被安全策略拦截",
            19024: "关键词不匹配：机器人设了自定义关键词，"
                   "但消息里没有该词",
            19022: "IP 不在白名单：GitHub Actions 出口 IP 不固定，"
                   "请改用关键词或签名模式",
        }.get(code, "")
        print(f"[notify] 飞书推送失败 code={code} msg={d.get('msg')}"
              + (f"  → {hint}" if hint else ""))
        return False
    except Exception:
        ok = r.status_code == 200 and '"code":0' in r.text.replace(" ", "")
        print(f"[notify] 飞书返回无法解析 (HTTP {r.status_code}): "
              f"{r.text[:160]}")
        return ok


def run(date: str | None = None, do_notify: bool = True) -> None:
    """
    建站点 + 飞书通知。

    ⚠️ 职责边界（2026-10-25 用户澄清）：
      **云端雷达要发飞书**——"做的稿件要发飞书"是用户明确要求。
      云端负责"今天产出了什么"（含 0 篇的说明），
      本机 publisher 负责"进了草稿箱没有"（推草稿箱后的回报）。
      两者是**不同的问题**，不是重复：
        · 云端卡片 = 产出播报（今天有几篇、评分达不达标、为什么 0 篇）
        · 本机卡片 = 推送播报（核验改稿结果、进了草稿箱哪些、哪些没推）
      需要临时关掉云端通知时设 RADAR_FEISHU_NOTIFY=0。
    """
    date = date or today_cst()
    build_site(date)
    posts = [p for p in _iter_posts() if p["date"] == date]
    if do_notify and os.getenv("RADAR_FEISHU_NOTIFY", "1").lower() not in ("0", "false", "no"):
        notify_feishu(posts, date,
                      base_url=os.getenv("SITE_BASE_URL", ""))
    else:
        print("[publish] 飞书通知已跳过（RADAR_FEISHU_NOTIFY=0）")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    run(sys.argv[1] if len(sys.argv) > 1 else None,
        do_notify="--no-notify" not in sys.argv)