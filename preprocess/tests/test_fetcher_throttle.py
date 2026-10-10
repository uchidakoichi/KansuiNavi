"""待ちを 0 にした取得器が、サーバの合図（429/503・Retry-After）に従うこと。"""

from __future__ import annotations

import datetime as dt
from email.utils import format_datetime

from kansui import fetcher as F


class _Resp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


def _fetcher(tmp_path, responses, sleeps, monkeypatch):
    f = F.TileFetcher(cache_dir=tmp_path, user_agent="test", sleep_sec=0.0, max_retries=3,
                      retry_backoff_sec=2.0, sleep_after_throttle_sec=1.0, max_retry_after_sec=900)
    it = iter(responses)
    monkeypatch.setattr(f._session, "get", lambda url, timeout: next(it))
    monkeypatch.setattr(F.time, "sleep", lambda s: sleeps.append(s))
    return f


def test_429_follows_retry_after_and_slows_down(tmp_path, monkeypatch):
    sleeps: list[float] = []
    f = _fetcher(tmp_path, [_Resp(429, headers={"Retry-After": "30"}), _Resp(200, b"png")],
                 sleeps, monkeypatch)
    assert f.fetch("https://example.test/a.png") == b"png"
    assert 30.0 in sleeps                      # ★Retry-After だけ待った
    assert f.sleep_sec == 1.0                  # ★以後は間隔を空ける
    assert f.stats.throttled == 1


def test_503_without_retry_after_uses_backoff(tmp_path, monkeypatch):
    sleeps: list[float] = []
    f = _fetcher(tmp_path, [_Resp(503), _Resp(200, b"png")], sleeps, monkeypatch)
    assert f.fetch("https://example.test/b.png") == b"png"
    assert 2.0 in sleeps and f.sleep_sec == 1.0


def test_retry_after_is_capped(tmp_path, monkeypatch):
    sleeps: list[float] = []
    f = _fetcher(tmp_path, [_Resp(429, headers={"Retry-After": "86400"}), _Resp(200, b"x")],
                 sleeps, monkeypatch)
    f.fetch("https://example.test/c.png")
    assert max(sleeps) == 900


def test_retry_after_http_date():
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=120)
    got = F._retry_after_sec(format_datetime(when, usegmt=True))
    assert 100 <= got <= 121
    assert F._retry_after_sec("garbage") is None
    assert F._retry_after_sec(None) is None


def test_500_does_not_slow_down(tmp_path, monkeypatch):
    """★5xx 全般で間隔を戻すのではない（一時的な障害と、多すぎるという合図は別）。"""
    sleeps: list[float] = []
    f = _fetcher(tmp_path, [_Resp(502), _Resp(200, b"png")], sleeps, monkeypatch)
    f.fetch("https://example.test/d.png")
    assert f.sleep_sec == 0.0


def test_retry_after_naive_date_is_utc():
    """★"-0000" や時差なしの日付で TypeError にしない（6回目のレビューで指摘）。"""
    got = F._retry_after_sec("Wed, 21 Oct 2099 07:28:00 -0000")
    assert got is not None and got > 0
    assert F._retry_after_sec("Wed, 21 Oct 2099 07:28:00") > 0
    assert F._retry_after_sec("０") is None          # ★全角は秒数と見なさない


def test_throttle_is_inherited_by_next_process(tmp_path, monkeypatch):
    """★429 を受けたら、後から作る取得器（次の地域のプロセス）も減速して始める。"""
    sleeps: list[float] = []
    f = _fetcher(tmp_path, [_Resp(429, headers={"Retry-After": "1"}), _Resp(200, b"png")],
                 sleeps, monkeypatch)
    f.fetch("https://example.test/e.png")
    assert F.recently_throttled(tmp_path, 3600)

    class _Cfg:
        v = {"tiles.cache_dir": tmp_path, "tiles.user_agent": "t", "tiles.sleep_sec": 0.0,
             "tiles.timeout_sec": 5, "tiles.max_retries": 3, "tiles.retry_backoff_sec": 2.0,
             "tiles.sleep_after_throttle_sec": 1.0, "tiles.max_retry_after_sec": 900,
             "tiles.throttle_memory_sec": 3600}

        def require(self, k):
            return self.v[k]

        def get(self, k, d=None):
            return self.v.get(k, d)

        def resolve_path(self, k):
            return self.v[k]

    assert F.TileFetcher.from_config(_Cfg()).sleep_sec == 1.0
    F.throttle_marker(tmp_path).unlink()
    assert F.TileFetcher.from_config(_Cfg()).sleep_sec == 0.0


def test_vector_tiles_429_retries_and_slows(tmp_path, monkeypatch):
    import io
    import urllib.error

    from kansui import gsi_vector as G

    calls = {"n": 0}

    class _Res(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "3"}, None)
        return _Res(b"mvt")

    sleeps: list[float] = []
    monkeypatch.setattr(G.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(G.time, "sleep", lambda s: sleeps.append(s))
    vt = G.VectorTiles(cache_dir=tmp_path, user_agent="t", sleep_sec=0.0)
    assert vt.fetch(16, 1, 2) == b"mvt"
    assert 3.0 in sleeps and vt.sleep_sec == 1.0
    assert F.recently_throttled(tmp_path, 3600)
