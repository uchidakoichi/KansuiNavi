"""config.yaml の読み込みと検証。

★藤沢版（`../../Kansui/preprocess/src/kansui/config.py`）から**そのまま**持ってきた。
  意図的に同じにしてある。片方だけ直さないこと。

未確定パラメータ（None）は「まだ決まっていない」ことを表す正当な値であり、
エラーにしない。使う側が require() で明示的に要求したときだけ失敗させる。
★これが絶対ルール10（未確定値を推測で埋めない）を**仕組みとして**支えている。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PREPROCESS_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PREPROCESS_ROOT / "config.yaml"


class ConfigError(RuntimeError):
    pass


class Config:
    def __init__(self, data: dict[str, Any], path: Path):
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path: Path | str | None = None) -> Config:
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        with p.open(encoding="utf-8") as f:
            return cls(yaml.safe_load(f), p)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in dotted.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def require(self, dotted: str) -> Any:
        """未確定(None)なら、推測で埋めずに落とす。"""
        value = self.get(dotted, None)
        if value is None:
            raise ConfigError(
                f"{self.path.name} の '{dotted}' が未確定(null)です。\n"
                "推測値で埋めず、一次情報を確認してから設定してください。"
            )
        return value

    def resolve_path(self, dotted: str) -> Path:
        """config 内の相対パスを preprocess/ 基準で絶対化する。"""
        return (PREPROCESS_ROOT / str(self.require(dotted))).resolve()

    def set(self, dotted: str, value: Any) -> None:
        node = self._data
        keys = dotted.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value

    @property
    def data(self) -> dict[str, Any]:
        return self._data
