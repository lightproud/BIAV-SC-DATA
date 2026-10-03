"""普查报告：单文件 HTML（手机可读），黑金配色（memory/style-guide.md）。只含统计与字段名。"""

from __future__ import annotations

from html import escape

_CSS = """
:root{--bg:#0a0b10;--card:#131210;--card2:#1a1915;--gold:#d2ab58;--gold2:#ecd48d;--text:#f2ede2;
--soft:#b8ad9c;--mut:#7a7468;--line:rgba(242,237,226,.14);--gline:rgba(210,171,88,.32)}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.65 -apple-system,"PingFang SC","Noto Sans SC",
"Microsoft YaHei",sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:28px;line-height:1.3;margin:8px 0 6px;color:var(--gold2)}
h2{font-size:21px;margin:40px 0 12px;color:var(--gold2);border-bottom:1px solid var(--gline);padding-bottom:6px}
h3{font-size:17px;margin:24px 0 8px;color:var(--text)}
p,li{color:var(--soft)}.meta{font-size:13px;color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:12px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:var(--card);border:1px solid var(--gline);border-radius:12px;padding:12px}
.kpi b{display:block;font-size:22px;color:var(--gold2);font-variant-numeric:tabular-nums}
.kpi span{font-size:13px;color:var(--soft)}
.tw{overflow-x:auto;margin:8px 0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--gold);font-weight:600;white-space:nowrap}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.9em;color:var(--gold2)}
details{margin:8px 0}summary{cursor:pointer;color:var(--gold)}
"""

_NOTES = [
    "时间：带时区的按其时区换算；无时区字符串按 UTC 理解；按月、按日一律按 UTC+8 归。",
    "完全重复只在同一目录（频道 / 来源）内比对：同作者重复 = 同作者 24 小时内相同文字；"
    "不同作者相同短句 = 少于 60 字、此前由另一作者发过的相同文字。占比的分母是有文字的消息。",
    "清洗试算按 T11 规则顺序：bot → system → empty → emoji_only → dup_same_author → dup_long_repost；"
    "长文转贴同样只在同目录内比对（T11 会跨目录，实际丢弃数可能更高）。有图无字单列，不算丢弃但不送标。",
    "机器人标记、系统消息只认字段（discord 的 author_bot、type≠0/19）；bots.txt 名单在 T10 才建，试算未用。",
    "discord 是紧凑 schema：缺字段 = 默认值。所以「样本里出现率」低不代表没有该字段，看「有值占比」才是实际覆盖。",
    "试切：聊天类按默认参数（间隔、条数上限）对清洗后留下的消息切段；评论、帖子、评价各算一个单元。",
    "语言是按文字脚本粗分：zh = 汉字为主、ja = 含假名、ko = 谚文、latin 涵盖英法德西等，不等于语种数。",
]


def _pct(x: float) -> str:
    return f"{x:.2%}"


def _num(x: int | float) -> str:
    return f"{x:,}" if isinstance(x, int) else f"{x:,.2f}"


def _table(headers: list[str], rows: list[list[str]], numeric: set[int] | None = None) -> str:
    numeric = numeric or set()
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>"
        + "".join(f'<td class="n">{c}</td>' if i in numeric else f"<td>{c}</td>" for i, c in enumerate(r))
        + "</tr>"
        for r in rows
    )
    return f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _kpi(value: str, label: str) -> str:
    return f'<div class="kpi"><b>{value}</b><span>{escape(label)}</span></div>'


