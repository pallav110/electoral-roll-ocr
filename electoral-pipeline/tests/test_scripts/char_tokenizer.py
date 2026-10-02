"""Char-level tokenizer for psidharth567/indic-xlit-270M.

The upstream repo documents `from char_tokenizer import CharTokenizer`, but
only ships the JSON vocab files -- the Python class was never uploaded. The
format is fully specified by char_tokenizer/tokenizer_config.json and the
ordered id_to_token.json, so it is reconstructed here rather than skipped:

    [PAD] [UNK] [BOS] [EOS] [SEP] [LATIN] [HINDI] ... <characters>

Input is  [BOS][LATIN]<hindi text>[SEP]   (target script is the language tag)
Output is whatever follows [SEP]          (Latin romanization)

Only chars present in the vocab are kept; anything else becomes [UNK]. That
matters here because the OCR output carries stray punctuation and the odd
lookalike glyph.
"""
from __future__ import annotations

import json
from pathlib import Path


PAD, UNK, BOS, EOS, SEP = "[PAD]", "[UNK]", "[BOS]", "[EOS]", "[SEP]"

LATIN_TAG = "[LATIN]"


class CharTokenizer:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.token_to_id = {tok: i for i, tok in enumerate(tokens)}
        self.id_to_token = {i: tok for i, tok in enumerate(tokens)}

        self.pad_token_id = self.token_to_id[PAD]
        self.unk_token_id = self.token_to_id[UNK]
        self.bos_token_id = self.token_to_id[BOS]
        self.eos_token_id = self.token_to_id[EOS]
        self.sep_token_id = self.token_to_id[SEP]
        self.pad_token = PAD
        self.unk_token = UNK
        self.bos_token = BOS
        self.eos_token = EOS
        self.sep_token = SEP

    # -- construction ---------------------------------------------------

    @classmethod
    def load(cls, path) -> "CharTokenizer":
        """Load from a directory containing id_to_token.json."""
        path = Path(path)
        id_to_token_file = path / "id_to_token.json"
        if not id_to_token_file.exists():
            id_to_token_file = path / "vocab.json"

        raw = json.loads(id_to_token_file.read_text(encoding="utf-8"))

        # Two shapes are in circulation: a {id: token} mapping (what the
        # repo actually ships) or a bare ordered list.
        if isinstance(raw, dict):
            if all(k.isdigit() for k in raw):
                tokens = [raw[str(i)] for i in range(len(raw))]
            else:
                tokens = [raw[k] for k in sorted(raw, key=int)]
        else:
            tokens = list(raw)

        return cls(tokens)

    # -- encoding -------------------------------------------------------

    def get_lang_token(self, language: str = "Latin") -> str:
        wanted = language.strip().upper()
        for tok in self.tokens:
            if tok == wanted:
                return tok
            if tok.strip("[]").upper() == wanted:
                return tok
        return LATIN_TAG

    def encode_with_special(self, text: str) -> list[int]:
        """Character-wise encode, mapping out-of-vocab chars to [UNK]."""
        return [self.token_to_id.get(ch, self.unk_token_id) for ch in text]

    def decode_with_special(self, ids, skip_special_tokens: bool = True) -> str:
        out = []
        for i in ids:
            token = self.id_to_token.get(i)
            if token is None:
                continue
            if skip_special_tokens and token in (PAD, BOS, EOS, SEP):
                continue
            out.append(token)
        return "".join(out)