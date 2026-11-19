"""Z předlohy assets/mailer-256.png vygeneruje ikonu aplikace a obrázky pro web.

Vytvoří:
  assets/mailer.ico   ikona exe a okna (16–256 px)
  assets/mailer-120.png   logo pro Google OAuth (120×120)
  site/icon.png, site/icon-32.png, site/apple-touch-icon.png   obrázky webu

Potřebuje Pillow (jen pro vývojáře, aplikace ho nepotřebuje): pip install pillow
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "mailer-256.png"
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def scaled(img, size):
    return img if img.width == size else img.resize((size, size), Image.LANCZOS)


def main():
    img = Image.open(SRC).convert("RGBA")
    if img.size != (256, 256):
        img = img.resize((256, 256), Image.LANCZOS)
    ico = ROOT / "assets" / "mailer.ico"
    img.save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    scaled(img, 120).save(ROOT / "assets" / "mailer-120.png", optimize=True)
    site = ROOT / "site"
    scaled(img, 256).save(site / "icon.png", optimize=True)
    scaled(img, 32).save(site / "icon-32.png", optimize=True)
    # iOS ořezává rohy samo a průhlednost vyplní černou: podklad doplníme barvou okraje
    touch = Image.new("RGBA", (180, 180), img.getpixel((40, 40)))
    touch.alpha_composite(scaled(img, 180))
    touch.convert("RGB").save(site / "apple-touch-icon.png", optimize=True)
    print("OK", ico, ico.stat().st_size, "B")


if __name__ == "__main__":
    main()
