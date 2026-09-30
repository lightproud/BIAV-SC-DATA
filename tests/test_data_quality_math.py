"""Logic tests for data_quality's engagement/threshold math.

Asserts the per-platform weighted-sum formulas and the >= hot threshold with
exact expected numbers — operator/constant logic that line coverage alone (the
module is at 100% lines) does NOT pin: a flipped weight or a `>=`->`>` would
keep coverage green but break these assertions. Imports via the package path so
the assertions bind the same module object the rest of the suite uses.
(data_quality is intentionally not under the mutmut gate — its sibling imports +
class/IO make whole-file mutation noisy; see setup.cfg — so these explicit value
assertions are the standing guard.)
"""
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
# repo root -> resolves the `projects.news.scripts.*` namespace package;
# the news scripts dir -> resolves data_quality's bare sibling imports.
import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）

from projects.news.scripts.data_quality import (  # noqa: E402
    normalize_engagement,
    is_hot_normalized,
)


# --- normalize_engagement: per-platform weighted sums ---
def test_bilibili_views_only_uses_view_weight():
    # only play count -> view * 0.001, no interaction terms
    item = {"source": "bilibili", "metadata": {"play": 100000}}
    assert normalize_engagement(item) == 100000 * 0.001  # == 100.0


def test_bilibili_full_weighted_formula():
    # view*0.001 + like + coin*2 + favorite*2 + share*3
    item = {
        "source": "bilibili",
        "metadata": {"play": 10000, "like": 10, "coin": 5, "favorite": 4, "share": 3},
    }
    expected = 10000 * 0.001 + 10 + 5 * 2 + 4 * 2 + 3 * 3
    assert normalize_engagement(item) == expected  # 10 + 10 + 10 + 8 + 9 = 47.0


def test_weibo_weighted_formula():
    # repost*3 + comment*2 + like*1
    item = {
        "source": "weibo",
        "metadata": {"reposts_count": 10, "comments_count": 5, "attitudes_count": 7},
    }
    assert normalize_engagement(item) == 10 * 3 + 5 * 2 + 7 * 1  # 47.0


def test_youtube_weighted_formula():
    # view*0.0001 + like*1 + comment*1
    item = {
        "source": "youtube",
        "metadata": {"viewCount": 100000, "likeCount": 20, "commentCount": 5},
    }
    assert normalize_engagement(item) == 100000 * 0.0001 + 20 + 5  # 10 + 25 = 35.0


def test_default_platform_passes_engagement_through():
    assert normalize_engagement({"source": "reddit", "engagement": 42}) == 42.0
    assert normalize_engagement({"source": "unknown_x", "engagement": 7}) == 7.0


# --- is_hot_normalized: the >= threshold boundary ---
def test_hot_threshold_is_inclusive_lower_bound():
    # bilibili threshold = 100; exactly-at must count as hot (>=, not >).
    at = {"source": "bilibili", "metadata": {"play": 100000}}  # 100.0
    assert normalize_engagement(at) == 100.0
    assert is_hot_normalized(at) is True


def test_below_threshold_is_not_hot():
    below = {"source": "bilibili", "metadata": {"play": 99000}}  # 99.0
    assert is_hot_normalized(below) is False


def test_default_threshold_boundary():
    # default threshold = 50.
    assert is_hot_normalized({"source": "reddit", "engagement": 50}) is True
    assert is_hot_normalized({"source": "reddit", "engagement": 49}) is False
