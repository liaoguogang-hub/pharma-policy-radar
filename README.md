# 医药 BD 雷达

每天早上 6:23（本机兜底任务 06:50），自动去 19 个医药网站抓当天 BD 新闻，用 LLM 打分，挑出最重要的 2-3 条写成中文分析，发布到网站并推送到飞书群。

**线上站点**：https://liaoguogang-hub.github.io/pharma-bd-radar/

---

## 它长什么样

一次典型运行（2026-10-10）：

```
采集 139 条  →  去噪去重 102 条  →  LLM 评分 97 条
    →  选题 3 篇  →  成稿 3/3  →  站点上线 + 飞书推送
```

评分维度（满分 10）：

| 维度 | 满分 | 判据 |
|---|---|---|
| 金额量级 | 2.5 | **首付**金额（不是总额） |
| 交易阶段 | 2.0 | III 期/NDA → 临床前 |
| 首创性 | 2.0 | 全球首个 → 跟跑 |
| 公司层级 | 1.5 | MNC → 二线 biotech |
| 外溢影响 | 2.0 | 改变赛道格局 → 常规交易 |

---

## 快速开始

### 1. Fork 或克隆

```bash
git clone https://github.com/liaoguogang-hub/pharma-bd-radar.git
cd pharma-bd-radar
pip install -r requirements.txt
```

### 2. 配置密钥

```bash
# LLM API Key（必填）——支持 DeepSeek / MiniMax / 任意 OpenAI 兼容端点
gh secret set LLM_API_KEY

# 搜索 API（选填但强烈建议）——配任意一个即可自动启用一手材料检索
gh secret set SERPER_API_KEY     # 推荐：每月 2500 次免费
# gh secret set TAVILY_API_KEY   # 备选：每月 1000 次免费
# gh secret set BRAVE_API_KEY    # 备选：每月 2000 次免费

# 飞书群自定义机器人（选填，不配则不推送，不影响其他步骤）
gh secret set FEISHU_WEBHOOK
gh secret set FEISHU_SECRET    # 仅当机器人开了「签名校验」
```

**为什么需要搜索 API**：新闻源的 RSS 摘要常常只有 200 字符。
实测一篇「Viatris 收购 Pacira」的稿子，素材只有一句话，
写出来既没有每股收购价（实际 $36.50/股），也没有交易日期——
读者根本看不出这是新闻。

配了搜索 key 后，`deepen.py` 会自动去找公司新闻室、
交易所公告等一手材料。

> 实测过 8 个免费通道（DuckDuckGo / Bing / Mojeek / Startpage /
> Searx / Brave HTML），**没有一个可用**：
> 或连接被拒，或像 Bing 那样强制跳转区域站并忽略查询词。
> 所以这一步确实需要注册。

再配一个 Variable（用于飞书卡片里的文章链接）：

```bash
gh variable set SITE_BASE_URL --body "https://<你的用户名>.github.io/pharma-bd-radar"
```

### 3. 启用 GitHub Pages

```bash
gh api -X POST repos/<用户名>/pharma-bd-radar/pages -f build_type=workflow
```

### 4. 手动跑一次

```bash
# 完整流程
gh workflow run radar.yml

# 冒烟测试：只抓取去重，不需要任何 API key
gh workflow run radar.yml -f skip_llm=true
```

之后每天 **06:23（北京时间）** 自动运行；本机计划任务在 06:50 再兜底触发一次，
重复触发由 guard 按「当天已有评分文件」拦掉。

> cron 用 UTC：`23 22 * * *` = 北京时间次日 06:23。
> 一天一趟意味着美股时段发布的新闻要到次日早上才抓到；周末可能出现 0 篇。

---

## 流程

```
┌─ fetch.py ────── 18 个源 + BioPharma Dive 结构化交易库
│                  噪声过滤（垂直源放宽 / 综合财经源收紧）
│                  按配额抓正文
├─ dedupe.py ───── 跨天去重（只对往日条目）+ 语义去重（标题相似度）
├─ score.py ────── LLM 五维打分，编码了行业核查规则
├─ pick.py ─────── 聚簇合并 + 选题，每天 2-3 篇
├─ compose.py ──── LLM 只输出轻量标记（不写 HTML）
│  render.py ───── 代码把标记渲染成固定模板 HTML
│                  质检不过则带错误清单重写（最多 3 次）
└─ publish.py ──── 生成站点 + 推送飞书卡片
```

