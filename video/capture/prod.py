"""Capture the scenes that show the production Home Assistant (VM 100).

The Genia Air panel is English-only, so these are captured once and reused in
every language. HA's sidebar is hidden so no localised UI text shows.

Scenes: hook_short, hook_long, control, more.

`control` writes to the real heat pump: it enables a full-control session,
nudges the Manual setpoint (only used in manual mode) by +0.5 °C, then
returns to read-only, which restores the pre-session value.

Usage: python prod.py --out ../raw/prod [--scenes a,b]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from halib import CURSOR_JS, H, Recorder, W, auth_js, content_offset, human_move, xvfb
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL = "http://192.168.1.131:8123"
PANEL = "/app/33c0b081_genia_air"
DEVICE_ID = "163900b89cff92ca24b193f7c2e1a8ab"  # "Genia Air (addon)" MQTT device on production
TOKEN = Path.home() / ".config/genia-video/prod.token"


class Prod:
    def __init__(self, page, rec: Recorder, out: Path):
        self.pg, self.rec, self.out = page, rec, out
        self.f = page.frame_locator("iframe").first

    def open(self, tab: str = "Overview"):
        self.pg.goto(URL + PANEL)
        self.f.get_by_text("Overview").first.wait_for(timeout=60000)
        time.sleep(3)
        if tab != "Overview":
            self.f.get_by_text(tab).first.click()
            time.sleep(4)

    def click(self, loc, mark=None, steps=25):
        loc.wait_for(state="visible", timeout=30000)
        if mark:
            self.rec.mark(mark)
        human_move(self.pg, loc, steps=steps)

    def hover(self, loc, secs=2.0, mark=None):
        loc.wait_for(state="visible", timeout=30000)
        if mark:
            self.rec.mark(mark)
        human_move(self.pg, loc, click=False)
        time.sleep(secs)

    def scene(self, name):
        self.rec.start(self.out / f"{name}.mkv")
        time.sleep(0.8)

    def end(self, hold=1.5):
        time.sleep(hold)
        self.rec.stop()

    def hook_short(self):
        self.open()
        self.scene("hook_short")
        for label in ("COP", "ΔT", "Thermal power"):
            self.hover(self.f.get_by_text(label, exact=True).first, 2.5)
        self.end()

    def hook_long(self):
        self.open()
        self.scene("hook_long")
        time.sleep(1.5)
        self.click(self.f.get_by_text("Charts").first, "charts")
        time.sleep(3)
        time.sleep(3)
        sel = self.f.locator("#chart-window")
        self.hover(sel, 0.8, mark="week")
        sel.select_option("168")
        time.sleep(0.6)
        self.click(self.f.get_by_text("Refresh", exact=False).first)
        time.sleep(5)
        self.pg.mouse.wheel(0, 700)
        time.sleep(5)
        self.pg.mouse.wheel(0, 700)
        time.sleep(4)
        self.click(self.f.get_by_text("Optimizer").first, "optimizer")
        time.sleep(6)
        self.click(self.f.get_by_text("Diagnostics").first, "diagnostics")
        time.sleep(3)
        self.hover(self.f.get_by_text("Hardware compatibility", exact=False).first, 4)
        self.end()

    def control(self):
        self.open("Controls")
        self.scene("control")
        f = self.f
        self.hover(f.get_by_text("Read-only", exact=False).first, 3.0)
        self.click(f.locator("#control-confirm-chk"), "tick")
        time.sleep(1.5)
        self.click(f.locator("#control-enable-btn"), "enable_click")
        time.sleep(2.5)
        self.rec.mark("enabled")
        self.hover(f.locator("#control-expires"), 2.5)
        manual = f.locator('input[type=range][data-msg="z1ManualTemp"]')
        self.hover(manual, 1.0, mark="setpoint")
        manual.focus()
        self.pg.keyboard.press("ArrowRight")
        time.sleep(3)
        self.click(f.get_by_text("Values look right", exact=False).first, "ack")
        time.sleep(3)
        self.click(f.get_by_text("Back to read-only", exact=False).first, "revert")
        time.sleep(4)
        f.locator("body").evaluate("() => buildSliders && buildSliders()")
        time.sleep(2)
        self.hover(manual, 3.0, mark="restored")
        self.end()

    def more(self):
        self.open("Charts")
        self.scene("more")
        self.pg.mouse.wheel(0, 500)
        time.sleep(3)
        self.click(self.f.get_by_text("Optimizer").first, "optimizer")
        time.sleep(4)
        # straight to the add-on's MQTT device: the devices list would show
        # every device in the house (HA ignores ?search= on that page)
        self.pg.goto(URL + f"/config/devices/device/{DEVICE_ID}")
        time.sleep(5)
        self.rec.mark("device")
        self.pg.mouse.wheel(0, 300)
        time.sleep(4)
        self.end()


def main():
    global URL, PANEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--scenes", default="hook_short,hook_long,control,more")
    ap.add_argument("--url", default=URL)
    ap.add_argument("--token", type=Path, default=TOKEN)
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--simulated", action="store_true",
                    help="the target runs simulate_hardware: mark the captures so the edit labels them")
    args = ap.parse_args()
    URL, PANEL = args.url, args.panel
    args.out.mkdir(parents=True, exist_ok=True)
    with xvfb(":97") as display, sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=[f"--window-size={W + 20},{H + 160}", "--window-position=0,0",
                                                           "--disable-infobars", "--hide-scrollbars"],
                                    ignore_default_args=["--enable-automation"])
        ctx = browser.new_context(viewport={"width": W, "height": H})
        ctx.add_init_script(auth_js(URL, args.token.read_text().strip(), "en"))
        ctx.add_init_script("localStorage.setItem('dockedSidebar', '\"always_hidden\"');")
        ctx.add_init_script(CURSOR_JS)
        page = ctx.new_page()
        page.goto(URL + PANEL)
        time.sleep(5)
        page.mouse.move(W / 2, H / 2)
        shot = Prod(page, Recorder(display, content_offset(page)), args.out)
        for name in args.scenes.split(","):
            print("scene", name, flush=True)
            try:
                getattr(shot, name)()
                marker = args.out / f"{name}.simulated"
                if args.simulated:
                    marker.write_text("captured from an instance running simulate_hardware\n")
                else:
                    marker.unlink(missing_ok=True)
            except (PWTimeout, TimeoutError) as exc:
                page.screenshot(path=str(args.out / f"{name}.FAILED.png"))
                if shot.rec.proc and shot.rec.proc.poll() is None:
                    shot.rec.stop()
                print(f"FAILED {name}: {exc}", file=sys.stderr)
        browser.close()


if __name__ == "__main__":
    main()
