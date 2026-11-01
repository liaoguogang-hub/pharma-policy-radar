# 医药政策雷达（Pharma Policy Radar）

每天自动抓取**中外医药政策 / 监管 / 医保 / 集采 / 产业政策**动态 → LLM 评分 → 选题
→ 查一手原文 → 写成中文政策分析稿 → 发布到 GitHub Pages + 飞书卡片。

第三个雷达，与 `clinical-data-radar`（临床数据）、`pharma-bd-radar`（医药 BD）同架构。
本机配套的公众号推送在 `wechat-publisher`（合集「医药政策」）。

**线上站点**：https://liaoguogang-hub.github.io/pharma-policy-radar/

---

## 管线

```
fetch  →  dedupe  →  noise  →  score  →  pick  →  compose  →  publish
采集      去重      噪声过滤   LLM评分   选题     成稿+校验    站点+飞书
```

```bash
cd src
python fetch.py   2026-11-01     # 采集（列表页 + 正文 + 发布日期）
python dedupe.py  2026-11-01     # 跨天去重 / 同事件归并
python score.py   2026-11-01     # 政策五维评分
python pick.py    2026-11-01     # 选题（最多 3 篇）
python compose.py 2026-11-01     # 成稿（结构校验 + 事实闸门 + 断言核验）
python publish.py 2026-11-01     # 建站点 + 飞书卡片
```

## 政策专用设计

### 入选范围（2026-11-01 收紧）

只放**国家 / 各省医保局、卫健委，国家药监局、CDE 发布的、对国家或省份（省际联盟）
产生影响的政策**。由 `src/scope.py` 做确定性闸门（不依赖模型）：

| 规则 | 说明 |
|---|---|
| 发文机关白名单 | 国家/省医保局、国家/省卫健委、国家药监局、CDE、联采办及其直属机构 |
| 联合发文 | 看**牵头机关**：医保局牵头 + 财政部会签 = 放行；公安部/工信部牵头 = 出局 |
| 排除主题 | 化妆品、牙膏、保健食品等非医药政策 |
| 排除事务性文件 | 参比制剂目录、品种转 OTC、说明书修订、中药品种保护受理、挂网价格公示 |
| 影响范围 | 必须 national / province / 省际联盟；单个地市、单家医院、单企业 = 出局 |
| 海外 | 只认 FDA/EMA/EC/HHS/CMS 的**政策类文件**（guidance/rule/regulation/framework）；单个药品批准出局 |

## 评分五维（合计 10 分）

| 维度 | 分值 | 判据 |
|---|---|---|
| policy_level 政策层级 | 0-3 | 国务院/多部委联合 3；单一部委 2.5；司局/直属机构 1.5；省级 0.8；市级及以下 0.3 |
| impact 影响面 | 0-3 | 全行业规则 3；单适应症/品类/环节 2；单企业 0.8；纯重申 0.3 |
| force 落地力度 | 0-2 | 已生效并执行 2；正式发布 1.5；征求意见稿 1.0；吹风/规划 0.5 |
| novelty 增量性 | 0-1.5 | 首次明确/推翻旧规 1.5；增量收紧 1.0；例行 0.4；纯重申 0 |
| verifiability 可核验度 | 0-0.5 | 官方原文+文号+生效日期 0.5；官方通知摘要 0.3；媒体转述 0.1 |

阈值：keep 6.0 / must 7.5；单日最多 3 篇；部委级正式文件可保底 1 篇。

### 四种文章类型

`policy_readout`（单一政策解读）/ `consultation`（征求意见稿解读）/
`enforcement`（执法处罚复盘）/ `policy_review`（多文件/时间线综述）

### 与另外两个雷达的关键差异

1. **一手锚点不是注册库，而是官方原文**：`compose.policy_anchors()` 从原文里
   用正则抽「文号 / 发布日 / 生效日 / 征求意见截止日 / 文件性质」，确定性地喂给成稿模型
2. **阶段不能写错**：`validate_content` 检查「征求意见稿 ≠ 已发布」「正式发布 ≠ 已实施」
3. **断言核验换类型**：`claimcheck` 的 kind 从交易条款改为
   `authority/doc_meta/clause/status/scope/impact`

## 配置

| 文件 | 内容 |
|---|---|
| `config/sources.yaml` | 源清单（中国监管 / 医保集采 / 产业政策 / 海外重点）+ 抓取配额 |
| `config/scoring.yaml` | 五维权重、阈值、文章类型、LLM provider（MiniMax 主用、DeepSeek 备用） |
| `config/style_guide.yaml` | 内容硬规则、AI 味词表、文章类型结构、字数 |

密钥：GitHub Secrets `LLM_API_KEY`（MiniMax）、`DEEPSEEK_API_KEY`（备用）、
`FEISHU_WEBHOOK`、`FEISHU_SECRET`；变量 `SITE_BASE_URL`。

## 定时

| 时间（北京） | 谁 |
|---|---|
| 06:45 | GitHub cron（UTC 22:45，避开整点） |
| 06:35 | 本机 `Radar-Policy-Fallback`：先查后触发云端 |
| 06:50 | 本机 `WeChatPub-Policy`：等稿 → 核验改稿 → 推公众号草稿箱（合集「医药政策」） |
| 每 30 分钟 | `WeChatPub-Watchdog`（覆盖三个雷达） |

## 已知约束

- **封面**：MiniMax 图片可用时出写真封面；额度用尽时自动退本地「数据卡」封面
  （`wechat-publisher/src/datacard.py`，数字有程序化校验）
- **原创声明**：2026-11-01 起微信恢复，publisher 会声明文字原创
- **草稿箱上限**：约 55-60 条，超了微信静默删旧稿，需要定期清理
- **政府站点反爬**：卫健委 412、HHS/CMS 403、gov.cn 政策文件库 403（见 sources.yaml 注释）
