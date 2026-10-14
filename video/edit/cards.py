"""Render HTML card templates to video, frame by frame.

Each template is a page in templates/ that reads its content from window.CTX
and animates with CSS. Rendering pauses every animation and seeks it to the
exact frame time, so output is deterministic and needs no display.

    render("safety", {"title": ...}, 6.0, Path("out.mov"), transparent=False)
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
W, H, FPS = 1920, 1080, 30

_SEEK_JS = """t => {
  for (const a of document.getAnimations()) { a.pause(); a.currentTime = t; }
}"""


class Renderer:
    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        return self

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()

    def render(self, template: str, ctx: dict, duration: float, out: Path, transparent: bool = False,
               background: Path | None = None) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        page = self._browser.new_page(viewport={"width": W, "height": H})
        ctx = dict(ctx, duration=duration, background=background.as_uri() if background else None)
        page.add_init_script(f"window.CTX = {json.dumps(ctx, ensure_ascii=False)};")
        page.goto((TEMPLATES / f"{template}.html").as_uri())
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready")
        codec = (["-c:v", "qtrle"] if transparent else
                 ["-c:v", "libx264", "-preset", "medium", "-crf", "14", "-pix_fmt", "yuv420p"])
        ff = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS),
                               "-i", "-", *codec, str(out)], stdin=subprocess.PIPE)
        for i in range(round(duration * FPS)):
            page.evaluate(_SEEK_JS, i * 1000 / FPS)
            ff.stdin.write(page.screenshot(type="png", omit_background=transparent))
        ff.stdin.close()
        ff.wait()
        page.close()
        return out