---

## 模块说明

| 文件 | 职责 |
|---|---|
| `src/common.py` | 数据模型与路径常量 |
| `src/fetch.py` | 采集：RSS / HTML / 追踪数据库 |
| `src/noise.py` | 两级噪声过滤 |
| `src/dedupe.py` | 去重（跨天 + 语义） |
| `src/score.py` | LLM 评分（含 DeepSeek/MiniMax 适配） |
| `src/pick.py` | 选题 |
| `src/compose.py` | 文章生成 + 内容层质检 |
| `src/render.py` | 固定 CSS + 标记解析 + HTML 渲染 |
| `src/trackers.py` | BioPharma Dive Datawrapper 数据库 |
| `src/deepen.py` | 搜索一手材料（公司 newsroom / 交易所公告） |
| `src/search_backend.py` | 搜索后端适配（Serper / Brave / Tavily / DDG） |
| `src/publish.py` | 站点生成 + 飞书卡片 |
| `src/probe_sources.py` | 源健康探针 |
| `src/validate.py` | HTML 层校验（历史遗留，用于交叉验证） |

---

## 配置

### `config/sources.yaml` — 源清单

每个源要声明 `access` 级别：

| 级别 | 含义 | 处理 |
|---|---|---|
| `full` | HTML 全文可读 | 抓正文 |
| `headline` | 只有标题/摘要 | 只用标题 |
| `rss` | 走 RSS feed | 用摘要 |
| `blocked` | 站点封爬（如 Cloudflare 403） | 跳过，不再重试 |
| `disabled` | 源无有效产出（地址失效/内容不对口） | 跳过，避免浪费请求 |
| `search` | 需外部检索 | 留钩子给 Actions |

**新增源时**：先跑 `python probe_sources.py <关键词>`，它会逐级报告漏斗
（HTTP → 选择器命中 → 有无标题 → 关键词 → 噪声），卡在哪一步一目了然。

### `config/scoring.yaml` — 评分与模型

```yaml
llm:                    # 评分用
  provider: minimax_domestic
gen_llm:                # 生成用（可分开配）
  provider: minimax_domestic
```

支持三档 provider，改 `provider` 一行即可切换：

| provider | base_url | 模型 |
|---|---|---|
| `deepseek` | api.deepseek.com/v1 | deepseek-chat |
| `minimax` | api.minimax.io/v1 | 国际版 |
| `minimax_domestic` | api.minimaxi.com/v1 | 国内版 |

> **注意**：MiniMax 国内版 key 只能用于 `api.minimaxi.com`，用国际端点会返回 401。

### `config/style_guide.yaml` — 文风与结构

- `generation`: 字数区间、重试次数、AI 味词容忍度
- `banned_words` / `banned_patterns`: AI 味黑名单
- `article_types`: 三种文章类型的结构模板

---

## 设计决策（都是踩过的坑）

### 1. LLM 不写 HTML，只写内容

**问题**：让 LLM 一次完成「写 3000 字 + 保证数字准确 + 写对 HTML 标签 + 抄 2400 字符 CSS」，
成功率只有 **1/3**。失败全部集中在格式层：标签不配平、CSS 丢失、输出截断。

**试过的失败方案**：用正则自动补标签。连试三轮，每次都引入新错误——
把 `</html>` 当标签对误删、生成多余的 `</td>`、跨标签垃圾嵌套。
**修复引入的错误比原错误更难发现。**

**现在的做法**：LLM 输出轻量标记，代码负责所有标签、CSS、转义。

```
LLM 输出：
  TITLE: xxx
  HOOK: xxx
  ## 01 小节
  正文...
  TABLE: 列1 | 列2
  ENDTABLE
  ENDCARD 标题 | 正文
  FN 1. 来源说明

代码渲染 → 完整 HTML
```

**效果**：成功率 1/3 → **3/3**；标签错误和 CSS 丢失从架构上不可能发生；
每篇耗时 2-4 分钟 → **46 秒**。

### 2. 格式负担移除后，推理模型反而更好

A/B 测试同一篇文章：

| 模型 | 耗时 | 输出 |
|---|---|---|
| MiniMax-M3.1-Flash | 113s | **0 字符**（空响应） |
| MiniMax-M2.7 | **46s** | 3291 字符，结构完整 |

