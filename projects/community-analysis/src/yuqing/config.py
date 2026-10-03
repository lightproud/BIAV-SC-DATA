"""配置：读 config/default.toml，环境变量可覆盖；路径与密钥只走环境变量。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 环境变量清单（方案「环境变量」节）。值只来自运行环境，不进仓库。
ENV_VARS: dict[str, str] = {
    "DATA_ROOT": "产出目录，必须在仓库之外",
    "LAKE_ROOT": "数据湖路径，只读",
    "AUTHOR_SALT": "作者哈希用的盐，由人生成并保管",
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


def check_outside_repo(path: Path, what: str, repo: Path | None = None) -> Path:
    """产出目录必须在仓库之外，否则拒绝运行。"""
    repo = repo or repo_root()
    if _is_within(path, repo):
        raise ConfigError(f"{what} 必须在仓库目录之外：{path} 位于 {repo} 内")
    return path.resolve()


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
        return check_outside_repo(Path(self.require("DATA_ROOT")["DATA_ROOT"]), "DATA_ROOT")


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
    cfg = Config(params=params, env=env)
    if "DATA_ROOT" in env:  # 启动即检查：设了就必须在仓库外
        cfg.data_root()
    return cfg
