#!/usr/bin/env python3
"""
sources.py — 采集源单一真相源（single source of truth）

历史上 split_output.py / archive_platforms.py / silent_sources_audit.py /
collect_global.py 各自维护一份源清单，长期漂移，导致「采了不归档」「归档了不审计」
的盲区。本模块集中定义全部源清单，上述脚本统一 import，杜绝漂移。

清单语义:
  KNOWN_SOURCES     —— 当前在产线采集的活跃源（原 split_output 据此分 *-latest.json；split_output 已随 2026-08-22 新闻流退役删除）
  SOURCE_ALIASES    —— 原始源名 → 规范源名（归一化）
  SPARSE_SOURCES    —— 稀疏源（评论 / 公告 / 同人），使用更宽时间窗口
  CORE_SOURCES      —— 主管线核心源，长期 0 产出视为故障（健康门控）
  ARCHIVE_PLATFORMS —— 需按日归档到 data/platforms/ 的源（= KNOWN - discord）
  BACKFILL_PLATFORMS—— backfill_platforms.py 实际支持回溯的源
  LEGACY_SOURCES    —— data/platforms/ 下仍有历史归档、但采集逻辑已移除的源
"""

# 当前在产线采集的活跃源（与 split_output 历史 KNOWN_SOURCES 对齐）
KNOWN_SOURCES = [
    'bilibili',
    'steam',
    'discord',
    'youtube',
    'reddit',
    'official',
    'steam_discussion',
    # 全球扩展平台
    'weibo',
    'bahamut',
    'appstore',
    'google_play',
    'pixiv',
    # 日语扩展
    'note_com',
    'prtimes',
    'four_gamer',
    # 韩语扩展
    'ruliweb',
    'arca_live',   # 守密人 2026-10-03 裁定重启
    # 俄语平台
    'stopgame',
    # 中文补充
    'weixin',
]
# twitter 已摘除（守密人 2026-07-30 裁定，归档完整性审计待裁项④）：挂名 1,126 天审计窗口
# 零产出、归档目录从未存在（syndication 接口 API 墙）。采集器 fetch_twitter 保留在
# global_collectors 但不再入 collect_global 编排；未来要采集须重新登记并接回编排。
#
# arca_live 曾于 2026-08-16 摘除（Cloudflare 封 Actions 机房 IP）、2026-09-30 采集器整体删除；
# **守密人 2026-10-03 裁定重启**（推翻删除裁定）：容器机房 IP 实测带桌面浏览器 UA 的列表页 / 帖子页均 200，
# 采集器重写为 arca_live_collector.py（列表 + 首帖正文补抓，挑战页识别降级），回归正常源（已出遗留源）。
# 历史归档 Record/Community/arca_live/（2026-07-01~07-11）只读，新产出同构落回同目录。
# 风险：Actions 运行机 IP 段仍可能被拦——被拦时列表返回空、不拖垮整轮，由静默源审计告警。
#
# taptap 族（taptap / taptap_review / taptap_post）采集器整体删除（守密人 2026-09-30 裁定）：
#   自 2026-08-25 无成功——taptap_collector.py / fetch_taptap / fetch_taptap_playwright / backfill_taptap 均删。
# 历史归档 Record/Community/taptap/ 保留不动；archive_layout 的 taptap
# 读侧布局映射（taptap_review 折叠认领 review/ 子目录）保留，读方照常可读历史档。
# taptap 归档目录在静默源审计里归入「遗留源（有归档但未注册采集）」只读展示，不再告警。

# 原始源名 → 规范源名
SOURCE_ALIASES = {
    'bilibili_articles': 'bilibili',
    'bilibili_dynamic': 'bilibili',
    'steam_review': 'steam',
}

# 稀疏源（split_output + collect_global 历史两份清单的并集）
SPARSE_SOURCES = {
    'official', 'prtimes', 'four_gamer',
    'appstore', 'google_play',
    'weixin',
    'pixiv',
    'stopgame',
    'note_com', 'ruliweb', 'bahamut', 'arca_live',   # taptap 族已删（2026-09-30）
    'discord',
}

# 主管线核心源（aggregator.py 直采）。长期 0 产出 = 采集故障，健康门控据此告警。
CORE_SOURCES = [
    'reddit', 'bilibili',
    'steam', 'official', 'youtube', 'discord',
]

# §4.2 R1 硬失败源：本次运行中崩溃且未被 fallback 救回即令整次失败（aggregator.py 据此）。
# 严格子集，区别于 CORE_SOURCES（长期健康门控）：这两个是「单次跑必须有的命脉源」，
# 不含 youtube/discord 等可因 AUTH_GATED 缺 cookie 而预期降级的源（taptap 2026-09-30 随源删除移出）。
R1_HARD_FAIL_SOURCES = {'reddit', 'bilibili'}

