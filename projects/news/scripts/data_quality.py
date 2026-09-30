#!/usr/bin/env python3
"""
数据质量增强模块

包含三个核心改进：
1. engagement 归一化 - 统一不同平台的互动数口径
2. 沉默平台降级 - 自动追踪并降级长期无数据的平台
3. 数据源健康监控 - 生成数据源健康报告

Usage:
    python scripts/data_quality.py --report     # 生成健康报告
    python scripts/data_quality.py --normalize  # 归一化 engagement
"""

import json
import logging
import sys
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import news_common  # 落盘脱敏单一真源（H3）
import archive_layout  # 日期基准单一真相源（北京日期）

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
HEALTH_PATH = archive_layout.news_state_root() / 'source-health.json'  # 跨轮状态（进 git）

# ============================================================
# 1. Engagement 归一化
# ============================================================

# 不同平台的 engagement 含义不同，需要归一化
# 归一化策略：转换为"等效互动分"
ENGAGEMENT_WEIGHTS = {
    # 视频平台 - 播放量权重低，互动权重高
    'bilibili': {'view_weight': 0.001, 'interact_weight': 1.0},
    'youtube': {'view_weight': 0.0001, 'interact_weight': 1.0},

    # 社区平台 - 直接互动
    'reddit': {'weight': 1.0},  # score = upvotes - downvotes
    'discord': {'weight': 1.0},  # 自定义 engagement score
    'steam_review': {'weight': 1.0},  # votes_up

    # 社交平台 - 转发评论权重高
    'weibo': {'repost_weight': 3.0, 'comment_weight': 2.0, 'like_weight': 1.0},
    'twitter': {'repost_weight': 3.0, 'reply_weight': 2.0, 'like_weight': 1.0},

    # 游戏社区
    'xiaohongshu': {'weight': 1.0},

    # 默认
    'default': {'weight': 1.0},
}

# 各平台热门阈值（归一化后）
HOT_THRESHOLDS = {
    'bilibili': 100,      # 等效互动分 > 100
    'youtube': 500,
    'reddit': 50,
    'discord': 10,
    'steam_review': 5,
    'weibo': 100,
    'twitter': 100,
    'xiaohongshu': 50,
    'default': 50,
}


def normalize_engagement(item: dict) -> float:
    """
    归一化 engagement 为等效互动分。

    对于有详细互动数据的平台，使用加权公式：
    - bilibili: play * 0.001 + (like + coin * 2 + favorite * 2 + share * 3)
    - weibo: repost * 3 + comment * 2 + like

    对于只有单一 engagement 数值的平台，直接使用。
    """
    source = item.get('source', 'default')
    engagement = item.get('engagement', 0)
    metadata = item.get('metadata', {})

    weights = ENGAGEMENT_WEIGHTS.get(source, ENGAGEMENT_WEIGHTS['default'])

    if source == 'bilibili':
        # B站: 播放量 × 0.001 + (点赞 + 投币×2 + 收藏×2 + 分享×3)
        view = metadata.get('play', engagement) or engagement
        like = metadata.get('like', 0) or 0
        coin = metadata.get('coin', 0) or 0
        favorite = metadata.get('favorite', 0) or 0
        share = metadata.get('share', 0) or 0

        # 如果只有 engagement，假设是播放量
        if not any([like, coin, favorite, share]):
            return view * weights['view_weight']

        return (view * weights['view_weight'] +
                like + coin * 2 + favorite * 2 + share * 3)

    if source == 'weibo':
        repost = metadata.get('reposts_count', 0) or 0
        comment = metadata.get('comments_count', 0) or 0
        like = metadata.get('attitudes_count', engagement) or engagement

        return (repost * weights['repost_weight'] +
                comment * weights['comment_weight'] +
                like * weights['like_weight'])

    if source == 'youtube':
        # YouTube: views × 0.0001 + (like + comment)
        view = metadata.get('viewCount', engagement) or engagement
        like = metadata.get('likeCount', 0) or 0
        comment = metadata.get('commentCount', 0) or 0

        return (view * weights['view_weight'] +
                like * weights['interact_weight'] +
                comment * weights['interact_weight'])

    # 其他平台直接使用 engagement
    return engagement * weights.get('weight', 1.0)


def is_hot_normalized(item: dict) -> bool:
    """判断是否热门（基于归一化 engagement）。"""
    source = item.get('source', 'default')
    threshold = HOT_THRESHOLDS.get(source, HOT_THRESHOLDS['default'])
    normalized = normalize_engagement(item)
    return normalized >= threshold


