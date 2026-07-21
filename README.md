# Paper Daily

Paper Daily 每天从 arXiv 等论文源抓取新论文，按研究方向评分，生成中文摘要，并通过 GitHub Pages 展示。

## 功能

- 按关键词、arXiv 分类、文本重叠和跨方向信号计算相关性。
- 根据收藏论文学习关键词、分类和作者偏好，并在后续采集中小幅加权。
- 使用平滑时效分，不会让 90 天内的论文获得完全相同的奖励。
- 截断每日结果前做多样性重排，减少单一方向和近似标题占满列表。
- 支持 arXiv、OpenAlex、Crossref、Semantic Scholar、SerpApi Google Scholar 和 RSS/Atom。
- 可选调用 OpenAI-compatible 模型生成中文技术摘要。
- 支持本地收藏、主题切换、方向/等级筛选和中英文摘要视图。

## 快速开始

### 1. 配置 GitHub Pages

进入仓库：

```text
Settings -> Pages -> Build and deployment -> Source
```

选择 `GitHub Actions`。

### 2. 配置研究方向

推荐新建一个标题为 `Research Interests` 的 Issue，并使用仓库中的 Issue 模板。也可以直接编辑 [`config/interests.json`](config/interests.json)。

最小配置：

```json
{
  "sources": [
    { "type": "arxiv", "name": "arXiv" }
  ],
  "topics": [
    {
      "id": "llm_inference",
      "name": "LLM Inference",
      "description": "Efficient serving and distributed inference.",
      "keywords": ["LLM inference", "KV cache", "tensor parallelism"],
      "arxiv_categories": ["cs.CL", "cs.LG", "cs.DC"]
    }
  ]
}
```

建议：

- 关键词优先写英文短语，具体短语比单个泛词更有效。
- 每个方向先保留 5 到 15 个高质量关键词。
- `arxiv_categories_whitelist` 是全局硬过滤；不确定时可以省略。
- 定时任务会同时抓取主题关键词和主题分类中的近期论文，再使用完整关键词表评分，避免较靠后的关键词对应论文在抓取阶段遗漏。
- `negative_terms` 是可选的降权词典，只应填写当前研究方向明确不需要的领域词。

### 3. 配置模型 API

模型 API 是可选项。未配置时仍会采集和评分，只生成基础摘要。

在 `Settings -> Secrets and variables -> Actions` 中添加：

| 类型 | 名称 | 说明 |
| --- | --- | --- |
| Secret | `DEEPSEEK_API_KEY` | DeepSeek API Key |
| Secret | `OPENAI_API_KEY` | OpenAI API Key |
| Secret | `LLM_API_KEY` | 其他 OpenAI-compatible 服务 |
| Variable | `LLM_BASE_URL` | 自定义兼容接口地址 |
| Variable | `LLM_MODEL` | 自定义模型名；使用 DeepSeek 时默认为 `deepseek-v4-flash` |

三种 Key 只需配置一种。不要把 Key 写进代码、Issue 或仓库文件。

### 4. 首次运行

进入：

```text
Actions -> Paper Daily -> Run workflow
```

首次可以把 `lookback_days` 设为 `7`。之后 workflow 默认每天北京时间 09:00 运行，并从上次成功生成时间开始增量采集。

## 推荐机制

完整的评分公式、偏好学习、多样性重排序和历史保留规则见 [算法说明](ALGORITHM.md)。

基础分由三部分组成：

```text
base_score =
  0.55 × 关键词分
  + 0.18 × 分类分
  + 0.17 × 文本重叠分
```

最终分还会加入：

- 跨方向桥接奖励，最高 `0.08`。
- 收藏偏好奖励，最高 `0.10`。
- 指数衰减的时效奖励，最高 `0.05`。
- 对明显跨领域噪声的有限降权。

关键词评分会：

- 提高标题命中的权重。
- 对更具体的多词短语给更高权重。
- 去除嵌套重复命中，例如同时配置 `motivic homotopy theory` 和 `homotopy theory` 时不会重复累计。

