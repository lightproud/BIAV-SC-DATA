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

## 收件箱通道（arca.live，T111，守密人 2026-10-03「方案 A」）

arca.live 对 GitHub Actions 机房 IP 返回 403，但 Claude 云会话容器带浏览器 UA 可访问，故采集改由每日云会话例程在容器里跑，
产出经「收件箱分支」交回数据仓，不产生每日 PR：

1. 例程：`git clone` 本仓 → `python projects/news/scripts/arca_live_collector.py --hours 36 --out arca/<YYYYMMDDTHHMM>.json`
   （只写该文件，不写 `Record/`；被拦或零条也写文件，被拦带 `"blocked": true`）→ 把该文件提交到孤儿分支 `inbox/arca` 并推送。
2. 入湖：`.github/workflows/arca-inbox-ingest.yml` 每 3 小时（或手动）拉 `origin/inbox/arca`，把 `arca/*.json` 交给
   `projects/news/scripts/ingest_inbox.py`；后者沿用 `archive_platforms.write_archive` / `item_key`，按条目自身日期（UTC+8）
   写 `Record/Community/arca_live/{date}.json`，与 collect_global 产出同构；已入湖文件登记在
   `projects/news/data/inbox_ingested.json`（文件名 -> sha256），重复跑幂等；提交到 main，加 `[skip ci]`。
3. 分支不存在时工作流正常退出；定时同样受仓库变量 `COLLECTION_ENABLED` 控制，手动触发不受限。
   `update-news.yml` 里的 `Arca.live` 采集器在 Actions 上仍会被拦并降级为空，属预期。

## Bluesky 与 Facebook 主页评论（守密人 2026-10-03 裁定：X 不付费，以 Bluesky 替代）

- **Bluesky**（源名 `bluesky`，`bluesky_collector.py`）：公开接口 `api.bsky.app` 的 `searchPosts`，免登录；
  `public.api.bsky.app` 对机房 IP 返回 403，不要用。关键词 `morimens` / `忘却前夜` / `忘卻前夜` / `モリメンス` / `망각전야`，
  归档位置 `Record/Community/bluesky/`。`morimens` 命中但无游戏线索词的帖标 `metadata.keyword_only`。稀疏源。
- **Facebook 官方主页**（源名 `facebook`，`facebook_page_collector.py`）：Graph API，取主页近期帖及其全部评论（含楼中楼）。
  归档位置 `Record/Community/facebook/`（评论与官方帖同档，官方帖标 `metadata.is_official_post=true`）。
  `FB_PAGE_ID` 或 `FB_PAGE_TOKEN` 任一缺失时采集器返回空并记「未配置，跳过」，不算失败。

### Facebook 主页令牌怎么拿

1. 在 Meta Business（business.facebook.com）的「商务设置 → 用户 → 系统用户」新建系统用户（角色选管理员），
   并在「添加资产」里把官方主页授给它，权限给「完全控制」。
2. 在 developers.facebook.com 建（或复用）一个商家类应用，关联到同一商务账户；应用需在
   「商务设置 → 账户 → 应用」里添加给该系统用户。
3. 在系统用户页点「生成令牌」，选该应用，令牌有效期选「永不过期」，勾选权限
   `pages_read_engagement`、`pages_read_user_content`，以及 `pages_show_list`。
4. 用该用户令牌调 `GET https://graph.facebook.com/v21.0/me/accounts?access_token=<用户令牌>`，
   返回里目标主页的 `access_token` 就是**长期主页令牌**，`id` 就是主页编号。
5. 存入数据仓（BIAV-SC-DATA）的 Actions 密钥 `FB_PAGE_TOKEN`；主页编号存为密钥或变量 `FB_PAGE_ID`
   （`update-news.yml` 的采集步骤已同时接受 `secrets.FB_PAGE_ID` 与 `vars.FB_PAGE_ID`）。
6. 令牌只经 `Authorization: Bearer` 头发送，不进日志。令牌失效或权限不足时采集器会抛错，该源出现在当轮「失败」清单里，
   此时按本节重新生成即可。Graph API 版本号常量在 `facebook_page_collector.GRAPH_VERSION`（当前 v21.0）。

## Reddit 应用怎么注册（评论采集，官方 OAuth API）

帖子采集（`fetch_reddit`，RSS / 公开 JSON）不需要凭据；**评论**在机房 IP 下评论 RSS 返回 429、`.json` 返回 403，只能走 Reddit
官方 OAuth API，所以需要守密人注册一个 script 类型应用（免费，一次性）：

1. 登录 reddit.com，打开 <https://www.reddit.com/prefs/apps>，点页面底部 **create app**（或 create another app）。
2. 类型选 **script**；name 随意（如 `biav-sc-news`）；redirect uri 填 `http://localhost:8080`（script 应用不真用它，但必填）。
3. 创建后记下两串：**client id** = 应用名称下方那串短字符（在 "personal use script" 字样下面）；**secret** = 标着 secret 的那一串。
4. 存入数据仓 BIAV-SC-DATA 的 Actions 密钥：`REDDIT_CLIENT_ID`、`REDDIT_CLIENT_SECRET`；可选 `REDDIT_USER_AGENT`
   （写你的 Reddit 用户名，或完整 UA `python:biav-sc-news:1.0 (by /u/<用户名>)`）。
5. 可选 `REDDIT_USERNAME` / `REDDIT_PASSWORD`（该应用所属账号，走 password grant）；不配则用 `client_credentials`（application-only），
   读公开子版块评论已够用。

