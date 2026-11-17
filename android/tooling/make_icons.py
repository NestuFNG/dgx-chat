#!/usr/bin/env python
"""Generate the DGX Chat launcher icon set, adaptive-icon foreground, splash
image and a 512x512 showcase PNG.

Design: dark rounded-square tile with a subtle vertical gradient, a thin steel
border and the "DGX" wordmark in white above a short electric-blue accent bar
(the "chat line").

Run:
  python android/tooling/make_icons.py
"""
from __future__ import annotations

import os
from PIL import Image, ImageDraw, ImageFont

# .../android  (this file lives in .../android/tooling)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "android", "app", "src", "main", "res")
OUT = os.path.join(ROOT, "dist-out")

# First bold sans-serif we can find; override with the ICON_FONT env variable.
FONT_CANDIDATES = [
    os.environ.get("ICON_FONT", ""),
    r"C:\Windows\Fonts\arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
FONT_BOLD = next((p for p in FONT_CANDIDATES if p and os.path.isfile(p)), "")
if not FONT_BOLD:
    raise SystemExit("make_icons: no bold font found; set ICON_FONT to a .ttf path.")

BG_TOP = (27, 40, 56)      # #1B2838
BG_BOTTOM = (9, 14, 21)    # #090E15
BORDER = (51, 70, 94)      # #33465E
WORD = (240, 246, 255)     # #F0F6FF
ACCENT = (78, 161, 255)    # #4EA1FF

SS = 4  # supersampling


def vgrad(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)
        )
    return img.resize((w, h), Image.BILINEAR)


def rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size * SS, size * SS), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle(
        (0, 0, size * SS - 1, size * SS - 1), radius=radius * SS, fill=255
    )
    return m.resize((size, size), Image.LANCZOS)


def circle_mask(size: int) -> Image.Image:
    m = Image.new("L", (size * SS, size * SS), 0)
    d = ImageDraw.Draw(m)
    d.ellipse((0, 0, size * SS - 1, size * SS - 1), fill=255)
    return m.resize((size, size), Image.LANCZOS)


def draw_wordmark(img: Image.Image, size: int, scale: float = 1.0,
                  with_accent: bool = True) -> None:
    """Draw DGX + accent bar centred on a square canvas of `size` px."""
    d = ImageDraw.Draw(img)
    text = "DGX"
    target_w = size * 0.60 * scale
    fs = int(size * 0.40 * scale)
    font = None
    for _ in range(80):
        font = ImageFont.truetype(FONT_BOLD, fs)
        w = d.textlength(text, font=font)
        if w <= target_w:
            break
        fs -= 1
    if font is None:
        font = ImageFont.truetype(FONT_BOLD, max(8, fs))

    track = size * 0.022 * scale          # letter spacing
    total_w = d.textlength(text, font=font) + track * (len(text) - 1)
    # vertical metrics
    asc, desc = font.getmetrics()
    cap_h = asc - desc
    bar_h = max(2, int(size * 0.042 * scale))
    gap = size * 0.055 * scale
    block_h = cap_h + (gap + bar_h if with_accent else 0)
    top = (size - block_h) / 2.0

    x = (size - total_w) / 2.0
    for ch in text:
        d.text((x, top), ch, font=font, fill=WORD)
        x += d.textlength(ch, font=font) + track

    if with_accent:
        bar_w = size * 0.34 * scale
        r = bar_h / 2.0
        x0 = (size - bar_w) / 2.0
        y0 = top + cap_h + gap
        d.rounded_rectangle((x0, y0, x0 + bar_w, y0 + bar_h), radius=r, fill=ACCENT)


def make_tile(size: int, radius_frac: float = 0.22,
              circle: bool = False) -> Image.Image:
    grad = vgrad(size, size).convert("RGBA")
    # soft top-left sheen
    sheen = Image.new("L", (size, size), 0)
    sd = ImageDraw.Draw(sheen)
    sd.ellipse(
        (-size * 0.35, -size * 0.55, size * 0.85, size * 0.45),
        fill=26,
    )
    sheen = sheen.filter(ImageFilterGaussian(size * 0.05))
    highlight = Image.new("RGBA", (size, size), (120, 170, 235, 255))
    grad = Image.composite(
        Image.blend(grad, highlight, 0.28), grad, sheen
    )

    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(grad, (0, 0))

    d = ImageDraw.Draw(tile)
    bw = max(1, round(size * 0.012))
    inset = max(1, round(size * 0.008))
    if circle:
        d.ellipse((inset, inset, size - 1 - inset, size - 1 - inset),
                  outline=BORDER, width=bw)
        mask = circle_mask(size)
    else:
        r = size * radius_frac
        d.rounded_rectangle((inset, inset, size - 1 - inset, size - 1 - inset),
                            radius=max(2, r - inset), outline=BORDER, width=bw)
        mask = rounded_mask(size, int(r))

    draw_wordmark(tile, size)
    tile.putalpha(mask)
    return tile


def ImageFilterGaussian(r: float):
    from PIL import ImageFilter
    return ImageFilter.GaussianBlur(radius=max(0.5, r))


def make_foreground(size: int) -> Image.Image:
    """Adaptive icon foreground: transparent, glyph inside the 66% safe zone."""
    fg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw_wordmark(fg, size, scale=0.72, with_accent=True)
    return fg


def make_splash(w: int, h: int) -> Image.Image:
    img = vgrad(w, h).convert("RGBA")
    d = ImageDraw.Draw(img)
    logo = make_tile(int(min(w, h) * 0.30))
    lw = logo.size[0]
    img.alpha_composite(logo, ((w - lw) // 2, int(h * 0.40) - lw // 2))
    fs = int(min(w, h) * 0.055)
    font = ImageFont.truetype(FONT_BOLD, fs)
    label = "DGX Chat"
    tw = d.textlength(label, font=font)
    d.text(((w - tw) / 2, int(h * 0.40) + lw // 2 + int(min(w, h) * 0.05)),
           label, font=font, fill=(139, 152, 168))
    return img


DENSITIES = {
    "mdpi": (48, 108),
    "hdpi": (72, 162),
    "xhdpi": (96, 216),
    "xxhdpi": (144, 324),
    "xxxhdpi": (192, 432),
}


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for dens, (icon_sz, fg_sz) in DENSITIES.items():
        dpath = os.path.join(RES, f"mipmap-{dens}")
        os.makedirs(dpath, exist_ok=True)
        make_tile(icon_sz).save(os.path.join(dpath, "ic_launcher.png"))
        make_tile(icon_sz, circle=True).save(
            os.path.join(dpath, "ic_launcher_round.png"))
        make_foreground(fg_sz).save(
            os.path.join(dpath, "ic_launcher_foreground.png"))
        print(f"wrote mipmap-{dens} ({icon_sz}px, fg {fg_sz}px)")

    os.makedirs(os.path.join(RES, "drawable"), exist_ok=True)
    make_splash(1080, 1920).convert("RGB").save(
        os.path.join(RES, "drawable", "splash.png"))
    print("wrote drawable/splash.png")

    make_tile(512).save(os.path.join(OUT, "DGX-Chat-icon-512.png"))
    make_tile(1024).save(os.path.join(OUT, "DGX-Chat-icon-1024.png"))
    print("wrote dist-out/DGX-Chat-icon-512.png (+1024)")


if __name__ == "__main__":
    main()