M2.7 此前表现差，是因为它要同时写 HTML+CSS。去掉格式负担后，推理模型的优势立刻显现。

### 3. `seen.json` 必须带日期

**问题**：历史去重把「当天已抓过的标题」也当成「已报道」。
工作流每天跑两次，于是**晚班永远产出 0 篇**。

CI 实测：输入 139 条 → 历史去重丢弃 110 条 → 只剩 2 条 → 成稿 0 篇。

**做法**：`seen.json` 升到 v2，每条带 `date`；只对 `date < today` 的条目去重。
v1 无日期格式**直接丢弃**——无法安全判断是否为往日条目。

### 4. 选择器配置表不能有重复键

PharmExec 的 `LIST_PATTERNS` 里同名键定义了两次，Python 字典后键覆盖前键，
生效的是错误的 `/news/`（实际路径是 `/view/`），产出 0 条。

**误判**：当时结论是「数据中心 IP 被拦」——实际是自己的代码 bug。
早先拿到 5 条是因为那时只有宽泛的 `/` 版本，后来被自己加的 `/news/` 覆盖了。

### 5. 校验工具必须复用真实函数

源健康探针的第一版自己重写了一遍过滤流程，结果因为「去重」和「标题长度检查」
的顺序与真实代码不同，把能用的源误报成「关键词全过滤」。

**工具自身的 bug 比被检测的 bug 更危险。**

### 6. 标题金额必须与正文一致

CI 生成的稿件标题写「GSK豪掷 **50亿** 美元首付」，正文却写「支付了 **5亿** 美元首付」。
正文是对的，标题错了 10 倍——而标题是读者第一眼看到的数字。

**规则**：标题里「金额 + 限定词」（首付/总额/里程碑）的组合，必须在正文中找到同一组合。

**为什么要带限定词**：只看数字的话，「50亿」会在正文表格里匹配到另一家公司的
「50亿美元总额」而误判通过。

### 7. 飞书签名算法与常规 HMAC 相反

飞书把 `"{timestamp}\n{secret}"` 当作 **HMAC 的密钥**，消息体是**空的**：

```python
hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256)
```

按常规写法（密钥当 key、时间戳当 msg）会一直被拒，而错误只报「sign 校验失败」。

### 8. 模块名要避开标准库

`src/select.py` 遮蔽了标准库的 `select`，导致 httpx 的 socket 层全面崩溃，
**所有源抓取失败**。报错是 `module 'select' has no attribute 'select'`，
完全指不到真正原因。已改名 `pick.py`。

### 9. 时间维度：不知道日期绝不能填"现在"

**症状**：写出来的稿子讲的是 2025 年的交易，读者一眼看出不对。

**三个独立缺陷，同一个坏习惯**——不知道日期就用抓取时刻顶上：

| 缺陷 | 后果 |
|---|---|
| 列表页条目填 `now_iso()` | BioPharma Dive 的专题归档页挂着几个月前的旧文，被当成当天新闻 |
| Datawrapper 版本号硬编码为 `1` | 读的是两年前的第一个快照：41 笔 / 最新 2025-08-11，而实际是 112 笔 / 最新 2026-09-29 |
| 交易库混进选题池 | 参考数据库被当成新闻源，旧交易抢走了选题位 |

**修法**：
- 日期未知就**留空**，`age_hours` 返回 `-1` 哨兵值（不是 999，那会把"未知"当"很旧"）
- 新增 `extract_pub_date()`：JSON-LD / `<time>` / 正文 / URL 四种来源，实测 8/8 命中
- 新增陈旧过滤：发布日期超 `max_age_days` 的丢弃（实测丢 28 条）
- 版本号**动态发现**：Datawrapper 基址会 302 到最新版
- 交易库单独存 `tracker_deals.json`，只作历史对比，不进选题池

**通用教训**：任何"外部会变的值"都不要硬编码——版本号、阈值、日期都一样。

### 10. 阈值要按真实分布标定，不是拍脑袋

8.5 / 7.5 这组阈值是在**数据被污染时期**定下的：当时 41 笔 2025 年旧交易
被当成新交易，撑出一批 8.2~9.0 的假高分，阈值看起来很合理。

清掉污染后，54 条的真实分布是：