采集器 `reddit_comments_collector.py`（源名 `reddit_comment`，归档 `Record/Community/reddit/global/comment/`）：id / secret 缺任一即整源
跳过（日志 info「未配置，跳过」，不算失败源）；额度 100 请求 / 分钟，按响应头 `X-Ratelimit-Remaining/Reset` 自适应，每轮请求总数、
每子版块帖子数、每帖 `morechildren` 次数均有上限。凭据只经环境变量读取，不入日志、不入归档。

B 站视频评论：采集器 `bilibili_comments_collector.py`（源名 `bilibili_comment`，归档 `Record/Community/bilibili/cn/comment/`），免登录免签名；
视频取本轮搜索结果 + 近 7 天已归档热门视频，评论按时间序增量，每轮请求上限 80、间隔 ≥1 秒，遇 -412 / -352 风控码即停本轮降级（保留已采部分）。

## 每日补充检索

已建立 ChatGPT 定时任务“忘却前夜新闻补充采集”，从 2026-10-02 起每天北京时间
早上约 08:00 执行（时间可在前后一小时内调整），首次执行尚待回执。
任务实际执行公开检索、核验和记录，而非只提醒用户手工采集。

检索范围：X 官方英日账号、贴吧、Facebook、QooApp、theqoo、Bluesky、Tumblr、
SomethingAwful、NGA、小红书，以及中英日韩俄语媒体遗漏。此清单是待复核范围，
不代表这些来源均已证实无法机械化或都已成功读取；找到可靠公开 RSS/API 后继续
实测接入。TapTap、arca_live 已于 2026-10-03 重启（arca_live 走上节收件箱通道）。

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

## 行为数据日快照（守密人 2026-10-03 批准）

供舆情系统做「言行对照」：玩家说了什么之外，再看他们做了什么。每天一次，抓**非发言类**热度指标，
存成时间序列。与 `store_patrol.py` 不重复——巡检盯 Steam 价格与评测总计的变更签名，本节记的是每日数值点。
像每天早上给各家店门口拍一张人流照片，而不是只在店招换了才记一笔。

- 脚本：`projects/news/scripts/behavior_snapshot.py`（标准库）；工作流 `.github/workflows/behavior-snapshot.yml`
  （北京时间 08:20 = UTC 00:20；受 `COLLECTION_ENABLED` 控制；手动触发可勾 `backfill`）。
- 落点：`Record/Behavior/<source>/<YYYY>.jsonl`，每行
  `{"date": "<UTC+8 日期>", "ts": "<UTC ISO>", "source": ..., "target": ..., "metrics": {...}}`。
  同一 `(date, source, target)` 重跑覆盖当天行，幂等。每源独立，一个失败只记 error（退出码 1），不影响其他源落盘。
- 请求纪律：浏览器 UA、超时 25 秒、失败重试一次、请求间隔 >= 1 秒，steamcharts 为 >= 3 秒。

| source / target | 指标（metrics 字段） | 说明 |
|---|---|---|
| `discord` / 邀请码 `morimens`（全球服）、`2hsGcAPcs9`（日服） | `member_count` `presence_count` `guild_id` | 邀请接口的近似总人数与近似在线数，无需 token |
| `steam` / `global`(3052450)、`jp`(4226130) | `current_players` `peak_24h_steamcharts` `yesterday_date/up/down` `reviews.{schinese,japanese,english,all}.total_positive/total_negative` | 当前在线；steamcharts 最近 24 小时在线峰值（无页面则缺）；评价直方图 `recent[]` 中标签为北京时间昨日那天的好评 / 差评（Steam 日桶按 UTC 日，是近似对齐，当日无评价时该日缺行则只记 warning）；分语言评价累计（须带 `filter=all&purchase_type=all`，否则 `num_per_page=0` 返回全 0） |
| `taptap` / `364992` | `rating_value`（满分 10）`rating_count`；`reserve_count` `fans_count` `wish_count` | 评分来自页面 JSON-LD；预约 / 粉丝 / 想玩来自 Nuxt `__NUXT_DATA__` 序列化数组里唯一含 `fans_count` 的统计对象，且其 `review_count` 必须等于 JSON-LD 的 `ratingCount`，否则只采 JSON-LD 并记 warning，不猜 |
| `appstore` / `global`(us)、`jp` | `average_rating` `rating_count` `version` `current_version_release_date` | iTunes lookup；global 用 us 区、jp 用 jp 区 |
| `google_play` / `global`(en-US)、`jp`(ja-JP) | `rating_value` `rating_count` `installs_tier` `package` | 详情页 JSON-LD 评分；安装档位取页面「Downloads / ダウンロード」前的档位文本（如 `100K+`、`10万+`），找不到则缺并记 warning |
| `youtube` / 频道 `UCF6iFnr28T4KjmVvPakmU3g`（日本版公式） | `videos[]`：`video_id` `published` `views` `star_count` | 官方 RSS 近 15 条视频的播放数与点赞星数（RSS 的 starRating count） |
| `fandom` / `morimens` | `pages` `articles` `edits` `activeusers` | MediaWiki `siteinfo` 统计 |
| `steamcharts` / `global`、`jp` | `players` `granularity`（`month`/`day`/`hour`）`app_id` | **仅回填**：`--backfill-steamcharts` 把 chart-data.json 全部历史原样写入（按点的 UTC+8 日期分年）；粒度由相邻点间隔判定；按 `(ts, source, target)` 去重，幂等 |

注意：Discord / Steam / TapTap 等数字是各平台公开页面的近似值与累计值，用于趋势对照，不当精确账本用。
