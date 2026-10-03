"""配置：读 config/default.toml，环境变量可覆盖；路径与密钥只走环境变量。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 环境变量清单（方案「环境变量」节）。值只来自运行环境，不进仓库。
ENV_VARS: dict[str, str] = {
    "DATA_ROOT": "产出目录；不设则用本子项目 data/（守密人 2026-10-03：产出留仓内）",
    "LAKE_ROOT": "数据湖路径，只读",
    "GATEWAY_BASE_URL": "模型网关地址",
    "GATEWAY_API_KEY": "模型网关密钥",
    "MODEL_LOW": "低价档模型名",
    "MODEL_MID": "中档模型名",
    "EMBED_BACKEND": "向量后端：local 或 gateway，及模型路径或名称",
    "DINGTALK_WEBHOOK": "推送地址，可空",
    "OPS_DROP": "运营聚合文件目录，仅黑池",
    "DECISIONS_PATH": "裁定簿路径",
}

PARAM_ENV_PREFIX = "YUQING_"


class ConfigError(RuntimeError):
    """配置缺失或不合规；消息直接写明是哪一项。"""


def project_root() -> Path:
    """子项目根：向上找含 config/default.toml 的目录。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "default.toml").is_file():
            return parent
    return Path.cwd()


def repo_root(start: Path | None = None) -> Path:
    """所在 git 仓库根；找不到 .git 就退回子项目根。"""
    base = (start or project_root()).resolve()
    for parent in (base, *base.parents):
        if (parent / ".git").exists():
            return parent
    return base


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def lake_roots(environ: dict[str, str] | None = None) -> list[Path]:
    """数据湖所在：本仓 Record/Community，以及设了的 LAKE_ROOT。"""
    env = os.environ if environ is None else environ
    roots = [repo_root() / "Record" / "Community"]
    if env.get("LAKE_ROOT"):
        roots.append(Path(env["LAKE_ROOT"]))
    return roots


def check_not_in_lake(path: Path, what: str, lakes: list[Path] | None = None) -> Path:
    """产出可以在仓库里（守密人 2026-10-03），但不能写进数据湖：原文只读。"""
    for lake in lakes if lakes is not None else lake_roots():
        if _is_within(path, lake):
            raise ConfigError(f"{what} 不能放进数据湖（原文只读）：{path} 位于 {lake} 内")
    return path.resolve()


def default_data_root() -> Path:
    return project_root() / "data"


def _coerce(raw: str, default: object, name: str) -> object:
    try:
        if isinstance(default, bool):
            if raw.lower() in ("1", "true", "yes", "on"):
                return True
            if raw.lower() in ("0", "false", "no", "off"):
                return False
            raise ValueError(raw)
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name}={raw!r} 无法转成 {type(default).__name__}") from exc
    return raw


@dataclass
class Config:
    params: dict[str, dict[str, object]]
    env: dict[str, str] = field(default_factory=dict)
    lakes: list[Path] | None = None

    def get(self, section: str, key: str) -> object:
        try:
            return self.params[section][key]
        except KeyError as exc:
            raise ConfigError(f"缺少参数 {section}.{key}（config/default.toml）") from exc

    def require(self, *names: str) -> dict[str, str]:
        """取必填环境变量；缺哪个就报哪个。"""
        missing = [n for n in names if not self.env.get(n)]
        if missing:
            detail = "、".join(f"{n}（{ENV_VARS.get(n, '')}）" for n in missing)
            raise ConfigError(f"缺少必填项：{detail}")
        return {n: self.env[n] for n in names}

    def data_root(self) -> Path:
        path = Path(self.env["DATA_ROOT"]) if self.env.get("DATA_ROOT") else default_data_root()
        return check_not_in_lake(path, "DATA_ROOT", self.lakes)


def load_config(path: Path | None = None, environ: dict[str, str] | None = None) -> Config:
    path = path or project_root() / "config" / "default.toml"
    environ = dict(os.environ if environ is None else environ)
    if not path.is_file():
        raise ConfigError(f"找不到参数文件：{path}")
    with path.open("rb") as fh:
        params: dict[str, dict[str, object]] = tomllib.load(fh)
    for section, values in params.items():
        for key, default in values.items():
            name = f"{PARAM_ENV_PREFIX}{section}_{key}".upper()
            if name in environ:
                values[key] = _coerce(environ[name], default, name)
    env = {name: environ[name] for name in ENV_VARS if environ.get(name)}
    cfg = Config(params=params, env=env, lakes=lake_roots(environ))
    if "DATA_ROOT" in env:  # 启动即检查：设了就不能在数据湖里
        cfg.data_root()
    return cfg