# ============================================================
# 2. 沉默平台追踪
# ============================================================

class SilentPlatformTracker:
    """追踪长期无数据的平台，自动降级监控频率。"""

    # 平台监控等级
    LEVEL_ACTIVE = 'active'       # 正常监控
    LEVEL_DEGRADED = 'degraded'   # 降级监控（降低频率）
    LEVEL_DORMANT = 'dormant'     # 休眠（暂停监控）

    # 降级阈值（连续沉默天数）
    DEGRADED_THRESHOLD = 7    # 7天无数据降级
    DORMANT_THRESHOLD = 30    # 30天无数据休眠

    def __init__(self, health_path: Path = HEALTH_PATH):
        self.health_path = health_path
        self.health_data = self._load_health()

    def _load_health(self) -> dict:
        """加载健康数据。损坏档不许把整个追踪器炸掉——回落空表 + 响亮告警。

        原实现直接 `json.load`：半截 JSON 让**构造函数**抛 JSONDecodeError,
        而两个调用方一个 `except ImportError`、一个 `except Exception: debug`,
        于是 tracker 永远是 None——沉默追踪整个哑掉，且没人再写这个文件、
        坏档会被工作流 `git add output/` 提交进仓库长期驻留（自愈路径为零）。
        """
        if self.health_path.exists():
            try:
                with open(self.health_path, encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get('platforms'), dict):
                    return data
                logger.warning(f'source-health 结构异常（回落空表）: {self.health_path}')
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning(
                    f'source-health 读取失败（回落空表，沉默天数计数将从零重建）: '
                    f'{type(exc).__name__}: {exc}'
                )
        return {
            'updated_at': None,
            'platforms': {},
        }

    def _save_health(self):
        """保存健康数据（原子替换，见 news_common.dump_json_atomic）。

        本档每更新一个平台就落一次盘（一轮采集十几次），直写在中途被杀即留下
        半截 JSON —— 见 _load_health 里那条「坏档 = 追踪器永久哑掉」的链路。
        """
        self.health_data['updated_at'] = datetime.now(UTC).isoformat()
        news_common.dump_json_atomic(self.health_path, self.health_data)

    def update_platform_status(self, platform: str, items_count: int,
                               error: str | None = None, note: str | None = None):
        """
        更新平台状态。

        Args:
            platform: 平台名称
            items_count: 本次采集到的条目数
            error: 如果失败，错误信息
            note: 降级原因标注（如「待配 NGA_COOKIE」），恢复产出后自动清除
        """
        # 「今天」取北京日期：source-health.json 的同名字段另有一个写方
        # （silent_sources_audit.write_health，last_success_date 直接取**北京**归档桶名、
        # consecutive_silent_days 也按北京 today 算）。原 datetime.now(UTC) 让同一字段
        # 被两套时钟轮流覆写：北京 00:00–08:00 这 8 小时里 UTC 日期比桶名早一天，
        # 于是「今天已成功」的守卫对不上（last_success_date 永不等于 today），
        # 静默天数被多记一天，报表里的 last_success 也随最后写方在两个日期间跳。
        today = archive_layout.archive_date_str()

        if platform not in self.health_data['platforms']:
            self.health_data['platforms'][platform] = {
                'level': self.LEVEL_ACTIVE,
                'last_success_date': None,
                'last_check_date': None,
                'consecutive_silent_days': 0,
                'total_items': 0,
                'errors': [],
            }

        p = self.health_data['platforms'][platform]

        if items_count > 0:
            p['last_success_date'] = today
            p['last_check_date'] = today
            p['consecutive_silent_days'] = 0
            p['total_items'] += items_count
            p['level'] = self.LEVEL_ACTIVE
        else:
            # 无数据：每天最多累加 1（aggregator 每小时跑一次，不能按次累计）
            if p.get('last_check_date') != today and p.get('last_success_date') != today:
                p['consecutive_silent_days'] += 1
            p['last_check_date'] = today

            # 检查是否需要降级
            if p['consecutive_silent_days'] >= self.DORMANT_THRESHOLD:
                p['level'] = self.LEVEL_DORMANT
            elif p['consecutive_silent_days'] >= self.DEGRADED_THRESHOLD:
                p['level'] = self.LEVEL_DEGRADED

        # 降级原因标注：恢复产出后清除，否则记录调用方给的 note（如「待配 cookie」）
        if items_count > 0:
            p.pop('note', None)
        elif note:
            p['note'] = note

        if error:
            p['errors'].append({
                'date': today,
                # H3: source-health.json 会提交进 git，错误文本先脱敏再截断
                'error': news_common.redact_secrets(error)[:200],
            })
            # 只保留最近 10 条错误
            p['errors'] = p['errors'][-10:]

        self._save_health()

    def get_platform_level(self, platform: str) -> str:
        """获取平台监控等级。"""
        p = self.health_data['platforms'].get(platform, {})
        return p.get('level', self.LEVEL_ACTIVE)

    def should_skip_platform(self, platform: str) -> bool:
        """判断是否应该跳过该平台（休眠状态）。

        休眠源每天保留一次探测机会（当日未检查过则放行），否则源一旦
        dormant 就永远不再尝试，上游恢复也无法被发现（telegram 死锁教训）。
        """
        p = self.health_data['platforms'].get(platform, {})
        if p.get('level', self.LEVEL_ACTIVE) != self.LEVEL_DORMANT:
            return False
        today = archive_layout.archive_date_str()  # 与写方 update_platform_status 同基准
        return p.get('last_check_date') == today

    def get_report(self) -> dict:
        """生成健康报告。"""
        now = datetime.now(UTC)

        active = []
        degraded = []
        dormant = []

        for platform, data in self.health_data.get('platforms', {}).items():
            level = data.get('level', self.LEVEL_ACTIVE)
            silent_days = data.get('consecutive_silent_days', 0)
            total = data.get('total_items', 0)
            last_success = data.get('last_success_date', 'never')

            entry = {
                'platform': platform,
                'level': level,
                'silent_days': silent_days,
                'total_items': total,
                'last_success': last_success,
            }

            if level == self.LEVEL_ACTIVE:
                active.append(entry)
            elif level == self.LEVEL_DEGRADED:
                degraded.append(entry)
            else:
                dormant.append(entry)

        return {
            'generated_at': now.isoformat(),
            'summary': {
                'active_count': len(active),
                'degraded_count': len(degraded),
                'dormant_count': len(dormant),
            },
            'active_platforms': active,
            'degraded_platforms': degraded,
            'dormant_platforms': dormant,
        }


