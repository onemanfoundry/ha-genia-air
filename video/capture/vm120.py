"""Capture the install walkthrough on the secondary Home Assistant (VM 120).

One run = one UI language. Scenes: mosquitto, install, configure, first_look,
outro_sim. Each scene is its own screen recording plus a .marks.json with the
timestamps of the key actions, which the editor uses to cut waits and sync
narration.

Prerequisites (done by --reset): Genia Air and Mosquitto uninstalled and the
Genia Air repository removed, so the install is genuinely from scratch. The
production add-on must be stopped before `configure` starts ebusd, because the
eBUS adapter accepts a single client.

Usage: python vm120.py --lang en --out ../raw/en [--reset] [--scenes a,b]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from halib import CURSOR_JS, H, Recorder, W, auth_js, content_offset, human_move, type_slow, xvfb
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
URL = "http://192.168.1.201"
REPO = "https://github.com/onemanfoundry/ha-genia-air"
REPO_SLUG = "cf9da99f"
SLUG = f"{REPO_SLUG}_genia_air"
ADAPTER = "ens:192.168.1.61:9999"
GX = HERE / "tools" / "gx.sh"


def gx(cmd: str, polls: int = 300) -> str:
    return subprocess.run([str(GX), "120", cmd, str(polls)], capture_output=True, text=True).stdout


def reset() -> None:
    print("reset:", gx(f"ha apps uninstall {SLUG} 2>&1; ha store delete {REPO_SLUG} 2>&1; "
                       "ha apps uninstall core_mosquitto 2>&1; ha store reload 2>&1").strip().replace("\n", " | "))


class Shot:
    def __init__(self, page, labels, rec: Recorder, out: Path):
        self.pg, self.L, self.rec, self.out = page, labels, rec, out

    # -- helpers ---------------------------------------------------------
    def click(self, locator, mark: str | None = None, steps: int = 25):
        locator.wait_for(state="visible", timeout=60000)
        if mark:
            self.rec.mark(mark)
        human_move(self.pg, locator, steps=steps)

    def hover(self, locator, secs: float = 1.5):
        locator.wait_for(state="visible", timeout=60000)
        human_move(self.pg, locator, click=False)
        time.sleep(secs)

    def text(self, label: str, root=None):
        return (root or self.pg).get_by_text(label, exact=True).first

    def tab(self, label: str):
        # by role: in some languages a tab shares its name with a sidebar entry
        # (es: Settings and the Configuration tab are both "Configuración")
        return self.pg.get_by_role("tab", name=label, exact=True).first

    def button(self, label: str, root=None):
        return (root or self.pg).get_by_role("button", name=label, exact=True).first

    def scene(self, name: str):
        self.rec.start(self.out / f"{name}.mkv")
        time.sleep(1.0)

    def end(self, hold: float = 1.5):
        time.sleep(hold)
        self.rec.stop()

    def wait_text(self, label: str, timeout_s: int):
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self.text(label).is_visible():
                return True
            time.sleep(2)
        raise TimeoutError(f"'{label}' never appeared")

    # -- scenes ----------------------------------------------------------
    def mosquitto(self):
        L, pg = self.L, self.pg
        pg.goto(URL + "/config/dashboard")
        time.sleep(4)
        self.scene("mosquitto")
        self.click(self.text(L["apps"]), "apps")
        time.sleep(1.5)
        self.click(self.text(L["install_app"]), "install_app")
        time.sleep(2.5)
        search = pg.get_by_placeholder(L["search"]).first
        self.click(search, "search")
        type_slow(pg, "Mosquitto")
        time.sleep(1.2)
        self.click(self.text("Mosquitto broker"), "card")
        time.sleep(2.5)
        self.click(self.button(L["install"]), "install_click")
        self.wait_text(L["start"], 600)
        self.rec.mark("installed")
        time.sleep(1.0)
        self.click(self.button(L["start"]), "start_click")
        time.sleep(6)
        self.rec.mark("started")
        self.end()

    def install(self):
        L, pg = self.L, self.pg
        pg.goto(URL + "/config/apps/installed")
        time.sleep(4)
        self.scene("install")
        self.click(self.text(L["install_app"]), "install_app")
        time.sleep(2.5)
        menu = pg.get_by_role("button", name=L["menu"], exact=True).last
        self.click(menu, "menu")
        time.sleep(1.0)
        self.click(self.text(L["repositories"]), "repositories")
        time.sleep(2)
        # Repositories is its own page; its "+ Add" button opens the URL dialog
        self.click(self.text(L["add"]), "repo_add_fab")
        time.sleep(1.5)
        field = pg.locator("ha-dialog input, ha-md-dialog input, ha-wa-dialog input, ha-adaptive-dialog input").last
        self.click(field, "repo_field")
        type_slow(pg, REPO, delay_ms=25)
        time.sleep(0.8)
        self.rec.mark("repo_typed")
        dialog = pg.locator("ha-dialog, ha-md-dialog, ha-wa-dialog, ha-adaptive-dialog").last
        self.click(self.button(L["add"], dialog), "repo_add")
        time.sleep(5)
        self.rec.mark("repo_added")
        time.sleep(2)
        self.click(pg.locator("ha-icon-button-arrow-prev").first, "back")
        time.sleep(2.5)
        search = pg.get_by_placeholder(L["search"]).first
        self.click(search, "search")
        type_slow(pg, "Genia")
        time.sleep(1.5)
        # the store also shows a section heading with the same name; click the card body
        self.click(pg.get_by_text("Standalone control", exact=False).first, "card")
        time.sleep(3)
        self.rec.mark("info_page")
        time.sleep(1.5)
        self.click(self.button(L["install"]), "install_click")
        self.wait_text(L["start"], 1800)
        self.rec.mark("installed")
        self.end(2.5)

    def configure(self):
        L, pg = self.L, self.pg
        if not pg.url.endswith("/info"):
            pg.goto(URL + f"/config/app/{SLUG}/info")
            time.sleep(4)
        self.scene("configure")
        self.click(self.tab(L["configuration"]), "config_tab")
        time.sleep(2.5)
        field = pg.locator("ha-form input").first
        self.click(field, "ebus_field")
        pg.keyboard.press("Control+A")
        time.sleep(0.3)
        type_slow(pg, ADAPTER, delay_ms=60)
        time.sleep(1.0)
        self.click(self.button(L["save"]), "save")
        time.sleep(2.5)
        self.click(self.tab(L["info"]), "info_tab")
        time.sleep(2)
        row = pg.locator("ha-row-item").filter(has_text=L["show_in_sidebar"]).first
        self.click(row.locator("ha-switch").first, "sidebar_toggle")
        time.sleep(1.5)
        self.click(self.button(L["start"]), "start_click")
        time.sleep(8)
        self.click(self.tab(L["log"]), "log_tab")
        time.sleep(30)
        self.rec.mark("log_shown")
        self.end()

    def _panel(self):
        return self.pg.frame_locator("iframe").first

    def first_look(self):
        self.scene("first_look")
        self.click(self.text("Genia Air"), "sidebar")
        time.sleep(6)
        panel = self._panel()
        self.click(panel.get_by_text("Diagnostics").first, "diagnostics")
        time.sleep(5)
        self.rec.mark("diag_shown")
        self.hover(panel.get_by_text("Hardware compatibility", exact=False).first, 3.5)
        self.rec.mark("compat_banner")
        self.hover(panel.get_by_text("Report on GitHub", exact=False).first, 3.5)
        self.rec.mark("report")
        self.click(panel.get_by_text("Overview").first, "overview")
        time.sleep(3)
        self.hover(panel.get_by_text("ΔT", exact=True).first, 3)
        self.rec.mark("delta_t")
        self.hover(panel.get_by_text("COP", exact=True).first, 3)
        self.rec.mark("cop")
        self.end()

    def outro_sim(self):
        pg = self.pg
        pg.goto(URL + f"/config/app/{SLUG}/config")
        time.sleep(5)
        self.scene("outro_sim")
        toggle = pg.get_by_text("simulate_hardware", exact=True).first
        toggle.scroll_into_view_if_needed()
        time.sleep(1.5)
        self.hover(toggle, 3)
        self.rec.mark("simulate")
        self.end()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--scenes", default="mosquitto,install,configure,first_look,outro_sim")
    args = ap.parse_args()
    labels = json.loads((HERE / "ha_labels.json").read_text())[args.lang]
    token = (Path.home() / ".config/genia-video/vm120.token").read_text().strip()
    if args.reset:
        reset()
    args.out.mkdir(parents=True, exist_ok=True)
    with xvfb() as display, sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=[f"--window-size={W + 20},{H + 160}", "--window-position=0,0",
                                                           "--disable-infobars", "--hide-scrollbars"],
                                    ignore_default_args=["--enable-automation"])
        ctx = browser.new_context(viewport={"width": W, "height": H})
        ctx.add_init_script(auth_js(URL, token, args.lang))
        ctx.add_init_script(CURSOR_JS)
        page = ctx.new_page()
        page.goto(URL + "/config/dashboard")
        time.sleep(5)
        print("viewport", page.evaluate("[innerWidth, innerHeight]"), flush=True)
        page.mouse.move(W / 2, H / 2)
        off = content_offset(page)
        print("content offset", off, flush=True)
        shot = Shot(page, labels, Recorder(display, off), args.out)
        for name in args.scenes.split(","):
            print("scene", name, flush=True)
            try:
                getattr(shot, name)()
            except (PWTimeout, TimeoutError) as exc:
                page.screenshot(path=str(args.out / f"{name}.FAILED.png"))
                if shot.rec.proc and shot.rec.proc.poll() is None:
                    shot.rec.stop()
                print(f"FAILED {name}: {exc}", file=sys.stderr)
                raise
        browser.close()


if __name__ == "__main__":
    main()
