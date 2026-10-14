"""Assemble the final video for one language.

Narration drives the timing: every line gets a slot (its TTS duration plus a
gap) and the visual for that slot is cut from the scene's source so that the
action the line talks about is on screen while it is said. Screen captures
carry action marks (.marks.json) that define those windows; windows are
time-stretched to the slot (fast-forwarded when the action is long, slowed or
frozen when it is short).

Usage: python build.py en [--out ../out] [--keep]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from cards import Renderer

HERE = Path(__file__).parent
VIDEO = HERE.parent
RAW, ASSETS, SCRIPT = VIDEO / "raw", VIDEO / "assets", VIDEO / "script"
HW = VIDEO.parent / "Videos hardware"
W, H, FPS = 1920, 1080, 30
LEAD, GAP, TAIL = 0.35, 0.40, 0.55
REPO_URL = "github.com/onemanfoundry/ha-genia-air"


def run(*cmd: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *cmd], check=True)


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


@dataclass
class Window:
    """A piece of source video shown during one or more narration slots."""
    src: Path
    start: float = 0.0
    end: float | None = None          # None = to the end of the source
    still: bool = False               # freeze the frame at `start`
    zoom: float = 1.0                 # Ken Burns end zoom for stills
    overlays: list[tuple[Path, float]] = field(default_factory=list)  # (transparent .mov, delay in window)
    blur: list[tuple[int, int, int, int]] = field(default_factory=list)  # privacy boxes x,y,w,h
    hold_end: bool = True             # pad with the last frame instead of slowing down
    focus: tuple[int, int, float] | None = None  # (cx, cy, zoom): ease into this area for legibility


class Builder:
    def __init__(self, lang: str, work: Path, renderer: Renderer, prod: Path = RAW / "prod"):
        self.lang, self.work, self.r = lang, work, renderer
        en = yaml.safe_load((SCRIPT / "en.yaml").read_text())
        self.order = [s["id"] for s in en["scenes"]]
        self.lines = ({s["id"]: s["lines"] for s in en["scenes"]} if lang == "en"
                      else yaml.safe_load((SCRIPT / f"{lang}.yaml").read_text())["scenes"])
        self.cards = yaml.safe_load((SCRIPT / "cards.yaml").read_text())[lang]
        self.tts = ASSETS / "tts" / lang
        self.manifest = json.loads((self.tts / "manifest.json").read_text())
        self.raw = RAW / lang
        self.prod = prod
        self.subs: list[tuple[float, float, str]] = []
        self.clock = 0.0
        work.mkdir(parents=True, exist_ok=True)

    # -- narration -------------------------------------------------------
    def slots(self, sid: str) -> list[tuple[float, float, Path]]:
        """(start, length, wav) per line, relative to the scene start."""
        t, out = LEAD, []
        for i, _ in enumerate(self.lines[sid]):
            wav = self.tts / f"{sid}_{i:02d}.wav"
            d = probe_duration(wav)
            out.append((t, d + GAP, wav))
            t += d + GAP
        return out

    def scene_audio(self, sid: str, slots, total: float) -> Path:
        out = self.work / f"{sid}.voice.wav"
        if not slots:
            run("-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo", out.as_posix())
            return out
        inputs, filt = [], []
        for k, (t, _, wav) in enumerate(slots):
            inputs += ["-i", wav.as_posix()]
            filt.append(f"[{k}:a]aresample=48000,pan=stereo|c0=c0|c1=c0,adelay={int(t * 1000)}:all=1[a{k}]")
        mix = "".join(f"[a{k}]" for k in range(len(slots)))
        filt.append(f"{mix}amix=inputs={len(slots)}:normalize=0,apad,atrim=0:{total:.3f}[out]")
        run(*inputs, "-filter_complex", ";".join(filt), "-map", "[out]", "-c:a", "pcm_s16le", out.as_posix())
        return out

    # -- visuals ---------------------------------------------------------
    def window_clip(self, w: Window, length: float, name: str) -> Path:
        out = self.work / f"{name}.mp4"
        base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}"
        if w.still:
            z = w.zoom
            vf = (f"scale={W * 2}:{H * 2},zoompan=z='1+({z}-1)*on/{int(length * FPS)}':x='iw/2-(iw/zoom/2)':"
                  f"y='ih/2-(ih/zoom/2)':d={int(length * FPS)}:s={W}x{H}:fps={FPS}")
            src = ["-ss", f"{w.start:.3f}", "-i", w.src.as_posix(), "-frames:v", "1"]
            still_png = self.work / f"{name}.still.png"
            run(*src, "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}", still_png.as_posix())
            chain = [f"[0:v]{vf},setsar=1[v0]"]
            inputs = ["-loop", "1", "-t", f"{length:.3f}", "-i", still_png.as_posix()]
        else:
            end = w.end if w.end is not None else probe_duration(w.src)
            natural = max(0.1, end - w.start)
            speed = natural / length
            if w.hold_end and speed < 1:
                speed = 1.0  # play at normal speed, then hold the last frame
            pts = f"setpts=(PTS-STARTPTS)/{speed:.5f}"
            zoom = ""
            if w.focus:
                cx, cy, z = w.focus
                ramp = f"if(lte(on,18),1+({z}-1)*(1-cos(PI*on/18))/2,{z})"
                zoom = (f",zoompan=z='{ramp}':x='max(0,min(iw-iw/zoom,{cx}-iw/zoom/2))':"
                        f"y='max(0,min(ih-ih/zoom,{cy}-ih/zoom/2))':d=1:s={W}x{H}:fps={FPS}")
            chain = [f"[0:v]trim={w.start:.3f}:{end:.3f},{pts},{base},tpad=stop_mode=clone:stop_duration={length:.3f},"
                     f"trim=0:{length:.3f}{zoom},setsar=1[v0]"]
            inputs = ["-i", w.src.as_posix()]
        last = "v0"
        for k, (x, y, bw, bh) in enumerate(w.blur):
            chain.append(f"[{last}]split[b{k}a][b{k}b];[b{k}b]crop={bw}:{bh}:{x}:{y},boxblur=8:4[b{k}c];"
                         f"[b{k}a][b{k}c]overlay={x}:{y}[vb{k}]")
            last = f"vb{k}"
        for k, (ov, delay) in enumerate(w.overlays):
            inputs += ["-itsoffset", f"{delay:.3f}", "-i", ov.as_posix()]
            chain.append(f"[{last}][{k + 1}:v]overlay=0:0:eof_action=pass[vo{k}]")
            last = f"vo{k}"
        run(*inputs, "-filter_complex", ";".join(chain), "-map", f"[{last}]", "-t", f"{length:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(FPS), out.as_posix())
        return out

    def concat(self, parts: list[Path], out: Path) -> Path:
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
        run("-f", "concat", "-safe", "0", "-i", lst.as_posix(), "-c", "copy", out.as_posix())
        return out

    def card(self, template: str, ctx: dict, length: float, name: str, **kw) -> Path:
        return self.r.render(template, ctx, length, self.work / f"{name}.mp4", **kw)

    def label(self, text: str, length: float, name: str, key: str = "", mono: bool = False,
              pos: dict | None = None, at: float = 0.2) -> Path:
        ctx = {"text": text, "key": key, "mono": mono, "at": at, "pos": pos or {"left": "110px", "bottom": "90px"}}
        return self.r.render("label", ctx, length, self.work / f"{name}.label.mov", transparent=True)

    # -- scene assembly ----------------------------------------------------
    def fit_windows(self, sid: str, slots, windows: list[tuple[int, int, Window]], tail: float = TAIL) -> tuple[Path, float]:
        """windows: (first_line, last_line, Window) covering every line in order."""
        total = (slots[-1][0] + slots[-1][1] if slots else 0) + tail
        parts, t = [], 0.0
        for n, (a, b, w) in enumerate(windows):
            end_t = total if n == len(windows) - 1 else slots[b][0] + slots[b][1]
            length = end_t - t
            parts.append(self.window_clip(w, length, f"{sid}.w{n}"))
            t = end_t
        return self.concat(parts, self.work / f"{sid}.video.mp4"), total

    def marks(self, path: Path) -> dict[str, float]:
        return {m["name"]: m["t"] for m in json.loads(path.with_suffix(".marks.json").read_text())}

    def mux_scene(self, sid: str, video: Path, total: float, slots) -> Path:
        voice = self.scene_audio(sid, slots, total)
        for (t, length, _), text in zip(slots, self.lines[sid]):
            self.subs.append((self.clock + t, self.clock + t + length - GAP, text))
        out = self.work / f"{sid}.scene.mp4"
        run("-i", video.as_posix(), "-i", voice.as_posix(), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{total:.3f}", out.as_posix())
        self.clock += total
        return out

    # -- scenes ------------------------------------------------------------
    def demo_badge(self, sid: str, video: Path, total: float) -> Path:
        """Label scenes captured from a simulate_hardware instance as such."""
        if not (self.prod / f"{sid}.simulated").exists():
            return video
        badge = self.label(self.cards["demo"], total, f"{sid}.demo", pos={"right": "40px", "top": "40px"}, at=0.0)
        out = self.work / f"{sid}.video.demo.mp4"
        run("-i", video.as_posix(), "-i", badge.as_posix(), "-filter_complex", "[0:v][1:v]overlay=0:0:eof_action=pass[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", out.as_posix())
        return out

    def s_hook_short(self, sid, slots):
        src = self.prod / "hook_short.mkv"
        video, total = self.fit_windows(sid, slots, [(0, len(slots) - 1, Window(src, 1.0))])
        return self.demo_badge(sid, video, total), total

    def s_intro(self, sid, slots):
        src = ASSETS / "intro_omf.mp4"
        out = self.work / "intro.scene.mp4"
        run("-i", src.as_posix(), "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", out.as_posix())
        d = probe_duration(out)
        self.clock += d
        return out

    def s_requirements(self, sid, slots):
        total = slots[-1][0] + slots[-1][1] + TAIL
        cues = [slots[i][0] for i in (1, 2, 3, 4)]
        video = self.card("requirements", {"req": self.cards["req"], "cues": cues}, total, f"{sid}.card")
        return video, total

    def s_hook_long(self, sid, slots):
        src = self.prod / "hook_long.mkv"
        m = self.marks(src)
        return self.fit_windows(sid, slots, [
            (0, 0, Window(src, 0.5, m["charts"])),
            (1, 2, Window(src, m["charts"], m["optimizer"])),
            (3, 3, Window(src, m["optimizer"], m["diagnostics"])),
            (4, 4, Window(src, m["diagnostics"])),
        ])

    def s_safety(self, sid, slots):
        total = slots[-1][0] + slots[-1][1] + TAIL
        t1 = slots[1][0]
        cues = [t1, t1 + slots[1][1] * 0.38, t1 + slots[1][1] * 0.68]
        return self.card("safety", {"safety": self.cards["safety"], "cues": cues}, total, f"{sid}.card"), total

    def s_ebusd_credit(self, sid, slots):
        total = slots[-1][0] + slots[-1][1] + TAIL
        bg = ASSETS / "web" / "adapter.png"
        cues = [slots[0][0] + 0.3, slots[2][0]]
        return self.card("credit", {"credit": self.cards["credit"], "cues": cues}, total, f"{sid}.card", background=bg), total

    def s_wiring(self, sid, slots):
        c = self.cards["wiring"]
        clip1, clip3, clip4 = (HW / f"PXL_20260818_{n}.mp4" for n in ("124853423", "125315896", "125425438"))
        bus_len = slots[2][0] + slots[2][1] - slots[1][0]
        callout = self.r.render("callout", {"rect": [800, 360, 150, 330], "text": c["bus"], "sub": c["polarity"],
                                            "at": 0.3, "at2": slots[2][0] - slots[1][0]},
                                bus_len, self.work / f"{sid}.callout.mov", transparent=True)
        led = self.label(c["led"], slots[3][1], f"{sid}.led", at=0.8)
        ip = self.label(c["ip"], slots[4][1] + TAIL, f"{sid}.ip", at=1.0)
        return self.fit_windows(sid, slots, [
            (0, 0, Window(clip1, 0.0, 4.0)),
            (1, 2, Window(clip3, 56.0, still=True, overlays=[(callout, 0.0)])),  # no zoom: the callout is pinned to pixels
            (3, 3, Window(clip3, 2.0, 9.5, overlays=[(led, 0.0)])),
            (4, 4, Window(clip4, 19.0, 27.0, overlays=[(ip, 0.0)])),
        ])

    def s_mosquitto(self, sid, slots):
        src = self.raw / "mosquitto.mkv"
        m = self.marks(src)
        return self.fit_windows(sid, slots, [
            (0, 0, Window(src, 0.5, m["card"])),
            (1, 1, Window(src, m["card"], None, hold_end=False, focus=(1150, 350, 1.45))),
        ])

    def s_install(self, sid, slots):
        src = self.raw / "install.mkv"
        m = self.marks(src)
        url = self.label(f"https://{REPO_URL}", slots[2][1], f"{sid}.url", key=self.cards["repo"], mono=True,
                         pos={"left": "110px", "top": "150px"}, at=0.5)
        later = self.label("⏩ " + self.cards["later"], slots[4][1] + TAIL, f"{sid}.later", pos={"right": "90px", "bottom": "90px"}, at=0.1)
        return self.fit_windows(sid, slots, [
            (0, 0, Window(src, 0.3, m["install_app"] - 0.2)),
            (1, 1, Window(src, m["install_app"] - 0.2, m["repositories"] + 0.8)),
            (2, 2, Window(src, m["repositories"] + 0.8, m["back"] + 0.5, overlays=[(url, 0.0)],
                          blur=[(885, 178, 290, 36)])),  # maintainer e-mail on the Repositories page
            (3, 3, Window(src, m["back"] + 0.5, m["install_click"] + 1.0)),
            (4, 4, Window(src, m["install_click"] + 1.0, None, hold_end=False, overlays=[(later, 0.0)])),
        ])

    def s_configure(self, sid, slots):
        src = self.raw / "configure.mkv"
        m = self.marks(src)
        return self.fit_windows(sid, slots, [
            (0, 0, Window(src, 0.3, m["ebus_field"])),
            (1, 2, Window(src, m["ebus_field"], m["save"] - 0.3, focus=(1080, 330, 1.6))),
            (3, 3, Window(src, m["save"] - 0.3, m["save"] + 1.2)),
            (4, 4, Window(src, m["save"] + 1.2, m["log_tab"], focus=(1400, 320, 1.6))),
            (5, 5, Window(src, m["log_tab"], m["log_tab"] + 12, focus=(1080, 700, 1.25))),
        ])

    def s_first_look(self, sid, slots):
        src = self.raw / "first_look.mkv"
        m = self.marks(src)
        later = self.label("⏩ " + self.cards["later"], 3.0, f"{sid}.later", pos={"right": "90px", "bottom": "90px"}, at=0.1)
        return self.fit_windows(sid, slots, [
            (0, 0, Window(src, 0.3, m["diag_shown"], overlays=[(later, 0.0)])),
            (1, 1, Window(src, m["diag_shown"], m["compat_banner"], focus=(800, 300, 1.6))),
            (2, 2, Window(src, m["compat_banner"], m["overview"], focus=(800, 300, 1.6))),  # incl. Report on GitHub
            (3, 3, Window(src, m["overview"], m["delta_t"] - 3)),
            (4, 4, Window(src, m["delta_t"] - 3, m["delta_t"])),
            (5, 5, Window(src, m["delta_t"], m["cop"] + 0.8)),
        ])

    def s_control(self, sid, slots):
        src = self.prod / "control.mkv"
        m = self.marks(src)
        gate, sliders = (560, 280, 1.6), None  # the gate card hugs the left edge; sliders need full width
        video, total = self.fit_windows(sid, slots, [
            (0, 1, Window(src, 0.3, m["tick"], focus=gate)),
            (2, 2, Window(src, m["tick"], m["enabled"], focus=gate)),
            (3, 3, Window(src, m["enabled"], m["ack"] - 0.3, focus=sliders)),
            (4, 4, Window(src, m["ack"] - 0.3, m["revert"] + 1.5, focus=gate)),
            (5, 5, Window(src, m["revert"] + 1.5, focus=sliders)),
        ])
        return self.demo_badge(sid, video, total), total

    def s_more(self, sid, slots):
        src = self.prod / "more.mkv"
        # the line names charts, the optimizer log and the HA device: show all three, sped up
        return self.fit_windows(sid, slots, [(0, 0, Window(src, 0.3, None, hold_end=False))])

    def s_outro(self, sid, slots):
        sim = self.raw / "outro_sim.mkv"
        first = self.fit_windows(f"{sid}a", slots[:1], [(0, 0, Window(sim, 0.5, focus=(1100, 800, 1.5)))], tail=0.0)[0]
        rest_t0 = slots[1][0]
        rest_len = slots[-1][0] + slots[-1][1] + TAIL - rest_t0
        cues = [0.4, slots[2][0] - rest_t0]
        logo = (VIDEO.parent / "genia_air" / "logo.png").as_uri()
        card = self.card("outro", {"outro": self.cards["outro"], "cues": cues, "logo": logo}, rest_len, f"{sid}.card")
        total = rest_t0 + rest_len
        return self.concat([first, card], self.work / f"{sid}.video.mp4"), total

    def credits(self) -> Path:
        omf = (VIDEO.parent.parent / "OMF" / "OMF-logo 2_redu.png").as_uri()
        length = 7.0
        video = self.card("credits", {"credits": self.cards["credits"], "omf": omf}, length, "credits.card")
        out = self.work / "credits.scene.mp4"
        run("-i", video.as_posix(), "-f", "lavfi", "-t", f"{length}", "-i", "anullsrc=r=48000:cl=stereo",
            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", out.as_posix())
        self.clock += length
        return out

    # -- whole video -------------------------------------------------------
    def build(self, out_dir: Path) -> Path:
        scenes, starts = [], {}
        for sid in self.order:
            starts[sid] = self.clock
            if sid == "intro":
                scenes.append(self.s_intro(sid, []))
                continue
            slots = self.slots(sid)
            video, total = getattr(self, f"s_{sid}")(sid, slots)
            scenes.append(self.mux_scene(sid, video, total, slots))
            print(f"  {sid:14} {total:6.1f}s", flush=True)
        starts["credits"] = self.clock
        scenes.append(self.credits())
        body = self.concat(scenes, self.work / "body.mp4")
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"genia-air-install-{self.lang}.mp4"
        # outro music bed: quiet under the outro narration, up for the credits
        music = ASSETS / "music" / "outro.wav"
        t_outro, t_credits, total = starts["outro"], starts["credits"], self.clock
        run("-i", body.as_posix(), "-i", music.as_posix(), "-filter_complex",
            f"[1:a]aresample=48000,atrim=0:{total - t_outro:.3f},volume='if(lt(t,{t_credits - t_outro:.3f}),0.12,0.55)':eval=frame,"
            f"afade=t=in:d=1.5,afade=t=out:st={total - t_outro - 2.5:.3f}:d=2.5,adelay={int(t_outro * 1000)}:all=1[m];"
            f"[0:a][m]amix=inputs=2:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            final.as_posix())
        self.write_srt(out_dir / f"genia-air-install-{self.lang}.srt")
        (out_dir / f"genia-air-install-{self.lang}.starts.json").write_text(json.dumps(starts, indent=1))
        self.write_youtube(out_dir / f"genia-air-install-{self.lang}.youtube.txt", starts)
        return final

    def write_youtube(self, path: Path, starts: dict[str, float]) -> None:
        """Title + description with chapters. YouTube wants the first chapter at
        0:00 and every chapter >= 10 s, so short scenes merge into the previous."""
        yt = yaml.safe_load((SCRIPT / "youtube.yaml").read_text())[self.lang]
        names = yt["chapters"]
        marks = [(0.0, names["hook_short"])]
        for sid, t in starts.items():
            if sid in names and sid != "hook_short":
                if t - marks[-1][0] < 10:
                    if len(marks) == 1:
                        continue  # never move the 0:00 chapter
                    marks.pop()  # previous chapter too short: fold it into the one before
                marks.append((t, names[sid]))
        chapters = "\n".join(f"{int(t // 60)}:{int(t % 60):02d} {n}" for t, n in marks)
        path.write_text(yt["title"] + "\n\n" + yt["description"].replace("{chapters}", chapters))

    def write_srt(self, path: Path) -> None:
        def ts(t):
            h, r = divmod(t, 3600)
            m, s = divmod(r, 60)
            return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"
        path.write_text("".join(f"{i}\n{ts(a)} --> {ts(b)}\n{text}\n\n" for i, (a, b, text) in enumerate(self.subs, 1)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("lang")
    ap.add_argument("--out", type=Path, default=VIDEO / "out")
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    ap.add_argument("--prod", type=Path, default=RAW / "prod", help="production captures directory")
    args = ap.parse_args()
    work = VIDEO / "build" / args.lang
    with Renderer() as r:
        final = Builder(args.lang, work, r, args.prod).build(args.out)
    print("done", final, f"{probe_duration(final):.1f}s")
    if not args.keep:
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