# ============================================================
# 3. 数据源健康报告
# ============================================================

def generate_health_report() -> dict:
    """
    生成数据源健康报告。

    数据源 = source-health.json（沉默追踪数据）。原第二源 all-latest.json 随
    2026-08-21 输出展示层删除不再产出，其补充分支已于 2026-09-27 移除。
    """
    tracker = SilentPlatformTracker()
    report = tracker.get_report()

    # 添加建议
    recommendations = []

    for p in report.get('dormant_platforms', []):
        recommendations.append({
            'platform': p['platform'],
            'action': 'skip',
            'reason': f"已沉默 {p['silent_days']} 天，建议暂停监控节省资源",
        })

    for p in report.get('degraded_platforms', []):
        recommendations.append({
            'platform': p['platform'],
            'action': 'investigate',
            'reason': f"已沉默 {p['silent_days']} 天，建议检查 API/爬虫状态",
        })

    report['recommendations'] = recommendations

    return report


def print_health_report():
    """打印健康报告到控制台。"""
    report = generate_health_report()

    print("\n" + "=" * 60)
    print("数据源健康报告")
    print("=" * 60)

    summary = report.get('summary', {})
    print(f"\n总览: {summary.get('active_count', 0)} 活跃 / "
          f"{summary.get('degraded_count', 0)} 降级 / "
          f"{summary.get('dormant_count', 0)} 休眠")

    if report.get('recommendations'):
        print("\n建议:")
        for rec in report['recommendations']:
            print(f"  - [{rec['platform']}] {rec['action']}: {rec['reason']}")

    print("\n" + "=" * 60)


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    import sys

    if '--report' in sys.argv or len(sys.argv) == 1:
        print_health_report()
    elif '--normalize' in sys.argv:
        # 从 stdin 读取 JSON，归一化后输出
        data = json.load(sys.stdin)
        if 'items' in data:
            for item in data['items']:
                item['normalized_engagement'] = normalize_engagement(item)
                item['is_hot_normalized'] = is_hot_normalized(item)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print("Usage: python data_quality.py --report | --normalize")
