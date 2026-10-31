"""
私企（未上市）公司背景补全：从公开工商信息站点取可引用的基础资料。

为什么需要（2026-10-27 实测）：
  `companyinfo` 只覆盖上市公司（openFDA / SEC XBRL / ClinicalTrials.gov）。
  BD 交易的卖方常常是未上市的私企——比如瀚辰星泰，
  成立时间、注册地、业务方向、投资方名单全都不在现有通道里，
  成稿要么写不了（信息缺失），要么核验时被打成"无出处"。

⚠️ 边界：
  工商聚合站点（天眼查/企查查）的页面结构会变，也可能反爬。
  抓不到就返回空串，绝不编。核验器要求逐字出处，
  补不上的字段就别写进稿子。

用法：
  from privco import enrich
  print(enrich("瀚辰星泰"))
"""
from __future__ import annotations

import re

import httpx

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/147.0.0.0 Safari/537.36")}

# 聚合工商信息站点。取 meta description / 结构化数据里的关键字段。
SITES = [
    "https://www.tianyancha.com/search?key={kw}",
    "https://www.qcc.com/web/search?key={kw}",
]

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _fetch(url: str, timeout: float = 25.0) -> str:
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers=UA) as cl:
        r = cl.get(url)
        r.raise_for_status()
        return r.text


def _clean(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = _TAG.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    return _WS.sub(" ", text)


# 从页面文本里抠出这几个字段。写成"就近取值"，
# 避免站点改版后正则整体失配（失配就取不到，不产生假数据）。
#
# ⚠️ 2026-10-27 实测踩坑：字段值的中文词边界太松时，
#    "法定代表人"会抓到后面无关的词（比如"专利"）。
#    所以值必须【不含标点】且【紧跟标签】，命中后再做白名单校验。
_FIELDS = [
    ("成立日期", re.compile(r"成立日期[:：\s]*([0-9]{4}[-年][0-9]{1,2}[-月][0-9]{1,2}[日]?)")),
    ("注册地", re.compile(r"(?:注册地|注册地址|企业地址)[:：\s]*"
                           r"([一-龥A-Za-z0-9（）()]{2,24})")),
    ("法定代表人", re.compile(r"法定代表人[:：\s]*"
                               r"([一-龥A-Za-z·]{2,20}?)(?=[,，;；|。\s]|成立|注册|经营|资本)"),
     re.compile(r"法定代表人[:：\s]*"
                r"([A-Z][A-Z\s]{2,30})(?=[,，;；|。\s]|成立|注册|经营|资本)")),
    ("注册资本", re.compile(r"注册资本[:：\s]*([0-9.]+\s*[万亿]?[元]{1,2})")),
]

# 明显不是人名的取值（机构、通用词）——命中就丢弃该字段
_BAD_NAME = re.compile(
    r"专利|商标|投资|管理|咨询|技术|有限|合伙|中心|代理|认证|"
    r"服务|贸易|实业|集团|资本|基金|银行|保险|咨询")


def enrich(company_cn: str) -> str:
    """返回私企背景素材块；抓不到就返回空串。"""
    if not company_cn or len(company_cn) < 2:
        return ""
    # ⚠️ 工商聚合站点会限流/要验证码，同一 URL 连续请求可能时有时无。
    #    所以每个站点试 2 次，再换下一个站点——单次失败就放弃会让
    #    整段素材凭空消失（2026-10-27 实测：同一份代码第一次抓成功、
    #    第二次返回空，稿子里对应的断言全被判"无出处"）。
    for tpl in SITES:
        for attempt in range(2):
            url = tpl.format(kw=company_cn)
            try:
                text = _clean(_fetch(url))
            except Exception as e:
                print(f"[privco] 抓取失败 {url[:60]}: {type(e).__name__}")
                continue
            got = _extract(company_cn, text)
            if got:
                return got
            # 命中页面但没解析出字段 → 多半是限流页，等一下再试
            if attempt == 0:
                import time
                time.sleep(2.0)
    return ""


def _extract(company_cn: str, text: str) -> str:
    """从页面文本里解析字段；解析不出返回空串。"""
    if company_cn not in text:
        return ""
    found = {}
    for spec in _FIELDS:
        name, rxs = spec[0], spec[1:]
        for rx in rxs:
            m = rx.search(text)
            if not m:
                continue
            val = m.group(1).strip()
            if name == "法定代表人" and _BAD_NAME.search(val):
                continue
            found[name] = val
            break
    if not found:
        return ""
    lines = [f"【{company_cn} 工商信息（可引用，来自公开工商信息站点）】"]
    for k, v in found.items():
        lines.append(f"  · {k}：{v}")
    lines.append("⚠️ 本段只提供上述工商字段。"
                 "融资历史、业务方向、团队规模等未列出的信息不要写。")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(enrich(sys.argv[1] if len(sys.argv) > 1 else "瀚辰星泰"))
