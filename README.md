# Product Hunt 每周 Top 10 解读器

每周一抓取 Product Hunt 上一整周（周一到周日，洛杉矶时区）的周榜前 10 名，用 DeepSeek 写成零基础也能看懂的中文解读，自动质检后输出到 [reports/](reports/index.md)。

## 本地运行

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python main.py
```

在 `.env` 里填两个值（该文件已被 git 忽略，不要提交）：

| 变量 | 从哪来 |
|---|---|
| `PH_TOKEN` | Product Hunt → API Dashboard → 你的应用 → Developer Token |
| `DEEPSEEK_API_KEY` | platform.deepseek.com → API keys（账户需有余额） |

参数：`--limit N` 只试跑前 N 个并打印费用；`--week YYYY-MM-DD` 补跑该日期所在的周；`--date YYYY-MM-DD` 跑某一天的日榜；`--dry-run` 只抓数据；`--skip-review` 跳过 LLM 审稿。
测试：`.venv\Scripts\python -m pytest`

## GitHub Actions

仓库 Settings → Secrets and variables → Actions → New repository secret，添加 `PH_TOKEN` 和 `DEEPSEEK_API_KEY`。
`GITHUB_TOKEN` 由 Actions 自动提供，不用配置。工作流在 [weekly.yml](.github/workflows/weekly.yml)，每周一 UTC 10:17（北京 18:17）运行，也可在 Actions 页手动运行并填该周任意一天。

注意：
- GitHub 定时任务可能晚几分钟到几十分钟。
- 工作日 UTC 01–04、06–10 点是 DeepSeek 高峰价（平时的 2 倍），改时间时避开。
- 公开仓库 60 天没有活动，GitHub 会停掉定时任务；每周有新提交就不会。

## 修改

- 练手项目的数据集：`datasets.txt`。每期先由模型统一分配“数据集 + 方法”（`prompts/plan.md`），程序保证同一期数据集不重复、同一方法最多 2 次、不与上一期重复（记录在 `data/datasets_used.json`）。
- 费用上限：`config.yaml` 的 `max_usd_per_run`，超过就停。2026-11-25 平价时段实测每次约 $0.04–0.07（¥0.3–0.5）。
- prompt：直接改 `prompts/*.md`。`{xxx}` 占位符要保留；六个二级标题如要改，同步改 `qa.py` 的 `SECTIONS`。
- 质检阈值、模型、并发、重试、价格：`config.yaml`。
- 术语表：`glossary.txt`，每行 `术语 | 解释`。

## 排名怎么来的

**周榜**：`order: VOTES` 加 `featured: true` 取整周前 20，再按官方字段 `weeklyRank` 排序。多天范围用 RANKING 会先返回最后一天的产品，不能直接用；名次不连续时报告会注明。

**日榜**（`--date`）：用 API 的 `order: RANKING` 加 `featured: true`，再按官方字段 `dailyRank` 排序。2026-11-25 对 11-21 至 11-23 三天做了比对：

- RANKING 去掉未精选产品后，前 10 与 `dailyRank` 1–10 逐条一致。
- `order: VOTES` 与 `dailyRank` 不完全一致（11-23 和 11-22 的第 6、7 名票数与名次相反），说明最终排名不等于票数排序，所以没有用票数。
- 不加 `featured: true` 时，RANKING 会把一个 0 票的未精选产品排在最前。
- `postedBefore` 是闭区间，传次日零点会混进次日产品，所以减 1 秒，并在本地用 `featuredAt` 再过滤一次。

没有做到的：排行榜网页有 Cloudflare 人机验证，没有绕过，因此**没有与网页逐条对照**，依据是官方 `dailyRank` 字段。你可以打开 `https://www.producthunt.com/leaderboard/daily/2026/11/23` 自行核对一次。

## 已知限制

- API 返回的官网链接是 producthunt.com 的跳转链接，同样有人机验证，程序不绕过。只有当 maker 评论或描述里出现与产品名吻合的域名时才能抓到官网正文，其余跳过并在报告里注明。
- 质检失败的链接分两类：404/5xx/超时算失效；401/403/429 表示网站拒绝程序访问，只提示“请自行确认”，不算失效，以免误判。
- 审稿用 flash + thinking（省钱；换回 pro 改 `review_model`）。开 thinking 时，DeepSeek 会忽略 `temperature`（仍按 ≤ 0.5 传）。设 `review_thinking: false` 可让它生效，但核查质量明显下降。
- 术语若仍未解释，程序会用 `glossary.txt` 的解释补在首次出现处，次数列在质检摘要的“自动补解释”。
- RSS 兜底没有票数、评论和最终排名，报告会标注数据不完整。

## 结构

`fetch.py` 抓数据 · `llm.py` 调 DeepSeek · `analyze.py` 写稿与修订 · `qa.py` 质检 · `render.py` 渲染 · `main.py` 入口 · `data/raw` 原始数据 · `data/qa` 质检结果 · `data/seen.json` 去重记录 · `logs/` 本地日志（不提交）
