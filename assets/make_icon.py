"""llterm.ico 生成スクリプト (Pillow).

デザイン = llterm v1 の本質をそのまま図案化:
- ダークな端末ウィンドウ (上部 = 子 TUI の素通し出力領域)
- 最下部に明るいアクセント色の「分離入力欄」バー + `ll>` プロンプト
実行:  py -3.11 assets/make_icon.py  →  assets/llterm.ico (マルチサイズ)
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent

# catppuccin mocha 系パレット (ll ファミリーの落ち着いたダーク)
BG = (30, 30, 46, 255)        # base
BORDER = (88, 91, 112, 255)   # surface2
TITLE = (49, 50, 68, 255)     # surface0
OUT_LINE = (108, 112, 134, 255)  # overlay0 (出力行の淡い線)
ACCENT = (137, 180, 250, 255)    # blue (分離入力欄)
PROMPT_FG = (17, 17, 27, 255)    # crust (入力欄上の文字)
DOT_R = (243, 139, 168, 255)
DOT_Y = (249, 226, 175, 255)
DOT_G = (166, 227, 161, 255)


def draw_icon(size: int = 256) -> Image.Image:
    s = size / 256.0  # スケール係数 (基準 256)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def px(v: float) -> int:
        return max(1, round(v * s))

    # 端末ウィンドウ本体 (角丸)
    m = px(14)
    radius = px(28)
    d.rounded_rectangle([m, m, size - m, size - m], radius=radius,
                        fill=BG, outline=BORDER, width=px(6))

    # タイトルバー + 信号ドット 3 つ
    bar_h = px(40)
    d.rounded_rectangle([m, m, size - m, m + bar_h + radius], radius=radius, fill=TITLE)
    d.rectangle([m, m + bar_h, size - m, m + bar_h + radius], fill=BG)  # 下側の角を消す
    cy = m + bar_h // 2
    for i, c in enumerate((DOT_R, DOT_Y, DOT_G)):
        cx = m + px(28) + i * px(34)
        r = px(9)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)

    # 出力領域: 子 TUI の出力を模した淡い線 (長さまちまち)
    x0 = m + px(24)
    y = m + bar_h + px(26)
    line_h = px(10)
    gap = px(22)
    for frac in (0.78, 0.55, 0.88, 0.42, 0.66):
        x1 = x0 + (size - 2 * m - px(48)) * frac
        d.rounded_rectangle([x0, y, x1, y + line_h], radius=line_h // 2, fill=OUT_LINE)
        y += gap

    # 分離入力欄 (llterm の特徴): 下部の明るいバー
    in_h = px(52)
    iy1 = size - m - px(18)
    iy0 = iy1 - in_h
    d.rounded_rectangle([m + px(14), iy0, size - m - px(14), iy1],
                        radius=px(12), fill=ACCENT)

    # プロンプト "ll>" + キャレット
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/consolab.ttf", px(38))
    except OSError:
        font = ImageFont.load_default()
    text = "ll>"
    tx = m + px(28)
    bbox = d.textbbox((0, 0), text, font=font)
    ty = iy0 + (in_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
    d.text((tx, ty), text, font=font, fill=PROMPT_FG)
    cx0 = tx + (bbox[2] - bbox[0]) + px(14)
    d.rectangle([cx0, iy0 + px(12), cx0 + px(16), iy1 - px(12)], fill=PROMPT_FG)

    return img


def main() -> None:
    base = draw_icon(256)
    out = HERE / "llterm.ico"
    base.save(out, format="ICO",
              sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    # プレビュー用 PNG も残す (README/将来の配布物用)
    base.save(HERE / "llterm_256.png", format="PNG")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
