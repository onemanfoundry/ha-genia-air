# Install video pipeline

Produces the install/setup tutorial in seven languages (en, es, de, fr, it,
nl, pl) with no manual editing: narration is AI voice, screen captures are
driven by Playwright, and the cut is assembled from the narration timing.

```
script/      narration per language (en.yaml is the master: scene ids, visuals,
             and the line count every translation must match), cards.yaml
             (on-screen text), youtube.yaml (title, description, chapters)
pipeline/    tts.py — narration on a CUDA box (Chatterbox Multilingual + Whisper)
capture/     vm120.py, vm120_batch.sh — install walkthrough on the test HA
             prod.py — production panel scenes (hooks, control)
edit/        build.py — final cut per language; cards.py + templates/ — titles,
             diagrams and overlays rendered frame by frame from HTML
assets/      music, intro, web screenshots, tts/<lang> (generated)
raw/         screen captures (generated)
out/         final videos, .srt subtitles, YouTube text (generated)
```

## Setup

- Python venv with `playwright pyyaml websockets pillow numpy`, plus
  `playwright install chromium`; `ffmpeg` and `Xvfb` on the PATH.
- Home Assistant long-lived tokens (never in git):
  `~/.config/genia-video/prod.token` (production, for `prod.py`) and
  `~/.config/genia-video/vm120.token` (the test HA, for `vm120.py`).
- `capture/tools/` runs shell commands inside the HA OS VMs through the
  Proxmox guest agent (`gx.sh VMID 'cmd'`, via `ssh zeratul`) and starts or
  stops the production add-on (`prod_addon.sh`). `vm120_batch.sh` copies the
  in-VM helpers (`setlat.sh`, `setopt.sh`, `scanwait.sh`) to VM 120 itself.

## Re-running

1. **Narration** (on blackwell, `~/AI/genia-video`, venv `tts-venv`): copy
   `script/*.yaml` and `pipeline/tts.py` there, then
   `python tts.py en es de fr it nl pl --vc-langs fr --script-dir script --ref voz/ref_sergio.wav --out out/tts`.
   Only lines whose text changed are regenerated. Copy `out/tts/` back to
   `assets/tts/`.
2. **Install captures** (needs the eBUS adapter, so production's add-on is
   stopped while each language's configure/first-look part runs, and always
   restarted): `capture/vm120_batch.sh en es de fr it nl pl`.
3. **Production captures** (English-only panel, once):
   `python capture/prod.py --out raw/prod`. The `control` scene writes to the
   real heat pump (Manual setpoint +0.5 °C, then restored by going back to
   read-only).
4. **Build**: `python edit/build.py <lang>` → `out/genia-air-install-<lang>.mp4`
   plus `.srt` and `.youtube.txt`.

## Licences of generated assets

- Voice: Chatterbox Multilingual (MIT), cloned from the author's own recording.
- Music: ACE-Step 1.5 (MIT weights and code).
- Fonts: Noto Sans (OFL), DejaVu (free licence).
- The hardware footage and the OMF intro are the author's own.

Nothing here is licensed NC, so the video can be monetized.

## Known limits

- The production scenes look best with the compressor running (live COP,
  thermal power). Recapture `prod.py` in heating season.
- On a WiFi adapter link with high latency the bus scan can miss the HMU;
  `vm120_batch.sh` re-scans address 08 for up to 10 minutes before recording
  the Diagnostics tab.
