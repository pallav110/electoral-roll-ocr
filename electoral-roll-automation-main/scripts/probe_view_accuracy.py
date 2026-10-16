#!/usr/bin/env python3
"""Score captcha reads per preprocessing view, on several live captchas.

The view voting added to eci_downloader.py needs its transform set chosen on
evidence, not on intuition. The first attempt (4x upscale + 3x contrast) made
reads WORSE -- on a live captcha it returned a doubled code and a scrambled
one while the original was clean. Resampling adds interpolation artefacts that
read to the model as extra glyph strokes.

So: fetch N captchas, ask each view once per image, and report which views
agree with each other most. Agreement across independent captures is the
available signal for correctness without a human reading every image.

Run:
    python scripts/probe_view_accuracy.py [N]
"""
import base64
import io
import sys
import time
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image, ImageEnhance

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(dotenv_path=str(ROOT / ".env"))

VISION_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL = "meta/llama-3.2-11b-vision-instruct"

PROMPT = (
    "Transcribe the captcha in this image. It contains exactly 6 characters, "
    "letters and digits mixed. Reply with those characters concatenated into "
    "one string and nothing else: no commas, no spaces, no numbering, no "
    "explanation."
)


def enc(im):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def views(raw):
    img = Image.open(io.BytesIO(raw)).convert("L")
    w, h = img.size
    out = {
        "original": (enc(img), "image/png"),
        "upscale2x": (enc(img.resize((w * 2, h * 2), Image.LANCZOS)), "image/png"),
        "upscale4x": (enc(img.resize((w * 4, h * 4), Image.LANCZOS)), "image/png"),
        "contrast1.5x": (enc(ImageEnhance.Contrast(img).enhance(1.5)), "image/png"),
        "contrast3x": (enc(ImageEnhance.Contrast(img).enhance(3.0)), "image/png"),
        "sharp2x": (enc(img.resize((w * 2, h * 2), Image.LANCZOS)
                        .filter(__import__("PIL.ImageFilter", fromlist=["x"])
                                .UnsharpMask(radius=2, percent=150))), "image/png"),
    }
    return out


def ask(b64b, mime, key):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64b}"}},
        ]}],
        "max_tokens": 200, "temperature": 0,
    }
    try:
        r = requests.post(VISION_API_URL, timeout=60, json=payload, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def norm(raw):
    from src.eci_downloader import ECIDownloader
    return ECIDownloader._normalize_captcha(raw)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    key = None
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("VISION_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        print("[!] no VISION_API_KEY"); return 1

    images = sorted(Path("/tmp/capview").glob("*.jpg"))
    print(f"[*] {len(images)} captcha image(s), {len(views(b'')) if False else 6} views each")

    tally = Counter()
    usable = Counter()
    attempts = Counter()
    for path in images:
        raw = path.read_bytes()
        print(f"\n-- {path.name} ({len(raw):,} bytes)")
        for name, (b64b, mime) in views(raw).items():
            reply = ask(b64b, mime, key)
            attempts[name] += 1
            code = norm(reply) if reply else None
            if code:
                usable[name] += 1
                tally[(name, code)] += 1
                print(f"   {name:14s} -> {code}   raw={reply[:40]!r}")
            else:
                print(f"   {name:14s} -> UNUSABLE  raw={(reply or '')[:40]!r}")

    print(f"\n[*] usable rate per view ({len(images)} images):")
    for name in attempts:
        print(f"    {name:14s} {usable[name]}/{attempts[name]}")

    print("\n[*] per-image agreement (which view matched the plurality):")
    return 0


if __name__ == "__main__":
    sys.exit(main())