# 需登录态 cookie / API key 才能采集的源 → 所需环境变量名（单一真相源）。
# 未配置对应 secret 时：该源 0 产出属预期降级（标注「待配」，不计采集故障）；
# 已配置 secret 仍 0 产出：才视为真故障。collect_global 据此区分「待配 cookie」与「核心源静默失败」。
AUTH_GATED = {
    'youtube': 'YOUTUBE_API_KEY',
    'discord': 'DISCORD_BOT_TOKEN',
}

# Discord 有独立归档器（discord_archiver.py），不走 archive_platforms 的按日归档
ARCHIVE_PLATFORMS = [s for s in KNOWN_SOURCES if s != 'discord']

# backfill_platforms.py 的 PLATFORM_BACKFILLERS 实际支持的源（务必与之同步）
# taptap 回填器已随源删除（2026-09-30）；arca_live 重启（2026-10-03）暂无回填器。
BACKFILL_PLATFORMS = [
    'bilibili', 'appstore', 'steam_review',
    'pixiv', 'ruliweb', 'weixin',
]

# 独立归档源：由专用采集器直写 Public-Info-Pool/Record/Community/，不经主线 news.json
# （故不进 KNOWN_SOURCES——历史原因：原 split_output 会按 news.json 切出空 latest 文件）。
# 活跃采集中，需纳入静默源审计的正常分级（见 silent_sources_audit.ALL_REGISTERED_SOURCES）。
INDEPENDENT_ARCHIVE_SOURCES = [
    'youtube_comments',  # collect_video_comments.py 直写 Record/Community/youtube_comments/
]

# data/platforms/ 下仍有历史归档、但采集逻辑已移除的遗留源。
# 不再产出新数据，仅供审计可见（避免被静默源审计无视）。
LEGACY_SOURCES = [
    'taptap_post',
]

# ─── 区服 app 标识：单一真相源（2026-06-21 命名规范 grilling 定案） ──────────
# 忘却前夜日本版 = AltPlus Inc. 独立发行，各平台为独立 app/账号 → 按规范拆 jp/ 区服。
# 准则：不同 appid / 独立运营才拆区服目录；同 appid 多国仍走 platform_region 字段、不拆。
#   global = 国际版（LingXi / qookkagames 系）；jp = 日本版（AltPlus）。
# 归档结构：<平台>/<区服>/<类型>/YYYY-MM-DD.json（区服上、类型下，详见 projects/news/CONTEXT.md）。
REGION_APPS = {
    'steam':       {'global': '3052450',                  'jp': '4226130'},
    'appstore':    {'global': '6447354150',               'jp': '6743462069'},
    'google_play': {'global': 'com.qookkagames.z1.gp.hk', 'jp': 'jp.co.altplus.boukyakuzenya'},
    # twitter 条目已随源摘除迁出（2026-07-30）：句柄→区服映射内聚 global_collectors._TWITTER_REGION_BY_HANDLE
    # YouTube：global 存 @handle（采集前需 channels.list?forHandle 转 channelId）；
    # jp 已解析为 channelId（频道「忘却前夜【日本版公式】」，2026-06-22 网络检索 + 频道页标题核验）。
    'youtube':     {'global': '@morimensofficial',        'jp': 'UCF6iFnr28T4KjmVvPakmU3g'},
}

# Discord 三服务器 → 区服（global/jp/volunteer）。归档至 discord/<区服>/<channel_id>/。
DISCORD_GUILDS = {
    'global':    '1131791637933199470',  # Morimens 官方全球服
    'jp':        '1377475512716234902',  # 【公式】忘却前夜サーバー（日本官方）
    'volunteer': '1402537664619479100',  # Morimens Volunteer Translators
}


def normalize_source(raw: str) -> str:
    """原始源名归一化为规范源名。"""
    return SOURCE_ALIASES.get(raw, raw)


# 归档平台折叠（甲方案 2026-06-21 命名规范）：steam 家族三子类（评价/公告/讨论）共享
# 归档 platform 段 'steam'，由 item 的 archive_subtype 区分 review/news/discussion，
# 落 steam/<区服>/<类型>/。仅作用于**数据归档层**（archive_platforms）。（原「不动 split_output 的
# *-latest.json 文件名」约束随 2026-08-21 输出展示层删除 / 08-22 split_output 退役失效。）
# steam_review 经 SOURCE_ALIASES 已归一为 steam；official=Steam 官方公告（决策项⑦ → steam/global/news）。
ARCHIVE_PLATFORM_FOLD = {
    'official':         'steam',  # Steam 官方公告 → steam/<区服>/news
    'steam_discussion': 'steam',  # Steam 社区讨论 → steam/<区服>/discussion
}


def archive_platform(raw: str) -> str:
    """归档 platform 段：先 normalize_source，再按 steam 家族折叠（仅归档层用）。"""
    s = normalize_source(raw)
    return ARCHIVE_PLATFORM_FOLD.get(s, s)

