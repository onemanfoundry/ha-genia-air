"""Narration TTS with best-of-N take selection.

For every line of video/script/<lang>.yaml, generate N takes with Chatterbox
Multilingual (voice cloned from a reference clip) and keep the one Whisper
transcribes most faithfully in the target language. Runs on a CUDA box.

Languages in --vc-langs (French: the cloned voice keeps a strong Spanish
accent there) are generated with the model's native default voice and then
converted to the reference timbre with Chatterbox VC: native accent, close
to the same voice.

Usage: python tts.py --script-dir script --ref voz/ref_sergio.wav --out out/tts [--vc-langs fr] en es de ...
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import re
import unicodedata
from pathlib import Path

import numpy as np
import scipy.signal as ss
import soundfile as sf
import yaml


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text).split())


def load_lines(script_dir: Path, lang: str) -> list[tuple[str, int, str]]:
    en = yaml.safe_load((script_dir / "en.yaml").read_text())
    order = [s["id"] for s in en["scenes"]]
    if lang == "en":
        by_id = {s["id"]: s["lines"] for s in en["scenes"]}
    else:
        by_id = yaml.safe_load((script_dir / f"{lang}.yaml").read_text())["scenes"]
    return [(sid, i, line) for sid in order for i, line in enumerate(by_id[sid])]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("langs", nargs="+")
    ap.add_argument("--script-dir", type=Path, required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--takes", type=int, default=3)
    ap.add_argument("--good", type=float, default=0.97, help="stop early when a take scores this high")
    ap.add_argument("--only", default="", help="comma list of scene ids to (re)generate")
    ap.add_argument("--vc-langs", default="", help="comma list of languages to voice-convert instead of clone")
    ap.add_argument("--redo", action="store_true", help="regenerate every line, not only changed ones")
    args = ap.parse_args()

    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    from faster_whisper import WhisperModel

    tts = ChatterboxMultilingualTTS.from_pretrained(device="cuda")
    # generate() without a prompt reuses whatever voice was conditioned last;
    # keep the model's own default voice to restore before native takes
    native_conds = copy.deepcopy(tts.conds)
    vc_langs = set(filter(None, args.vc_langs.split(",")))
    vc = None
    if vc_langs:
        from chatterbox.vc import ChatterboxVC
        vc = ChatterboxVC.from_pretrained("cuda")
    asr = WhisperModel("medium", device="cuda", compute_type="float16")
    only = set(filter(None, args.only.split(",")))

    for lang in args.langs:
        out_dir = args.out / lang
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = out_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        takes = args.takes + (2 if lang == "fr" else 0)
        for sid, idx, text in load_lines(args.script_dir, lang):
            key = f"{sid}_{idx:02d}"
            mode = "vc" if lang in vc_langs else "clone"
            done = manifest.get(key)
            if (done and done["text"] == text and done.get("mode", "clone") == mode and not args.redo
                    and not (only and sid in only)):
                continue
            best = None
            for t in range(takes):
                if mode == "vc":
                    tts.conds = native_conds
                    native = tts.generate(text, language_id=lang).squeeze(0).cpu().numpy()
                    tmp = out_dir / f"{key}.native.wav"
                    sf.write(tmp, native, tts.sr)
                    wav = vc.generate(str(tmp), target_voice_path=args.ref).squeeze(0).cpu().numpy()
                    tmp.unlink()
                else:
                    wav = tts.generate(text, language_id=lang, audio_prompt_path=args.ref).squeeze(0).cpu().numpy()
                a16 = ss.resample_poly(wav, 16000, tts.sr).astype(np.float32)
                segs, info = asr.transcribe(a16, beam_size=1)
                heard = " ".join(s.text for s in segs)
                p_lang = dict(info.all_language_probs).get(lang, 0.0)
                acc = difflib.SequenceMatcher(None, norm(text), norm(heard)).ratio()
                dur = len(wav) / tts.sr
                # runaway generations (long tails, babbling) are far longer than the text warrants
                runaway = dur > 0.11 * len(text) + 2.5
                score = acc * (0.5 + 0.5 * p_lang) * (0.3 if runaway else 1.0)
                if best is None or score > best["score"]:
                    best = dict(score=score, acc=acc, p_lang=p_lang, dur=dur, heard=heard.strip(), wav=wav, take=t)
                if score >= args.good:
                    break
            sf.write(out_dir / f"{key}.wav", best.pop("wav"), tts.sr)
            manifest[key] = dict(text=text, mode=mode, **best)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
            print(f"{lang} {key} score={best['score']:.2f} acc={best['acc']:.2f} p={best['p_lang']:.2f} "
                  f"{best['dur']:.1f}s take={best['take']}", flush=True)


if __name__ == "__main__":
    main()
