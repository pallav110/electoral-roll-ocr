"""Probe NVIDIA's hosted API for which vision models actually answer today.

The free tier's catalogue shifts, so VISION_MODELS in src/eci_downloader.py is
only as good as the last time this was run. Prints a usable/not-usable verdict
per model and exits 0 if at least 2 answer, so it doubles as a roster check.
"""
import base64, os, sys, time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
URL = "https://integrate.api.nvidia.com/v1/chat/completions"

CANDIDATES = [
    "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.2-90b-vision-instruct",
    "microsoft/phi-3-vision-128k-instruct",
    "microsoft/phi-3.5-vision-instruct",
    "nvidia/llama-3.2-nv-visionqa-72b-v2",
    "qwen/qwen2.5-vl-72b-instruct",
    "qwen/qwen2-vl-7b-instruct",
    "nvidia/nemotron-nano-vl-v2",
    "baidu/qwen-vl-72b-instruct",
    "google/gemma-3-27b-it",
]

def main():
    key = os.environ["VISION_API_KEY"]
    img = ROOT / "step_captcha.png"
    if not img.exists():
        print("no step_captcha.png - run scripts/step_probe.py first")
        return 2
    b64 = base64.b64encode(img.read_bytes()).decode()
    ok = []
    for m in CANDIDATES:
        payload = {
            "model": m,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Transcribe the captcha: exactly 6 "
                                          "alphanumeric characters, concatenated, "
                                          "nothing else."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
            "max_tokens": 20, "temperature": 0,
        }
        t0 = time.time()
        try:
            r = requests.post(URL, headers={"Authorization": f"Bearer {key}",
                                            "Content-Type": "application/json"},
                              json=payload, timeout=90)
            dt = time.time() - t0
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"].strip()
                print(f"OK   {dt:5.1f}s {m:42} raw={raw[:44]!r}")
                ok.append(m)
            else:
                print(f"{r.status_code:<4} {dt:5.1f}s {m:42} {r.text[:90]}")
        except Exception as exc:
            print(f"ERR  {time.time()-t0:5.1f}s {m:42} {type(exc).__name__}")
        time.sleep(1.2)
    print(f"\n{len(ok)}/{len(CANDIDATES)} answered:")
    for m in ok:
        print(f"  {m!r},")
    return 0 if len(ok) >= 2 else 1

if __name__ == "__main__":
    sys.exit(main())
