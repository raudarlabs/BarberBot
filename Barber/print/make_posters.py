"""
Printable QR posters for the Telegram bot.

    pip install segno
    python print/make_posters.py            # writes print/*.html, then renders PDF + PNG with Chrome

Each poster links to t.me/custom_true_bot?start=<source>, so the bot counts who came from where
(dashboard → Ігри → "Звідки приходять у бот"). Colours and fonts follow the website
(raudarlabs/Barb-Landing: src/styles/global.css). The logo is the site's src/assets/brand/logo.svg.
"""
import os
import shutil
import subprocess
import sys

import segno

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = "https://t.me/custom_true_bot?start=%s"
LOGO = os.environ.get("CT_LOGO_SVG", "/Volumes/Claude/Barber/src/assets/brand/logo.svg")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

INK, SMOKE, LINE, BONE, ASH, GOLD, GOLD_HI, GOLD_LO = ("#0b0a08", "#1d1a16", "#2c2720", "#efe8da", "#a59d8f",
                                                     "#d4af5a", "#f6e3a1", "#9c7630")
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Cormorant:ital,wght@0,500;0,600;1,600&family=Manrope:wght@400;600;700&display=block" rel="stylesheet">')
PERKS = ["Запис за хвилину — майстер, послуга, час",
         "Нагадування й перенесення в один дотик",
         "Звільнився час — напишемо першими",
         "Ігри з призом щомісяця"]


def qr_svg(source: str) -> str:
    q = segno.make(BOT % source, error="h")
    svg = q.svg_inline(dark=INK, light=None, border=0, omitsize=True)
    return svg


def logo_svg() -> str:
    s = open(LOGO, encoding="utf-8").read()
    return s.replace("<svg ", '<svg class="logo" ', 1)


BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:%(ink)s}
body{font-family:Manrope,system-ui,sans-serif;color:%(bone)s;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.page{position:relative;overflow:hidden;background:%(ink)s;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
.page>*{flex-shrink:0}
.glow{position:absolute;border-radius:50%%;filter:blur(18mm);opacity:.38;background:radial-gradient(circle,%(gold)s,transparent 70%%)}
.frame{position:absolute;inset:4mm;border:.35mm solid %(gold_lo)s;border-radius:5mm;opacity:.7}
.logo{color:%(gold)s;display:block}
.eyebrow{font-size:2.3mm;letter-spacing:.55mm;text-transform:uppercase;color:%(gold)s;font-weight:700}
h1{font-family:Cormorant,Georgia,serif;font-weight:600;line-height:.95;color:%(bone)s}
h1 em{font-style:italic;background:linear-gradient(100deg,%(gold_lo)s,%(gold_hi)s 45%%,%(gold)s 70%%,%(gold_lo)s);-webkit-background-clip:text;background-clip:text;color:transparent}
.tile{background:%(bone)s;border-radius:4mm;box-shadow:0 0 0 .5mm %(gold)s,0 0 9mm rgba(212,175,90,.35)}
.tile svg{display:block;width:100%%;height:100%%}
.handle{font-weight:700;color:%(gold_hi)s;letter-spacing:.1mm}
.hint{color:%(ash)s}
ul{list-style:none;text-align:left}
li{display:flex;gap:2mm;align-items:baseline;color:%(bone)s}
li:before{content:"✓";color:%(gold)s;font-weight:700}
""" % dict(ink=INK, bone=BONE, gold=GOLD, gold_lo=GOLD_LO, gold_hi=GOLD_HI, ash=ASH)


def desk() -> str:
    """A6 portrait, for the reception desk / a table stand."""
    perks = "".join("<li>%s</li>" % p for p in PERKS)
    return """<!doctype html><html lang="uk"><head><meta charset="utf-8"><title>Custom True — QR на рецепцію (A6)</title>%s
<style>%s
@page{size:105mm 148mm;margin:0}
.page{width:105mm;height:148mm;padding:9mm 9mm 8mm}
.logo{width:28mm;height:20mm;margin-bottom:3.5mm}
h1{font-size:10.2mm;margin:2mm 0 4.5mm}
.tile{width:46mm;height:46mm;padding:3mm}
.handle{font-size:4mm;margin-top:4mm}
.hint{font-size:2.7mm;margin-top:.8mm}
ul{margin-top:4.5mm;display:grid;gap:1.4mm;font-size:2.8mm}
</style></head><body>
<div class="page"><div class="glow" style="width:90mm;height:90mm;top:-40mm;right:-40mm"></div><div class="frame"></div>
%s
<p class="eyebrow">Telegram-бот</p>
<h1>Записуйтесь<br>у <em>Telegram</em></h1>
<div class="tile">%s</div>
<p class="handle">@custom_true_bot</p>
<p class="hint">Наведіть камеру телефону на код</p>
<ul>%s</ul>
</div></body></html>""" % (FONTS, BASE_CSS, logo_svg(), qr_svg("qr_desk"), perks)


def mirror() -> str:
    """100×100 mm square sticker for the mirrors."""
    return """<!doctype html><html lang="uk"><head><meta charset="utf-8"><title>Custom True — QR на дзеркало (100×100)</title>%s
<style>%s
@page{size:100mm 100mm;margin:0}
.page{width:100mm;height:100mm;padding:8mm 8mm 7mm;justify-content:center}
.logo{width:22mm;height:15.7mm;margin-bottom:2.5mm}
h1{font-size:7.4mm;margin:1.6mm 0 3.6mm}
.tile{width:42mm;height:42mm;padding:2.6mm}
.handle{font-size:3.6mm;margin-top:3mm}
.hint{font-size:2.5mm;margin-top:.8mm}
</style></head><body>
<div class="page"><div class="glow" style="width:70mm;height:70mm;bottom:-34mm;left:-30mm"></div><div class="frame"></div>
%s
<p class="eyebrow">Наступний візит</p>
<h1>Записуйтесь у <em>Telegram</em></h1>
<div class="tile">%s</div>
<p class="handle">@custom_true_bot</p>
<p class="hint">Нагадування · перенесення · вільний час</p>
</div></body></html>""" % (FONTS, BASE_CSS, logo_svg(), qr_svg("qr_mirror"))


def render(name: str, html: str, w_mm: float, h_mm: float) -> None:
    path = os.path.join(HERE, name + ".html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    if not os.path.exists(CHROME):
        print("Chrome not found: open %s and print to PDF" % path)
        return
    url = "file://" + path
    common = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--virtual-time-budget=8000",
              "--run-all-compositor-stages-before-draw"]
    subprocess.run(common + ["--no-pdf-header-footer", "--print-to-pdf=" + os.path.join(HERE, name + ".pdf"), url],
                   check=True, capture_output=True)
    px = lambda mm: round(mm / 25.4 * 96)  # noqa: E731
    subprocess.run(common + ["--force-device-scale-factor=3", "--window-size=%d,%d" % (px(w_mm), px(h_mm)),
                             "--screenshot=" + os.path.join(HERE, name + ".png"), url], check=True, capture_output=True)
    print("wrote", name + ".html/.pdf/.png")


if __name__ == "__main__":
    render("qr-desk-a6", desk(), 105, 148)
    render("qr-mirror-100", mirror(), 100, 100)
    sys.exit(0)
