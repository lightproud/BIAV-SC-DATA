"""taptap_collector 滚动深采（_autoscroll_collect）核心逻辑单测。

不依赖真实浏览器：用 mock async page 模拟「每滚动一轮触发一批新 XHR 响应」，
验证多响应合并去重、cutoff 深度停止、到底（连续无新增）停止三条行为。
"""

import asyncio
from datetime import datetime, timedelta, UTC

import _paths  # noqa: F401  直跑路径引导（pytest 侧见 pyproject.toml）
import taptap_collector  # noqa: E402


def _parse(body):
    """测试用 parse_fn：body['items'] 即原始条目列表。"""
    return body.get("items", [])


def _item(iid, days_ago=0):
    t = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return {"item_id": iid, "url": f"https://t/{iid}", "title": f"t{iid}", "created": t}


class _MockPage:
    """每次 wait_for_timeout 往 captured 追加下一批响应，模拟滚动懒加载。"""

    def __init__(self, batches, captured):
        self._batches = batches
        self._captured = captured
        self._i = 0

    async def evaluate(self, _js):
        return None

    async def wait_for_timeout(self, _ms):
        if self._i < len(self._batches):
            self._captured.append(("url", {"items": self._batches[self._i]}))
            self._i += 1


def test_autoscroll_merges_and_dedups():
    captured = [("url", {"items": [_item("1")]})]  # 首屏
    batches = [
        [_item("2"), _item("1")],  # 第二批含重复 1
        [_item("3")],
    ]
    page = _MockPage(batches, captured)
    items = asyncio.run(
        taptap_collector._autoscroll_collect(page, _parse, captured, max_scrolls=5, cutoff=None)
    )
    ids = sorted(i["item_id"] for i in items)
    assert ids == ["1", "2", "3"], f"应合并去重为 1/2/3，实得 {ids}"


def test_autoscroll_stops_at_cutoff_depth():
    captured = [("url", {"items": [_item("a", days_ago=1)]})]
    # 第二批引入 200 天前的老条目 → 触达 cutoff（180 天）应停止继续滚动
    batches = [
        [_item("b", days_ago=200)],
        [_item("c", days_ago=1)],  # 不应被抓到（已在上一轮后 break）
    ]
    page = _MockPage(batches, captured)
    cutoff = datetime.now(UTC) - timedelta(days=180)
    items = asyncio.run(
        taptap_collector._autoscroll_collect(page, _parse, captured, max_scrolls=10, cutoff=cutoff)
    )
    ids = {i["item_id"] for i in items}
    assert "b" in ids, "应抓到触达 cutoff 的老条目 b"
    assert "c" not in ids, "触达 cutoff 后应停止，不再抓 c"


def test_autoscroll_stops_when_exhausted():
    # 无新批次：连续无新增应在有限轮内停止（不空转满 max_scrolls）
    captured = [("url", {"items": [_item("x")]})]
    page = _MockPage([], captured)
    items = asyncio.run(
        taptap_collector._autoscroll_collect(page, _parse, captured, max_scrolls=100, cutoff=None)
    )
    assert len(items) == 1 and items[0]["item_id"] == "x"
    assert page._i == 0  # 无批次可注入


if __name__ == "__main__":
    test_autoscroll_merges_and_dedups()
    test_autoscroll_stops_at_cutoff_depth()
    test_autoscroll_stops_when_exhausted()
    print("all passed")
