"""IndicXlit (romanization) vs IndicTrans2 (translation) vs rule-based.

IndicTrans2 is a *translation* model: पति -> "husband", आकाश -> "the sky".
That is correct translation and wrong output for an electoral roll, where a
relation label must stay literal and a name must stay a name.

IndicXlit is built for the other job -- native Indic script -> Latin
alphabet -- which is what a voter roll actually wants.

Runs both over the real OCR corpus and reports which wins per field group.

Usage:
    python tests/test_scripts/eval_indirect_xlit.py
    python tests/test_scripts/eval_indirect_xlit.py --device cpu
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_indictrans2_vs_rulebased import collect_hindi, load_corpus, rule_based  # noqa: E402

import re

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")


def indicxlit(texts, device="cuda", batch_size=4, model_name=None):
    """IndicXlit romanization: script conversion, no translation.

    ai4bharat/IndicXlit ships only a fairseq checkpoint, which does not
    build on Windows and pins an ancient torch. psidharth567/indic-xlit-270M
    is a transformers-native port of the same idea (Gemma-3-270M, char-level
    tokenizer) and is what we use here.

    Format is [BOS][LANG]<source>[SEP]<target>[EOS] -- a causal LM, so this
    generates rather than encodes/decodes.
    """
    import torch
    from transformers import AutoModelForCausalLM

    model_name = model_name or "psidharth567/indic-xlit-270M"

    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        trust_remote_code=True,
        torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
    ).to(device)
    model.eval()

    tokenizer = _load_char_tokenizer(model_name)

    results = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        lang = tokenizer.get_lang_token("Latin")

        prompts = [
            f"{tokenizer.bos_token}{lang}{text}{tokenizer.sep_token}" for text in batch
        ]
        encoded = [tokenizer.encode_with_special(p) for p in prompts]

        max_len = max(len(ids) for ids in encoded)
        pad_id = tokenizer.pad_token_id
        input_ids, attention = [], []
        for ids in encoded:
            pad = max_len - len(ids)
            input_ids.append(ids + [pad_id] * pad)
            attention.append([1] * len(ids) + [0] * pad)

        input_ids = torch.tensor(input_ids, device=device)
        attention = torch.tensor(attention, device=device)

        with torch.inference_mode():
            generated = model.generate(
                input_ids=input_ids,
                attention_mask=attention,
                max_new_tokens=96,
                num_beams=5,
                num_return_sequences=1,
                pad_token_id=pad_id,
                eos_token_id=tokenizer.eos_token_id,
            )

        # generate() returns prompt + completion. Slice per row using that
        # row's own prompt length -- padding differs across the batch, so a
        # single shared index would cut into other rows' prompts.
        prompt_len = max_len
        for row in generated:
            completion = row[prompt_len:].tolist()
            text = tokenizer.decode_with_special(completion, skip_special_tokens=True)
            results.append(text.strip())

        print(f"  xlit: {min(start + batch_size, len(texts))}/{len(texts)}", flush=True)

    return results


def _load_char_tokenizer(model_name):
    """Download the vocab files and build a tokenizer around them.

    The repo documents a CharTokenizer class but does not ship it, so
    char_tokenizer.py in this directory reconstructs it from
    char_tokenizer/tokenizer_config.json + id_to_token.json.
    """
    from huggingface_hub import hf_hub_download

    from char_tokenizer import CharTokenizer

    files = ["char_tokenizer/id_to_token.json", "char_tokenizer/tokenizer_config.json"]
    for name in files:
        hf_hub_download(repo_id=model_name, filename=name)

    first = hf_hub_download(repo_id=model_name, filename=files[0])
    return CharTokenizer.load(Path(first).parent)


# A model that "translated" these has drifted away from the answer, however
# fluent the English is.
SEMANTIC_DRIFT = {
    "पुरुष": ("male", "man", "the men's"),
    "महिला": ("woman", "female"),
    "पति": ("husband",),
    "पिता": ("father",),
    "माता": ("mother",),
    "पुत्र": ("son",),
    "पुत्री": ("daughter",),
    "भाई": ("brother",),
    "बहन": ("sister",),
}


def is_drift(hindi: str, english: str) -> bool:
    """Did the model answer a different question than the field asks?"""
    triggers = SEMANTIC_DRIFT.get(hindi.strip())
    if not triggers:
        return False
    return english.strip().lower().startswith(triggers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--batch-size", type=int, default=4,
                        help="small by default: an 8GB laptop GPU runs out "
                             "of memory with a larger batch plus beam search")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default=str(REPO_ROOT / "indicxlit_eval.csv"))
    args = parser.parse_args()

    unique, field_of, counts, file_count = load_corpus(None)
    if args.limit:
        unique = unique[: args.limit]

    print(f"Unique Hindi strings: {len(unique)}")

    rule = rule_based(unique)

    print(f"\nLoading IndicXlit on {args.device}...")
    t0 = time.perf_counter()
    xlit = indicxlit(unique, args.device, args.batch_size)
    xlit_secs = time.perf_counter() - t0
    print(f"IndicXlit: {xlit_secs:.2f}s for {len(unique)} "
          f"({xlit_secs / len(unique) * 1000:.1f} ms/string)\n")

    with open(args.out, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["field", "occurrences", "hindi", "rule_based", "indicxlit",
                         "digits_ok", "semantic_drift"])
        for text, r, x in zip(unique, rule, xlit):
            writer.writerow([
                field_of.get(text, ""),
                counts.get(text, 0),
                text,
                r or "",
                x or "",
                set(re.findall(r"\d", text)) <= set(re.findall(r"\d", x or "")),
                is_drift(text, x),
            ])
    print(f"Wrote {args.out}\n")

    drifts = [t for t, x in zip(unique, xlit) if is_drift(t, x)]
    lost = [t for t, x in zip(unique, xlit)
            if not set(re.findall(r"\d", t)) <= set(re.findall(r"\d", x or ""))]
    leftover = [t for t, x in zip(unique, xlit) if DEVANAGARI_RE.search(x or "")]

    print("=" * 72)
    print(f"IndicXlit: semantic drift {len(drifts)} / {len(unique)}")
    print("=" * 72)
    for t in drifts[:15]:
        print(f"  {t!r}")

    print(f"\nDigits lost: {len(lost)}")
    for t in lost[:15]:
        print(f"  {t!r}")

    print(f"Devanagari remaining: {len(leftover)}")
    for t in leftover[:15]:
        print(f"  {t!r}")

    print("\n" + "=" * 72)
    print("SIDE BY SIDE -- names, relations, house numbers")
    print("=" * 72)
    groups = {
        "RELATIONS (must stay literal)": {"relation_name", "gender"},
        "HOUSE NUMBERS (must stay literal)": {"house_no"},
        "NAMES (must stay names)": {"voter_first_name", "voter_sur_name",
                                    "voter_middle_name"},
    }
    for title, fields in groups.items():
        print(f"\n{title}")
        rows = [(t, r, x) for t, r, x in zip(unique, rule, xlit)
                if field_of.get(t) in fields]
        seen = set()
        shown = 0
        for t, r, x in rows:
            if t in seen:
                continue
            seen.add(t)
            print(f"  {t:<24} rule: {r:<20} xlit: {x}")
            shown += 1
            if shown >= 18:
                break

    print(f"\nFull comparison: {args.out}")


if __name__ == "__main__":
    main()