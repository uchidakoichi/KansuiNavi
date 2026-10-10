"""各ページの詳細図: 地図画像の位置と、赤い丸の中心（地図画像の画素座標）を決まった手順で求める。LLM を使わない。"""
import fitz, numpy as np, json
from scipy import ndimage
doc = fitz.open("000889858.pdf")
res = []
for pno, page in enumerate(doc, 1):
    # 詳細図＝ページ右下寄りの赤枠の中にある画像。ページ下半分・横中央にある画像のうち最も右下のもの
    W, H = page.rect.width, page.rect.height
    infos = [i for i in page.get_image_info(xrefs=True) if i["bbox"][0] > W * 0.25 and i["bbox"][1] > H * 0.6
             and i["bbox"][2] < W * 0.62 and (i["bbox"][2] - i["bbox"][0]) > W * 0.2]
    if not infos: res.append(dict(page=pno, err="詳細図が見つからない")); continue
    info = max(infos, key=lambda i: (i["bbox"][2]-i["bbox"][0])*(i["bbox"][3]-i["bbox"][1]))
    x0, y0, x1, y1 = info["bbox"]
    zoom = 4
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(x0, y0, x1, y1))
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].astype(int)
    red = (a[:, :, 0] > 200) & (a[:, :, 1] < 60) & (a[:, :, 2] < 60)
    lab, n = ndimage.label(red)
    cands = []
    for s in ndimage.find_objects(lab):
        h, w = s[0].stop - s[0].start, s[1].stop - s[1].start
        if 8 * zoom <= h <= 40 * zoom and 8 * zoom <= w <= 40 * zoom and 0.6 < h / w < 1.6:
            cands.append(((s[1].start + s[1].stop) / 2 / pix.w, (s[0].start + s[0].stop) / 2 / pix.h, w / pix.w, h / pix.h))
    # 画像そのもの（注記の無い元画像）も保存する
    img = doc.extract_image(info["xref"])
    fn = f"img/detail_{pno:02d}.{img['ext']}"; open(fn, "wb").write(img["image"])
    pix.save(f"img/render_{pno:02d}.png")
    res.append(dict(page=pno, image=fn, circles=cands))
for r in res: print(r)
json.dump(res, open("circles.json", "w"), indent=0)
