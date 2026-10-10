"""道路冠水想定箇所（NK-09）を道路の区間に結び付ける・配布用の一覧を作る（2026-10-02）。

★段（tier）の扱いは利用者の判断（2026-10-02）:
  confirmed … 経路計算に効かせる（区間を state=known・最上位のスコアに）
  uncertain … 表示だけ（おおよその円）。経路計算には効かせない
  unlocated … 地図に出さない（資料の一覧だけ）
★確定の点でも、**橋の上の区間には結び付けない**（立体交差では点のすぐ上を橋が通る。
  冠水するのは下の道。山梨 No.1 は最寄りの道路が上を通るスバルラインだった）。
★結び付けられなかった確定の点は uncertain に落とす（黙って経路計算から外さない・黙って効かせない）。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TIERS = ("confirmed", "uncertain", "unlocated")


def _seg_dist_m(lon: float, lat: float, lon0, lat0, lon1, lat1) -> np.ndarray:
    k = 111320.0 * math.cos(math.radians(lat))
    ax, ay = (lon0 - lon) * k, (lat0 - lat) * 110570.0
    bx, by = (lon1 - lon) * k, (lat1 - lat) * 110570.0
    dx, dy = bx - ax, by - ay
    t = np.clip(-(ax * dx + ay * dy) / np.maximum(dx * dx + dy * dy, 1e-9), 0.0, 1.0)
    return np.hypot(ax + t * dx, ay + t * dy)


def known_mask(points: pd.DataFrame, lon0, lat0, lon1, lat1, label, eligible,
               snap_m: float, same_label_m: float) -> tuple[np.ndarray, list[bool]]:
    """確定の点を区間に結び付ける。

    points   … lon / lat の列を持つ（その地域の confirmed だけ）
    label    … 区間の道路名（無ければ路線番号）。上下線が別の区間になっている道をまとめて拾うため
    eligible … 結び付けてよい区間（配布に入る・橋でない）
    戻り値   … (区間ごとの known の印, 点ごとに結び付けられたか)
    手順: 点から snap_m 以内で最も近い区間を採り、同じ名前で same_label_m 以内の区間も印を付ける
          （名前が無ければ最も近い区間と、その逆向きの区間だけ）
    """
    lon0, lat0, lon1, lat1 = (np.asarray(a, np.float64) for a in (lon0, lat0, lon1, lat1))
    label = np.asarray(pd.Series(label).fillna("").astype(str))
    eligible = np.asarray(eligible, bool)
    mask = np.zeros(len(lon0), bool)
    snapped: list[bool] = []
    pad = max(snap_m, same_label_m) / 80000.0 + 1e-4
    for lon, lat in zip(points["lon"].astype(float), points["lat"].astype(float)):
        near = np.flatnonzero(eligible
                              & (np.minimum(lon0, lon1) < lon + pad) & (np.maximum(lon0, lon1) > lon - pad)
                              & (np.minimum(lat0, lat1) < lat + pad) & (np.maximum(lat0, lat1) > lat - pad))
        if not len(near):
            snapped.append(False)
            continue
        d = _seg_dist_m(lon, lat, lon0[near], lat0[near], lon1[near], lat1[near])
        i = int(np.argmin(d))
        if d[i] > snap_m:
            snapped.append(False)
            continue
        best = near[i]
        lab = label[best]
        if lab:
            pick = near[(d <= same_label_m) & (label[near] == lab)]
        else:
            same = ((np.isclose(lon0[near], lon1[best]) & np.isclose(lat0[near], lat1[best])
                     & np.isclose(lon1[near], lon0[best]) & np.isclose(lat1[near], lat0[best])))
            pick = np.r_[best, near[same]]
        mask[pick] = True
        snapped.append(True)
    return mask, snapped


def display_records(points: pd.DataFrame) -> list[dict]:
    """配布する一覧（road_kansui.json の items）。★画面で目視の確認を案内するための情報も入れる。"""
    out = []
    def s(v) -> str:
        # ★空欄（NaN / None）は空文字にする。NaN を JSON に書くと不正な値になる
        return "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)
    for _, r in points.iterrows():
        rec = {
            "id": s(r["id"]), "tier": s(r["tier"]), "route": s(r["route"]), "place": s(r["place"]),
            "manager": s(r["manager"]), "method": s(r["method"]),
            "src": {"title": s(r["src_title"]), "publisher": s(r["src_publisher"]), "date": s(r["src_date"]),
                    "url": s(r["src_url"]), "page": int(float(r["check_page"])), "hint": s(r["check_hint"])},
        }
        if r["tier"] != "unlocated":
            rec["lon"], rec["lat"] = round(float(r["lon"]), 6), round(float(r["lat"]), 6)
        if r["tier"] == "uncertain":
            rec["radiusM"] = int(float(r["radius_m"]))
        out.append(rec)
    return out
