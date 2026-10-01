# BIAV-SC-DATA

忘却前夜（Morimens）社区公开信息数据仓：各平台社区讨论的全量归档，以及产出这些归档的采集器。

## 目录

| 路径 | 内容 |
|---|---|
| `Record/Community/` | 社区全量档案（Discord 三区服 + 其余平台），按日期分档；上上个月及更早压成 `.gz` 冷层 |
| `Record/media/` | 媒体回填台账（二进制本体在 Releases） |
| `Record/store-patrol/` | 应用商店页面每日快照与变更日志 |
| `projects/news/scripts/` | 采集、归档、回填、冷热压缩脚本（2026-09-29 自私有仓 BIAV-SC-CODE 迁入） |
| `projects/news/data/` | 采集跨轮状态（源健康度、游标等） |
| `.github/workflows/` | 定时采集工作流 |

## 采集工作流

2026-09-29 起采集在本仓运行（公开仓 Actions 免费）。定时任务只在仓库变量 `COLLECTION_ENABLED` 为 `true` 时生效，
未开启前只能手动触发，用于验证。

所需 Secrets（Settings → Secrets and variables → Actions）。写本仓数据与 Release 一律用内置 `GITHUB_TOKEN`，不需要个人令牌：

| 类别 | Secret | 用途 |
|---|---|---|
| 必配 | `DISCORD_BOT_TOKEN` | Discord 三区服归档、历史回填、同人图、媒体链接刷新 |
| 必配 | `YOUTUBE_API_KEY` | YouTube 视频与评论 |
| 可选 | `WEIBO_COOKIE` | 微博不配也能采，配了成功率更高 |

工作流里还引用了其他平台的 Secrets（推特、Facebook、Instagram、Twitch、Telegram、小红书、抖音、知乎、NGA、Naver、QQ、DC 等），
这些平台 2026-09-29 时均无产出，不必配置；未配置的平台采集器会跳过，不会中断整轮采集。

本仓公开，Actions 运行日志任何人可见；工作流步骤不得打印凭据。

## Steam 讨论时间字段

新增讨论条目分别记录 `created_at`（发帖时间，当前列表页无法可靠取得，记为
`null`）、`last_reply_at`（最后回复时间，缺失记为 `null`）和 `fetched_at`
（本页采集时间，UTC ISO 8601）。不得用回复或采集时间补造发帖时间。

`time` 保留旧消费者需要的活动时间：有最后回复时间时取 `last_reply_at`，否则
取 `fetched_at`，同时保留 `time_is_approximate=true`。`time_basis` 明确记录
`last_reply_at` 或 `fetched_at`；`time` **不是发帖时间**。验证和格式转换透传
这些字段。旧归档无需迁移；没有 `time_basis` 的历史条目不能据 `time` 推断发帖时间。

滚动窗口和北京日期分桶仍使用兼容字段 `time`，桶日期是回复/采集日期，
不是创建日期。采集端仍按最后回复时间过滤。去重仍优先使用帖子 URL；
跨日期的活动记录仍可能在不同日期桶中出现。同日归档合并保留先前版本，
不回填旧条目的未知时间字段。


## PR TIMES 日本官方新闻

`prtimes` 通过日本发行方株式会社オルトプラス的公开 RSS（公司 ID 53906）发现
《忘却前夜》新闻，读取正文和官方图片链接，接入现有 `collect_global` 三小时采集。
默认使用稀疏源 30 天窗口；更大的 `HOURS_LOOKBACK` 可用于补采。仅收本游戏标题、
该公司的 PR TIMES 文章 URL 和真实带时区发布日期，不用采集时间补造发布时间。
归档位置为 `Record/Community/prtimes/jp/news/YYYY-MM-DD.json`，按北京发布日期分桶、
按 URL 去重，并纳入现有来源健康审计。

正文读取失败时保留 RSS 摘要，并设置 `metadata.summary_is_excerpt=true` 和
`article_fetch_failed=true`；互动数未知由 `engagement_is_unknown=true` 标记，
数字 `engagement=0` 仅兼容现有排序。全部正文图片链接保留在 `metadata.media_urls`；
现有媒体下载器自动处理首图 `media_url`，其余图片本轮仅保存链接。

2026-10-01 在线验收已采到 9 月 3 日和 9 月 17 日两篇公告的正文与 27 个图片链接，
首次归档随本次接入一并提交。来源复核见
[采集复核记录](Record/collection-coverage-2026-10-01.md)。
