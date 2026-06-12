"""llterm.ico 生成スクリプト (Pillow)。

llterm v0.2 で TUI → Qt GUI へ転換。アイコンも本質=「自走ループ駆動」を図案化:
- ダークな角丸地 (ll ファミリーの catppuccin パレット)
- 青い循環ループ矢印 (↻) = headless セッションを回す自走ループ
- 中央に緑のカーソルブロック = 実行中の端末 / Claude
実行:  py -3.11 assets/make_icon.py  →  assets/llterm.ico (マルチサイズ) + llterm_256.png
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent

BG = (30, 30, 46, 255)        # base
BORDER = (69, 71, 90, 255)    # surface1
LOOP = (137, 180, 250, 255)   # blue (自走ループ)
CURSOR = (166, 227, 161, 255)  # green (実行中カーソル)
SS = 4  # supersample 係数 (アンチエイリアス用)


def draw_icon(size: int = 256) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 角丸の地
    m = int(s * 0.055)
    radius = int(s * 0.18)
    d.rounded_rectangle([m, m, s - m, s - m], radius=radius, fill=BG,
                        outline=BORDER, width=max(2, int(s * 0.018)))

    # 循環ループ環 (上右に隙間)。PIL arc: 0°=3時, 時計回り。
    cx = cy = s / 2.0
    r = s * 0.30
    w = int(s * 0.095)
    end_deg = 262
    d.arc([cx - r, cy - r, cx + r, cy + r], start=0, end=end_deg, fill=LOOP, width=w)

    # 終端 (≈上) に進行方向(時計回り)の矢印
    ex = cx + r * math.cos(math.radians(end_deg))
    ey = cy + r * math.sin(math.radians(end_deg))
    tx, ty = -math.sin(math.radians(end_deg)), math.cos(math.radians(end_deg))  # 接線(時計回り)
    nx, ny = ty, -tx  # 法線
    ah = w * 1.5
    tip = (ex + tx * ah, ey + ty * ah)
    b1 = (ex + nx * ah * 0.95, ey + ny * ah * 0.95)
    b2 = (ex - nx * ah * 0.95, ey - ny * ah * 0.95)
    d.polygon([tip, b1, b2], fill=LOOP)

    # 中央: 端末カーソルブロック (緑)
    cw, ch = s * 0.075, s * 0.17
    d.rounded_rectangle([cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2],
                        radius=int(s * 0.02), fill=CURSOR)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    base = draw_icon(256)
    out = HERE / "llterm.ico"
    base.save(out, format="ICO",
              sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    base.save(HERE / "llterm_256.png", format="PNG")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
