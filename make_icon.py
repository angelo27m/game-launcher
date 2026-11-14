"""Erzeugt das Launcher-Logo (L mit Play-Pfeil) als .ico + .png."""
import os
import sys
from PIL import Image, ImageDraw, ImageFilter

S = 1024
out_dir = sys.argv[1] if len(sys.argv) > 1 else "."


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# Hintergrund: diagonaler Verlauf Violett -> Blau -> Cyan
c1, c2, c3 = (88, 28, 220), (20, 110, 255), (0, 210, 255)
grad = Image.new("RGB", (S, S))
px = grad.load()
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S - 2)
        px[x, y] = lerp(c1, c2, t / 0.6) if t < 0.6 else lerp(c2, c3, (t - 0.6) / 0.4)

# Abgerundetes Quadrat als Maske
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=230, fill=255)
icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
icon.paste(grad, (0, 0), mask)

# Dunkle Glas-Flaeche innen fuer Tiefe
inner = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(inner).rounded_rectangle((70, 70, S - 70, S - 70), radius=200, fill=(10, 10, 30, 150))
icon = Image.alpha_composite(icon, inner)

# Glanz oben
shine = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shine).ellipse((-200, -650, S + 200, 420), fill=(255, 255, 255, 38))
shine_m = Image.new("L", (S, S), 0)
ImageDraw.Draw(shine_m).rounded_rectangle((40, 40, S - 40, S - 40), radius=230, fill=255)
icon.paste(Image.alpha_composite(icon, shine), (0, 0), shine_m)

# Das "L" (mit Glow)
L = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(L)
x0, y0, stem, foot, bottom = 270, 230, 150, 520, 790
d.rounded_rectangle((x0, y0, x0 + stem, bottom), radius=40, fill=(255, 255, 255, 255))           # senkrecht
d.rounded_rectangle((x0, bottom - stem, x0 + foot, bottom), radius=40, fill=(255, 255, 255, 255))  # Fuss
# Play-Dreieck oben rechts im Winkel des L
tx, ty = 520, 300
d.polygon([(tx, ty), (tx, ty + 270), (tx + 230, ty + 135)], fill=(255, 255, 255, 255))
glow = L.copy()
glow_col = Image.new("RGBA", (S, S), (0, 230, 255, 0))
glow_col.putalpha(glow.split()[3])
glow_col = glow_col.filter(ImageFilter.GaussianBlur(40))
icon = Image.alpha_composite(icon, glow_col)
icon = Image.alpha_composite(icon, glow_col)
icon = Image.alpha_composite(icon, L)

os.makedirs(out_dir, exist_ok=True)
icon.resize((512, 512), Image.LANCZOS).save(os.path.join(out_dir, "launcher.png"))
icon.save(os.path.join(out_dir, "launcher.ico"),
          sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
print("ok")
