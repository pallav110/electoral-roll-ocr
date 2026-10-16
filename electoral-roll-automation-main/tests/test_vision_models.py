"""Verify every vision model in VISION_MODELS actually answers.

Consensus is only worth anything if the models disagree. If a model 404s or is
unavailable on the free tier, the vote silently degrades to fewer voters -- and
a majority of one is just a coin flip again.

Prints each model's HTTP status and its raw reply to the same image.
Run:  .venv-test/bin/python tests/test_vision_models.py <image.png>
"""
import base64
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from eci_downloader import (  # noqa: E402
    VISION_API_URL, VISION_MODELS, ECIDownloader,
)

PROMPT = ("Transcribe the captcha in this image. It contains exactly 6 "
          "characters, letters and digits mixed. Reply with those characters "
          "concatenated into one string and nothing else: no commas, no "
          "spaces, no numbering, no explanation.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    image = Path(sys.argv[1])
    with open(image, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    key = os.environ["VISION_API_KEY"]
    ok = 0
    for model in VISION_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            "max_tokens": 20,
            "temperature": 0,
        }
        try:
            r = requests.post(
                VISION_API_URL,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=45)
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"].strip()
                code = ECIDownloader._normalize_captcha(raw)
                print(f"200 {model:48} raw={raw[:40]!r:44} code={code!r}")
                ok += 1
            else:
                print(f"{r.status_code} {model:46} {r.text[:110]}")
        except Exception as exc:
            print(f"ERR {model:47} {type(exc).__name__}: {exc}")
        time.sleep(1.5)

    print(f"\n{ok}/{len(VISION_MODELS)} models answered.")
    if ok < 2:
        print("WARNING: fewer than 2 working models means consensus is "
              "meaningless - the downloader will effectively be single-shot.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())