每日结果超过上限时，会对重复方向和近似标题施加软惩罚，在保持高相关性的同时增加覆盖面。

## 收藏与偏好

网页收藏保存在浏览器 `localStorage`，不会要求把 GitHub Token 暴露给静态网页。

后端偏好学习读取 `web/data/likes.json`。当其中至少有 3 篇收藏论文时，采集器会生成 `web/data/preferences.json`；下一次运行会把这些偏好用于评分。`likes.json` 和 `dislikes.json` 可由可信的私有自动化或手动流程更新，不能通过公开 GitHub Pages 安全写回仓库。

偏好学习使用“收藏论文中的文档频率”与“全部论文中的背景频率”对比，避免某个词在单篇摘要里重复出现就被误判为偏好。

## 数据与保留

- `web/data/papers.json`：页面使用的论文数据。
- `web/data/likes.json`：供后端学习使用的收藏 ID。
- `web/data/dislikes.json`：供后端过滤使用的“不感兴趣”论文 ID。
- `web/data/preferences.json`：学习到的偏好。
- `web/data/schedule-probe/`：GitHub Actions 07:00-08:00（北京时间）排队延迟探针结果。
- 收藏论文不会被存储裁剪删除；如果收藏数量本身超过上限，数据文件会保留收藏并报告 `storage_limit_exceeded_by_likes`。
- 当所有来源都失败时，已有论文数据会被保留。

分析 GitHub Actions 排队时间：

```bash
python3 scripts/analyze_schedule_probe.py
```

默认值：

| 变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `MAX_NEW_PAPERS` | `30` | 每次最多新增论文 |
| `MAX_STORED_PAPERS` | `150` | 最多保留论文 |
| `MAX_SUMMARIES` | `20` | 每次最多生成的模型摘要 |
| `MIN_PAPER_SCORE` | `0.10` | 有摘要论文最低分 |
| `MIN_TITLE_ONLY_SCORE` | `0.20` | 无摘要论文最低分 |
| `MIN_KEYWORD_MATCH_SCORE` | `0.10` | 即使命中关键词也必须达到的最低分 |
| `MIN_DAILY_PAPERS` | `8` | 当日不足时的回填目标 |
| `DAILY_BACKFILL_DAYS` | `14` | 回填最多回看天数 |
| `RECENT_HISTORY_DAYS` | `90` | 低相关历史论文保留窗口 |

## 论文来源

默认配置只启用 arXiv。可在 `sources` 中增加：

```json
[
  { "type": "openalex", "name": "OpenAlex" },
  { "type": "crossref", "name": "Crossref" },
  { "type": "feed", "name": "Journal Feed", "url": "https://example.com/feed.xml" }
]
```

Semantic Scholar 默认关闭，启用时同时设置：

```text
ENABLE_SEMANTIC_SCHOLAR=true
```

Google Scholar 需要 `SERPAPI_API_KEY`，来源类型为 `google_scholar_serpapi`。不要直接爬取 Google Scholar HTML。

私有 Feed 可通过 `headers_env` 或 `bearer_token_env` 引用 Actions Secret 对应的环境变量名。

## 本地运行

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

采集论文：

```bash
python3 scripts/collect_papers.py --days 7 --clear-cache
```

预览网页：

```bash
python3 -m http.server 8000 --directory web
```

访问 `http://localhost:8000`。

企业微信推送：

```bash
WECHAT_WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." \
python3 collect_and_push.py
```

## 关键文件

| 文件 | 作用 |
| --- | --- |
| `scripts/collect_papers.py` | 采集、评分、偏好学习、摘要和数据保留 |
| `scripts/wechat_bot.py` | 企业微信群机器人推送 |
| `.github/workflows/daily.yml` | 定时采集和 Pages 部署 |
| `web/app.js` | 前端筛选、排序、收藏和渲染 |
| `config/interests.json` | 默认研究方向 |
