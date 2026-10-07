"""Erzeugt das FotoArchiv-Icon: app/static/icon.svg, icon.png (Browser) sowie tools/paket/FotoArchiv.ico/.icns."""
import os

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(BASE, "app", "static")
PAKET = os.path.join(BASE, "tools", "paket")

TOP, BOTTOM = (59, 142, 234), (29, 95, 176)
SUN = (246, 196, 69)
SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3b8eea"/><stop offset="1" stop-color="#1d5fb0"/></linearGradient></defs>
<rect x="32" y="32" width="960" height="960" rx="220" fill="url(#g)"/>
<rect x="262" y="214" width="560" height="430" rx="56" fill="#fff" fill-opacity=".45"/>
<rect x="200" y="300" width="624" height="480" rx="60" fill="#fff"/>
<rect x="244" y="344" width="536" height="392" rx="28" fill="#dcebfd"/>
<circle cx="652" cy="450" r="52" fill="#f6c445"/>
<polygon points="244,736 244,640 410,470 560,640 620,580 780,736" fill="#2f7fd8"/>
<polygon points="244,736 244,690 330,610 470,736" fill="#1d5fb0"/>
</svg>
"""


def render(size):
    s = 4  # Kantenglättung
    n = 1024 * s
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    grad = Image.new("RGBA", (n, n))
    gd = ImageDraw.Draw(grad)
    for y in range(n):
        t = y / (n - 1)
        gd.line([(0, y), (n, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle([32 * s, 32 * s, 992 * s, 992 * s], 220 * s, fill=255)
    im.paste(grad, (0, 0), mask)
    over = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    od = ImageDraw.Draw(over)
    od.rounded_rectangle([262 * s, 214 * s, 822 * s, 644 * s], 56 * s, fill=(255, 255, 255, 115))
    im = Image.alpha_composite(im, over)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([200 * s, 300 * s, 824 * s, 780 * s], 60 * s, fill=(255, 255, 255, 255))
    d.rounded_rectangle([244 * s, 344 * s, 780 * s, 736 * s], 28 * s, fill=(220, 235, 253, 255))
    d.ellipse([600 * s, 398 * s, 704 * s, 502 * s], fill=SUN + (255,))
    pts = lambda p: [(x * s, y * s) for x, y in p]
    d.polygon(pts([(244, 736), (244, 640), (410, 470), (560, 640), (620, 580), (780, 736)]), fill=(47, 127, 216, 255))
    d.polygon(pts([(244, 736), (244, 690), (330, 610), (470, 736)]), fill=(29, 95, 176, 255))
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    os.makedirs(PAKET, exist_ok=True)
    with open(os.path.join(STATIC, "icon.svg"), "w", encoding="utf-8") as f:
        f.write(SVG)
    big = render(1024)
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(STATIC, "icon.png"))
    big.save(os.path.join(PAKET, "FotoArchiv.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(os.path.join(PAKET, "FotoArchiv.icns"))
    big.resize((512, 512), Image.LANCZOS).save(os.path.join(PAKET, "icon-512.png"))
    print("Icons geschrieben.")
