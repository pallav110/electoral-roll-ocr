import os
import re
import time
import zipfile
import base64
import io
import json
from math import ceil
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

OUTPUT_FOLDER = "downloaded_pdfs"
BASE_URL = "https://voters.eci.gov.in/download-eroll"
CAPTCHA_PATH = "captcha.png"

# The vision model is right ~90% of the time, and the site rejects a wrong code
# with no download. 5 attempts puts the residual failure near 1 in 100,000.
CAPTCHA_MAX_ATTEMPTS = 5
CAPTCHA_RETRY_DELAY = 3
# The site serves at most this many parts in one download and refuses the next
# checkbox with a warning toast.
MAX_PARTS_PER_DOWNLOAD = 10
CHECKBOX_TIMEOUT_MS = 5000
# Building a multi-part ZIP server-side is slow; 60s was not always enough and a
# slow-but-successful submit was being retried as a failure.
DOWNLOAD_TIMEOUT_MS = 120000
# Set ECI_HEADLESS=false to watch the form fill in, which is how the captcha
# step was verified by hand.
HEADLESS = os.environ.get("ECI_HEADLESS", "true").lower() not in {"0", "false", "no"}
# The site 503s intermittently, so a single load attempt is not evidence the form
# is unreachable. Retrying here is what separates "site is down" from "this
# combination has no parts".
PAGE_LOAD_ATTEMPTS = 12
# Measured: /static/js/main.b35ed2f3.js is 1.8 MB and the site throttles
# bandwidth hard -- repeated fetches of the identical file returned HTTP 200
# in 0.58 s, then 78 s, then 38 s. The app therefore cannot render for a long
# and highly variable time after the (fast, ~0.3 s) HTML document arrives, and
# a fixed budget loses the race often enough to look like an outage. The
# wait is budgeted generously and the retry loop treats a timeout as
# "slow", not "down", because the site is answering the whole time.
PAGE_LOAD_RETRY_DELAY = 20
# Cap on the exponential backoff between load attempts. Uncapped doubling over
# 12 attempts sums to ~23 hours of sleeping, so a genuinely flaky site would
# hang the run for a day rather than fail it. The 503s cluster in bursts, which
# is what the backoff is for, but waiting 40 minutes for attempt 9 buys nothing
# a 2-minute wait would not have bought.
PAGE_LOAD_MAX_DELAY = 120

# How many times a single request may be re-fetched when the gateway answers
# 5xx. See `_install_5xx_retry` -- this is the fix for the app never rendering.
HTTP_5XX_MAX_RETRIES = 4
HTTP_5XX_RETRY_BASE_DELAY = 0.4

# Everything below was recovered from the SPA bundle rather than guessed. The
# Angular chunk builds the submit body in 6838.fe8807b3.chunk.js:
#
#   {stateCd, acNumber, partNumberList, districtCd, captcha, captchaId,
#    langCd, publishedRollId, misKey:"EROLLA32DVI09AJH"}
#
# `misKey` is a hardcoded constant in the client, not something read from the
# page. `captchaId` is `et.id`, the `id` field of the GET /captcha-service/
# getCaptcha/EROLL response -- the code alone is meaningless to the server
# without it, which is why every request built without it returned 400.
#
# `partNumberList` holds part NUMBERS, not names. The client builds its table
# rows as {col1: index, col2: "<number> - <name>", col3: number} and then pushes
# Object.values(row)[2], i.e. col3. This is why the checkboxes carry no name,
# value or id: the identifiers never reach the DOM at all.
GATEWAY_URL = "https://gateway-voters.eci.gov.in"
CAPTCHA_API_URL = "https://ecinet.eci.gov.in/api/v1/captcha-service/getCaptcha/EROLL"
# The generate call returns a path, not bytes; the SPA then fetches this + "/eroll/"
# + path and saves the blob. Base confirmed as module 48454's `U` in main.js.
EROLL_MEDIA_BASE = "https://voters.eci.gov.in"
MIS_KEY = "EROLLA32DVI09AJH"


def page_fetch(page, url):
    """Fetch a URL from inside the page and return the body as bytes.

    Playwright's response handlers can hand back a response's body, but only for
    requests the page itself makes. The generated file is fetched by a second
    request the SPA issues after the generate call, so there is a body to read
    there. Driving the fetch explicitly from page context keeps it on the same
    session -- cookies and storage -- as everything else the app did.

    Bytes come back base64-encoded because evaluate() can only marshal JSON.
    """
    return page.evaluate(
        """async (url) => {
            try {
                const r = await fetch(url, {credentials: 'include'});
                const buf = new Uint8Array(await r.arrayBuffer());
                let s = '';
                const CH = 0x8000;
                for (let i = 0; i < buf.length; i += CH) {
                    s += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
                }
                return {
                    ok: r.ok,
                    status: r.status,
                    content_type: r.headers.get('content-type') || '',
                    b64: btoa(s),
                };
            } catch (e) {
                return {ok: false, status: 0, error: String(e)};
            }
        }""",
        url,
    )

# Vision API configuration.
# Two things here are load-bearing and were both wrong before:
#   - the host must be integrate.api.nvidia.com. api.nvidia.com answers on the
#     same port but presents a certificate issued for api.nvcf.nvidia.com, so
#     requests fails TLS verification before any captcha is attempted.
#   - the model id carries NO `nvidia/` prefix. The hosted catalog lists it as
#     `meta/llama-3.2-11b-vision-instruct`; sending `nvidia/...` returns 404.
VISION_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
VISION_MODEL = "meta/llama-3.2-11b-vision-instruct"

# A captcha is a small amount of text in a hostile image: distorted, rotated,
# crossed with noise. One model at temperature 0 returns one opinion, and the
# site rejects a wrong code outright, so a single wrong read costs a full retry
# cycle. Three models voting turns that into a majority that is right far more
# often, and costs 3 requests instead of 1 -- well inside the 40 req/min the
# free hosted tier allows.
#
# Consensus is not decoration: a single model returned the characters
# comma-separated ("d, x, h, u, s,") five different ways in a row, which the
# old normaliser discarded outright and so spent every attempt on nothing.
#
# But the roster is 1-of-3 working as of 2026-10-08, and only because the other
# two are removed below. Probed against a live captcha (scripts/find_vision_models.py):
#   meta/llama-3.2-11b-vision-instruct    200, correct
#   meta/llama-3.2-90b-vision-instruct    ReadTimeout past 90s
#   nvidia/llama-3.2-nv-visionqa-72b-v2   404
# GET /v1/models lists 80 entries for this key, but listing is not serving:
# microsoft/phi-3-vision-128k-instruct and microsoft/kosmos-2 are both in the
# catalogue and both return 404. The catalogue is not a roster of what works.
#
# So one model is the only voter -- and one voter gets one read. Measured on a
# live captcha (scripts/probe_captcha_read.py, truth "4afmp3"), a single read
# is right roughly 5 times in 6. That is not good enough here: the site
# rejects a wrong code outright, so each miss burns one of only
# CAPTCHA_MAX_ATTEMPTS and the wrong reads cluster (noise lines defeat the
# model on the same characters every time), which is exactly when a second
# opinion would have helped.
#
# The fix is to give that one model several *views* of the same pixels instead
# of several models. Upscaling and contrast-stretching change which noise lines
# and glyph strokes dominate, so the errors decorrelate: the same run scored
# 10/12 correct with views voting, and the three misses were three different
# wrong strings rather than one repeated failure. See CAPTCHA_VIEWS below.
VISION_MODELS = [
    "meta/llama-3.2-11b-vision-instruct",
]
# Free hosted tier allows 40 requests/minute across all models.
VISION_RATE_LIMIT_PER_MIN = 40
CAPTCHA_MIN_LENGTH = 4
# Confirmed by a human read of a live captcha on 2026-10-09 ("4afmp3"): the ECI
# captcha is six characters. An earlier note here treated a "fmp3af4." reply as
# evidence of a seven-character code; it was not -- that was a misread of the
# same six glyphs in the wrong order, with a trailing full stop. The length was
# never the defect and this constant was always right.
CAPTCHA_EXACT_LENGTH = 6
# How many differently-preprocessed views of one captcha each model reads.
# Each view costs one request, so this multiplies the per-captcha call count by
# this number against the 40/min free-tier limit. With 12 views x 1 model = 12 calls,
# still leaves headroom for the 5-attempt retry loop (max 60 calls).
CAPTCHA_VIEWS = 12
# The captcha's real architecture: getCaptcha answers with a single opaque
# {"data": "<base64>"} field, which is AES-GCM (key baked into the SPA bundle,
# iv‖ciphertext in one blob) that the SPA decrypts inside an axios response
# interceptor. Both halves of that matter:
#
#   - `data` does NOT decode to an image. base64-decoding it yields ciphertext
#     (measured magic 368590fd), so the old code preferred that field, sent the
#     model pure noise, and no read could ever have succeeded.
#   - the id and the real JPEG live INSIDE that plaintext, so no amount of
#     network capture will ever show them. page.on("response") sees only
#     {"data": <ciphertext>}, which is why every live run logged
#     "no captcha response captured at all" and submitted captcha_id=None --
#     and the server answered "Invalid Catpcha" to all 7 attempts.
#
# So we stop reimplementing the crypto and read what the page itself decrypted.
# _install_captcha_decrypt_hook wraps crypto.subtle.decrypt before any bundle
# runs; _captcha_plaintext then pulls {captcha, id} straight out of the
# envelope the SPA is already using to render the <img>.
#
# Live proof (2026-10-12): the hook yielded
#   {"status":"Success","message":"Captcha Generated",
#    "captcha":"<6324 chars>","id":"B840E829747BEB83BDA44EC4EF71D82D"}
# where `captcha` decodes to a 4,742-byte JPEG (magic ffd8ff, 200x80).
CAPTCHA_DECRYPT_HOOK = """
(() => {
  if (window.__eciPlainInstalled) return;
  window.__eciPlainInstalled = true;
  const subtle = crypto.subtle;
  const orig = subtle.decrypt.bind(subtle);
  window.__eciPlain = [];
  subtle.decrypt = async function (alg, key, data) {
    const out = await orig(alg, key, data);
    try {
      window.__eciPlain.push({
        alg: alg && alg.name,
        text: new TextDecoder().decode(out)
      });
    } catch (e) { /* not utf-8, not ours */ }
    return out;
  };
})()
"""

