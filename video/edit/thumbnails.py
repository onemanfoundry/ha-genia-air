"""YouTube thumbnails (1280x720 PNG) per language: python thumbnails.py [lang ...]"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
VIDEO = HERE.parent


def main(langs: list[str]) -> None:
    cards = yaml.safe_load((VIDEO / "script" / "cards.yaml").read_text())
    bg = (VIDEO / "assets" / "web" / "thumb_bg.png").as_uri()
    out = VIDEO / "out"
    out.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for lang in langs or list(cards):
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            ctx = {"thumb": cards[lang]["thumb"], "background": bg}
            page.add_init_script(f"window.CTX = {__import__('json').dumps(ctx, ensure_ascii=False)};")
            page.goto((HERE / "templates" / "thumbnail.html").as_uri())
            page.evaluate("document.fonts.ready")
            png = out / f"genia-air-install-{lang}.thumb.png"
            page.screenshot(path=str(png))
            page.close()
            print(png)
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1:])
