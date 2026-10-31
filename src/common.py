"""
数据模型与共用工具。
所有模块通过这里交换结构化数据，避免各模块自己定义格式。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

# ============ 路径 ============
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SCORED_DIR = DATA_DIR / "scored"
SEEN_FILE = DATA_DIR / "seen.json"

CST = timezone(timedelta(hours=8))  # 医药新闻按北京时间判断新鲜度


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, SCORED_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ============ 配置加载 ============
def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============ 核心数据结构 ============
AccessLevel = Literal["full", "headline", "rss", "blocked"]


@dataclass
class RawItem:
    """采集到的原始条目"""
    id: str
    source: str
    lang: str
    title: str
    url: str
    published: str                    # ISO8601
    access: AccessLevel
    mode: str                          # rss | html | pdf
    content: str = ""                  # full 才有正文（已剥离新闻站实体标签行）
    entity_tags: list[str] = field(default_factory=list)   # 被剥离的实体标签（≠交易内容）
    summary: str = ""                  # rss/headline 的摘要
    weight: float = 1.0
    topics: list[str] = field(default_factory=list)
    fetch_time: str = ""
    # ⚠️ 源配了 fetch_body: true → 即使 access=headline 或走 RSS，
    #    也要去抓正文（2026-10-25）。实测 10-25 有 88% 的候选 content 为空，
    #    根因就是 headline 源被硬性排除在正文抓取之外。
    fetch_body: bool = False

    @property
    def age_hours(self) -> float:
        """
        距抓取时刻的小时数。
        日期未知时返回 -1（哨兵值，表示"未知"，不是"很旧"）。

        ⚠️ 早期实现有两种误导：
        - 列表页条目一律填抓取时刻 → 几个月前的旧文看起来像刚发布
        - 未知时返回 999 → 未知被当成"非常旧"而误杀
        未知就是未知，要能被上层识别并区别对待。
        """
        if not self.published:
            return -1.0
        try:
            dt = datetime.fromisoformat(self.published)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            return -1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RawItem":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ScoredTopic:
    """LLM 评分后的选题"""
    id: str
    title: str
    url: str
    score: float
    dims: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reasoning: str = ""
    article_type: str = "event_report"     # event_report|landscape_review|historical_compare
    verify_todo: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)   # 同题多个来源
    content: str = ""                              # 可用正文素材（已剥离实体标签行）
    entity_tags: list[str] = field(default_factory=list)  # 新闻站实体标签（≠交易内容）
    scored_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ============ 工具 ============
def make_id(title: str, url: str = "") -> str:
    raw = f"{title.strip().lower()}|{url.split('?')[0]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    s = s.replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# 新闻站（摩熵/pharnexcloud 等）会把自动实体标注放在正文最前面，形如：
#   "…摩熵医药 2026/10/08 2846  Eli Lilly & Co  TYK2
#        北京诺诚健华医药科技有限公司  益方生物科技（上海）股份有限公司
#        近期，诺诚健华公告称，公司全资子公司……"
#
# 那串只是新闻站打的【实体标签】，不是交易内容。实测它害人不浅：成稿模型把
# 标签里的 "TYK2" 当成交易标的，把"至多5项创新靶点的平台合作"改写成"TYK2 平台
# 授权"，还顺着它捏造了一个不存在的分子代号（见 data/postmortem_b4985179.md）。
#
# ⚠️ 识别策略刻意保守（skill §6：规则不能比问题更伤人，误伤正文的代价比漏一行
#    标签大得多）。三个信号必须同时成立才动手：
#      ① 正文里有【日期 + 阅读数】的 CMS byline
#      ② byline 后紧跟 ≥2 个"长得像实体名词"的 token
#      ③ 这些 token 之后必须是一段像新闻导语的正文（足够长）
#    任一条不满足就原样返回。
_BYLINE = re.compile(r"20\d\d[/-]\d{1,2}[/-]\d{1,2}\s+\d{3,7}(?=\s)")

# "长得像实体名词"的三种形态
_ENT_CJK_TAIL = ("公司", "药业", "生物", "科技", "医药", "制药", "股份",
                 "集团", "有限", "控股", "研究院", "大学")
_ENT_UPPER_CODE = re.compile(r"^[A-Z][A-Z0-9/-]{1,11}$")
_ENT_CJK_WORD = re.compile(
    r"^[一-鿿A-Za-z0-9（）()·]{2,40}(" + "|".join(_ENT_CJK_TAIL) + r")[\s]*$")
_LATIN_TOKEN = re.compile(r"^[A-Za-z][A-Za-z.&'-]*$")
_LATIN_CONNECT = {"&", "and", "of", "de", "the", "inc", "corp", "ltd", "gmbh"}


def _consume_latin_run(raw: str) -> tuple[str, int]:
    """
    吃掉一个可能跨多个 token 的拉丁公司名（Eli Lilly & Co / Dice Therapeutics）。

    要求至少两个拉丁词——单个英文词太容易把普通句子的开头误判成实体。
    """
    pos, words = 0, []
    while True:
        mm = re.match(r"\s*(\S+)", raw[pos:])
        if not mm:
            break
        t = mm.group(1)
        if _LATIN_TOKEN.match(t) or t.lower() in _LATIN_CONNECT:
            words.append(t)
            pos += mm.end()
        else:
            break
    # 去掉尾部的孤立连接词（"Eli Lilly &" 这种残缺形式）
    while words and words[-1].lower() in _LATIN_CONNECT:
        words.pop()
    if len(words) < 2:
        return "", 0
    return " ".join(words), pos


def _consume_entities(rest: str, limit: int = 8) -> tuple[list[str], int]:
    """从 rest 开头连续吃掉"实体标签" token，返回 (标签列表, 消耗掉的字符数)。"""
    tags: list[str] = []
    pos = 0
    while len(tags) < limit:
        # 跳过实体之间的分隔空格（拉丁公司名 consume 完可能正好停在空格上）
        while pos < len(rest) and rest[pos].isspace():
            pos += 1
        mm = re.match(r"\S+\s*", rest[pos:])
        if not mm:
            break
        tok = mm.group(0).rstrip()
        if _ENT_CJK_WORD.match(tok):
            tags.append(tok)
            pos += mm.end()
            continue
        if _ENT_UPPER_CODE.match(tok):
            tags.append(tok)
            pos += mm.end()
            continue
        # 拉丁公司名可能跨多个 token（Eli Lilly & Co）
        run, used = _consume_latin_run(rest[pos:])
        if run:
            tags.append(run)
            pos += used
            continue
        break
    return tags, pos


def split_entity_tags(content: str) -> tuple[str, list[str]]:
    """
    把正文开头的新闻站实体标签行剥离出来。

    返回 (干净的正文, 标签列表)。识别不到就原样返回，不做任何改动。
    """
    if not content:
        return content, []
    m = _BYLINE.search(content)
    if not m:
        return content, []

    rest = content[m.end():].lstrip()
    tags, used = _consume_entities(rest)

    if len(tags) < 2:
        return content, []                    # 单个 token 风险太高，不动
    body = rest[used:].lstrip()
    # 标签之后必须是一段真正的新闻正文
    if len(body) < 200 or body.count("。") < 2:
        return content, []
    if not re.match(r"(?:近期|据悉|日前|近日|昨日|本周|宣布|公告)", body):
        return content, []
    return body, tags


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def normalize_deal_key(key: str) -> str:
    """
    统一 deal_key 的写法，便于跨天/跨来源比对。

    小写、去重音、非字母数字转 '-'、前两段（交易双方）按字母排序。
    例："Roche|Hansoh|ALK7 RNAi" -> "hansoh|roche|alk7-rnai"
    """
    s = unicodedata.normalize("NFKD", str(key or "")).encode(
        "ascii", "ignore").decode("ascii")
    parts = []
    for p in s.split("|"):
        p = re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-")
        if p:
            parts.append(p)
    if len(parts) >= 2:
        parts = sorted(parts[:2]) + parts[2:]
    return "|".join(parts)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default