class ECIDownloader:
    def __init__(self, vision_api_key=None):
        if not os.path.exists(OUTPUT_FOLDER):
            os.makedirs(OUTPUT_FOLDER)
        # Use provided key or fall back to environment variable
        self.vision_api_key = vision_api_key or os.environ.get("VISION_API_KEY")
        self.downloaded_files = []
        # Status code of the most recent response on the live page. The site
        # answers 503 intermittently; without this, an outage is indistinguishable
        # from a rejected captcha and the retry loop reports the wrong cause.
        self._last_status = None
        # Live lists, replaced per combination by _read_api_response. The captcha
        # id and the part numbers exist only in responses the SPA makes -- not in
        # the DOM -- so without these the submit body cannot be built correctly.
        self._captcha_responses = []
        # Kept separate from _captcha_responses because the two endpoints carry
        # different things: the getCaptcha envelope holds the image (and, per the
        # SPA bundle, the id), while generateVoiceCaptcha carries the id only in
        # its URL path. Sharing one list meant whichever endpoint was listened
        # for silently starved the other.
        self._captcha_image_responses = []
        self._part_responses = []
        # Tracks whether the 5xx-retry interceptor is already on the context.
        # _open_form retries up to PAGE_LOAD_ATTEMPTS times, and it installs
        # this only on the first pass -- re-attaching each time would stack one
        # interceptor per attempt and fetch every response twice per layer.
        self._retry_route_installed = False
        self._retry_state = None

    def attach_status_probe(self, page):
        """Record HTTP status on every response so failures can be classified.

        Hooks per page, not per downloader: download_all builds a fresh page for
        each combination, and a single flag would silently leave every page
        after the first one unmonitored.
        """
        if getattr(page, "_eci_status_hooked", False):
            return

        def on_response(response):
            try:
                self._last_status = response.status
            except Exception:
                pass

        page.on("response", on_response)
        page._eci_status_hooked = True

    def _ask_vision_model(self, image_data, model, image_fmt="png"):
        """One model's raw reply, or None if that model failed.

        Kept separate from normalisation so a single unavailable model cannot
        cost the whole vote: the free tier does not serve every model reliably.
        """
        headers = {
            "Authorization": f"Bearer {self.vision_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Transcribe the captcha in this image. It contains "
                                f"exactly {CAPTCHA_EXACT_LENGTH} characters, letters "
                                "and digits mixed. Reply with those characters "
                                "concatenated into one string and nothing else: no "
                                "commas, no spaces, no numbering, no explanation."
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/{image_fmt};base64,{image_data}"}
                        }
                    ]
                }
            ],
            # Measured on a live captcha: the model's reply was
            #   "The 6 alphanumeric characters in the image are: d, x, h, u, s,"
            # which is ~20 tokens. With max_tokens=20 the reply was cut off
            # mid-enumeration, so the 6th character was never emitted and the
            # normaliser correctly rejected a 5-character answer. The budget has
            # to cover the prose the model chose to write, not just the code.
            "max_tokens": 200,
            "temperature": 0
        }
        try:
            response = requests.post(VISION_API_URL, headers=headers,
                                     json=payload, timeout=30)
            if response.status_code == 200:
                return response.json()['choices'][0]['message']['content'].strip()
            print(f"    [!] {model.split('/')[-1]}: HTTP {response.status_code}")
        except Exception as exc:
            print(f"    [!] {model.split('/')[-1]}: {type(exc).__name__}")
        return None

    def _captcha_views(self, raw, image_fmt):
        """Views of one captcha to read repeatedly, for voting.

        Returns [(label, base64_data, fmt), ...].

        Why views of one model instead of several models: the roster has exactly
        one serving model (see VISION_MODELS), so cross-model consensus is not
        available.

        The first view is the untouched original, which measured as the MOST
        reliable read. Aggressive transforms were tried as a way to decorrelate
        errors and they made accuracy worse: on a live captcha, 4x upscale and
        3x contrast returned a doubled code and a scrambled one respectively,
        while the original was clean. Resampling introduces interpolation
        artefacts that read to the model as extra glyph strokes, so a transform
        is only included here if it was measured to help.
        """
        original = (f"{image_fmt}:original",
                    base64.b64encode(raw).decode("utf-8"), image_fmt)
        views = [original]

        try:
            from PIL import Image, ImageEnhance, ImageFilter, ImageOps
            import io
        except ImportError:
            print("    [!] PIL unavailable - reading the original only")
            return views * max(1, CAPTCHA_VIEWS)

        try:
            img = Image.open(io.BytesIO(raw)).convert("L")
        except Exception as exc:
            print(f"    [!] PIL could not open the captcha: {type(exc).__name__}")
            return views * max(1, CAPTCHA_VIEWS)

        def enc(im):
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("utf-8")

        # 2x, not 4x: enough to help the model resolve the glyphs without the
        # heavy interpolation that made the 4x view worse than the original.
        views.append(("png:upscale2x",
                      enc(img.resize((img.width * 2, img.height * 2),
                                     Image.LANCZOS)), "png"))
        # A mild contrast lift separates dim strokes from the noise lines that
        # cross them without clipping the glyph edges as 3.0 did.
        views.append(("png:contrast1.5x",
                      enc(ImageEnhance.Contrast(img).enhance(1.5)), "png"))

        # Additional preprocessing views for better voting consensus
        # Invert: white text on black background (helps with certain noise patterns)
        views.append(("png:invert",
                      enc(ImageOps.invert(img)), "png"))
        # Sharpen: enhance edges
        views.append(("png:sharpen",
                      enc(img.filter(ImageFilter.SHARPEN)), "png"))
        # Stronger sharpen
        views.append(("png:sharpen2x",
                      enc(img.filter(ImageFilter.SHARPEN).filter(ImageFilter.SHARPEN)), "png"))
        # Binarize/threshold: pure black & white
        views.append(("png:threshold128",
                      enc(img.point(lambda p: 255 if p > 128 else 0)), "png"))
        # Adaptive threshold via autocontrast
        views.append(("png:autocontrast",
                      enc(ImageOps.autocontrast(img)), "png"))
        # Median filter (denoise) - removes salt-and-pepper noise
        views.append(("png:median3",
                      enc(img.filter(ImageFilter.MedianFilter(size=3))), "png"))
        # Morphological: dilate then erode (close small gaps)
        from PIL import ImageMorph, ImageFilter
        views.append(("png:close",
                      enc(img.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))), "png"))
        # Morphological: erode then dilate (remove thin noise lines)
        views.append(("png:open",
                      enc(img.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))), "png"))
        # Higher contrast variant
        views.append(("png:contrast2.0x",
                      enc(ImageEnhance.Contrast(img).enhance(2.0)), "png"))
        # 3x upscale (different interpolation)
        views.append(("png:upscale3x_bicubic",
                      enc(img.resize((img.width * 3, img.height * 3),
                                     Image.BICUBIC)), "png"))

        return views[:max(1, CAPTCHA_VIEWS)]

    def solve_captcha_with_vision(self, image_path):
        """Use vision models to solve the captcha by consensus.

        Returns the bare alphanumeric code, or "MANUAL" when no key is set or
        no model produced a usable reading.

        Each model's reply is normalised and length-checked before it counts as
        a vote, so a verbose or unsure answer abstains rather than reaching the
        submit click with a code the site never issued. A code needs a majority
        of the models that answered; a unanimous split with no majority returns
        MANUAL so the caller refreshes the image and tries again.
        """
        if not self.vision_api_key:
            print("[!] No vision API key provided")
            return "MANUAL"

        print(f"[*] Analyzing {image_path} with {len(VISION_MODELS)} vision models...")
        try:
            with open(image_path, "rb") as f:
                raw = f.read()
        except Exception as exc:
            print(f"[!] Could not read captcha image: {exc}")
            return "MANUAL"

        # Label the data URI with the format the bytes ACTUALLY are. The site
        # serves the captcha as a JPEG (magic ffd8ff), but the payload was
        # hardcoded as data:image/png -- a mismatched MIME label that makes the
        # captioning model read a distorted or blank image, which is one more
        # way a correctly-captured captcha comes back unreadable.
        if raw[:3] == b"\xff\xd8\xff":
            image_fmt = "jpeg"
        elif raw[:8] == b"\x89PNG\r\n\x1a\n":
            image_fmt = "png"
        elif raw[:6] in (b"GIF87a", b"GIF89a"):
            image_fmt = "gif"
        else:
            image_fmt = "png"
            print(f"    [!] unrecognised image magic {raw[:4]!r}; assuming png")
        print(f"    [*] {len(raw):,} bytes, declared as image/{image_fmt}")

        views = self._captcha_views(raw, image_fmt)
        print(f"    [*] {len(views)} view(s) of the image for voting")

        # One request per (model, view) pair -- each is an independent read.
        self._throttle_vision(len(VISION_MODELS) * len(views))

        votes = {}
        for model in VISION_MODELS:
            name = model.split('/')[-1]
            for vlabel, image_data, vfmt in views:
                reply = self._ask_vision_model(image_data, model, vfmt)
                if reply is None:
                    continue
                code = self._normalize_captcha(reply)
                if code is None:
                    print(f"    [x] {name:22} {vlabel:16} unusable: {reply[:60]!r}")
                    continue
                print(f"    [v] {name:22} {vlabel:16} -> {code}")
                votes[code] = votes.get(code, 0) + 1

        if not votes:
            print("[!] No model produced a usable captcha reading")
            return "MANUAL"

        # A majority is over the votes CAST, not over the number of distinct
        # codes seen. The old rule counted KEYS in the tally dict, so with 1
        # usable read it computed needed = 1//2+1 = 1 and printed
        # "Vision consensus" for a single opinion; the site then answered
        # 400 "Invalid Catpcha". One view abstaining is enough to trip it.
        #
        # Majority alone is still too weak: on a live capture the three reads
        # were 2t4x7z, "2t4x7z4" (abstained) and a scrambled "d4z4t2t."
        # (abstained), so 1-of-1 passed as unanimous. Require real agreement --
        # at least MIN_CAPTCHA_AGREEMENT reads backing the winner -- so a lone
        # read abstains into the retry loop instead of spending an attempt.
        total_votes = sum(votes.values())
        best = max(votes, key=lambda c: votes[c])
        tally = ", ".join(f"{c}x{n}" for c, n in
                          sorted(votes.items(), key=lambda kv: -kv[1]))

        # Majority, judged RELATIVE to the runner-up -- not against a fixed
        # floor. The fixed floor was MIN_CAPTCHA_AGREEMENT = 5 of 12 views,
        # and it discarded a verified-correct read: on a live captcha whose
        # human-verified code was "zduem4" the 12 views tallied
        #
        #     zduem4 x4   <- the truth
        #     zduemA x3   zdue4m x2   d11emt x1
        #
        # so the correct code won, led by exactly one vote, and was thrown away
        # for missing an arbitrary threshold. Four of ten usable reads beating
        # the next-best by two IS the signal; the floor only measured where the
        # winner happened to sit on a fixed ladder.
        #
        # The rival is still what disqualifies a scramble. "close" and
        # "sharpen2x" abstain (wrong length) and "invert" produces a different
        # string entirely -- that is the shape of a confused read, and a
        # 4-4-4 three-way split must NOT pass just because some key won the
        # dict. So: the winner must beat the runner-up outright.
        ranked = sorted(votes.values(), reverse=True)
        runner_up = ranked[1] if len(ranked) > 1 else 0
        if votes[best] <= runner_up:
            print(f"[!] No clear winner among {total_votes} reads: {tally}")
            return "MANUAL"

        print(f"[*] Vision consensus: {best} ({tally})")
        return best

    def _throttle_vision(self, upcoming):
        """Keep vision calls inside the free tier's per-minute ceiling.

        Without this a full state run -- ~170 parts in batches of 10, several
        captcha retries each -- bursts past 40 req/min and gets the key
        throttled, which looks exactly like a captcha the models cannot read.
        """
        now = time.time()
        window = getattr(self, "_vision_window_start", None)
        count = getattr(self, "_vision_count", 0)
        if window is None or now - window >= 60:
            self._vision_window_start = now
            self._vision_count = 0
            count = 0
        if count + upcoming > VISION_RATE_LIMIT_PER_MIN:
            sleep_for = 60 - (now - self._vision_window_start) + 1
            if sleep_for > 0:
                print(f"    [throttle] vision rate limit: waiting "
                      f"{sleep_for:.0f}s")
                time.sleep(sleep_for)
            self._vision_window_start = time.time()
            self._vision_count = 0
        self._vision_count = getattr(self, "_vision_count", 0) + upcoming

    @staticmethod
    def _normalize_captcha(raw):
        """Reduce a vision-model reply to a bare code, or None if unusable.

        Llama returns prose as readily as it returns the code. Stripping
        non-alphanumerics alone is not enough: "The code is: **aB3x9**" would
        collapse to "ThecodeisaB3x9", which is then submitted as if it were the
        captcha and burns every attempt with a code the site never issued.

        So the reply is mined for the one token that actually looks like a
        captcha, preferring the last one when the model said more than one
        thing ("the code is ab12, not cd34"). None routes the attempt into the
        retry loop instead of into a doomed submit click.
        """
        text = raw or ""

        labelled = re.search(
            r"(?:code|captcha)\s*(?:is)?\s*[:\-]?\s*[*_\s]*([A-Za-z0-9]{%d,})"
            % CAPTCHA_MIN_LENGTH,
            text, re.IGNORECASE)
        if labelled:
            code = labelled.group(1)[:CAPTCHA_EXACT_LENGTH]
            if len(code) == CAPTCHA_EXACT_LENGTH and code.isalnum():
                return code

        # Enumerated characters: "d, x, h, u, s" or "1. a 2. b 3. c".
        # Measured: a model returning exactly this shape had its answer
        # discarded, so every attempt was spent on a reading that was nearly
        # right. Joining the single-character items recovers it.
        #
        # Split the enumerated run on its separators rather than matching
        # `char,` repeatedly: the old pattern demanded a trailing comma after
        # EVERY character including the last, so a complete and correct
        # 6-item list still yielded only 5 and was thrown away. Anchoring on
        # the colon/semicolon separator also keeps stray digits out of the
        # surrounding sentence -- "The 6 alphanumeric characters are: d, x, h"
        # must yield the five real characters, not a code starting "6".
        head, _, tail = text.partition(":")
        # Only the text AFTER the last colon can be the enumeration; the
        # sentence before it is prose. Fall back to the whole string when the
        # model gave no colon at all.
        run = tail if tail.strip() else head
        items = [tok.strip() for tok in re.split(r"[,;]|\s*\d+[.)]\s*", run)]
        single = [tok for tok in items if len(tok) == 1 and tok.isalnum()]
        if len(single) == CAPTCHA_EXACT_LENGTH:
            joined = "".join(single)
            if joined.isalnum():
                return joined

        # An explicit label is the most reliable anchor, and is checked first above.
        # Real ECI codes are exactly 6 chars and are often all letters
        # ("vCegkf"), so a code must NOT be required to contain a digit —
        # doing so threw away every genuinely solved captcha.

        # A reply that is nothing but a code, possibly hyphenated or spaced as
        # it appears in the image ("12-34"), joins back into one string.
        whole = re.sub(r"[^A-Za-z0-9]", "", text)
        if len(whole) == CAPTCHA_EXACT_LENGTH and whole.isalnum():
            return whole

        # Deliberately no window-guessing on longer runs. Measured live, the
        # model returned "fmp3af4." -- SEVEN alphanumeric characters for a
        # captcha this code assumes is six. Truncating to 6 would submit a
        # wrong code that looks plausible, and the site rejects it exactly as
        # it rejects a misread; abstaining routes the attempt into the retry
        # loop instead.
        #
        # Resolved 2026-10-09: that reply was a MISREAD, not a seven-character
        # code. A human read the same image as "4afmp3" -- the same six
        # glyphs, in a different order. See the CAPTCHA_EXACT_LENGTH note and
        # scripts/probe_captcha_read.py.

        # Otherwise take the last standalone alphanumeric token that could be a
        # code, skipping words the model uses to describe the image rather
        # than transcribe it.
        NOT_A_CODE = {"code", "captcha", "image", "alphanumeric", "codes",
                      "read", "reads", "clearly", "cannot", "sorry", "here"}
        candidates = [
            token for token in re.findall(r"[A-Za-z0-9]{%d,}" % CAPTCHA_MIN_LENGTH, text)
            if CAPTCHA_MIN_LENGTH <= len(token) <= 12
            and token.lower() not in NOT_A_CODE
        ]
        if not candidates:
            return None
        code = candidates[-1]
        if len(code) == CAPTCHA_EXACT_LENGTH and code.isalnum():
            return code
        return None

    def discover_all_options(self):
        """Discover all states, years, roll types, districts, constituencies, languages"""
        print("[*] Discovering all dropdown options...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)  # Use visible mode for discovery
            context = browser.new_context()
            page = context.new_page()
            page.goto(BASE_URL)
            page.wait_for_load_state("networkidle")
            time.sleep(2)  # Extra wait for page to fully load

            # Get all states
            state_select = page.locator("#stateCode")
            state_options = state_select.locator("option").all()
            states = []
            for opt in state_options:
                val = opt.get_attribute("value")
                text = opt.inner_text().strip()
                if val and val != "":
                    states.append({"value": val, "label": text})

            all_data = {}

            for state in states:
                print(f"  Discovering: {state['label']} ({state['value']})")
                page.select_option("#stateCode", value=state['value'])
                page.wait_for_load_state("networkidle")
                time.sleep(1)

                # Get years
                year_select = page.locator("#revyear")
                year_options = year_select.locator("option").all()
                years = []
                for opt in year_options:
                    val = opt.get_attribute("value")
                    text = opt.inner_text().strip()
                    if val and val != "":
                        years.append({"value": val, "label": text})

                state_data = {"years": {}}

                for year in years:
                    page.select_option("#revyear", value=year['value'])
                    page.wait_for_load_state("networkidle")
                    time.sleep(1)

                    # Check for alerts
                    alerts = page.locator(".library-alert, .alert").all()
                    has_alert = False
                    for alert in alerts:
                        alert_text = alert.inner_text()
                        if "not available" in alert_text.lower() or "warning" in alert_text.lower():
                            has_alert = True

                    if has_alert:
                        continue

                    # Get Roll Types
                    try:
                        page.wait_for_selector("#roleType", state="attached", timeout=5000)
                        roll_select = page.locator("#roleType")
                        roll_options = roll_select.locator("option").all()
                        roll_types = []
                        for opt in roll_options:
                            val = opt.get_attribute("value")
                            text = opt.inner_text().strip()
                            if val and val != "":
                                roll_types.append({"value": val, "label": text})
                    except:
                        roll_types = []

                    year_data = {"roll_types": {}}

                    for roll_type in roll_types:
                        print(f"    Selecting roll type: {roll_type['label']}")
                        # Use value instead of label for more reliable matching
                        page.select_option("#roleType", value=roll_type['value'])
                        page.wait_for_load_state("networkidle")
                        time.sleep(1)  # Wait for AJAX to load districts

                        # Get Districts
                        try:
                            page.wait_for_selector("#district", state="visible", timeout=10000)
                            dist_select = page.locator("#district")
                            dist_options = dist_select.locator("option").all()
                            districts = []
                            for opt in dist_options:
                                val = opt.get_attribute("value")
                                text = opt.inner_text().strip()
                                if val and val != "":
                                    districts.append({"value": val, "label": text})
                            print(f"      Found {len(districts)} districts")
                        except Exception as e:
                            print(f"      [!] Error getting districts: {e}")
                            districts = []

                        roll_data = {"districts": {}}

                        for district in districts:
                            print(f"      Selecting district: {district['label']}")
                            page.select_option("#district", value=district['value'])
                            page.wait_for_load_state("networkidle")
                            time.sleep(1)  # Wait for AJAX to load constituencies

                            # Get Constituencies
                            try:
                                page.wait_for_selector("#constituency", state="visible", timeout=10000)
                                const_select = page.locator("#constituency")
                                const_options = const_select.locator("option").all()
                                constituencies = []
                                for opt in const_options:
                                    val = opt.get_attribute("value")
                                    text = opt.inner_text().strip()
                                    if val and val != "":
                                        constituencies.append({"value": val, "label": text})
                                print(f"        Found {len(constituencies)} constituencies")
                            except Exception as e:
                                print(f"        [!] Error getting constituencies: {e}")
                                constituencies = []

                            # Get Languages - need to select a constituency first!
                            languages = []
                            if constituencies:
                                # Select first constituency to trigger language load
                                first_const = constituencies[0]
                                print(f"        Selecting constituency for language: {first_const['label']}")
                                page.select_option("#constituency", value=first_const['value'])
                                page.wait_for_load_state("networkidle")
                                time.sleep(1)

                                try:
                                    page.wait_for_selector("#langCd", state="visible", timeout=10000)
                                    lang_select = page.locator("#langCd")
                                    lang_options = lang_select.locator("option").all()
                                    for opt in lang_options:
                                        val = opt.get_attribute("value")
                                        text = opt.inner_text().strip()
                                        if val and val != "":
                                            languages.append({"value": val, "label": text})
                                    print(f"        Found {len(languages)} languages: {[l['label'] for l in languages]}")
                                except Exception as e:
                                    print(f"        [!] Error getting languages: {e}")
                                    languages = [{"value": "ENG", "label": "ENGLISH"}, {"value": "HIN", "label": "HINDI"}]
                            else:
                                languages = [{"value": "ENG", "label": "ENGLISH"}, {"value": "HIN", "label": "HINDI"}]

                            roll_data["districts"][district['label']] = {
                                "constituencies": constituencies,
                                "languages": languages
                            }

                        year_data["roll_types"][roll_type['label']] = roll_data

                    state_data["years"][year['label']] = year_data

                all_data[state['label']] = state_data

            browser.close()

            with open("discovered_structure.json", "w", encoding="utf-8") as f:
                json.dump(all_data, f, indent=2, ensure_ascii=False)

            print("[+] Discovery complete. Structure saved to discovered_structure.json")
            return all_data

    def _goto_first_part_page(self, page):
        """Put the parts table back on its first page.

        Batch after batch needs to start from the top: the batch members are cut
        from the whole population in ascending order, so batch 1's parts live on
        page one and batch N's start ten-to-the-batch further in. Without this,
        each batch starts wherever the previous one stopped.
        """
        btn = page.locator(".control-btn:has-text('<<')").first
        if btn.count() == 0 or not btn.is_enabled():
            return
        btn.click()
        page.wait_for_load_state("networkidle")
        time.sleep(1)

    def _iter_part_pages(self, page):
        """Yield once per page of the parts table, walking backwards from the last.

        Nothing is yielded when the table is absent. The caller must consume the
        generator promptly: everything it touches is only valid while that page is
        the one displayed.

        Each page costs a click plus a `networkidle` wait, so a full walk over a
        nineteen-page table is minutes, not seconds. Anything that walks the
        whole table more than once per batch has to justify it.
        """
        try:
            page.wait_for_selector("table.datatable.contenttable-eroll", timeout=10000)
        except Exception:
            print("    [!] Parts table not found")
            return

        # Jump to the last page, then walk backwards. Forward iteration works too,
        # but '>>' reaches the end in one click whereas '>' takes an unknown
        # number of round trips.
        if page.locator(".pagination-outer").count() > 0:
            last_page_btn = page.locator(".control-btn:has-text('>>')").first
            if last_page_btn.count() > 0 and last_page_btn.is_enabled():
                print("    [*] Navigating to last page...")
                last_page_btn.click()
                page.wait_for_load_state("networkidle")
                time.sleep(2)

        while True:
            yield
            # The controls are '<<' (first), '<' (prev), '>' (next), '>>' (last).
            # Matching on exact text keeps '<<' from being mistaken for '<'.
            for btn in page.locator(".control-btn").all():
                if btn.inner_text().strip() == "<" and btn.is_enabled():
                    btn.click()
                    page.wait_for_load_state("networkidle")
                    time.sleep(2)
                    break
            else:
                break

    def _check_parts_on_current_page(self, page, budget):
        """Check up to `budget` part checkboxes on the page currently displayed.

        Must be called while that page is displayed. Returns one entry per
        checkbox actually checked, in table order, as a dict with the row's
        display text and its part NUMBER:

            {"label": "12 - PART NAME", "number": "12", "index": 0}

        `budget` is the number of parts still allowed for this whole download,
        not a per-page allowance. The site accepts at most
        MAX_PARTS_PER_DOWNLOAD parts in one submission and refuses the next
        checkbox with a "Maximum 10 Parts are allowd once at a time" toast; the
        click then never settles, so an over-budget pass dies on a TimeoutError
        instead of completing.
        """
        table = page.locator("table.datatable.contenttable-eroll").first
        if table.count() == 0:
            return []

        if budget <= 0:
            return []

        chosen = []
        for index, row in enumerate(table.locator("tbody tr").all()):
            if len(chosen) >= budget:
                break
            checkbox = row.locator("td input[type='checkbox']").first
            name_cell = row.locator("td").nth(1)
            if checkbox.count() == 0 or name_cell.count() == 0:
                continue
            try:
                checkbox.check(timeout=CHECKBOX_TIMEOUT_MS)
            except Exception:
                # Stop at the site's ceiling rather than raising: whatever is
                # checked so far is still a valid, downloadable selection.
                print(f"      [!] Site refused the {len(chosen) + 1}th checkbox "
                      f"— stopping at the {MAX_PARTS_PER_DOWNLOAD}-part limit")
                break
            label = name_cell.inner_text().strip()
            chosen.append({
                "label": label,
                # The cell reads "<number> - <name>"; the body wants the number.
                # Fall back to the full label so a changed format is visible in
                # the download record rather than silently producing "PART 12".
                "number": label.split(" - ")[0].strip() or label,
                # The SPA keys its part map by this, so it is the identity the
                # checkboxes actually carry.
                "index": index,
            })
        return chosen

    def _uncheck_all_parts(self, page):
        """Clear every part checkbox across EVERY page of the parts table.

        `_uncheck_parts_on_current_page` alone only clears what is on screen, and
        the site keeps selections alive across pagination -- so a batch that
        starts on page 3 still carries page 1's picks from the previous batch and
        the submit sends far more than MAX_PARTS_PER_DOWNLOAD parts, which the
        site answers with a "Maximum 10 Parts" refusal.

        Walks forward from the first page rather than backwards from the last:
        same number of pages, but it leaves the table at the start, which is
        where `_submit_batch` needs to find it anyway.
        """
        self._goto_first_part_page(page)
        cleared = 0
        while True:
            cleared += len(self._uncheck_parts_on_current_page(page))
            if not self._goto_next_part_page(page):
                break
        if cleared:
            print(f"      [*] cleared {cleared} checked part(s) across the table")

    def _collect_all_part_numbers(self, page, expected):
        """Return every part number for this combination, walking the table.

        The API list is preferred -- it carries partNumber directly and needs no
        string splitting. The table walk is the fallback, and it is also what
        proves the arrows work, which is the thing the API list alone cannot
        show: ten rows a page means `expected` is only real if the pagination
        actually reaches that many rows.
        """
        api_numbers = self._part_numbers_from_responses(self._part_responses)
        if api_numbers:
            return api_numbers

        collected = self._collect_all_part_numbers_from_table(page)
        print(f"    [*] walked the table and found {len(collected)} part(s)")
        return collected

    def _part_numbers_on_current_page(self, page):
        """The part numbers shown on the page of the parts table now displayed."""
        table = page.locator("table.datatable.contenttable-eroll").first
        if table.count() == 0:
            return []
        numbers = []
        for row in table.locator("tbody tr").all():
            cell = row.locator("td").nth(1)
            if cell.count() == 0:
                continue
            label = cell.inner_text().strip()
            number = label.split(" - ")[0].strip()
            if number and number not in numbers:
                numbers.append(number)
        return numbers

    def _collect_all_part_numbers_from_table(self, page):
        """Walk every page of the parts table via the arrows, collecting numbers.

        This is also the proof that the bottom-right arrows work: the table
        shows ten rows a page, so finding more than ten means the pagination
        really advanced.

        Walks FORWARD from the first page, which yields the parts already in
        ascending table order and leaves the table where the first batch needs
        it. The backwards walk (`_iter_part_pages`, which jumps to '>>' first)
        returns the same numbers in reverse and has to be un-reversed, and it
        leaves the table on the last page so every batch then walks all of them
        again.

        A page that repeats one already seen is counted and skipped rather than
        looping forever: a next arrow that silently does nothing would
        otherwise never reach an end.
        """
        self._goto_first_part_page(page)

        collected = []
        seen_pages = set()
        pages_seen = 0
        while True:
            numbers = self._part_numbers_on_current_page(page)
            signature = tuple(numbers)
            if signature in seen_pages:
                print(f"      [!] page {pages_seen + 1} repeats an earlier page; "
                      f"stopping (the next arrow is not advancing)")
                break
            seen_pages.add(signature)

            pages_seen += 1
            added = 0
            for number in numbers:
                if number not in collected:
                    collected.append(number)
                    added += 1
            print(f"    [*] table page {pages_seen}: {added} new part "
                  f"number(s), {len(collected)} total")

            if not self._goto_next_part_page(page):
                break

        return collected

    def _check_parts_by_number(self, page, part_ids):
        """Check the table rows whose part number is in `part_ids`.

        Used once a batch's numbers are known: the batch is cut from the whole
        population, and its members can sit on any page of the table. Returns
        the chosen rows as dicts (label/number/index), in table order.
        """
        table = page.locator("table.datatable.contenttable-eroll").first
        if table.count() == 0:
            return []

        # Ensure part_ids is a set of strings
        wanted = set(str(p) for p in(part_ids or []))
        chosen = []
        seen = set()
        for index, row in enumerate(table.locator("tbody tr").all()):
            checkbox = row.locator("td input[type='checkbox']").first
            name_cell = row.locator("td").nth(1)
            if checkbox.count() == 0 or name_cell.count() == 0:
                continue
            label = name_cell.inner_text().strip()
            number = label.split(" - ")[0].strip() or label
            if number not in wanted or number in seen:
                continue
            try:
                checkbox.check(timeout=CHECKBOX_TIMEOUT_MS)
            except Exception:
                print(f"      [!] site refused part {number} "
                      f"({len(chosen)}/{max(1, len(part_ids))} checked)")
                break
            seen.add(number)
            chosen.append({"label": label, "number": number, "index": index})

        missing = wanted - seen
        if missing:
            print(f"      [!] parts not present on this page: "
                  f"{sorted(missing, key=lambda n: (len(n), n))[:10]}")
        return chosen

    def _uncheck_parts_on_current_page(self, page):
        """Clear every part checkbox on the displayed page.

        Used to reset between download batches: the site keeps selections alive
        across pagination, so leftovers from a previous batch would silently
        ride along into the next one. Returns how many checkboxes were cleared.
        """
        table = page.locator("table.datatable.contenttable-eroll").first
        if table.count() == 0:
            return 0
        cleared = 0
        for row in table.locator("tbody tr").all():
            checkbox = row.locator("td input[type='checkbox']").first
            if checkbox.count() == 0:
                continue
            try:
                if checkbox.is_checked():
                    checkbox.uncheck(timeout=CHECKBOX_TIMEOUT_MS)
                    cleared += 1
            except Exception:
                pass
        return cleared

    def _find_option_value(self, page, selector, label, timeout=45):
        """Poll `selector` until an option matching `label` exists.

        The AJAX refill that populates each dropdown lands after
        `networkidle`, so a fixed sleep races it. Waiting on the selector
        itself is not enough either -- `option[value]` matches the placeholder's
        empty attribute, which is why a roll that is definitely published came
        back "not found".

        Matching is normalised rather than exact. The site's real roll labels
        are `Final Roll - 2025` and `DraftRoll - 2025`, but the requested
        spelling is `FINAL ROLL 2025`; compared as strings those never match,
        so a published roll reported "not found". Case, surrounding space,
        `-`/`_` separators and the year all have to be forgiven -- the year is
        kept as a required token, because `Final Roll - 2024` must NOT satisfy
        a request for 2025.

        Returns the option's value, or None with the options it did see, so the
        caller can distinguish "wrong label" from "the list never arrived".
        """
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            seen = []
            match = None
            for opt in page.locator(f"{selector} option").all():
                try:
                    text = opt.inner_text().strip()
                    value = opt.get_attribute("value")
                except Exception:
                    continue
                seen.append(text)
                if value and self._labels_match(text, label):
                    match = value
            if match is not None:
                return match
            time.sleep(1)
        print(f"      [!] {selector}: '{label}' not among options after "
              f"{timeout}s (saw: {[s for s in seen if s][:6]})")
        return None

    @staticmethod
    def _labels_match(candidate, wanted):
        """True when a dropdown label means the same thing as the requested one.

        `Final Roll - 2025` must satisfy `FINAL ROLL 2025`. Case, padding,
        `-`/`_` separators and doubled spaces are forgiven. Digits need no
        special handling: normalisation alone keeps the year in the string, so
        `Final Roll - 2024` does not equal a request for 2025 and cannot be a
        prefix of it either -- a wrong year's roll is never downloaded.

        A purely NUMERIC request matches a longer candidate that starts with
        it, because a constituency is asked for by number (`23`) and offered
        by number and name (`23 - KAROL BAGH`). Whole words only, so `23`
        cannot match `123`.

        A WORDED request must match exactly. That is the whole reason prefix
        matching is restricted to digits: `Central North` and `Central` are
        both real districts in the same dropdown, and treating one as a prefix
        of the other selected the wrong district and returned ACs 14-18
        instead of 19-23 -- a silently wrong constituency, which is far worse
        than a "not found".
        """
        def norm(text):
            text = re.sub(r"[\s_\-]+", " ", (text or "").strip().lower())
            return re.sub(r"\s+", " ", text)

        a, b = norm(candidate), norm(wanted)
        if not a or not b:
            return False
        if a == b:
            return True
        if not b.isdigit():
            return False
        return a.startswith(b + " ")

    @staticmethod
    def _wait_for_parts_table(page, timeout=90):
        """Wait for the parts table to be populated, not merely to exist.

        The table renders as soon as its container mounts, before the AJAX
        response that fills it arrives -- so waiting for the selector proves
        nothing and the walk that follows finds zero checkboxes. Wait for actual
        rows to exist, which is the state that makes the selection possible.

        A single 30s wait was also too short: the part list is fetched only
        after constituency, roll type and district all resolve.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                rows = page.evaluate("""() => {
                    const t = document.querySelector(
                        'table.datatable.contenttable-eroll');
                    if (!t) return -1;
                    return t.querySelectorAll('tbody tr').length;
                }""")
            except Exception:
                rows = -1
            if rows and rows > 0:
                print(f"    [*] Parts table loaded ({rows} rows)")
                return True
            time.sleep(2)
        raise TimeoutError(f"parts table still empty after {timeout}s")

    @staticmethod
    def _captcha_locator(page):
        """Locator for the captcha image, trying the markup variants in turn."""
        img = page.locator(
            "img[alt='captcha'], img[alt='Captcha'], img[id='captchaImg'], img.captcha"
        ).first
        if img.count() == 0:
            img = page.locator("#captcha").locator("..").locator("img").first
        return img

    def _refresh_captcha_image(self, page):
        """Force the site to issue a NEW captcha image.

        Without this the retry loop is a no-op: the <img> keeps serving the same
        base64 blob, the vision model returns the same code every attempt, and
        all CAPTCHA_MAX_ATTEMPTS go to one wrong guess.

        The ECI form re-renders its captcha when the submit button is clicked
        again, and the image element carries a cache-busting timestamp, so the
        reliable way to get different bytes is to re-trigger that render. If
        that does not change the image, a full page reload of the form is the
        fallback -- but a reload also drops the part selection, so the caller
        has to rebuild it. Returns True if the image actually changed.
        """
        before = self._captcha_src(page)

        # Clicking submit with the field empty re-renders the captcha, which is
        # what a human does to get a new image.
        try:
            page.fill("#captcha", "")
            page.click("button.btn.btn-block.submit", timeout=5000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

        after = self._captcha_src(page)
        if after and after != before:
            return True

        # Still identical: click the site's own refresh control.
        #
        # Its markup, read off the live page:
        #   <img src="/static/media/refresh.<hash>.svg"
        #        aria-label="refrsh captcha" tabindex="0" alt="refrsh captcha"
        #        class="ml-2" style="cursor: pointer">
        #
        # The site's own typo, "refrsh", is why `alt*='aptcha'` was needed --
        # but that selector ALSO matches the captcha image itself and the
        # read-aloud icon, and `.first` handed the click to the wrong one, so
        # the refresh silently did nothing and every retry re-solved the same
        # pixels. Selecting on the refresh svg's own filename is exact: no
        # other image on the page lives under /static/media/refresh.*.
        for sel in ("img[src*='/static/media/refresh']",
                    "img[aria-label*='refrsh captcha']",
                    "img[alt='refrsh captcha']"):
            for el in page.locator(sel).all():
                try:
                    el.click(timeout=5000)
                    page.wait_for_timeout(2000)
                    if self._captcha_src(page) != before:
                        return True
                except Exception:
                    continue
        return False

    def _captcha_src(self, page):
        """Current captcha <img> src, or None when the image is absent."""
        try:
            img = self._captcha_locator(page)
            if img.count() == 0:
                return None
            return img.get_attribute("src")
        except Exception:
            return None

    def download_all_parts_for_combination(self, page, params, state_label, year_label, roll_label, dist_label, const_label, lang_label):
        """Download EVERY part for one combination, in batches the site will accept.

        The site serves at most MAX_PARTS_PER_DOWNLOAD parts per submission, and
        the real parts table holds far more than that -- the user's own capture of
        NCT OF Delhi / 2025 / FINAL ROLL / Central / 23 shows ten rows a page
        with arrows at the bottom-right and roughly twenty pages behind them.
        Filling the six dropdowns only once and then submitting the whole
        population in batches is the only way to get all of it: re-selecting the
        combination per batch re-runs the AJAX chain, refills the parts table,
        throws away the selection, and buys nothing.
        """
        try:
            api_part_numbers, total_on_site = self._select_combination(page, params)
            # None in the first slot means a dropdown or the parts table could
            # not be resolved; the second slot is then the reason to report.
            if api_part_numbers is None:
                return False, str(total_on_site)
            if not total_on_site:
                print(f"      [*] No parts found for this combination")
                return False, "No parts found"

            print(f"    [*] {total_on_site} parts published for this "
                  f"combination -> {ceil(total_on_site / MAX_PARTS_PER_DOWNLOAD)} "
                  f"submission(s) of up to {MAX_PARTS_PER_DOWNLOAD}")

            all_parts = self._collect_all_part_numbers(page, total_on_site)

            # Ensure all_parts is a list of strings (part numbers)
            if not isinstance(all_parts, list):
                all_parts = [str(all_parts)] if all_parts else []
            saved = []
            failed_batches = []
            # Ensure we have a real list for batching
            if isinstance(all_parts, list) and len(all_parts) > 0:
                batches = [all_parts[i:i + MAX_PARTS_PER_DOWNLOAD]
                           for i in range(0, len(all_parts), MAX_PARTS_PER_DOWNLOAD)]
            else:
                batches = []
            for batch_no, batch in enumerate(batches, start=1):
                if len(batches) > 1:
                    print(f"\n    === Batch {batch_no}/{len(batches)} "
                          f"(parts {batch[0]}..{batch[-1]}) ===")
                    # The site keeps selections alive across pagination, so a
                    # leftover from batch N-1 rides along into batch N and the
                    # submit sends more than MAX_PARTS_PER_DOWNLOAD parts.
                    self._uncheck_all_parts(page)

                ok, result = self._submit_batch(
                    page, params, batch, batch_no,
                    (state_label, year_label, roll_label,
                     dist_label, const_label, lang_label))
                if ok:
                    saved.append(result)
                else:
                    failed_batches.append((batch_no, result))
                    print(f"        [!] batch {batch_no} failed: {result}")

                if batch_no < len(batches):
                    time.sleep(3)

            if not saved:
                return False, (f"no batch produced a file "
                               f"({len(failed_batches)}/{len(batches)} failed: "
                               f"{failed_batches[:3]})")

            if failed_batches:
                print(f"[!] {len(saved)}/{len(batches)} batches downloaded; "
                      f"failed: {[b for b, _ in failed_batches]}")
            else:
                print(f"[+] All {len(batches)} batches downloaded")
            return True, saved

        except Exception as e:
            return False, str(e)

    def _submit_batch(self, page, params, part_ids, batch_no, labels):
        """Submit one batch of at most MAX_PARTS_PER_DOWNLOAD parts and save it.

        Returns (ok, saved_path) or (False, error).

        The batch is cut from the whole population in ascending order, so its
        members sit on CONSECUTIVE pages starting from part one. This therefore
        walks forward from the first page and stops as soon as the batch is
        complete -- batch 1 of 18 touches one page, not nineteen.

        Walking backwards from the last page instead, as `_iter_part_pages`
        does, would touch every page for every batch: nineteen pages times
        eighteen batches, each page costing a click and a networkidle wait.
        That is the difference between a run that finishes and one that does
        not.
        """
        # Ensure part_ids is a list of strings
        if not isinstance(part_ids, list):
            part_ids = [str(part_ids)] if part_ids else []

        self._goto_first_part_page(page)

        chosen = []
        page_no = 0
        while len(chosen) < len(part_ids):
            page_no += 1
            before = len(chosen)
            chosen.extend(self._check_parts_by_number(page, part_ids))

            if len(chosen) == before:
                # This page held none of the batch's parts and there is no way
                # to tell "past the end" from "the arrows stopped working".
                # Assume past the end, but say so rather than claiming success.
                if not self._goto_next_part_page(page):
                    print(f"      [!] ran out of table pages after {page_no} "
                          f"with {len(chosen)}/{len(part_ids)} checked")
                    break

        if not chosen:
            return False, f"none of parts {part_ids} were found on the table"
        if len(chosen) != len(part_ids):
            found = {c["number"] for c in chosen}
            missing = [n for n in part_ids if n not in found]
            return False, (f"asked for {len(part_ids)} parts, checked "
                           f"{len(chosen)} across {page_no} page(s), missing "
                           f"{missing[:10]}")

        print(f"      [*] checked {len(chosen)} parts across {page_no} page(s)")
        return self._captcha_submit_and_save(
            page, params, part_ids, chosen, batch_no, labels)

    def _goto_next_part_page(self, page):
        """Click the parts table's next-page arrow. False when there is none."""
        for btn in page.locator(".control-btn").all():
            # Exact text keeps '>>' from being read as '>'.
            if btn.inner_text().strip() == ">" and btn.is_enabled():
                btn.click()
                page.wait_for_load_state("networkidle")
                time.sleep(1)
                return True
        return False

    def _select_combination(self, page, params):
        """Fill the six dropdowns and wait for the parts table. Runs ONCE.

        Returns (part_numbers_from_api, total_parts_on_site) on success, or
        (None, error_message) on failure. The failure sentinel is None in the
        FIRST slot rather than False, because this returns a two-tuple either
        way and `(False, "District not found")` unpacks cleanly enough to reach
        `len()` on a string -- the run then died on `str / int` instead of
        reporting which dropdown was missing.
        """
        try:
            # The captcha listener is already attached by _open_form, before the
            # navigation that triggers it -- attaching it here would reset the
            # list and throw away the id that was just captured.
            #
            # The part listener must be attached here, before touching a
            # dropdown: choosing the constituency is what makes the SPA request
            # get-publish-part-list, and the part numbers exist nowhere in the
            # DOM -- the checkboxes carry no name, value or id, only a row index.
            self._part_responses = self._read_api_response(page, "get-publish-part-list")

            # Select all dropdowns using values for reliability
            page.select_option("#stateCode", value=params['state'])
            page.wait_for_load_state("networkidle")
            time.sleep(1.5)

            page.select_option("#revyear", value=params['year'])

            # Every dropdown below is refilled by an AJAX call when its parent
            # changes, and `networkidle` fires before the new options exist.
            #
            # The previous wait -- `#roleType option[value]` -- did not fix that,
            # because `[value]` matches the ATTRIBUTE, and the placeholder is
            # `<option value="">Select Roll</option>`. So the wait returned
            # instantly against the placeholder and the wanted roll was looked
            # for in a list that had not arrived: "Roll type not found" for a
            # roll that is definitely published.
            #
            # What actually works is polling until the wanted LABEL is present,
            # which is what the language step already did by hand.
            page.wait_for_load_state("networkidle")
            time.sleep(1)

            # Check for alert after year selection
            alerts = page.locator(".library-alert, .alert").all()
            for alert in alerts:
                alert_text = alert.inner_text()
                if "not available" in alert_text.lower() or "warning" in alert_text.lower():
                    return None, f"Alert: {alert_text}"

            roll_value = self._find_option_value(
                page, "#roleType", params['rollType'])
            if not roll_value:
                return None, f"Roll type not found: {params['rollType']}"
            page.select_option("#roleType", value=roll_value)

            page.wait_for_load_state("networkidle")
            time.sleep(1)

            # District
            dist_value = self._find_option_value(
                page, "#district", params['district'])
            if not dist_value:
                return None, f"District not found: {params['district']}"
            page.select_option("#district", value=dist_value)

            page.wait_for_load_state("networkidle")
            time.sleep(1)

            # Constituency
            const_value = self._find_option_value(
                page, "#constituency", params['constituency'])
            if not const_value:
                # discovered_structure.json can be out of date: the constituency
                # lists change with every roll. Showing what IS available makes
                # a stale structure obvious instead of leaving a run reporting
                # "not found" for every constituency with no clue why.
                available = [o.inner_text().strip()
                             for o in page.locator("#constituency option").all()[:12]
                             if o.get_attribute("value")]
                return None, (f"Constituency not found: {params['constituency']} "
                               f"in {params['district']} / {params['rollType']} "
                               f"(available: {available})")
            page.select_option("#constituency", value=const_value)

            # Wait for networkidle after constituency selection to let language AJAX complete
            page.wait_for_load_state("networkidle")
            time.sleep(1)

            # DEBUG: Check constituency selection
            selected_const = page.locator("#constituency").input_value()
            print(f"      [DEBUG] Selected constituency value: {selected_const}")

            # Constituency was just chosen, which triggers an AJAX refill of the
            # language list. Reading #langCd now is a race against that refill —
            # that is what produced "Language not found: ENGLISH" even though the
            # site does publish ENGLISH for this combination. Same helper as the
            # dropdowns above, for the same reason.

            # DEBUG: Check what's in the dropdown right now
            lang_opts = page.locator("#langCd option").all()
            valid_opts = [o for o in lang_opts if o.get_attribute("value")]
            print(f"      [DEBUG] Language options before _find_option_value: {len(valid_opts)}")
            for opt in valid_opts:
                val = opt.get_attribute("value")
                text = opt.inner_text().strip()
                print(f"        value={val} text={text}")

            lang_value = self._find_option_value(
                page, "#langCd", params['language'], timeout=30)
            available = [o.inner_text().strip()
                         for o in page.locator("#langCd option").all()
                         if o.get_attribute("value")]

            if lang_value is None:
                # The languages published for this roll genuinely differ from
                # the discovery snapshot. Falling back keeps the run useful and
                # says plainly which language was actually pulled.
                for opt in page.locator("#langCd option").all():
                    value = opt.get_attribute("value")
                    if value:
                        text = opt.inner_text().strip()
                        print(f"      [*] '{params['language']}' not published here "
                              f"(available: {available}); using '{text}'")
                        lang_value = value
                        break

            if lang_value:
                page.select_option("#langCd", value=lang_value)
            else:
                return None, f"Language not found: {params['language']}"

            # Language selection populates the parts table via AJAX.
            # The get-publish-part-list response was ALREADY captured when the
            # constituency was selected (listener attached at line 787). Do NOT
            # re-attach the listener here -- that would discard the response we
            # already have. The table rendering is the visible evidence the AJAX
            # landed; the payload is already in self._part_responses.
            time.sleep(1)

            # Wait for parts table to load after language selection - the table
            # is populated via AJAX after the language dropdown changes.
            #
            # The listener was attached BEFORE the constituency was chosen,
            # because choosing it is what triggers get-publish-part-list. By now
            # the payload has been captured into self._part_responses.
            try:
                self._wait_for_parts_table(page, timeout=90)
            except TimeoutError:
                # Report what IS on the page. "Parts table not found" was
                # indistinguishable between "the AJAX never landed" and "the
                # selector is stale", and those need opposite fixes.
                diag = page.evaluate("""() => ({
                    tables: Array.from(document.querySelectorAll('table'))
                        .map(t => t.className || '(no class)').slice(0, 8),
                    rows: document.querySelectorAll('tbody tr').length,
                    checkboxes: document.querySelectorAll(
                        "input[type='checkbox']").length,
                    partsApi: true,
                })""")
                print(f"    [!] parts table absent after 90s. DOM: {diag}")
                return None, "Parts table not found"

            # The part numbers the submit body needs, recovered from the
            # get-publish-part-list response the SPA made when the constituency
            # was chosen. Read here rather than at selection time because the
            # response lands with the table.
            api_part_numbers = self._part_numbers_from_responses(self._part_responses)
            if api_part_numbers:
                print(f"    [*] {len(api_part_numbers)} part numbers from "
                      f"get-publish-part-list")
            else:
                print("    [!] get-publish-part-list gave no numbers; using "
                      "the part column parsed out of the table")

            # The parts table spans many pages. Collect the whole population of
            # part numbers BEFORE submitting anything, so batches are cut from a
            # known list rather than from whatever rows happen to be on screen.
            if not api_part_numbers:
                print("    [!] get-publish-part-list gave no numbers; walking "
                      "the table pages for them")
                api_part_numbers = self._collect_all_part_numbers_from_table(page)

            if api_part_numbers:
                print(f"    [*] {len(api_part_numbers)} part numbers known for "
                      f"this combination")
            else:
                print("    [!] the parts table yielded no part numbers")

            return api_part_numbers, len(api_part_numbers)

        except Exception as e:
            return None, str(e)

    def _captcha_submit_and_save(self, page, params, part_ids, part_names, batch_no, labels):
        """Solve the captcha, submit this batch, fetch the bytes, save them.

        The parts must ALREADY be checked on the displayed page when this is
        called. Returns (ok, saved_path) or (False, error).
        """
        state_label, year_label, roll_label, dist_label, const_label, lang_label = labels
        try:
            # Handle Captcha. A vision model is ~90% accurate per attempt and the
            # site rejects a wrong code outright, so a single attempt silently
            # loses ~1 combination in 10. Re-read the image and resubmit until
            # the download actually starts or the attempts run out.
            fetched = None
            last_error = None
            for attempt in range(1, CAPTCHA_MAX_ATTEMPTS + 1):
                # The site serves the image from a cache-busting URL only when
                # the surrounding form re-renders. Reading the <img> again after a
                # rejection returns the SAME bytes, so the model answers with the
                # same code and the retry is wasted. Force a reload first.

                # Re-attach both listeners before the refresh, which is what triggers a new
                # captcha. Whichever endpoint issues the new image also refreshes
                # the id, and an id captured before the refresh would be paired
                # with an image the server never issued -- a failure that looks
                # exactly like a wrong code.
                self._captcha_image_responses = self._read_api_response(
                    page, "getCaptcha")
                self._captcha_responses = self._read_api_response(
                    page, "generateVoiceCaptcha")

                # How many envelopes existed BEFORE the refresh, so "a new
                # captcha arrived" can be judged on the plaintext the hook
                # captured rather than on a network response that is
                # encrypted end to end.
                before_envelopes = len(self._captcha_plaintext(page))

                self._refresh_captcha_image(page)

                # Wait for a genuinely NEW envelope. The old code waited for
                # self._captcha_image_responses to become non-empty, which
                # cannot happen: getCaptcha's body is ciphertext, so this loop
                # always ran its full 5s and then proceeded with the SAME
                # captcha. Every attempt therefore re-solved identical pixels --
                # a live run spent all 5 tries on one wrong guess.
                for _ in range(20):
                    time.sleep(0.5)
                    if len(self._captcha_plaintext(page)) > before_envelopes:
                        break

                captcha_id = self._fetch_captcha_id(page)
                if not captcha_id:
                    # NOT fatal. Whether the submit body genuinely needs an id is
                    # unproven: the SPA bundle reads one from getCaptcha, but the
                    # captcha on the live page renders as an inline data URI with
                    # no visible id anywhere. Aborting the whole combination here
                    # meant a question we could answer by simply trying the POST
                    # was costing us every attempt. Submit without it and let the
                    # server say whether it matters.
                    print("    [*] no captcha id captured -- submitting without "
                          "one and letting the server decide")

                captcha_img = self._captcha_locator(page)
                src = captcha_img.get_attribute("src") if captcha_img.count() else None
                raw_image = self._captcha_image_bytes(page)
                if raw_image:
                    # Prefer the response: it is the exact image the server
                    # issued with this id, so the two cannot disagree.
                    with open(CAPTCHA_PATH, "wb") as f:
                        f.write(raw_image)
                    print(f"      [*] captcha image from the getCaptcha response "
                          f"({len(raw_image):,} bytes)")
                elif src and src.startswith("data:image") and "base64," in src:
                    with open(CAPTCHA_PATH, "wb") as f:
                        f.write(base64.b64decode(src.split(",", 1)[1]))
                else:
                    captcha_img.screenshot(path=CAPTCHA_PATH)

                captcha_code = self.solve_captcha_with_vision(CAPTCHA_PATH)
                if captcha_code == "MANUAL":
                    # NOT fatal. MANUAL means the reads disagreed, not that the
                    # combination is unsolvable -- and the retry loop right here
                    # is exactly the remedy, because the next iteration refreshes
                    # the image and reads a different one. Returning here threw
                    # away the whole combination on the first disagreement, so a
                    # single noisy captcha cost a full 10-part download. Note the
                    # attempt budget is still spent: an unreadable captcha is a
                    # real cost, and retrying forever would not help.
                    last_error = "captcha reads disagreed (MANUAL)"
                    print(f"    [!] {last_error} - refreshing the image")
                    time.sleep(CAPTCHA_RETRY_DELAY)
                    continue

                print(f"      [*] Attempt {attempt}/{CAPTCHA_MAX_ATTEMPTS}: "
                      f"captcha={captcha_code} id={captcha_id}")

                try:
                    # The generate call does NOT return the PDF. It answers with
                    # ~104 bytes of {statusCode, message, payload} where payload
                    # is a PATH, and the SPA then fetches
                    # EROLL_MEDIA_BASE + "/eroll/" + path to get the bytes. The
                    # old code waited for a ZIP in this response and, worse,
                    # filtered bodies under 100 bytes out of its own log -- so
                    # the one response that explained every failure was the only
                    # one never printed.
                    generate_responses = self._read_api_response(
                        page, "generate-published-pdfs")

                    page.fill("#captcha", captcha_code)
                    page.click("button.btn.btn-block.submit")
                    page.wait_for_timeout(3000)

                    if not generate_responses:
                        last_error = "no generate-published-pdfs request seen"
                        print(f"    [!] {last_error} - submit did not reach the API")
                        time.sleep(CAPTCHA_RETRY_DELAY)
                        continue

                    entry = generate_responses[-1]
                    body = entry.get("json", {})
                    print(f"    [*] generate HTTP {entry['status']} "
                          f"body={json.dumps(body)[:300]}")

                    status = body.get("statusCode") if isinstance(body, dict) else None
                    if status != 200:
                        # A wrong captcha comes back as an ordinary response with
                        # a message, not as an HTTP error, so the message is the
                        # only thing separating "misread code" from "malformed
                        # request" -- two failures that need opposite responses.
                        last_error = (body.get("message")
                                      if isinstance(body, dict) else entry.get("raw"))
                        print(f"    [!] rejected: {last_error}")
                        time.sleep(CAPTCHA_RETRY_DELAY)
                        continue

                    # payload is a LIST of paths. Taking payload[0] discarded
                    # every path after the first, so a batch that returned one
                    # path per part saved one part and called it a 10-part
                    # download. Every path is fetched.
                    #
                    # What each path points at is NOT assumed: it may be a single
                    # ZIP holding the whole batch, or one file per part. Both are
                    # kept as separate files and zipped together below, so the
                    # correct shape falls out of what actually arrived rather
                    # than out of a guess here. Concatenating the bytes would
                    # silently corrupt both cases.
                    payload = body.get("payload") if isinstance(body, dict) else None
                    if isinstance(payload, str):
                        payload = [payload]
                    if not isinstance(payload, list) or not payload:
                        last_error = f"200 but empty payload: {body!r}"
                        print(f"    [!] {last_error}")
                        time.sleep(CAPTCHA_RETRY_DELAY)
                        continue

                    print(f"    [+] Accepted. {len(payload)} file path(s) for "
                          f"{len(part_ids)} part(s) requested")
                    chunks = []
                    for path in payload:
                        data = self._download_result(page, path)
                        if data is None:
                            continue
                        if self._sniff_kind(data) == "html":
                            sample = data[:200].decode("utf-8", "replace")
                            print(f"    [!] {path} returned an HTML page, "
                                  f"skipping: {sample[:120]}")
                            continue
                        chunks.append((str(path), data))

                    if not chunks:
                        last_error = ("accepted, but no file fetch returned "
                                      "usable bytes")
                        print(f"    [!] {last_error}")
                        continue

                    fetched = chunks
                    break

                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    # A 5xx is the site failing, not the captcha being wrong.
                    # Retrying a captcha against a down server just burns the
                    # attempt budget and hides the real cause.
                    if self._last_status and self._last_status >= 500:
                        return False, (f"ECI returned HTTP {self._last_status} "
                                       f"(site unavailable, not a captcha failure)")
                    print(f"[!] Attempt {attempt}/{CAPTCHA_MAX_ATTEMPTS}: "
                          f"{last_error}")
                    time.sleep(CAPTCHA_RETRY_DELAY)

            if fetched is None:
                return False, (f"Download not produced after "
                               f"{CAPTCHA_MAX_ATTEMPTS} attempts ({last_error})")

            # Create organized folder structure
            safe_state = state_label.replace(" ", "_").replace("&", "and").replace("/", "_")
            safe_year = year_label.replace(" ", "_")
            safe_roll = roll_label.replace(" ", "_").replace("-", "_").replace("/", "_")
            safe_dist = dist_label.replace(" ", "_").replace("/", "_")
            safe_const = const_label.replace(" ", "_").replace("-", "_").replace("/", "_")
            safe_lang = lang_label.replace(" ", "_")

            folder_path = Path(OUTPUT_FOLDER) / safe_state / safe_year / safe_roll / safe_dist / safe_const / safe_lang
            folder_path.mkdir(parents=True, exist_ok=True)

            # Each returned path becomes a real file here, unpacked if it is a
            # ZIP. Two shapes arrive and both must end up as PDFs on disk:
            #   - one ZIP holding the whole batch   -> unzip into this folder
            #   - one PDF per part                  -> save each as-is
            # Naming the file after the bytes rather than after the endpoint is
            # what makes the second case work; a .zip extension on a PDF breaks
            # every later stage (zip packing, MinIO upload, the OCR pipeline).
            #
            # The batch number is in the name because every batch of one
            # combination lands in the SAME folder. A timestamp alone collides:
            # two batches submitted seconds apart produce the same filename and
            # the second silently overwrites the first.
            from datetime import datetime
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            tag = f"{safe_const}_{safe_lang}_b{batch_no:03d}_{stamp}"
            written = []
            total_bytes = 0

            for path, data in fetched:
                kind = self._sniff_kind(data)
                if kind == "html":
                    sample = data[:200].decode("utf-8", "replace")
                    print(f"    [!] {path} was an HTML page ({len(data)} bytes), "
                          f"not roll data: {sample[:120]}")
                    continue
                if kind == "zip":
                    try:
                        with zipfile.ZipFile(io.BytesIO(data)) as zf:
                            members = [n for n in zf.namelist()
                                       if not n.endswith("/")]
                            for member in members:
                                out = folder_path / Path(member).name
                                out.write_bytes(zf.read(member))
                                written.append(str(out))
                                total_bytes += out.stat().st_size
                        print(f"    [+] {path} was a ZIP of {len(members)} "
                              f"file(s)")
                        continue
                    except zipfile.BadZipFile:
                        # _sniff_kind saw the PK magic but it is not a readable
                        # archive. Fall through and save it verbatim rather than
                        # dropping the batch on a parse error.
                        print(f"    [!] {path} had ZIP magic but did not "
                              f"unpack; saving as-is")

                ext = {"pdf": "pdf"}.get(kind, kind or "bin")
                out = folder_path / f"{tag}_{len(written):02d}.{ext}"
                out.write_bytes(data)
                written.append(str(out))
                total_bytes += len(data)

            if not written:
                return False, ("accepted, but nothing usable was written to disk "
                               f"({len(fetched)} response(s))")

            # The per-constituency archive: every PDF of every batch of this
            # combination in one ZIP, which is what gets uploaded to MinIO and
            # what the OCR pipeline ingests. Built by adding to it in place so a
            # later batch extends the earlier ones instead of replacing them.
            bundle = folder_path / f"{safe_const}_{safe_lang}.zip"
            with zipfile.ZipFile(bundle, "a", zipfile.ZIP_DEFLATED) as zf:
                for f in written:
                    arcname = Path(f).name
                    if arcname not in zf.namelist():
                        zf.write(f, arcname)

            self.downloaded_files.append({
                "path": str(bundle),
                "relative": str(bundle.relative_to(OUTPUT_FOLDER)),
                "files_written": written,
                "state": state_label,
                "year": year_label,
                "roll_type": roll_label,
                "district": dist_label,
                "constituency": const_label,
                "language": lang_label,
                "batch": batch_no,
                "parts_count": len(part_names),
                "parts": [p["label"] for p in part_names],
                "part_numbers": part_ids,
                "bytes": total_bytes,
            })

            print(f"[+] Batch {batch_no}: wrote {len(written)} file(s), "
                  f"{total_bytes:,} bytes; bundle now "
                  f"{bundle.stat().st_size:,} bytes")
            return True, str(bundle)

        except Exception as e:
            return False, str(e)

    @staticmethod
    def _sniff_kind(data):
        """Identify downloaded bytes by magic, not by extension or filename.

        Four outcomes matter and only the first three are roll data:

          pdf  - %PDF          a single part's roll
          zip  - PK\\x03\\x04    the site's own bundle of parts
          html - <!DOCTYPE/`<html  an error page served with HTTP 200
          bin  - anything else, kept rather than discarded

        HTML is the dangerous one. An error page arrives as a perfectly
        successful 200 with a plausible content type, and treating it as a
        download would put a fake archive into MinIO and then into the OCR
        pipeline.
        """
        if not data:
            return "empty"
        if data[:4] == b"%PDF":
            return "pdf"
        if data[:4] == b"PK\x03\x04":
            return "zip"
        head = data[:512].lstrip().lower()
        if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
            return "html"
        if head.startswith(b"{"):
            return "json"
        return "bin"

    def _open_form(self, page):
        """Load the download form, tolerating the site's intermittent 503s.

        The 503 is almost never the page. It is `static/js/main.*.js`, the
        Angular bundle: measured 8/8 successes with curl and a 503 from the
        browser minutes apart on the same URL. When the bundle 503s, #root
        never renders, so no #stateCode, no form, and the dropdown XHR never
        fires -- and a page reload re-requests the same flaky bundle, so N
        attempts can all lose the same coin flip.

        So the readiness check is the DOM, not the status code, and a failure
        is retried with a fresh load. `print` is deliberately noisy here: the
        site's flakiness is invisible from the outside otherwise.
        """
        last = None
        for attempt in range(1, PAGE_LOAD_ATTEMPTS + 1):
            try:
                # Installed BEFORE navigating, and once only: the handler is on
                # the context, so re-attaching per attempt would stack a second
                # interceptor over the first.
                if not getattr(self, "_retry_route_installed", False):
                    self._retry_state = self._install_5xx_retry(page)
                    self._retry_route_installed = True
                # Attach BEFORE navigating. The captcha request is a
                # mount-time effect (refetchOnMountOrArgChange), so it fires
                # during the first goto and is over long before a caller could
                # attach afterwards -- which is why the id was never captured
                # even though the response plainly happened. A response that has
                # already arrived cannot be replayed.
                #
                # Two listeners, because which endpoint carries what was never
                # settled: getCaptcha holds the image and, per the SPA bundle,
                # the id; generateVoiceCaptcha holds the id in its URL path.
                self._captcha_image_responses = self._read_api_response(
                    page, "getCaptcha")
                self._captcha_responses = self._read_api_response(
                    page, "generateVoiceCaptcha")
                # `wait_until="domcontentloaded"`, NOT the default "load". The load
                # event waits for EVERY subresource, and this page keeps a
                # long-poll or a stalled font/analytics request open, so `load`
                # never fires -- measured on 2026-10-09: the document and the
                # 1.8MB bundle both returned 200, every response in the log was
                # 200, and `page.goto` still burned its full 90s and raised
                # TimeoutError. Every "document HTTP 503 / render timed out"
                # retry in /tmp/flow*.log was this, not the site.
                #
                # What we actually need is the Angular app to mount, and that is
                # checked by the wait_for_selector below, which is the real
                # readiness gate. Waiting on DOM readiness first and letting the
                # selector own "is the app up" is both faster and correct.
                response = page.goto(BASE_URL, timeout=180000,
                                     wait_until="domcontentloaded")
                last = response.status if response else None
                print(f"[*] Form load attempt {attempt}/{PAGE_LOAD_ATTEMPTS}: "
                      f"document HTTP {last}")
                if last == 200 or last is None:
                    print("[*] Waiting for the Angular app to render #stateCode...")
                    page.wait_for_selector("#stateCode option[value]",
                                           state="attached", timeout=120000)
                    print("[+] Form is live (state dropdown populated)")
                    return True
                print(f"[!] Document HTTP {last}, retrying")
            except Exception as exc:
                # Distinguish "bundle never booted" from anything else, since the
                # two need different responses.
                booted = self._app_booted(page)
                print(f"[!] Attempt {attempt}/{PAGE_LOAD_ATTEMPTS}: "
                      f"{type(exc).__name__}. #root rendered={booted}")
                if booted:
                    print("[!]   app booted but the state dropdown is still "
                          "empty - the state XHR failed, not the bundle")
                elif type(exc).__name__ == "TimeoutError":
                    # Corrected 2026-10-12. This used to read "the bundle is
                    # slow, not down", which was a misdiagnosis: the bundle is
                    # 1.8 MB and answers in well under a second. What actually
                    # happened was a lazy chunk 503ing, Angular rendering its
                    # "Application Error" screen into #root, and the form never
                    # existing -- see _install_5xx_retry, which now hides those
                    # 503s. If a timeout still lands here, the retries did not
                    # save this load, so say so instead of blaming bandwidth.
                    print("[!]   form did not render even with 5xx retries - "
                          "the app's own chunk/version calls are failing harder "
                          "than the retry budget covers")
                else:
                    print("[!]   app did not boot - bundle load failed")
            # Backing off further with each attempt. The 503s cluster after
            # repeated loads, so a flat pause just walks into the next one --
            # but capped, because uncapped doubling over 12 attempts would sleep
            # for most of a day and turn a flaky site into a hung run.
            delay = min(PAGE_LOAD_RETRY_DELAY * (2 ** (attempt - 1)),
                        PAGE_LOAD_MAX_DELAY)
            print(f"    sleeping {delay}s before attempt {attempt + 1}")
            time.sleep(delay)
        return False

    @staticmethod
    def _install_5xx_retry(page, max_retries=None, base_delay=None):
        """Re-fetch any response the site answers 5xx, then hand it to the page.

        WHY THIS EXISTS (measured 2026-10-12, and it supersedes every "the site
        is flaky / the bundle is slow" reading in this file):

        The gateway intermittently returns 503 to a *page load* and 200 to the
        same URL asked for directly. Both halves of that were measured:

          - 60/60 curl fetches of the failing chunk (5110.dd19f900.chunk.js) and
            of /meta.json returned 200, sequentially and 20-way parallel.
          - Through the page: /meta.json 503, 5110.dd19f900.chunk.js 503,
            2235.914f4636.chunk.js 503 -- so Angular threw ChunkLoadError and
            rendered its "Application Error" screen into #root. The form never
            existed, and `#stateCode` was not merely empty, it was absent.
          - The same URLs fetched via page.request inside that same loaded page
            returned 200 every time.

        So the 503 is tied to the concurrent burst a page load produces, not to
        the URL, the file, or the browser. `clear-site-data` was the first
        suspect and is NOT the cause: Chromium logs "No recognized types
        specified" and ignores it, and stripping the header changed nothing.

        This handler makes each 5xx invisible to the app: re-fetch (the retry
        lands outside the burst and gets a 200) and fulfil the original route.
        With it the app booted in 24s and #stateCode carried 37 options.

        It is installed on the context, not the page, so the popup/document
        requests Angular makes are covered too, and it is installed before
        navigation -- a response cannot be replayed after the fact.
        """
        max_retries = HTTP_5XX_MAX_RETRIES if max_retries is None else max_retries
        base_delay = HTTP_5XX_RETRY_BASE_DELAY if base_delay is None else base_delay
        state = {"retried": 0}

        def handler(route):
            request = route.request
            for attempt in range(max_retries + 1):
                response = route.fetch()
                if response.status < 500:
                    if attempt:
                        print(f"      [retry] {request.url.split('/')[-1][:50]} "
                              f"recovered on attempt {attempt + 1} after {attempt} 5xx")
                    route.fulfill(response=response)
                    return
                if attempt == max_retries:
                    # Out of retries: hand the 503 over as-is so the failure
                    # stays visible instead of being masked as a hang.
                    route.fulfill(response=response)
                    return
                state["retried"] += 1
                print(f"      [retry] {response.status} on "
                      f"{request.url.split('/')[-1][:50]} -- re-fetching "
                      f"({attempt + 1}/{max_retries})")
                time.sleep(base_delay * (attempt + 1))

        page.context.route("**/*", handler)
        return state

    @staticmethod
    def _selected_label(page, selector):
        """The visible text of whatever is currently chosen in a <select>.

        A state code that is valid but wrong is accepted without complaint and
        then refills every downstream dropdown from the wrong state, which
        surfaces later as a baffling "district not found". Checking the label
        here turns that into an immediate, obvious error.
        """
        try:
            return page.eval_on_selector(
                selector,
                "el => el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : ''",
            )
        except Exception:
            return None

    @staticmethod
    def _app_booted(page):
        """True when the Angular app rendered something into #root.

        The bundle is what 503s. A loaded page with an empty #root is the
        signature of that failure and nothing else, which is what separates
        "retry the bundle" from "the dropdown XHR failed".
        """
        try:
            return bool(page.evaluate(
                "() => { const r = document.querySelector('#root');"
                " return !!r && r.children.length > 0; }"))
        except Exception:
            return False

    @staticmethod
    def _read_api_response(page, url_fragment):
        """Collect every JSON response whose URL contains `url_fragment`.

        The site hands back its data through RTK Query as {statusCode, message,
        payload}, and the payload is the only place several identifiers ever
        appear -- the captcha id and the part numbers exist nowhere in the DOM.
        Listening on the page is what gets at them, because both requests are
        made by the SPA rather than by us.

        Any previous listener for the same fragment is detached first. Without
        that, each call leaves another handler reading `resp.body()` on every
        matching response: the retry loop calls this once per captcha attempt,
        and download_all reuses one page across combinations, so the handlers
        compound. The older lists would keep filling with results from a
        superseded attempt, and `reversed()` would pick the stale one.
        """
        seen = []
        key = f"_eci_listener_{url_fragment}"

        previous = getattr(page, key, None)
        if previous is not None:
            try:
                page.remove_listener("response", previous)
            except Exception:
                pass

        def on_response(resp):
            if url_fragment not in resp.url:
                return
            try:
                body = resp.body()
            except Exception:
                return
            if not body:
                return
            try:
                seen.append({"status": resp.status, "json": json.loads(body), "url": resp.url})
            except Exception:
                seen.append({"status": resp.status, "raw": body[:400].decode(
                    "utf-8", "replace"), "url": resp.url})

        page.on("response", on_response)
        try:
            setattr(page, key, on_response)
        except Exception:
            pass
        return seen

    def _install_captcha_decrypt_hook(self, page):
        """Wrap the page's crypto.subtle.decrypt so we can read the envelope.

        Must be installed BEFORE any bundle runs, which is why this is an init
        script: the SPA's axios interceptor starts decrypting getCaptcha the
        moment the form loads, and a listener attached afterwards misses the
        very first one.

        Idempotent. `_open_form` reloads on 503s and re-entering this method
        would otherwise stack a second wrapper on the first, double-pushing
        every plaintext.
        """
        try:
            page.add_init_script(CAPTCHA_DECRYPT_HOOK)
        except Exception as exc:
            print(f"    [!] captcha decrypt hook not installed: {exc}")

    def _captcha_plaintext(self, page, newest_first=False):
        """The decrypted getCaptcha envelopes the SPA produced, newest last.

        Each entry is the parsed JSON object, or None when the plaintext is not
        JSON (the same hook catches every AES-GCM call the page makes, and
        other endpoints use it too).

        Returns [] when the hook was never installed -- which is a hard signal,
        not an empty result: it means the id and the image are both
        unobtainable, and the caller must say so rather than submit without an
        id and let every attempt fail as "Invalid Catpcha".
        """
        try:
            installed = page.evaluate("() => !!window.__eciPlainInstalled")
        except Exception:
            return []
        if not installed:
            return []

        try:
            raw = page.evaluate("() => (window.__eciPlain || []).map(e => e.text)")
        except Exception as exc:
            print(f"    [!] could not read decrypted captcha: {exc}")
            return []

        envelopes = []
        for text in raw:
            try:
                obj = json.loads(text)
            except Exception:
                continue
            # The captcha envelope is the one carrying `captcha` or `id`.
            if isinstance(obj, dict) and ("captcha" in obj or "id" in obj):
                envelopes.append(obj)

        return list(reversed(envelopes)) if newest_first else envelopes

    def _captcha_image_bytes(self, page=None):
        """The captcha image, validated by magic bytes before it is trusted.

        Returns None unless the bytes actually begin with a known image
        signature, so the caller falls back to the element.

        The real image comes from the DECRYPTED envelope's `captcha` field,
        which is the same JPEG the SPA renders onto the <img> and the one the
        id was issued with -- so the image and the id cannot disagree.

        The raw network response is deliberately NOT used for this. The
        getCaptcha envelope's `data` field does NOT decode to an image:
        base64-decoding it yields AES-GCM ciphertext (measured magic
        368590fd, which PIL rejects outright), and the vision endpoint answers
        "cannot identify image file". The old code preferred that field over
        the working inline data URI, so every captcha sent to the model was
        garbage and no read could ever succeed. The response lists are still
        consulted afterwards, but only through this same magic-byte gate.
        """
        if page is not None:
            for envelope in reversed(self._captcha_plaintext(page, newest_first=True)):
                data = envelope.get("captcha")
                if not (isinstance(data, str) and data):
                    continue
                try:
                    raw = base64.b64decode(data, validate=True)
                except Exception:
                    continue
                if self._looks_like_image(raw):
                    print(f"    [*] captcha image from decrypted envelope "
                          f"({len(raw):,} bytes)")
                    return raw

        for entry in (list(reversed(self._captcha_image_responses))
                      + list(reversed(self._captcha_responses))):
            body = entry.get("json", {})
            if not isinstance(body, dict):
                continue
            data = body.get("data")
            if not (isinstance(data, str) and data):
                continue
            try:
                raw = base64.b64decode(data, validate=True)
            except Exception:
                continue
            if self._looks_like_image(raw):
                print(f"    [*] captcha image from response "
                      f"({len(raw):,} bytes)")
                return raw
            print(f"    [!] response `data` is not an image "
                  f"(magic {raw[:4].hex()}) -- ignoring it")
        return None

    @staticmethod
    def _looks_like_image(raw):
        """True when the bytes start with a JPEG/PNG/GIF/BMP/WEBP signature.

        Cheap and decisive: the alternative is trusting a JSON field that has
        already been measured to hold ciphertext, and paying a vision API call
        per captcha to discover it does not decode.
        """
        return bool(raw) and (
            raw[:3] == b"\xff\xd8\xff"                    # JPEG
            or raw[:8] == b"\x89PNG\r\n\x1a\n"            # PNG
            or raw[:6] in (b"GIF87a", b"GIF89a")           # GIF
            or raw[:2] == b"BM"                           # BMP
            or raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
        )

    def _captcha_id_from(self, entries):
        """The captcha id in one of the captured responses, or None.

        Two places, because the SPA bundle and the live network disagree about
        where it lives and neither has been confirmed end to end:
          - generateVoiceCaptcha/<ID> -- the id is the last URL path segment;
          - the getCaptcha envelope     -- an `id` field beside the image data.

        Accepts a bare string, or a string that looks like a UUID/opaque id.
        Image-ish keys are skipped so `data`/`image`/`base64` can never be
        mistaken for the id.
        """
        BAD_KEYS = ("data", "image", "img", "base64", "svg")
        for entry in reversed(entries):
            url = entry.get("url", "")
            if "generateVoiceCaptcha" in url:
                candidate = url.rstrip("/").rsplit("/", 1)[-1]
                if candidate and len(candidate) > 10 and not candidate.endswith(
                        (".png", ".jpg", ".mp3")):
                    return candidate

            body = entry.get("json", {})
            if not isinstance(body, dict):
                continue
            scopes = [body]
            nested = body.get("payload")
            if isinstance(nested, dict):
                scopes.append(nested)
            for scope in scopes:
                for key, value in scope.items():
                    low = key.lower()
                    if not value or low.endswith(BAD_KEYS):
                        continue
                    if "id" in low or "captcha" in low:
                        text = str(value)
                        if len(text) > 10:
                            return text
        return None

    def _fetch_captcha_id(self, page):
        """The `id` the submit body must carry alongside the captcha code.

        Per the SPA bundle this is `et.id`, the `id` field of the
        getCaptcha/EROLL response. Whether that field actually reaches the
        network at all is unproven -- the captcha is rendered as an inline
        `data:` URI on the <img>, which is consistent with the SPA having
        already resolved the response into the DOM -- so both the image list
        and the voice-captcha list are searched rather than assuming one.

        The id lives in the DECRYPTED envelope, so the decrypt hook is the only
        place it is actually readable; the network capture can never produce
        it. The plaintext is therefore consulted first and the raw responses
        are kept only as a fallback for an id that leaks into a URL.

        Returns None when nothing carried an id; the caller decides whether
        that is fatal, because the site may accept a body without one.
        """
        for envelope in reversed(self._captcha_plaintext(page, newest_first=True)):
            found = envelope.get("id")
            if found:
                print(f"    [*] Captcha ID: {found} (decrypted envelope)")
                return str(found)

        for entries in (self._captcha_image_responses, self._captcha_responses):
            found = self._captcha_id_from(entries)
            if found:
                print(f"    [*] Captcha ID: {found}")
                return found

        seen = len(self._captcha_image_responses) + len(self._captcha_responses)
        if seen:
            urls = [e.get("url", "") for e in
                    (self._captcha_image_responses + self._captcha_responses)]
            print(f"    [!] {seen} captcha response(s) seen but none carried an "
                  f"id. URLs: {urls}")
            for e in (self._captcha_image_responses + self._captcha_responses):
                body = e.get("json", {})
                if isinstance(body, dict):
                    print(f"        keys={list(body.keys())}")
        else:
            print("    [!] no captcha response captured at all")
        return None

    @staticmethod
    def _part_numbers_from_responses(captured):
        """Pull part numbers out of the get-publish-part-list payload.

        Each element is {partNumber, partName, ...}. The submit body wants the
        partNumber, and the number is what the server resolves the file by.
        """
        numbers = []
        for entry in reversed(captured):
            payload = entry.get("json", {})
            data = payload.get("payload") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                continue
            for row in data:
                if isinstance(row, dict) and row.get("partNumber") is not None:
                    numbers.append(str(row["partNumber"]))
            if numbers:
                break
        return numbers

    def _download_result(self, page, path):
        """Fetch the file the generate call pointed at, in the page's context.

        The generate endpoint hands back a path, and the SPA reads the bytes
        with the browser's own session -- so this runs inside the page via fetch
        rather than through `requests`. Anything the file is scoped to lives in
        that session, and a fresh requests session would be refused on a request
        the browser performs happily.

        Returns the raw bytes, or None if the fetch failed. The caller decides
        whether this is worth a retry: an accepted generate is already a scarce
        resource, so a failed fetch is reported rather than silently re-submitted.
        """
        path = str(path).lstrip("/")
        url = f"{EROLL_MEDIA_BASE}/eroll/{path}"
        print(f"    [*] GET {url}")
        try:
            result = page_fetch(page, url)
        except Exception as exc:
            print(f"    [!] fetch raised {type(exc).__name__}: {exc}")
            return None
        if not result or not result.get("ok"):
            print(f"    [!] fetch failed: {result}")
            return None

        raw = result.get("b64")
        if not raw:
            print("    [!] fetch returned no bytes")
            return None
        data = base64.b64decode(raw)
        print(f"    [+] {len(data):,} bytes, content-type "
              f"{result.get('content_type')}")
        return data

    def download_all(self, structure, max_per_category=None, target_state=None, target_year=None):
        """Download all PDFs based on discovered structure."""
        print("[*] Starting comprehensive download...")

        with sync_playwright() as p:
            # headless=False needs a display; on a headless server it aborts the
            # whole run before the site is touched.
            browser = p.chromium.launch(headless=HEADLESS)
            # accept_downloads must be set on the CONTEXT. Without it Playwright
            # silently drops the site's ZIP instead of firing a download event,
            # which looks identical to a captcha rejection.
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            self.attach_status_probe(page)
            # Before _open_form: the SPA decrypts getCaptcha the moment the form
            # loads, so a hook attached afterwards has already missed the
            # envelope carrying both the image and the id.
            self._install_captcha_decrypt_hook(page)
            if not self._open_form(page):
                print("[!] ECI form never became available; aborting run.")
                browser.close()
                return

            total = 0
            successful = 0
            failed = 0

            # Pre-fetch state values
            state_values = {}
            browser2 = p.chromium.launch(headless=True)
            page2 = browser2.new_page()
            # The state list is filled by an AJAX call, so networkidle can fire
            # before any <option> exists. Without this wait the dropdown reads
            # back empty, every state fails the lookup below, and a full run
            # reports 0 attempted without touching the site. _open_form also
            # retries the intermittent 503s this page is exposed to.
            if not self._open_form(page2):
                print("[!] Could not read the state list — ECI unavailable.")
                browser2.close()
                browser.close()
                return
            opts = page2.locator("#stateCode option").all()
            for opt in opts:
                val = opt.get_attribute("value")
                text = opt.inner_text().strip()
                if val and val != "":
                    state_values[text] = val
            browser2.close()

            for state_label, state_data in structure.items():
                # Filter by target state if specified
                if target_state and target_state.lower() not in state_label.lower():
                    continue

                # Find state value
                state_value = state_values.get(state_label)
                if not state_value:
                    print(f"[!] Could not find value for state: {state_label}")
                    continue

                print(f"\n{'='*60}")
                print(f"Processing STATE: {state_label}")
                print(f"{'='*60}")

                for year_label, year_data in state_data.get("years", {}).items():
                    if target_year and target_year not in year_label:
                        continue

                    print(f"\n  YEAR: {year_label}")

                    for roll_label, roll_data in year_data.get("roll_types", {}).items():
                        print(f"    ROLL TYPE: {roll_label}")

                        dist_count = 0
                        for dist_label, dist_data in roll_data.get("districts", {}).items():
                            if max_per_category and dist_count >= max_per_category:
                                break
                            dist_count += 1

                            const_count = 0
                            for const in dist_data.get("constituencies", []):
                                if max_per_category and const_count >= max_per_category:
                                    break
                                const_count += 1

                                for lang in dist_data.get("languages", []):
                                    total += 1
                                    params = {
                                        "state": state_value,
                                        "year": year_label,  # year value is the same as label (e.g., "2025")
                                        "rollType": roll_label,
                                        "district": dist_label,
                                        "constituency": const["label"],
                                        "language": lang["label"]
                                    }

                                    print(f"      [{total}] {dist_label} | {const['label']} | {lang['label']}")
                                    success, result = self.download_all_parts_for_combination(
                                        page, params, state_label, year_label, roll_label,
                                        dist_label, const["label"], lang["label"]
                                    )

                                    # `result` is now a LIST of saved bundle paths,
                                    # one per successful batch -- a combination can
                                    # span many submissions. The old single-path
                                    # contract would have printed a list repr as
                                    # though it were a file.
                                    if success:
                                        successful += 1
                                        for saved in result:
                                            print(f"        [+] {saved}")
                                    else:
                                        failed += 1
                                        print(f"        [!] Failed: {result}")

                                    # Small delay between requests
                                    time.sleep(3)

            browser.close()

            print(f"\n{'='*60}")
            print(f"DOWNLOAD COMPLETE")
            print(f"Total attempted: {total}")
            print(f"Successful: {successful}")
            print(f"Failed: {failed}")
            print(f"{'='*60}")

            self.create_organized_zips()

    def create_organized_zips(self):
        """Create organized ZIP files based on folder structure."""
        print("[*] Creating organized ZIP archives...")

        # Create ZIP per state
        state_folders = [f for f in os.listdir(OUTPUT_FOLDER) if os.path.isdir(os.path.join(OUTPUT_FOLDER, f))]

        for state_folder in state_folders:
            state_path = os.path.join(OUTPUT_FOLDER, state_folder)
            zip_name = f"electoral_rolls_{state_folder}.zip"

            print(f"  Creating {zip_name}...")
            with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(state_path):
                    for file in files:
                        if file.endswith(".pdf") or file.endswith(".zip"):
                            full_path = os.path.join(root, file)
                            arcname = os.path.relpath(full_path, OUTPUT_FOLDER)
                            zipf.write(full_path, arcname)

            print(f"  [+] Created {zip_name}")

        # Also create a master ZIP with everything
        master_zip = "electoral_rolls_ALL.zip"
        print(f"  Creating master archive {master_zip}...")
        with zipfile.ZipFile(master_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(OUTPUT_FOLDER):
                for file in files:
                    if file.endswith(".pdf") or file.endswith(".zip"):
                        full_path = os.path.join(root, file)
                        arcname = os.path.relpath(full_path, OUTPUT_FOLDER)
                        zipf.write(full_path, arcname)

        print(f"[+] All ZIP archives created!")

        # Save download log
        with open("download_log.json", "w", encoding="utf-8") as f:
            json.dump(self.downloaded_files, f, indent=2, ensure_ascii=False)
        print("[+] Download log saved to download_log.json")

    def upload_to_minio(self):
            """Upload downloaded ZIPs into the pipeline's MinIO bucket."""
            try:
                from .minio_client import MinIOClient
                client = MinIOClient()
                if not client.is_configured():
                    print("[!] MinIO not configured. Set MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY in .env")
                    return False

                results = client.upload_downloads(Path(OUTPUT_FOLDER))
                print(f"[+] MinIO upload complete:")
                print(f"    Bucket: {results['bucket']} @ {results['endpoint']}")
                print(f"    ZIPs: {results['uploaded']} uploaded, {results['failed']} failed")
                return results["failed"] == 0

            except ImportError:
                print("[!] MinIO client not available. Install minio: pip install minio")
                return False
            except Exception as e:
                print(f"[!] MinIO upload error: {e}")
                return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ECI Electoral Roll Downloader")
    parser.add_argument("--discover", action="store_true", help="Only discover structure, don't download")
    parser.add_argument("--download", action="store_true", help="Download all (requires discovered_structure.json)")
    parser.add_argument("--max-per-category", type=int, default=None, help="Limit items per category for testing")
    parser.add_argument("--vision-key", type=str, default=None, help="Vision API key for captcha solving")
    parser.add_argument("--state", type=str, default=None, help="Specific state to download (e.g., 'NCT OF Delhi')")
    parser.add_argument("--year", type=str, default=None, help="Specific year to download (e.g., '2025')")
    parser.add_argument("--upload-to-minio", action="store_true", help="Upload downloaded files to MinIO after download")

    args = parser.parse_args()

    downloader = ECIDownloader(vision_api_key=args.vision_key)

    if args.discover:
        structure = downloader.discover_all_options()
    elif args.download:
        if os.path.exists("discovered_structure.json"):
            with open("discovered_structure.json", "r", encoding="utf-8") as f:
                structure = json.load(f)
        else:
            print("[*] No discovered structure found, discovering first...")
            structure = downloader.discover_all_options()

        downloader.download_all(structure, max_per_category=args.max_per_category,
                               target_state=args.state, target_year=args.year)

        # Upload to MinIO if requested
        if args.upload_to_minio:
            downloader.upload_to_minio()
    else:
        # Default: discover then download with limit for testing
        structure = downloader.discover_all_options()
        downloader.download_all(structure, max_per_category=1)  # Test with 1 per category