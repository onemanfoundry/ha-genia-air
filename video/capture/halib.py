"""Shared helpers to drive the Home Assistant frontend for screen captures.

Auth is injected as a long-lived token in the frontend's localStorage, so no
password ever goes through the browser. A fake cursor with click ripples is
injected because headless/Xvfb captures show no pointer.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

W, H = 1920, 1080

CURSOR_JS = r"""
(() => {
  if (window.__vcursor) return; window.__vcursor = true;
  const install = () => {
    const c = document.createElement('div');
    c.id = '__vcursor';
    c.innerHTML = '<svg width="28" height="28" viewBox="0 0 28 28"><path d="M4 2 L4 22 L9.5 17 L13 25 L16.5 23.5 L13 15.5 L20 15.5 Z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, {position:'fixed', left:'-50px', top:'-50px', zIndex:2147483647, pointerEvents:'none',
      filter:'drop-shadow(0 2px 3px rgba(0,0,0,.45))', transform:'translate(-4px,-2px)'});
    document.documentElement.appendChild(c);
    addEventListener('mousemove', e => { c.style.display = 'block'; c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
    // the pointer entering an iframe (ingress panels) leaves this document: hide this copy, the frame shows its own
    document.addEventListener('mouseout', e => { if (!e.relatedTarget) c.style.display = 'none'; }, true);
    addEventListener('mousedown', e => {
      const r = document.createElement('div');
      Object.assign(r.style, {position:'fixed', left:(e.clientX-22)+'px', top:(e.clientY-22)+'px', width:'44px', height:'44px',
        borderRadius:'50%', border:'3px solid #ff8a00', zIndex:2147483646, pointerEvents:'none',
        transition:'transform .45s ease-out, opacity .45s ease-out', transform:'scale(.3)', opacity:'1'});
      document.documentElement.appendChild(r);
      requestAnimationFrame(() => { r.style.transform = 'scale(1.4)'; r.style.opacity = '0'; });
      setTimeout(() => r.remove(), 600);
    }, true);
  };
  if (document.documentElement) install(); else addEventListener('DOMContentLoaded', install);
})();
"""


def auth_js(url: str, token: str, lang: str) -> str:
    tokens = {"access_token": token, "token_type": "Bearer", "expires_in": 315360000,
              "hassUrl": url, "clientId": url + "/", "expires": 4102444800000, "refresh_token": ""}
    return (f"localStorage.setItem('hassTokens', {json.dumps(json.dumps(tokens))});"
            f"localStorage.setItem('selectedLanguage', {json.dumps(json.dumps(lang))});"
            "localStorage.setItem('dockedSidebar', '\"docked\"');")


@contextmanager
def xvfb(display: str = ":99"):
    # taller than the capture: the browser keeps its own toolbar above the page
    proc = subprocess.Popen(["Xvfb", display, "-screen", "0", f"{W + 40}x{H + 200}x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    os.environ["DISPLAY"] = display
    try:
        yield display
    finally:
        proc.terminate()


class Recorder:
    """ffmpeg x11grab of the whole virtual screen; one file per scene."""

    def __init__(self, display: str, offset: tuple[int, int] = (0, 0)):
        self.display, self.offset, self.proc, self.t0, self.marks = display, offset, None, 0.0, []

    def start(self, out: Path):
        out.parent.mkdir(parents=True, exist_ok=True)
        self.out = out
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-f", "x11grab", "-framerate", "30", "-video_size", f"{W}x{H}",
             "-draw_mouse", "0", "-i", f"{self.display}+{self.offset[0]},{self.offset[1]}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
             "-pix_fmt", "yuv420p", str(out)], stdin=subprocess.PIPE)
        self.t0, self.marks = time.monotonic(), []

    def mark(self, name: str):
        self.marks.append({"t": round(time.monotonic() - self.t0, 2), "name": name})

    def stop(self):
        self.proc.stdin.write(b"q")
        self.proc.stdin.flush()
        self.proc.wait(timeout=30)
        self.out.with_suffix(".marks.json").write_text(json.dumps(self.marks, indent=1))


def human_move(page, locator, steps: int = 25, click: bool = True, pause: float = 0.35):
    """Glide the (fake) cursor to the element centre, then optionally click."""
    locator.scroll_into_view_if_needed()
    box = locator.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y, steps=steps)
    time.sleep(pause)
    if click:
        page.mouse.down()
        time.sleep(0.08)
        page.mouse.up()
    return x, y


def type_slow(page, text: str, delay_ms: int = 45):
    page.keyboard.type(text, delay=delay_ms)


def content_offset(page) -> tuple[int, int]:
    """Screen position of the page's top-left pixel (below the browser toolbar)."""
    x, y = page.evaluate("[screenX + (outerWidth - innerWidth), screenY + (outerHeight - innerHeight)]")
    return int(x), int(y)
