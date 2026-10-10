"""タイル・ファイル取得層。

★藤沢版（`../../Kansui/preprocess/src/kansui/fetcher.py`）から**そのまま**持ってきた。
  絶対ルール3 の実装そのものなので、意図的に同じにしてある。片方だけ直さないこと。
  違いは `from_config` が読む config のキー名だけ（`tile.` → `tiles.`）。

元の説明:


★ 守るべきマナー（CLAUDE.md 絶対ルール3 / 要件定義書 T-04）:
   - 逐次リクエストのみ。同時接続1。並列化しない。
   - 1リクエストごとに sleep を入れる。
   - ローカルキャッシュ必須。**同一タイルの重複取得を絶対に行わない。**
   - 404（タイル不存在）もキャッシュする。存在確認の再問い合わせを防ぐため。
   - User-Agent を明示する（アプリ名・版・連絡先）。

地理院タイルに公式な数値レート上限は存在しない。規約にあるのは
「本サービスの正常な運用を妨げ、またはその恐れが生じる程度の負荷をサーバに与える通信に対して、
  予告なくアクセスの遮断を行う場合があります」という免責のみ。
よって sleep 値は PoC 側の自主基準である。

★★2026-09-26（全国版のみ）: sleep を 0 にした（逐次・同時接続1 は維持）。根拠と経緯は
  config.yaml の tiles 節。**待ちを無くした代わりに、サーバの合図に従う**:
  429/503 を受けたら Retry-After（無ければ指数バックオフ）だけ待ち、
  その実行の残りは `sleep_after_throttle_sec` の間隔に戻す。
  ★藤沢版とはここが違う（藤沢版は 1.0秒のまま）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

NOT_FOUND = object()  # 404 を「取得成功して不存在が確定した」状態として表す番兵


@dataclass
class FetchStats:
    network: int = 0
    cache_hit: int = 0
    negative_cache_hit: int = 0
    not_found: int = 0
    bytes_downloaded: int = 0
    retries: int = 0
    throttled: int = 0      # 429/503 を受けた回数

    def summary(self) -> str:
        return (
            f"ネットワーク取得 {self.network}件 / キャッシュヒット {self.cache_hit}件 / "
            f"不存在キャッシュ {self.negative_cache_hit}件 / 404 {self.not_found}件 / "
            f"リトライ {self.retries}回 / 429・503 {self.throttled}回 / 転送量 {self.bytes_downloaded / 1e6:.1f}MB"
        )


@dataclass
class TileFetcher:
    cache_dir: Path
    user_agent: str
    sleep_sec: float = 1.0
    timeout_sec: int = 30
    max_retries: int = 3
    retry_backoff_sec: float = 2.0
    cache_negative: bool = True
    #: ★429/503 を受けた後の間隔と、Retry-After の上限（config の tiles 節）
    sleep_after_throttle_sec: float = 1.0
    max_retry_after_sec: float = 900.0
    stats: FetchStats = field(default_factory=FetchStats)
    _session: requests.Session | None = field(default=None, init=False, repr=False)
    _last_request_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})

    def _cache_path(self, url: str) -> Path:
        # URL のパスをそのままディレクトリ構造にする（例: xyz/dem5a_png/15/29077/12938.png）
        return self.cache_dir / urlparse(url).path.lstrip("/")

    def _throttle(self) -> None:
        """前回のネットワークリクエストから sleep_sec 以上空ける。"""
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.sleep_sec:
            time.sleep(self.sleep_sec - elapsed)

    def fetch(self, url: str) -> bytes | object | None:
        """URL を取得する。

        戻り値:
          bytes     … 取得成功（キャッシュ含む）
          NOT_FOUND … 404。タイルが存在しないことが確定
          None      … リトライを尽くして失敗（ネットワーク障害・5xx）
        """
        path = self._cache_path(url)
        miss_marker = path.with_suffix(path.suffix + ".404")

        if path.exists():
            self.stats.cache_hit += 1
            return path.read_bytes()
        if self.cache_negative and miss_marker.exists():
            self.stats.negative_cache_hit += 1
            return NOT_FOUND

        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                resp = self._session.get(url, timeout=self.timeout_sec)
            except requests.RequestException as exc:
                log.warning("取得失敗 (%s/%s) %s: %s", attempt + 1, self.max_retries, url, exc)
            else:
                if resp.status_code == 200:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # ★一時ファイルに書いてから置き換える。途中で止まった（空き不足・強制終了）
                    #   壊れた PNG が残ると、その地域は作り直しても同じタイルで落ち続ける
                    #   （2026-09-23 のレビューで指摘）
                    part = path.with_suffix(path.suffix + ".part")
                    part.write_bytes(resp.content)
                    part.replace(path)
                    self.stats.network += 1
                    self.stats.bytes_downloaded += len(resp.content)
                    return resp.content
                if resp.status_code == 404:
                    self.stats.network += 1
                    self.stats.not_found += 1
                    if self.cache_negative:
                        miss_marker.parent.mkdir(parents=True, exist_ok=True)
                        miss_marker.write_bytes(b"")
                    return NOT_FOUND
                log.warning(
                    "HTTP %s (%s/%s) %s",
                    resp.status_code, attempt + 1, self.max_retries, url,
                )
                if resp.status_code in (429, 503):
                    # ★★サーバが「多すぎる／混んでいる」と言った。以後は間隔を空ける
                    if self.sleep_sec < self.sleep_after_throttle_sec:
                        log.warning("★%s を受けた。以後の間隔を %.1f秒に戻す",
                                    resp.status_code, self.sleep_after_throttle_sec)
                        self.sleep_sec = self.sleep_after_throttle_sec
                    self.stats.throttled += 1
                    # ★★次に始まるプロセス（連鎖の次の地域・やり直し）にも減速を引き継ぐ。
                    #   インスタンスの値だけ変えると、地域ごとに新しいプロセスが 0秒から始め、
                    #   合図を受けた直後のサーバへまた詰めて送る（6回目のレビューで指摘）
                    note_throttled(self.cache_dir)
                    wait = _retry_after_sec(resp.headers.get("Retry-After"))
                    if wait is not None and attempt < self.max_retries:
                        self.stats.retries += 1
                        time.sleep(min(max(wait, self.retry_backoff_sec * (2**attempt)),
                                       self.max_retry_after_sec))
                        continue

            if attempt < self.max_retries:
                self.stats.retries += 1
                # 指数バックオフ。遮断された場合に畳みかけないため。
                time.sleep(self.retry_backoff_sec * (2**attempt))

        log.error("リトライ上限に到達: %s", url)
        return None

    def fetch_file(self, url: str, dest: Path) -> Path:
        """zip 等の単発ファイルを取得する（キャッシュあり）。"""
        dest = Path(dest)
        if dest.exists():
            self.stats.cache_hit += 1
            return dest
        data = self.fetch(url)
        if data is None or data is NOT_FOUND:
            raise RuntimeError(f"取得できませんでした: {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    @classmethod
    def from_config(cls, cfg) -> TileFetcher:
        cache_dir = cfg.resolve_path("tiles.cache_dir")
        sleep = float(cfg.require("tiles.sleep_sec"))
        slow = float(cfg.require("tiles.sleep_after_throttle_sec"))
        # ★最近 429/503 を受けていたら、最初から減速した間隔で始める
        if recently_throttled(cache_dir, float(cfg.require("tiles.throttle_memory_sec"))):
            log.warning("★最近 429/503 を受けている（%s）。間隔 %.1f秒で始める",
                        throttle_marker(cache_dir), slow)
            sleep = max(sleep, slow)
        return cls(
            cache_dir=cache_dir,
            user_agent=cfg.require("tiles.user_agent"),
            sleep_sec=sleep,
            timeout_sec=int(cfg.require("tiles.timeout_sec")),
            max_retries=int(cfg.require("tiles.max_retries")),
            retry_backoff_sec=float(cfg.require("tiles.retry_backoff_sec")),
            cache_negative=bool(cfg.get("tiles.cache_negative", True)),
            sleep_after_throttle_sec=float(cfg.require("tiles.sleep_after_throttle_sec")),
            max_retry_after_sec=float(cfg.require("tiles.max_retry_after_sec")),
        )


def _retry_after_sec(value: str | None) -> float | None:
    """Retry-After（秒数 または HTTP 日付。RFC 9110 §10.2.3）→ 待つ秒数。読めなければ None。"""
    if not value:
        return None
    value = value.strip()
    # ★半角だけ（str.isdigit は全角「０」も数字と見なす）
    if value and all("0" <= c <= "9" for c in value):
        return float(value)
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    import datetime as _dt

    # ★HTTP-date は常に GMT（RFC 9110）。"-0000" や時差なしは naive で返るので UTC と見なす。
    #   見なさないと aware との引き算で TypeError になり、取得ごと落ちる（6回目のレビューで指摘）
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds())


def throttle_marker(cache_dir: Path) -> Path:
    """429/503 を受けたことを、後から始まるプロセスへ伝える印（中身は受けた時刻）。"""
    return Path(cache_dir) / ".throttled"


def recently_throttled(cache_dir: Path, within_sec: float) -> bool:
    m = throttle_marker(cache_dir)
    try:
        return (time.time() - float(m.read_text().strip() or 0)) < within_sec
    except (OSError, ValueError):
        return False


def note_throttled(cache_dir: Path) -> None:
    m = throttle_marker(cache_dir)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(f"{time.time():.0f}")
