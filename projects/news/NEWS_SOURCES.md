# 《忘却前夜》新闻采集覆盖与补充例程

## 机械采集

主管线 `.github/workflows/update-news.yml` 每 3 小时运行；定时启用条件沿用
`COLLECTION_ENABLED=true`。2026-10-01 03:02 UTC 的定时运行成功，未修改开关。

| 来源 | 公开入口 | 归档位置 | 实测边界 |
|---|---|---|---|
| PR TIMES 日本发行方 | 公司 53906 的 RSS + 正文（PR #16） | `Record/Community/prtimes/jp/news/` | 两篇 9 月公告已归档；后续沿用现有采集器 |
| 4Gamer | Android 游戏 G091980 和 PC 游戏 G098902 的专属 RSS | `Record/Community/four_gamer/jp/news/` | 两路 HTTP 200，跨平台按 URL 去重；移动端 6 条、PC 3 条，最新相关发布日期为 2026-06-02 |

4Gamer 只保存 RSS 标题、链接、真实带时区发布日期及来源元数据，不抓文章正文。
新闻日期与抓取日期分别保存；互动指标未知明确标记。默认稀疏窗口 30 天，旧新闻
不会反复报为新增，首次接入也不会把历史新闻改写成今天发布。
HTTP/XML 错误向编排抛出，不用空列表掩盖故障；正常无新发布允许空结果。
来源已登记为稀疏源、日期归档源及 T1 模块，进入现有审计。

## 每日补充检索

已建立 ChatGPT 定时任务“忘却前夜新闻补充采集”，从 2026-10-02 起每天北京时间
早上约 08:00 执行（时间可在前后一小时内调整），首次执行尚待回执。
任务实际执行公开检索、核验和记录，而非只提醒用户手工采集。

检索范围：X 官方英日账号、贴吧、Facebook、QooApp、theqoo、Bluesky、Tumblr、
SomethingAwful、NGA、小红书，以及中英日韩俄语媒体遗漏。此清单是待复核范围，
不代表这些来源均已证实无法机械化或都已成功读取；找到可靠公开 RSS/API 后继续
实测接入。TapTap、arca_live 沿用退役决定，不恢复。

检查结果落 `Record/NewsDiscovery/YYYY-MM-DD.json`，同日追加、不覆盖历史；包括
来源访问状态、核验依据与新增条目，按原始 URL 跨日去重。使用搜索摘要/镜像的条目
保持 `lead_only`，访问墙保持 `blocked`，缺失发布日期写 null，不能算作已采到原文。
`no_new` 仅描述本次公开检索没有新发现，不代表平台零声量。记录是检索回执，
不混入已核验的 `Record/Community/` 内容归档。提交限本记录并加 `[skip ci]`。

仅有价值的新消息或连续访问/入库故障时通知；避开已有美术更新任务的重复提醒。
仓库写入失败要报告原因并在任务回复保留结果，不宣称归档成功。


### 沉默告警的公开源日期证据

`silent_sources_audit.py --write --check-source-freshness` 对沉默的
`steam/global/news`、`appstore/jp` 请求公开 API，并将核对时间、来源 URL、
最新发布日期和归档日期写入 `source-health.json` 的 `source_date_checks`。
日期统一按北京时间比较；默认不传该参数时不联网。

- `newer_source_date`：源站有比最后归档日更晚的发布日期，需要排查采集。
- `no_newer_source_date`：本次接口结果未发现更晚日期，不证明内容已全部归档。
- `unverified`：接口失败、响应无效、没有评论/公告或该叶尚无核对器。

核对不改变核心源 never/dormant 或单源校验丢弃 ≥50 条的门控判据，
也不跳过采集或关闭告警。像查看店铺最后上新日期：能帮助解释店里为何安静，
但不能证明库存账本没有漏记。Steam 公告的健康键是 `official`，评论是 `steam`，
与归档来源及健康登记保持一致。
