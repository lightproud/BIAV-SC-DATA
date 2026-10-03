"""`yuqing unitize review`（T13）：抽 N 段出单文件 HTML，让人判断「单看这一段能不能懂」。

- 每段先显示段前的只读上下文（灰色），再显示正文；段内的只读上下文（纯表情、极短等）按时间夹在正文里，也是灰色。
- 作者只显示段内代号（甲、乙、丙……），不显示名字或哈希；正文里的 Discord @提及换成段内代号或「@某人」。
- 每段「看得懂」「看不懂」两个按钮，页面底部实时显示比例；结果可导出为一段 JSON 文本供复制。
  页面不联网、不落盘（不用 localStorage），关掉就没了。
- 同 seed 同输入，页面逐字节相同。
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb

from yuqing.census.fields import CN_TZ
from yuqing.config import ConfigError, check_outside_repo, load_config
from yuqing.normalize.clean import MESSAGES_DEDUP_SQL, current_clean_ver
from yuqing.normalize.messages import author_hash
from yuqing.normalize.run import messages_root
from yuqing.normalize.store import glob_of, parquet_files, read_rows
from yuqing.unitize.run import unit_ver_of, units_root

CODES = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&\d+>")
_CHANNEL_MENTION = re.compile(r"<#\d+>")
_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")


def code_of(i: int) -> str:
    return CODES[i] if i < len(CODES) else f"人{i + 1}"


def parse_days(specs: list[str] | None) -> set[date] | None:
    """'2026-09-01' 或 '2026-09-01..2026-09-30'，可重复。"""
    if not specs:
        return None
    out: set[date] = set()
    for spec in specs:
        lo, _, hi = spec.partition("..")
        a = date.fromisoformat(lo)
        b = date.fromisoformat(hi) if hi else a
        while a <= b:
            out.add(a)
            a += timedelta(days=1)
    return out


def pick_units(units: list[dict], channels: list[str] | None, days: set[date] | None, n: int, seed: int) -> list[dict]:
    pool = [
        u
        for u in units
        if u["kind"] == "chat" and (not channels or u["channel"] in channels) and (days is None or u["day_cn"] in days)
    ]
    pool.sort(key=lambda u: u["unit_id"])
    picked = random.Random(seed).sample(pool, min(n, len(pool)))
    return sorted(picked, key=lambda u: (u["ts_start"], u["unit_id"]))


@dataclass
class Line:
    msg_id: str
    t: float | None
    who: str
    text: str
    ctx: bool
    reply: str | None


def _clean_text(text: str, mention_codes: dict[str, str]) -> str:
    text = _USER_MENTION.sub(lambda m: "@" + mention_codes.get(m.group(1), "某人"), text)
    text = _ROLE_MENTION.sub("@身份组", text)
    text = _CHANNEL_MENTION.sub("#频道", text)
    return _CUSTOM_EMOJI.sub(lambda m: f":{m.group(1)}:", text)


def _hhmm(t: float | None) -> str:
    return datetime.fromtimestamp(t, tz=UTC).astimezone(CN_TZ).strftime("%m-%d %H:%M:%S") if t is not None else "--"


def build_segment(unit: dict, msgs: dict[str, dict], platform_salt: tuple[str, str] | None) -> list[Line]:
    body = [i for i in unit["msg_ids"] if i in msgs]
    ctx = [i for i in unit["ctx_msg_ids"] if i in msgs]
    order = sorted(set(body) | set(ctx), key=lambda i: (msgs[i]["t"] is None, msgs[i]["t"] or 0, i))
    in_page = set(order)
    codes: dict[str | None, str] = {}
    for i in order:
        a = msgs[i]["author_hash"]
        if a is not None and a not in codes:
            codes[a] = code_of(len(codes))
    # @提及：用盐现算被提及者的哈希，是段内的人就显示代号
    mention_codes: dict[str, str] = {}
    if platform_salt:
        platform, salt = platform_salt
        for i in order:
            for uid in _USER_MENTION.findall(msgs[i]["text"] or ""):
                h = author_hash(salt, platform, uid)
                if h in codes:
                    mention_codes[uid] = codes[h]
    start = unit["ts_start"].timestamp() if unit["ts_start"] else None
    body_set = set(body)
    pre = [i for i in order if i not in body_set and (start is None or msgs[i]["t"] is None or msgs[i]["t"] < start)]
    rest = [i for i in order if i not in pre]
    lines = []
    for i in pre + rest:
        m = msgs[i]
        text = m["text"] or ""
        if not text.strip():
            text = "［图片］" if m["has_image"] else "［空］"
        reply = None
        if m["reply_to"]:
            tgt = msgs.get(m["reply_to"]) if m["reply_to"] in in_page else None
            reply = f"回复 {codes.get(tgt['author_hash'], '？')}" if tgt else "回复（不在本段）"
        who = codes.get(m["author_hash"], "？")
        lines.append(Line(i, m["t"], who, _clean_text(text, mention_codes), i not in body_set, reply))
    return lines


_CSS = """
:root{--bg:#fafaf8;--fg:#1d1d1b;--muted:#6b6b66;--ctx:#9a9a94;--card:#fff;--line:#e4e4df;--ok:#1f7a4d;--bad:#b3261e;
--okbg:#e6f4ec;--badbg:#fbe9e7}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#a3a39d;--ctx:#7c7c77;--card:#20201f;
--line:#33332f;--ok:#6fcf97;--bad:#f28b82;--okbg:#1d3a2a;--badbg:#3d201d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,"PingFang SC","Hiragino Sans","Noto Sans CJK SC",sans-serif}
main{max-width:760px;margin:0 auto;padding:16px 16px 120px}h1{font-size:20px;margin:8px 0}
.meta,.hint{color:var(--muted);font-size:13px}.seg{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:12px;margin:14px 0}.seg.ok{border-color:var(--ok)}.seg.bad{border-color:var(--bad)}
.sh{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:12px;margin-bottom:6px;flex-wrap:wrap}
.m{display:flex;gap:8px;padding:3px 0;word-break:break-word;overflow-wrap:anywhere}
.m .w{flex:0 0 auto;font-weight:600}.m .t{flex:0 0 auto;color:var(--muted);font-size:12px;padding-top:2px}
.m.c{color:var(--ctx)}.m.c .w{font-weight:400}.r{color:var(--muted);font-size:12px;margin-right:4px}
.sep{border-top:1px dashed var(--line);margin:6px 0;font-size:11px;color:var(--ctx)}
.btns{display:flex;gap:8px;margin-top:10px}button{flex:1;padding:10px;border-radius:8px;border:1px solid var(--line);
background:transparent;color:var(--fg);font-size:15px}.seg.ok .y{background:var(--okbg);border-color:var(--ok)}
.seg.bad .n{background:var(--badbg);border-color:var(--bad)}footer{position:fixed;left:0;right:0;bottom:0;
background:var(--card);border-top:1px solid var(--line);padding:10px 16px}footer .in{max-width:760px;margin:0 auto;
display:flex;gap:10px;align-items:center;flex-wrap:wrap}#stat{flex:1;min-width:180px}footer button{flex:0 0 auto}
textarea{width:100%;height:160px;margin-top:8px;font:12px monospace;background:var(--bg);color:var(--fg);
border:1px solid var(--line);border-radius:6px}#out{display:none;max-width:760px;margin:0 auto}
"""

_JS = """
const R={};const segs=[...document.querySelectorAll('.seg')];const N=segs.length;
function upd(){const v=Object.values(R);const y=v.filter(x=>x==='ok').length;const d=v.length;
document.getElementById('stat').textContent='已判 '+d+' / '+N+' · 看得懂 '+y+(d?'（'+Math.round(100*y/d)+'%）':'')+
(d===N?' · 全部判完，九成及格线 '+Math.ceil(N*0.9)+' 段':'');}
segs.forEach(s=>{s.querySelector('.y').onclick=()=>{R[s.dataset.unit]='ok';s.className='seg ok';upd();};
s.querySelector('.n').onclick=()=>{R[s.dataset.unit]='bad';s.className='seg bad';upd();};});
document.getElementById('exp').onclick=()=>{const meta=JSON.parse(document.getElementById('meta').textContent);
const out=Object.assign({},meta,{results:segs.map(s=>({unit_id:s.dataset.unit,verdict:R[s.dataset.unit]||null}))});
const ta=document.getElementById('json');ta.value=JSON.stringify(out,null,1);
document.getElementById('out').style.display='block';ta.focus();ta.select();
try{navigator.clipboard.writeText(ta.value);}catch(e){}};upd();
"""


def render_html(segments: list[tuple[dict, list[Line]]], meta: dict) -> str:
    e = html.escape
    cards = []
    for k, (u, lines) in enumerate(segments, 1):
        rows = []
        start = u["ts_start"].timestamp() if u["ts_start"] else None
        shown_sep = False
        for ln in lines:
            if not ln.ctx and not shown_sep:
                rows.append('<div class="sep">正文</div>')
                shown_sep = True
            pre_ctx = ln.ctx and not shown_sep and (start is None or ln.t is None or ln.t < start)
            label = "上文" if pre_ctx else ("旁注" if ln.ctx else "")
            reply = f'<span class="r">↪ {e(ln.reply)}</span>' if ln.reply else ""
            rows.append(
                f'<div class="m{" c" if ln.ctx else ""}"><span class="t">{e(_hhmm(ln.t))}</span>'
                f'<span class="w">{e(ln.who)}{("·" + label) if label else ""}</span>'
                f"<span>{reply}{e(ln.text)}</span></div>"
            )
        cards.append(
            f'<section class="seg" data-unit="{e(u["unit_id"])}"><div class="sh">'
            f"<span>第 {k} 段 · 频道 {e(u['channel'] or '')} · {e(str(u['day_cn']))}</span>"
            f"<span>正文 {u['n_msgs']} 条 · {u['n_authors']} 人 · 上下文 {len(u['ctx_msg_ids'])} 条</span></div>"
            f"{''.join(rows)}"
            '<div class="btns"><button class="y" type="button">看得懂</button>'
            '<button class="n" type="button">看不懂</button></div></section>'
        )
    p = meta["params"]
    head = (
        f"<h1>试切检查 · {len(segments)} 段</h1>"
        f'<p class="meta">unit_ver {e(meta["unit_ver"])} · seed {meta["seed"]} · 间隔 {p["gap_minutes"]} 分钟 · '
        f"每段至多 {p['max_msgs']} 条 · 重叠 {p['overlap_msgs']} 条 · "
        f"频道 {e('、'.join(meta['channels']) or '全部')} · 日期 {e(meta['days'] or '全部')}</p>"
        '<p class="hint">只问一件事：<b>单看这一段，能看懂大家在说什么吗？</b>'
        "灰色是只读上下文（“上文”是前一段末尾与被回复的消息，"
        "“旁注”是段内的纯表情、极短回复等），不计入正文。作者按段内出场顺序记作甲、乙、丙。时间为 UTC+8。"
        "全部判完后点「导出结果」，把 JSON 复制回来即可；页面不联网、不保存，关掉就没了。</p>"
    )
    meta_json = json.dumps(meta, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>试切检查</title>'
        f"<style>{_CSS}</style></head><body><main>{head}{''.join(cards)}"
        '<div id="out"><textarea id="json" readonly></textarea></div></main>'
        '<footer><div class="in"><span id="stat"></span><button id="exp" type="button">导出结果</button></div></footer>'
        f'<script type="application/json" id="meta">{meta_json}</script><script>{_JS}</script></body></html>\n'
    )


def _pq(root: Path) -> str:
    return f"read_parquet('{glob_of(root)}', hive_partitioning=false, union_by_name=true)"


def load_messages(data_root: Path, ids: set[str]) -> dict[str, dict]:
    if not ids:
        return {}
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE want(msg_id VARCHAR)")
    con.executemany("INSERT INTO want VALUES (?)", [(i,) for i in sorted(ids)])
    src = MESSAGES_DEDUP_SQL.format(src=_pq(messages_root(data_root)))
    rows = con.execute(
        f"""SELECT m.msg_id, m.platform, m.author_hash, epoch(m.ts_utc) AS t, m.text, m.reply_to, m.has_image
            FROM ({src}) m JOIN want USING (msg_id)"""
    ).fetchall()
    con.close()
    keys = ("msg_id", "platform", "author_hash", "t", "text", "reply_to", "has_image")
    return {r[0]: dict(zip(keys, r, strict=True)) for r in rows}


def review(
    data_root: Path,
    unit_ver: str,
    params: dict,
    channels: list[str] | None,
    day_specs: list[str] | None,
    n: int,
    seed: int,
    salt: str | None,
) -> tuple[str, int]:
    root = units_root(data_root, unit_ver)
    if not parquet_files(root):
        raise ConfigError(f"找不到 unit_ver={unit_ver} 的单元：先跑 yuqing unitize")
    units = pick_units(read_rows(root), channels, parse_days(day_specs), n, seed)
    want = {i for u in units for i in u["msg_ids"] + u["ctx_msg_ids"]}
    msgs = load_messages(data_root, want)
    segments = []
    for u in units:
        platform = next((msgs[i]["platform"] for i in u["msg_ids"] if i in msgs), None)
        segments.append((u, build_segment(u, msgs, (platform, salt) if salt and platform else None)))
    meta = {
        "unit_ver": unit_ver,
        "seed": seed,
        "n": n,
        "channels": sorted(channels or []),
        "days": ",".join(day_specs or []),
        "params": {k: params[k] for k in ("gap_minutes", "max_msgs", "overlap_msgs", "ctx_inline_max")},
    }
    return render_html(segments, meta), len(segments)


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--channel", action="append", help="频道（可重复；不给则全部聊天频道）")
    p.add_argument("--day", action="append", help="日期（UTC+8）YYYY-MM-DD 或 起..止，可重复")
    p.add_argument("--n", type=int, default=50, help="抽几段（默认 50）")
    p.add_argument("--seed", type=int, default=1, help="随机种子（默认 1）")
    p.add_argument("--out", required=True, help="输出的 HTML 路径（必须在仓库外）")
    p.add_argument("--unit-ver", help="用哪一版单元（默认按当前配置算出的 unit_ver）")


def run_cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    data_root = cfg.data_root()
    params = cfg.params["unitize"]
    unit_ver = args.unit_ver or unit_ver_of(params, args.clean_ver or current_clean_ver(cfg))
    out = check_outside_repo(Path(args.out), "检查页")
    page, k = review(data_root, unit_ver, params, args.channel, args.day, args.n, args.seed, cfg.env.get("AUTHOR_SALT"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"试切检查页：{k} 段（unit_ver {unit_ver}，seed {args.seed}）→ {out}")
    if k < args.n:
        print(f"注意：符合条件的段只有 {k} 段，少于要求的 {args.n} 段。")
    return 0
