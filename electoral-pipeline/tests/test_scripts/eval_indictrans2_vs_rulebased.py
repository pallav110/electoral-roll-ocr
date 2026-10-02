"""Compare IndicTrans2 (neural MT) against the rule-based transliterator.

Runs on the real OCR output in
OCR/ground_truth_per_page_values_verified/json_files/ so the comparison is
over strings this pipeline actually produces, not invented examples.

The question this answers: does the model handle arbitrary Hindi more
gracefully than the rule-based path, and is it fast enough to be worth
running alongside it?

Writes a three-way CSV (hindi / rule-based / model) for eyeballing, and
prints a summary with timing.

Usage:
    python tests/test_scripts/eval_indictrans2_vs_rulebased.py
    python tests/test_scripts/eval_indictrans2_vs_rulebased.py --device cpu
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

# Fields where a wrong answer is a data error rather than a cosmetic one.
# These get reported separately from the bulk of the corpus.
HIGH_RISK_FIELDS = {"house_no"}

# Closed vocabularies already handled by STATIC_MAPPINGS in transliterate.py.
# A model that "translates" पुरुष into prose here is a regression, not a win.
CLOSED_VOCAB = {"पुरुष": "Male", "महिला": "Female"}


def collect_hindi(obj, field="", out=None):
    """Every (field_name, string) pair in the tree containing Devanagari."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            collect_hindi(value, key, out)
    elif isinstance(obj, list):
        for value in obj:
            collect_hindi(value, field, out)
    elif isinstance(obj, str) and DEVANAGARI_RE.search(obj):
        out.append((field, obj))
    return out


def load_corpus(pattern: str | None):
    """Unique Hindi strings plus the field each was first seen in."""
    if pattern:
        paths = sorted(glob.glob(pattern))
        described = pattern
    else:
        default = (
            REPO_ROOT
            / "OCR"
            / "ground_truth_per_page_values_verified"
            / "json_files"
            / "*.json"
        )
        paths = sorted(glob.glob(str(default)))
        described = str(default)

    if not paths:
        raise SystemExit(f"No OCR JSON files matched: {described}")

    pairs = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            collect_hindi(json.load(handle), out=pairs)

    first_field: dict[str, str] = {}
    for field, text in pairs:
        first_field.setdefault(text, field)

    counts: dict[str, int] = {}
    for _, text in pairs:
        counts[text] = counts.get(text, 0) + 1

    unique = sorted(first_field)
    return unique, first_field, counts, len(paths)


def rule_based(texts):
    """The production transliterator."""
    from app.transliterate import transliterate

    out = []
    for text in texts:
        try:
            out.append(transliterate(text))
        except Exception as exc:  # a crash is a data point, not a stop
            out.append(f"<ERROR {type(exc).__name__}: {exc}>")
    return out


class _ManualProcessor:
    """Stand-in for IndicProcessor when the package is unavailable.

    indictranstoolkit needs a C toolchain to build on Windows, and pip
    rolls back the whole install transaction when that fails -- taking
    transformers with it.

    The model's own tokenizer does `src_lang, tgt_lang, text = text.split(" ", 2)`
    (tokenization_indictrans.py:200), so the language tags are not optional:
    they are part of the input format. Everything else the toolkit does for
    hin_Deva -> eng_Latn is script normalisation, which the tokenizer's
    sentencepiece model handles. Announced in the output so nobody mistakes
    this for a fully clean run.
    """

    available = False

    def preprocess_batch(self, batch, src_lang, tgt_lang):
        return [f"{src_lang} {tgt_lang} {text}" for text in batch]

    def postprocess_batch(self, batch, lang):
        return [t.strip() for t in batch]


def _processor():
    """Prefer the real IndicProcessor; fall back to the manual equivalent."""
    try:
        from IndicTransToolkit import IndicProcessor

        proc = IndicProcessor(inference=True)
        print("Using IndicTransToolkit IndicProcessor")
        return proc
    except Exception as exc:
        print(f"NOTE: IndicProcessor unavailable ({type(exc).__name__}: {exc}).")
        print("      Using manual language tagging instead -- equivalent for")
        print("      hin_Deva -> eng_Latn. For a fully faithful run, install:")
        print("        pip install indictranstoolkit")
        print("      (requires Microsoft C++ Build Tools on Windows)")
        return _ManualProcessor()


