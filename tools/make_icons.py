"""Regenerate the PWA icons from the SVG source (needs rsvg-convert).

    python3 tools/make_icons.py
"""
import math
import os
import subprocess

OUT = os.path.join(os.path.dirname(__file__), "..", "app", "static", "icons")


def chip(cx, cy, r):
    parts = [f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#d8413c"/>']
    # 8 white edge inserts
    for i in range(8):
        a0 = math.radians(i * 45 - 9)
        a1 = math.radians(i * 45 + 9)
        r0, r1 = r * 0.74, r
        pts = [(cx + r1 * math.cos(a0), cy + r1 * math.sin(a0)), (cx + r1 * math.cos(a1), cy + r1 * math.sin(a1)),
               (cx + r0 * math.cos(a1), cy + r0 * math.sin(a1)), (cx + r0 * math.cos(a0), cy + r0 * math.sin(a0))]
        d = (f"M{pts[0][0]:.2f},{pts[0][1]:.2f} A{r1},{r1} 0 0 1 {pts[1][0]:.2f},{pts[1][1]:.2f} "
             f"L{pts[2][0]:.2f},{pts[2][1]:.2f} A{r0},{r0} 0 0 0 {pts[3][0]:.2f},{pts[3][1]:.2f} Z")
        parts.append(f'<path d="{d}" fill="#f6f1e6"/>')
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r * 0.62}" fill="none" stroke="#f6f1e6" stroke-width="{r * 0.045:.2f}" '
                 f'stroke-dasharray="{r * 0.12:.2f} {r * 0.07:.2f}"/>')
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r * 0.52}" fill="#b5302c"/>')
    # spade
    s = r * 0.0042
    parts.append(
        f'<g transform="translate({cx},{cy + r * 0.03}) scale({s:.4f})">'
        '<path d="M0,-75 C25,-45 70,-20 70,12 C70,40 45,55 22,48 C14,45 8,40 5,34 '
        'C8,52 16,66 30,76 L-30,76 C-16,66 -8,52 -5,34 C-8,40 -14,45 -22,48 '
        'C-45,55 -70,40 -70,12 C-70,-20 -25,-45 0,-75 Z" fill="#f6f1e6"/></g>')
    return "".join(parts)


def svg(full_bleed: bool, chip_r: float) -> str:
    bg_r = 0 if full_bleed else 112
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
<defs><radialGradient id="g" cx="30%" cy="20%" r="95%"><stop offset="0" stop-color="#2a8a59"/><stop offset=".55" stop-color="#15583a"/><stop offset="1" stop-color="#0c3424"/></radialGradient></defs>
<rect width="512" height="512" rx="{bg_r}" fill="url(#g)"/>
<circle cx="262" cy="270" r="{chip_r}" fill="#000" opacity=".28"/>
{chip(256, 256, chip_r)}
</svg>
'''


def main():
    os.makedirs(OUT, exist_ok=True)
    any_svg = svg(False, 176)
    mask_svg = svg(True, 150)  # inside the 80% maskable safe zone
    with open(os.path.join(OUT, "icon.svg"), "w") as f:
        f.write(any_svg)
    tmp = os.path.join(OUT, "_mask.svg")
    with open(tmp, "w") as f:
        f.write(mask_svg)
    for name, src, size in (("icon-192.png", "icon.svg", 192), ("icon-512.png", "icon.svg", 512),
                            ("icon-maskable-512.png", "_mask.svg", 512), ("apple-touch-icon.png", "_mask.svg", 180)):
        subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), "-o", os.path.join(OUT, name),
                        os.path.join(OUT, src)], check=True)
    os.remove(tmp)


if __name__ == "__main__":
    main()
