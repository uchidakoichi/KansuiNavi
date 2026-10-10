"""詳細図の画像を地理院タイル（標準地図 z17）に重ねて、赤い丸の経緯度を画像だけから求める。LLM も OSM も使わない。
★タイル取得はプロジェクトの TileFetcher（逐次・キャッシュ・UA。絶対ルール3）。範囲は地先名の代表点の周り。"""
import sys, json, math, io, os
import numpy as np, cv2
from PIL import Image
sys.path.insert(0, ".")
from kansui.config import Config
from kansui.fetcher import TileFetcher, NOT_FOUND
import locate as L   # 地先名の代表点（places.json）
from rows import ROWS
cfg = Config.load(); F = TileFetcher.from_config(cfg)
Z = 17; R_M = 1600
URL = "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png"
def tile_xy(lon, lat, z):
    n = 2**z; return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
def to_ll(fx, fy, z):
    n = 2**z; return fx / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * fy / n))))
circ = {c["page"]: c for c in json.load(open(os.path.join(os.environ["NK09_PILOT_TMP"], "circles.json")))}
out = []
for no, mgr, route, dname, alias, rk, dists, cross in ROWS:
    pts = [p for d in dists[1:] for p in L.district(dists[0], d)]
    clo, cla = np.mean([p[0] for p in pts]), np.mean([p[1] for p in pts])
    fx, fy = tile_xy(clo, cla, Z)
    span = R_M / (40075016 * math.cos(math.radians(cla)) / 2**Z)   # タイル枚数（片側）
    x0, x1, y0, y1 = int(fx - span), int(fx + span), int(fy - span), int(fy + span)
    mos = np.full(((y1 - y0 + 1) * 256, (x1 - x0 + 1) * 256, 3), 255, np.uint8)
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            d = F.fetch(URL.format(z=Z, x=x, y=y))
            if d is None: raise SystemExit("通信の失敗")
            if d is NOT_FOUND: continue
            mos[(y - y0) * 256:(y - y0 + 1) * 256, (x - x0) * 256:(x - x0 + 1) * 256] = np.array(Image.open(io.BytesIO(d)).convert("RGB"))
    c = circ[no]; tpl = np.array(Image.open(os.path.join(os.environ["NK09_PILOT_TMP"], c["image"])).convert("RGB"))
    cx, cy = c["circles"][0][0] * tpl.shape[1], c["circles"][0][1] * tpl.shape[0]
    g = lambda im: cv2.Canny(cv2.cvtColor(im, cv2.COLOR_RGB2GRAY), 60, 160).astype(np.float32)
    gm = cv2.GaussianBlur(g(mos), (0, 0), 2)
    best = (-1,)
    for s in np.geomspace(0.15, 1.6, 40):
        t = cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        if t.shape[0] < 40 or t.shape[0] >= gm.shape[0] or t.shape[1] >= gm.shape[1]: continue
        gt = cv2.GaussianBlur(g(t), (0, 0), 2)
        r = cv2.matchTemplate(gm, gt, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx > best[0]:
            # 2番目の山（1位から 1/3 テンプレ幅以上離れた所）との差も記録する
            rr = r.copy(); h, w = gt.shape; yy, xx = loc[1], loc[0]
            rr[max(0, yy - h // 3):yy + h // 3, max(0, xx - w // 3):xx + w // 3] = -1
            best = (mx, s, loc, float(rr.max()))
    mx, s, loc, second = best
    px, py = loc[0] + cx * s, loc[1] + cy * s
    lon, lat = to_ll(x0 + px / 256, y0 + py / 256, Z)
    out.append(dict(no=no, lon=round(lon, 6), lat=round(lat, 6), score=round(mx, 3), second=round(second, 3), scale=round(s, 3),
                    m_per_px=round(40075016 * math.cos(math.radians(lat)) / 2**Z / 256 / s, 2)))
    print(out[-1], flush=True)
json.dump(out, open(os.path.join(os.environ["NK09_PILOT_TMP"], "register_out.json"), "w"))
