#!/usr/bin/env python3
"""Measure captcha read accuracy across image preprocessing variants.

The captcha length is settled: a human read of /tmp/cap_x6.png gave
"4afmp3" -- six characters. The vision model returned "fmp3af4." for the
same image: the same six glyphs, in the wrong order, with a trailing
period. So the length assumption in eci_downloader.py is right and the
defect is read accuracy.

This probe takes a captcha image and a known-correct answer, then asks
the model to read several differently-preprocessed versions of it. Each
preprocessing is a different "view" of the same pixels, so a single model
can still produce a real vote -- one wrong view does not sink the answer.

Run:
    python scripts/probe_captcha_read.py captcha.png 4afmp3
"""
import base64
import io
import os
import sys
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(dotenv_path=str(ROOT / ".env"))

VISION_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL = "meta/llama-3.2-11b-vision-instruct"

# Deliberately does NOT state a length. The old prompt asserted "exactly 6
# characters", which invites the model to pad or re-order to reach the
# number it was told. Let it read first and report what it sees.
PROMPTS = {
    "plain": (
        "Read the characters in this captcha image. Reply with only the "
        "characters, in the order they appear from left to right, as one "
        "string with no spaces, commas or punctuation."
    ),
    "perchar": (
        "This is a CAPTCHA. Count the characters carefully. Output only the "
        "characters separated by hyphens, in left-to-right order, and no "
        "other text. Example of the required format: a-b-1-c-d-2."
    ),
    "careful": (
        "Transcribe this CAPTCHA. Some characters have noise lines drawn "
        "across them -- ignore the lines and read the letters and digits "
        "themselves. Output only the characters left to right, with nothing "
        "else."
    ),
    # The prompt eci_downloader.py shipped until 2026-10-09. Kept here as the
    # control: it asserts the length and forbids numbering, and it is what
    # returned "fmp3af4." for an image that reads cleanly as "4afmp3".
    "OLD_control": (
        "Transcribe the captcha in this image. It contains exactly 6 "
        "characters, letters and digits mixed. Reply with those characters "
        "concatenated into one string and nothing else: no commas, no "
        "spaces, no numbering, no explanation."
    ),
}


def b64(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode()


def variants(path):
    """Different views of one captcha. Keyed by name, (bytes, mime)."""
    im = Image.open(path).convert("L")
    out = {}

    out["original"] = (b64(im.convert("RGB")), "image/png")

    big = im.resize((im.width * 4, im.height * 4), Image.LANCZOS)
    out["upscale4x"] = (b64(big.convert("RGB")), "image/png")

    # Push contrast so faint noise lines fall below the glyph strokes.
    hi = ImageEnhance.Contrast(im).enhance(3.0)
    out["contrast3x"] = (b64(hi.convert("RGB")), "image/png")

    sharp = im.resize((im.width * 4, im.height * 4), Image.LANCZOS)
    sharp = sharp.filter(ImageFilter.UnsharpMask(radius=2, percent=180))
    out["sharp4x"] = (b64(sharp.convert("RGB")), "image/png")

    return out


def ask(image_b64, mime, prompt, key):
    payload = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
            ],
        }],
        "max_tokens": 200,
        "temperature": 0,
    }
    try:
        r = requests.post(
            VISION_API_URL,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json=payload, timeout=60,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        return f"<HTTP {r.status_code}>"
    except Exception as exc:
        return f"<{type(exc).__name__}>"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path = sys.argv[1]
    truth = sys.argv[2].strip().lower()
    key = os.environ.get("VISION_API_KEY")
    if not key:
        print("[!] VISION_API_KEY not set")
        return 1

    print(f"[*] image={path}  truth={truth}")
    votes = Counter()
    per_variant = {}
    calls = 0

    for vname, (img_b64, mime) in variants(path).items():
        for pname, prompt in PROMPTS.items():
            raw = ask(img_b64, mime, prompt, key)
            calls += 1
            flat = raw.replace("-", "").replace(" ", "").strip().lower()
            alnum = "".join(c for c in flat if c.isalnum())
            hit = "HIT " if alnum == truth else "    "
            tag = f"{vname}/{pname}"
            per_variant.setdefault(vname, []).append((pname, alnum))
            if alnum:
                votes[alnum] += 1
            print(f"  [{hit}] {tag:22s} -> {alnum!r:12s} raw={raw[:60]!r}")

    print(f"\n[*] {calls} calls. Vote tally (top 6):")
    for code, n in votes.most_common(6):
        mark = "HIT" if code == truth else "   "
        print(f"    [{mark}] {code!r:12s} x{n}")
    if votes:
        top, n = votes.most_common(1)[0]
        print(f"\n[*] top vote {top!r} with {n}/{sum(votes.values())} "
              f"-> {'CORRECT' if top == truth else 'WRONG'}")
        agree = sum(c for c in votes.values() if c == votes[top])
        print(f"[*] {agree}/{sum(votes.values())} reads agree with the winner")
    return 0


if __name__ == "__main__":
    sys.exit(main())