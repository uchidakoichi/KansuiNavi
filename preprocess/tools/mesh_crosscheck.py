"""★TypeScript 側が吐いた区画コードを、Python 側と突き合わせる。

★ここがずれると黙って壊れる（`web/tools/mesh-crosscheck.mjs` の説明を参照）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from kansui.mesh import mesh_code, meshes_for_bbox, neighbors

SRC = Path("/tmp/ts_mesh.json")


def main() -> int:
    if not SRC.exists():
        print(f"★{SRC} が無い。先に TypeScript 側を走らせること")
        return 1
    d = json.loads(SRC.read_text(encoding="utf-8"))
    bad = [(lon, lat, lv, got, mesh_code(lon, lat, lv))
           for lon, lat, lv, got in d["out"] if mesh_code(lon, lat, lv) != got]
    badb = [(bb, got, meshes_for_bbox(*bb, 2))
            for bb, got in d["boxes"] if meshes_for_bbox(*bb, 2) != got]
    print(f"点の照合: {len(d['out']):,} 件 / ★不一致 {len(bad)} 件")
    for b in bad[:10]:
        print("   ", b)
    print(f"bbox の照合: {len(d['boxes'])} 件 / ★不一致 {len(badb)} 件")
    for b in badb:
        print("   ", b)
    badn = [(c, got, neighbors(c, 1)) for c, got in d.get("nbs", [])
            if sorted(neighbors(c, 1)) != sorted(got)]
    print(f"隣の照合: {len(d.get('nbs', []))} 件 / ★不一致 {len(badn)} 件")
    for b in badn:
        print("   ", b)
    ok = not bad and not badb and not badn
    print("\n" + ("★TypeScript と Python が一致" if ok else "★★不一致がある。直すこと"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