def _platform(name: str, p: dict) -> str:
    out = [f'<h2 id="p-{escape(name)}">{escape(name)}</h2>']
    out.append(
        '<div class="kpis">'
        + _kpi(_num(p["messages"]), "条数")
        + _kpi(_num(p["authors_unique"]), "独立作者")
        + _kpi(_num(p["files"]), "文件")
        + _kpi(_pct(p["clean_trial"]["retention"]), "清洗后留存")
        + "</div>"
    )
    out.append("<h3>目录层级与格式</h3>")
    out.append(
        _table(
            ["路径模式", "文件数"],
            [[f"<code>{escape(k)}</code>", _num(v)] for k, v in p["layouts"].items()],
            {1},
        )
    )
    kinds = "、".join(f"{escape(k)} {_num(v)}" for k, v in p["kind"].items())
    out.append(
        f"<p>种类：{kinds}。坏记录 {_num(p['bad_records'])} 条；无可解析时间 {_num(p['timestamp_missing'])} 条。</p>"
    )
    out.append("<h3>按月条数（UTC+8）</h3>")
    out.append(_table(["月份", "条数"], [[k, _num(v)] for k, v in p["by_month"].items()], {1}))
    tl = p["text_len_chars"]
    out.append("<h3>文本与作者</h3>")
    rows = [
        ["文本长度（字符）P50 / P90 / P99 / 最大", f"{tl['p50']} / {tl['p90']} / {tl['p99']} / {tl['max']}"],
        [
            "粗估 token 合计 / 每条均值",
            f"{_num(p['tokens_est']['total'])} / {p['tokens_est']['per_msg_mean']}",
        ],
        ["认不出作者的条数", _num(p["author_missing"])],
        ["空消息占比", _pct(p["share"]["empty"])],
        ["纯表情占比", _pct(p["share"]["emoji_only"])],
        ["有附件无文字占比", _pct(p["share"]["image_only"])],
        ["同作者重复率", _pct(p["dup"]["same_author"])],
        ["不同作者的相同短句率", _pct(p["dup"]["cross_author_short"])],
    ]
    out.append(_table(["项目", "数值"], rows, {1}))
    out.append("<h3>文字脚本（粗分语言）</h3>")
    out.append(_table(["脚本", "条数"], [[k, _num(v)] for k, v in p["script"].items()], {1}))
    out.append("<h3>四类专项字段</h3>")
    sp_rows = [
        [
            escape(v["label"]),
            escape("、".join(v["fields_in_sample"]) or "样本里没有"),
            _pct(v["filled_share"]),
        ]
        for v in p["special"].values()
    ]
    out.append(_table(["字段类", "样本里出现的字段名", "全量有值占比"], sp_rows, {2}))
    f = p["fields"]
    out.append(f"<details><summary>字段清单（抽样 {f['sample_n']} 条）</summary>")
    out.append(
        _table(
            ["字段", "类型", "出现率", "有值率"],
            [
                [
                    f"<code>{escape(k)}</code>",
                    escape("、".join(v["types"])),
                    _pct(v["present"]),
                    _pct(v["non_null"]),
                ]
                for k, v in f["fields"].items()
            ],
            {2, 3},
        )
    )
    out.append("</details>")
    if p["chat_channels"]:
        chans = sorted(p["chat_channels"].items(), key=lambda kv: (-kv[1]["messages"], kv[0]))
        out.append(f"<h3>聊天频道相邻消息间隔（共 {len(chans)} 个频道，按条数排序）</h3>")
        out.append(
            _table(
                ["频道", "条数", "天数", "日均", "间隔 P50 秒", "P90", "P99"],
                [
                    [
                        f"<code>{escape(k)}</code>",
                        _num(v["messages"]),
                        _num(v["days"]),
                        str(v["per_day_mean"]),
                        _num(v["gap_seconds"]["p50"]),
                        _num(v["gap_seconds"]["p90"]),
                        _num(v["gap_seconds"]["p99"]),
                    ]
                    for k, v in chans
                ],
                {1, 2, 3, 4, 5, 6},
            )
        )
    ct, ut = p["clean_trial"], p["unitize_trial"]
    out.append("<h3>清洗与切段试算</h3>")
    drops = [[escape(k), _num(v)] for k, v in ct["drops"].items()] or [["（无）", "0"]]
    out.append(_table(["原因", "条数"], drops, {1}))
    out.append(
        f"<p>留下 {_num(ct['kept'])} 条；聊天试切 {_num(ut['segments'])} 段，平均 {ut['avg_segment_len']} 条/段；"
        f"单元合计 {_num(ut['units_total'])}。</p>"
    )
    return "\n".join(out)


def render_html(result: dict, generated: str) -> str:
    t = result["totals"]
    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>数据普查报告</title><style>{_CSS}</style></head><body><main>",
        "<p class='meta'>玩家舆情分析系统 · T01 数据普查</p>",
        "<h1>数据普查报告</h1>",
        f"<p class='meta'>生成于 {escape(generated)}（UTC+8）· 读取：{escape(result['reader'])} · "
        f"普查版本 {escape(result['census_ver'])}</p>",
        '<div class="kpis">',
        _kpi(_num(t["messages"]), "原文总条数"),
        _kpi(_pct(t["chat_share"]), "聊天类占比"),
        _kpi(_pct(t["retention"]), "清洗后留存"),
        _kpi(_num(t["trial_units_total"]), "试切单元总数"),
        "</div>",
        "<h2>方案假设与实测</h2>",
        _table(
            ["项目", "方案假设", "实测"],
            [[escape(a["item"]), escape(a["assumed"]), escape(a["actual"])] for a in result["assumptions_vs_actual"]],
        ),
        "<h2>全局</h2>",
        _table(
            ["项目", "数值"],
            [
                ["文件数", _num(result["lake"]["files"])],
                *[[f"跳过：{escape(k)}", _num(v)] for k, v in result["lake"]["skipped"].items()],
                ["粗估 token 合计", _num(t["tokens_est_total"])],
                ["试切平均段长", f"{t['trial_avg_segment_len']} 条/段"],
                *[[f"脚本 {escape(k)}", _num(v)] for k, v in t["script"].items()],
            ],
            {1},
        ),
        "<h2>平台一览</h2>",
        _table(
            ["平台", "条数", "独立作者", "留存", "回复指向", "机器人标记", "原生 ID", "附件"],
            [
                [
                    f'<a href="#p-{escape(n)}" style="color:var(--gold2)">{escape(n)}</a>',
                    _num(p["messages"]),
                    _num(p["authors_unique"]),
                    _pct(p["clean_trial"]["retention"]),
                    *[_pct(p["special"][k]["filled_share"]) for k in ("reply", "bot", "native_id", "attachment")],
                ]
                for n, p in result["platforms"].items()
            ],
            {1, 2, 3, 4, 5, 6, 7},
        ),
    ]
    parts += [_platform(n, p) for n, p in result["platforms"].items()]
    parts.append("<h2>口径说明</h2><ul>" + "".join(f"<li>{escape(x)}</li>" for x in _NOTES) + "</ul>")
    parts.append("</main></body></html>")
    return "\n".join(parts)
