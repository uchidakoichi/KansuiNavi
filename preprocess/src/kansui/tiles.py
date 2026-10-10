"""タイル格子の計算。

★藤沢版からそのまま持ってきた（地域に依存しない）。片方だけ直さないこと。

元の説明:
Web Mercator タイル座標の計算。

★ このモジュールが返す EPSG:3857 座標は「タイル格子」を表すためのものであり、
   地上メートルではない。距離計算に使ってはならない（EPSG:6677 を使うこと）。
   実測: 藤沢(北緯35.34度)では測地線100mが EPSG:3857 上で約122.45m になる。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from affine import Affine

TILE_SIZE = 256
# Web Mercator の世界の半幅 [m]（= 赤道半径 6378137 × π）
EARTH_HALF_CIRCUMFERENCE = 20037508.342789244


@dataclass(frozen=True)
class TileRange:
    z: int
    x_min: int
    x_max: int
    y_min: int
    y_max: int

    @property
    def width(self) -> int:
        return self.x_max - self.x_min + 1

    @property
    def height(self) -> int:
        return self.y_max - self.y_min + 1

    @property
    def count(self) -> int:
        return self.width * self.height

    def __iter__(self):
        for y in range(self.y_min, self.y_max + 1):
            for x in range(self.x_min, self.x_max + 1):
                yield x, y


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int(math.floor((lon + 180.0) / 360.0 * n))
    lat_rad = math.radians(lat)
    y = int(math.floor((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n))
    # 端の丸め込み（lat=±85.05 付近や lon=180 でのはみ出しを防ぐ）
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_range_for_bbox(bbox: tuple[float, float, float, float], z: int) -> TileRange:
    """bbox = (min_lon, min_lat, max_lon, max_lat)。"""
    min_lon, min_lat, max_lon, max_lat = bbox
    x_min, y_max = lonlat_to_tile(min_lon, min_lat, z)  # 南西 → y は大きい
    x_max, y_min = lonlat_to_tile(max_lon, max_lat, z)  # 北東 → y は小さい
    return TileRange(z=z, x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max)


def buffer_bbox_km(
    bbox: tuple[float, float, float, float], km: float
) -> tuple[float, float, float, float]:
    """bbox を km 単位で膨らませる。経度方向は中心緯度の cos で補正する。

    ここでの近似（1度=111.320km）はタイル範囲を「多めに」取るための概算であり、
    解析用の距離計算には使わない。
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    center_lat = (min_lat + max_lat) / 2.0
    d_lat = km / 111.320
    d_lon = km / (111.320 * math.cos(math.radians(center_lat)))
    return (min_lon - d_lon, min_lat - d_lat, max_lon + d_lon, max_lat + d_lat)


def tile_transform_3857(x: int, y: int, z: int, size: int = TILE_SIZE) -> Affine:
    """タイルの EPSG:3857 アフィン変換。rasterio.features.rasterize にそのまま渡せる。"""
    tile_span = 2 * EARTH_HALF_CIRCUMFERENCE / (2**z)
    west = -EARTH_HALF_CIRCUMFERENCE + x * tile_span
    north = EARTH_HALF_CIRCUMFERENCE - y * tile_span
    res = tile_span / size
    return Affine(res, 0.0, west, 0.0, -res, north)


def tile_bounds_lonlat(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """タイルの経緯度バウンズ (west, south, east, north)。"""
    n = 2**z

    def _lat(ty: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))

    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    return west, _lat(y + 1), east, _lat(y)


def child_tile(x: int, y: int, z: int, target_z: int, offset: int = 0) -> tuple[int, int]:
    """z のタイル内で、より深い target_z のタイル座標を返す。

    offset は 0 で北西端、scale//2 でおおよそ中央。DEM1A(z17) の代表点プローブに使う。
    """
    if target_z < z:
        raise ValueError("target_z must be >= z")
    scale = 2 ** (target_z - z)
    off = min(offset, scale - 1)
    return x * scale + off, y * scale + off


def parent_tile(x: int, y: int, z: int, target_z: int) -> tuple[int, int]:
    """z のタイルを含む、より浅い target_z のタイル座標を返す（DEM10B の z14 用）。"""
    if target_z > z:
        raise ValueError("target_z must be <= z")
    scale = 2 ** (z - target_z)
    return x // scale, y // scale