| 区间 | 条数 |
|---|---|
| ≥8.5 | 0 |
| 7.5-8.5 | 1 |
| 7.0-7.5 | 1 |
| 6.0-7.0 | 5 |
| <5.0 | 43 |

沿用旧阈值 → 多数日子产出 0-1 篇。现改为 `must_publish: 8.0` / `keep: 6.5`。

**教训**：阈值必须在**干净数据**上标定。数据有问题时调的参，数据修好后全是错的。

### 11. BD 雷达必须先判断"这是不是一笔交易"

**症状**：一篇《全球首个双周GLP-1/GIP双靶点减重药临床成功，值多少钱？》，
正文的"交易要素"表里授权方/受让方/首付款/里程碑**全是"未找到公开数据"**，
副标题还写着"这款中国资产凭什么让跨国药企出手"——**素材里根本没有这笔交易**。

**根因**：源素材只有一行标题（`material_chars = 0`）：

> 长效减重新选择，全球首个双周给药GLP-1R/GIPR双受体激动剂临床三期成功

这是**临床数据读出**，不是 BD 事件。但：

1. 关键词过滤放它进来（`临床`/`三期` 都在白名单里）
2. 评分给了 6.5（III期=2.0 分 + 全球首个）
3. 成稿模板强行套「交易要素」表 → 全空
4. 模型为了填满 2800 字，**脑补出"跨国药企出手"的情节**

**实测**：54 条候选里**只有 16 条是交易**，38 条不是（临床读出、监管获批、
股价波动、人事变动、学术论文）。杂质率 74%。

**三层修复**：

| 层 | 修法 |
|---|---|
| 评分 | prompt 加第 ⑧ 条：非交易条目 `score ≤ 3` 并标 `not_bd`；第 ⑨ 条：素材量为 0 要标 `verify_todo` 并压低置信度 |
| 选题 | `pick.py` 再拦一道，剔除带 `not_bd` 的条目（不能只靠 LLM 听话） |
| 成稿 | prompt 禁止脑补素材外的交易方；新增**空壳表格检测**——某表格 ≥50% 且 ≥3 行是"未找到公开数据"就判为套模板，打回重写 |

**实测效果**：重新评分后 42/57 条被判为非交易，那篇 GLP-1 稿得 0 分
（`flags=['not_bd','clinical_only']`）。

**通用教训**：一个垂直领域的采集器，**"属于这个领域"必须是显式判断**，
不能靠关键词白名单默认通过。关键词只能说"可能相关"，
真正的门槛要由判断层来把。

---

## 故障排查

| 现象 | 排查 |
|---|---|
| 某个源 0 条 | `python probe_sources.py <源名>`，看卡在哪一级 |
| 产出 0 篇 | ① 查 `data/seen.json` 是否为 v3、基准是否过宽 ② 查阈值与当日分数分布 |
| 文章讲的是旧交易 | 查源条目的 `published` 是否为真实日期（不应是抓取时刻） |
| 交易库数据陈旧 | `trackers.py` 的版本号是否走了动态发现 |
| 评分步骤只收到几条 | 历史去重过滤过度，见 `dedupe.filter_history` |
| LLM 返回空 | 检查 `max_tokens`；MiniMax 的该参数含思考 token |
| 401 认证失败 | MiniMax 国内 key 只能用 `api.minimaxi.com` |
| 飞书推送失败 | 看日志 code：19021 签名错 / 19024 关键词不匹配 / 19022 IP 白名单（不可用） |
| 生成的 HTML 排版崩 | 不应发生（标签由代码生成）；若发生检查 `render.py` |

---

## 成本

| 项目 | 估算 |
|---|---|
| GitHub Actions | 免费额度内（每天约 20 分钟） |
| LLM | **约 ¥10-25/月**（每天 1 次评分 + 2-3 次生成） |
| GitHub Pages | 免费（公开仓库） |

---

## 已知限制

- **Fierce Biotech / Fierce Pharma**：全站 CloudFront 403，RSS 同样被封，需走搜索后端
- **BioWorld**：本地与 GitHub runner 双向实测 403（Cloudflare），已停用
- **上海证券报**：综合财经首页无医药内容，找不到稳定医药入口，已停用
- **飞书 IP 白名单模式**：GitHub Actions 出口 IP 不固定，原理上不可用
- **PDF 解析**：Actions 内可 `pip install pdfplumber`，但当前未接入交易所公告抓取

---

## 许可

MIT
