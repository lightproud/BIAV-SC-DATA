"""标注契约（T21）：提示词渲染、输出 JSON Schema、校验器、失败单元重试一次。

- 词表 config/taxonomy.toml 带 taxo_ver；提示词目录名就是 prompt_ver。改任何一个都要升版本：
  提示词目录里的 manifest.json 记着各文件的 sha256，内容对不上就拒绝加载。
- 词表与对象词典按「推进计划 §二.2」：设了环境变量 YUQING_CONFIG_DIR 就从那里读，否则回落仓内样例。
- 渲染：system 只放固定部分（规则、词表、示例），逐字节稳定，便于网关前缀缓存；user 放本批单元。
- 校验不编造：枚举不在词表、anchors 越界或指向上下文、要点超长等任一问题，整个单元判无效；
  无效与缺失的单元重发一次，仍不行记 failed。词表外的角度不进 aspects，转入 new_aspect 候选。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from yuqing.config import ConfigError, project_root

EMOTIONS = ("anger", "disappointment", "worry", "anticipation", "satisfaction", "joking")  # 数据契约固定
GIST_MAX = 40
CONFIG_DIR_ENV = "YUQING_CONFIG_DIR"
DEFAULT_PROMPT_VER = "v1"
_PROMPT_FILES = ("system.md", "user.md", "example.md")


# -- 词表与对象词典 ---------------------------------------------------------------
def config_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env[CONFIG_DIR_ENV]) if env.get(CONFIG_DIR_ENV) else project_root() / "config"


@dataclass(frozen=True)
class Taxonomy:
    taxo_ver: str
    emotion: dict[str, str]
    aspects: dict[str, str]
    self_identity: dict[str, str]
    self_intent: dict[str, str]

    @classmethod
    def load(cls, path: Path | None = None) -> Taxonomy:
        path = path or config_dir() / "taxonomy.toml"
        if not path.is_file():
            raise ConfigError(f"找不到词表：{path}")
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        ver = raw.get("taxo_ver")
        if not ver:
            raise ConfigError(f"词表缺 taxo_ver：{path}")
        tx = cls(
            taxo_ver=str(ver),
            emotion=dict(raw.get("emotion", {})),
            aspects=dict(raw.get("aspects", {})),
            self_identity=dict(raw.get("self_identity", {})),
            self_intent=dict(raw.get("self_intent", {})),
        )
        if tuple(tx.emotion) != EMOTIONS:
            raise ConfigError(f"词表的 emotion 必须正好是数据契约的六类（按序）：{'、'.join(EMOTIONS)}")
        if not tx.aspects:
            raise ConfigError("词表的 aspects 为空")
        return tx


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).strip().casefold()


@dataclass(frozen=True)
class Entities:
    """对象词典：别名 → 规范名。由人从配表导出；没有就是空表，归一不了的保留原文写法。"""

    alias: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> Entities:
        path = path or config_dir() / "entities.tsv"
        if not path.is_file():
            return cls()
        alias: dict[str, str] = {}
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip() or line.startswith("#") or (i == 0 and line.startswith("canonical\t")):
                continue
            parts = line.split("\t")
            canon = parts[0].strip()
            if not canon:
                continue
            for name in (canon, *parts[1:]):
                if name.strip():
                    alias[_norm(name)] = canon
        return cls(alias)

    def normalize(self, name: str) -> str:
        return self.alias.get(_norm(name), name.strip())


# -- 输入：一批单元 ---------------------------------------------------------------
@dataclass(frozen=True)
class UnitInput:
    """一个待标单元。body / ctx 是 (msg_id, 文字) 列表；正文序号 1..n，上下文序号 C1..Ck。"""

    unit_id: str
    body: Sequence[tuple[str, str]]
    ctx: Sequence[tuple[str, str]] = ()
    ctx_text: str = ""  # 评论或帖子的父级标题、文本


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    tag: str
    keys: dict[str, str]  # 批内单元编号 u1… → unit_id
    prompt_ver: str
    taxo_ver: str


def _bullet(d: dict[str, str]) -> str:
    return "\n".join(f"- {k}：{v}" for k, v in d.items())


class PromptSet:
    def __init__(self, prompt_ver: str = DEFAULT_PROMPT_VER, root: Path | None = None):
        root = root or project_root() / "prompts" / "annotate"
        d = root / prompt_ver
        if not d.is_dir():
            raise ConfigError(f"找不到提示词目录：{d}")
        manifest_path = d / "manifest.json"
        if not manifest_path.is_file():
            raise ConfigError(f"提示词目录缺 manifest.json：{d}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.files: dict[str, str] = {}
        for name in _PROMPT_FILES:
            data = (d / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != manifest.get(name):
                raise ConfigError(f"提示词 {prompt_ver}/{name} 与 manifest 不符：改了提示词就要另开新版本目录")
            self.files[name] = data.decode("utf-8")
        self.prompt_ver = prompt_ver  # 目录名即版本

    def system(self, tx: Taxonomy) -> str:
        """固定前缀：只依赖提示词版本与词表，与本批单元无关。"""
        out = self.files["system.md"]
        for key, val in (
            ("{EMOTIONS}", _bullet(tx.emotion)),
            ("{ASPECTS}", _bullet(tx.aspects)),
            ("{IDENTITIES}", _bullet(tx.self_identity)),
            ("{INTENTS}", _bullet(tx.self_intent)),
            ("{EXAMPLE}", self.files["example.md"].strip()),
        ):
            out = out.replace(key, val)
        return out.rstrip() + "\n"

    def render(self, units: Sequence[UnitInput], tx: Taxonomy) -> Prompt:
        if not units:
            raise ValueError("空批次")
        tag = batch_tag(units)
        keys: dict[str, str] = {}
        blocks = []
        for i, u in enumerate(units, start=1):
            if not u.body:
                raise ValueError(f"单元 {u.unit_id} 没有正文消息")
            key = f"u{i}"
            keys[key] = u.unit_id
            lines = [f'<{tag} unit="{key}">']
            if u.ctx_text:
                lines.append(f"父帖（仅供理解，不要从中抽取）: {_flat(u.ctx_text)}")
            lines += [f"C{j}: {_flat(t)}" for j, (_, t) in enumerate(u.ctx, start=1)]
            lines += [f"{j}: {_flat(t)}" for j, (_, t) in enumerate(u.body, start=1)]
            lines.append(f"</{tag}>")
            blocks.append("\n".join(lines))
        user = (
            self.files["user.md"]
            .replace("{TAG}", tag)
            .replace("{N}", str(len(units)))
            .replace("{UNITS}", "\n\n".join(blocks))
        )
        return Prompt(self.system(tx), user.rstrip() + "\n", tag, keys, self.prompt_ver, tx.taxo_ver)


def _flat(text: str) -> str:
    """消息内换行改成 ⏎，免得一行原文伪装成下一条带序号的消息。"""
    return re.sub(r"\r\n|\r|\n", " ⏎ ", text)


def batch_tag(units: Sequence[UnitInput]) -> str:
    """带随机编号的标签名：由本批内容哈希得出（同输入同结果），且保证不出现在任何原文里。"""
    texts = [t for u in units for _, t in (*u.ctx, *u.body)] + [u.ctx_text for u in units]
    seed = "\x1f".join([u.unit_id for u in units] + texts)
    for salt in range(1000):
        tag = "t" + hashlib.sha256(f"{salt}\x1e{seed}".encode()).hexdigest()[:10]
        if not any(tag in t for t in texts):
            return tag
    raise ValueError("找不到不与原文冲突的标签编号")  # pragma: no cover


# -- 输出：JSON Schema 与校验 --------------------------------------------------------
def output_json_schema(tx: Taxonomy) -> dict:
    """给网关的结构化输出约束。aspects 不在这里锁枚举：词表外角度要由校验器转入候选，而不是被网关拒掉。"""
    strs = {"type": "array", "items": {"type": "string"}}
    point = {
        "type": "object",
        "properties": {
            "anchors": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
            "emotion": {"type": "string", "enum": list(tx.emotion)},
            "intensity": {"type": "integer", "minimum": 1, "maximum": 3},
            "aspects": strs,
            "new_aspect": strs,
            "objects": strs,
            "self_identity": {"type": "string", "enum": ["", *tx.self_identity]},
            "self_intent": {"type": "string", "enum": ["", *tx.self_intent]},
            "gist_zh": {"type": "string", "maxLength": GIST_MAX},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "anchors",
            "emotion",
            "intensity",
            "aspects",
            "new_aspect",
            "objects",
            "self_identity",
            "self_intent",
            "gist_zh",
            "confidence",
        ],
        "additionalProperties": False,
    }
    unit = {
        "type": "object",
        "properties": {"unit": {"type": "string"}, "points": {"type": "array", "items": point}},
        "required": ["unit", "points"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"units": {"type": "array", "items": unit}},
        "required": ["units"],
        "additionalProperties": False,
    }


@dataclass
class UnitResult:
    unit_id: str
    status: str  # ok 或 failed
    points: list[dict] = field(default_factory=list)
    new_aspects: list[str] = field(default_factory=list)  # 词表外角度候选，不入主字段
    errors: list[str] = field(default_factory=list)
    attempts: int = 1


class _Invalid(ValueError):
    pass


def _str_list(v: object, name: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise _Invalid(f"{name} 必须是字符串数组")
    return [x.strip() for x in v if x.strip()]


def _validate_point(p: object, unit: UnitInput, tx: Taxonomy, ents: Entities) -> tuple[dict, list[str]]:
    if not isinstance(p, dict):
        raise _Invalid("反馈点不是对象")
    anchors = p.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        raise _Invalid("anchors 至少一个")
    n = len(unit.body)
    seqs: list[int] = []
    for a in anchors:
        if isinstance(a, str) and a.strip().upper().startswith("C"):
            raise _Invalid(f"anchors 指向上下文 {a}")
        if isinstance(a, bool) or not isinstance(a, int | str):
            raise _Invalid(f"anchors 元素不是序号：{a!r}")
        try:
            k = int(a)
        except ValueError as exc:
            raise _Invalid(f"anchors 元素不是序号：{a!r}") from exc
        if not 1 <= k <= n:
            raise _Invalid(f"anchors 越界：{k} 不在正文 1..{n}")
        if k not in seqs:
            seqs.append(k)
    emotion = p.get("emotion")
    if emotion not in tx.emotion:
        raise _Invalid(f"emotion 不在词表：{emotion!r}")
    intensity = p.get("intensity")
    if isinstance(intensity, bool) or intensity not in (1, 2, 3):
        raise _Invalid(f"intensity 必须是 1 到 3：{intensity!r}")
    for key, vocab in (("self_identity", tx.self_identity), ("self_intent", tx.self_intent)):
        v = p.get(key) or ""
        if not isinstance(v, str) or (v and v not in vocab):
            raise _Invalid(f"{key} 不在词表：{v!r}")
    gist = p.get("gist_zh")
    if not isinstance(gist, str) or not gist.strip():
        raise _Invalid("gist_zh 为空")
    if len(gist.strip()) > GIST_MAX:
        raise _Invalid(f"gist_zh 超过 {GIST_MAX} 字")
    conf = p.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, int | float) or not 0 <= conf <= 1:
        raise _Invalid(f"confidence 必须在 0 到 1：{conf!r}")
    aspects_in = _str_list(p.get("aspects"), "aspects")
    candidates = _str_list(p.get("new_aspect"), "new_aspect")
    aspects: list[str] = []
    for a in aspects_in:
        if a in tx.aspects:
            if a not in aspects:
                aspects.append(a)
        elif a not in candidates:
            candidates.append(a)  # 词表外角度：进候选，不入主字段
    objects: list[str] = []
    for o in _str_list(p.get("objects"), "objects"):
        canon = ents.normalize(o)
        if canon not in objects:
            objects.append(canon)
    point = {
        "anchors": [unit.body[k - 1][0] for k in sorted(seqs)],  # 段内序号 → msg_id
        "emotion": emotion,
        "intensity": intensity,
        "aspects": aspects,
        "objects": objects,
        "self_identity": p.get("self_identity") or "",
        "self_intent": p.get("self_intent") or "",
        "gist_zh": gist.strip(),
        "confidence": float(conf),
    }
    return point, candidates


def parse_output(text: str, prompt: Prompt, units: Sequence[UnitInput], tx: Taxonomy, ents: Entities) -> dict:
    """解析一批的输出 → {unit_id: UnitResult}。解析不了或缺席的单元记 failed，不编造。"""
    by_key = {k: u for k, u in zip(prompt.keys, units, strict=True)}
    results = {u.unit_id: UnitResult(u.unit_id, "failed", errors=["输出里没有这个单元"]) for u in units}
    try:
        data = json.loads(_strip_fence(text))
        entries = data["units"]
        if not isinstance(entries, list):
            raise TypeError("units 不是数组")
    except (ValueError, KeyError, TypeError) as exc:
        for r in results.values():
            r.errors = [f"整批解析失败：{type(exc).__name__}"]
        return results
    seen: set[str] = set()
    for entry in entries:
        key = entry.get("unit") if isinstance(entry, dict) else None
        if key not in by_key:
            continue  # 不认识的单元编号：忽略
        unit = by_key[key]
        if key in seen:
            results[unit.unit_id] = UnitResult(unit.unit_id, "failed", errors=["同一单元输出了两次"])
            continue
        seen.add(key)
        pts = entry.get("points")
        if not isinstance(pts, list):
            results[unit.unit_id] = UnitResult(unit.unit_id, "failed", errors=["points 不是数组"])
            continue
        try:
            points, cands = [], []
            for p in pts:
                point, c = _validate_point(p, unit, tx, ents)
                points.append(point)
                cands += [x for x in c if x not in cands]
            results[unit.unit_id] = UnitResult(unit.unit_id, "ok", points, cands)
        except _Invalid as exc:
            results[unit.unit_id] = UnitResult(unit.unit_id, "failed", errors=[str(exc)])
    return results


def _strip_fence(text: str) -> str:
    t = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, flags=re.S)
    return m.group(1) if m else t


def annotate_batch(
    call: Callable[[str, str, dict], str],
    units: Sequence[UnitInput],
    tx: Taxonomy,
    ents: Entities,
    prompts: PromptSet,
) -> list[UnitResult]:
    """送一批：call(system, user, json_schema) 返回模型文本。失败的单元单独重发一次，仍失败记 failed。"""
    schema = output_json_schema(tx)
    prompt = prompts.render(units, tx)
    results = parse_output(call(prompt.system, prompt.user, schema), prompt, units, tx, ents)
    retry = [u for u in units if results[u.unit_id].status != "ok"]
    if retry:
        p2 = prompts.render(retry, tx)
        again = parse_output(call(p2.system, p2.user, schema), p2, retry, tx, ents)
        for u in retry:
            r = again[u.unit_id]
            r.attempts = 2
            if r.status != "ok":
                r.errors = results[u.unit_id].errors + r.errors
            results[u.unit_id] = r
    return [results[u.unit_id] for u in units]


def prompt_manifest(prompt_dir: Path) -> dict[str, str]:
    """生成 manifest.json 的内容（新开提示词版本时用）。"""
    return {name: hashlib.sha256((prompt_dir / name).read_bytes()).hexdigest() for name in _PROMPT_FILES}