def indictrans2(texts, device="cuda", batch_size=16, num_beams=5, model_name=None):
    """The Colab approach, minus the parts that only work on a notebook."""
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    model_name = model_name or "ai4bharat/indictrans2-indic-en-dist-200M"
    proc = _processor()

    try:
        tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModelForSeq2SeqLM.from_pretrained(
            model_name,
            trust_remote_code=True,
            torch_dtype=torch.float16 if device == "cuda" else torch.float32,
        ).to(device)
    except OSError as exc:
        # The 200M dist checkpoint is gated: HuggingFace wants an account
        # with the licence accepted, which is why the Colab version calls
        # login() before loading anything.
        if "gated" in str(exc).lower() or "401" in str(exc):
            raise SystemExit(
                f"\n{model_name} is a gated repo and you are not authenticated.\n\n"
                f"  1. Accept the licence at:\n"
                f"     https://huggingface.co/ai4bharat/indictrans2-indic-en-dist-200M\n"
                f"     (Requires a free HuggingFace account.)\n"
                f"  2. Then run:\n"
                f"     huggingface-cli login\n"
                f"     -- or set HF_TOKEN in your environment.\n\n"
                f"Any other --model that is ungated also works, e.g.\n"
                f"  --model ai4bharat/indictrans2-indic-en-dist-200M\n"
            )
        raise
    model.eval()

    results = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        processed = proc.preprocess_batch(batch, src_lang="hin_Deva", tgt_lang="eng_Latn")
        inputs = tokenizer(
            processed,
            truncation=True,
            padding="longest",
            return_tensors="pt",
            return_attention_mask=True,
        ).to(device)

        with torch.inference_mode():
            generated = model.generate(
                **inputs,
                use_cache=True,
                min_length=0,
                max_length=128,
                num_beams=num_beams,
                num_return_sequences=1,
            )

        decoded = tokenizer.batch_decode(
            generated, skip_special_tokens=True, clean_up_tokenization_spaces=True
        )
        results.extend(proc.postprocess_batch(decoded, lang="eng_Latn"))
        print(f"  model: {min(start + batch_size, len(texts))}/{len(texts)}", flush=True)

    return results


def digits_preserved(hindi: str, english: str) -> bool:
    """Numbers must survive translation. An address that loses its digits is
    not a cosmetic failure -- it points at the wrong house."""
    return set(re.findall(r"\d", hindi)) <= set(re.findall(r"\d", english or ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--num-beams", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    parser.add_argument("--glob", default=None, help="override JSON discovery")
    parser.add_argument("--out", default=str(REPO_ROOT / "indictrans2_eval.csv"))
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    unique, field_of, counts, file_count = load_corpus(args.glob)
    if args.limit:
        unique = unique[: args.limit]

    print(f"Files: {file_count}")
    print(f"Unique Hindi strings: {len(unique)}")

    t0 = time.perf_counter()
    rule = rule_based(unique)
    rule_secs = time.perf_counter() - t0
    print(f"Rule-based: {rule_secs:.3f}s for {len(unique)} strings "
          f"({rule_secs / len(unique) * 1000:.2f} ms/string)\n")

    print(f"Loading IndicTrans2 on {args.device}...")
    t0 = time.perf_counter()
    try:
        model_out = indictrans2(
            unique, args.device, args.batch_size, args.num_beams, args.model
        )
    except ImportError as exc:
        raise SystemExit(
            f"\nMissing dependency: {exc}\n"
            "Install with:\n"
            "  pip install torch torchvision --index-url https://download.pytorch.org/whl/cu132\n"
            "  pip install transformers sentencepiece sacremoses accelerate "
            "pandas indictranstoolkit"
        )
    model_secs = time.perf_counter() - t0
    print(f"\nModel: {model_secs:.3f}s for {len(unique)} strings "
          f"({model_secs / len(unique) * 1000:.2f} ms/string)")
    print(f"Speed ratio: model is {model_secs / rule_secs:.1f}x the rule-based cost\n")

    with open(args.out, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["field", "occurrences", "hindi", "rule_based", "indictrans2", "digits_ok"])
        for text, r, m in zip(unique, rule, model_out):
            writer.writerow(
                [
                    field_of.get(text, ""),
                    counts.get(text, 0),
                    text,
                    r or "",
                    m or "",
                    digits_preserved(text, m or ""),
                ]
            )
    print(f"Wrote {args.out}\n")

    high_risk = [(t, r, m) for t, r, m in zip(unique, rule, model_out)
                 if field_of.get(t) in HIGH_RISK_FIELDS]
    closed = [(t, r, m) for t, r, m in zip(unique, rule, model_out) if t in CLOSED_VOCAB]
    lost_digits = [(t, m) for t, m in zip(unique, model_out) if not digits_preserved(t, m)]
    still_hindi = [t for t, m in zip(unique, model_out) if DEVANAGARI_RE.search(m or "")]

    print("=" * 70)
    print("HOUSE NUMBERS -- must stay literal, not be translated")
    print("=" * 70)
    for t, r, m in high_risk:
        print(f"  {t!r}\n      rule: {r!r}\n      model: {m!r}")

    print()
    print("=" * 70)
    print("CLOSED VOCAB (gender) -- rule-based should win")
    print("=" * 70)
    for t, r, m in closed:
        print(f"  {t!r}\n      rule: {r!r}\n      model: {m!r}")

    print()
    print(f"DIGITS LOST by model: {len(lost_digits)}")
    for t, m in lost_digits[:20]:
        print(f"  {t!r} -> {m!r}")

    print(f"\nDEVANAGARI REMAINING in model output: {len(still_hindi)}")
    for t in still_hindi[:20]:
        print(f"  {t!r}")

    rule_empty = sum(1 for r in rule if not r or r.startswith("<ERROR"))
    model_empty = sum(1 for m in model_out if not m)
    print(f"\nEmpty/error output -- rule: {rule_empty}, model: {model_empty}")
    print(f"\nEyeball the full comparison: {args.out}")


if __name__ == "__main__":
    main()