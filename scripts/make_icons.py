"""Generates the Armory Fit PWA icons into public/ (run: npm run icons)."""
from PIL import Image, ImageDraw
import os

OUT = os.path.join(os.path.dirname(__file__), "..", "public")
BLUE = (30, 155, 255)
BG = (8, 10, 13)


def draw(size: int, safe: float) -> Image.Image:
    """safe = fraction of the canvas the glyph may use (maskable icons need ~0.6)."""
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)
    s = size
    r = int(s * safe / 2)
    cx = cy = s // 2
    # shield outline
    pts = [(cx - r, cy - r * 0.8), (cx, cy - r), (cx + r, cy - r * 0.8), (cx + r, cy + r * 0.1),
           (cx, cy + r), (cx - r, cy + r * 0.1)]
    d.polygon(pts, outline=BLUE, width=max(4, s // 40))
    # bold "A" chevron
    w = r * 0.62
    top = cy - r * 0.5
    bot = cy + r * 0.45
    t = max(6, s // 26)
    d.line([(cx - w, bot), (cx, top), (cx + w, bot)], fill=(255, 255, 255), width=t * 2, joint="curve")
    d.line([(cx - w * 0.45, cy + r * 0.12), (cx + w * 0.45, cy + r * 0.12)], fill=BLUE, width=t)
    return img


for name, size, safe in [("icon-192.png", 192, 0.78), ("icon-512.png", 512, 0.78),
                         ("icon-maskable-512.png", 512, 0.56), ("apple-touch-icon.png", 180, 0.78)]:
    draw(size, safe).save(os.path.join(OUT, name))
    print("wrote", name)
