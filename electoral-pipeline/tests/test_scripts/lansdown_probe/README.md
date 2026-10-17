# Probe scripts

Throwaway diagnostics, kept for provenance rather than for re-running. Each one
answers one measured question about a bug; the answer is recorded in the commit
that fixed the bug, and most of these will fail now that the code has moved on.

They are NOT part of the test suite and are not imported by it. `pytest`
discovers only `tests/test_*.py`, so nothing here runs in CI.

Run one from inside the OCR container, which has the live bind-mounted module:

```bash
docker cp tests/test_scripts/lansdown_probe/<folder>/<script>.py \
    electoral-pipeline-ocr-1:/app/
docker exec electoral-pipeline-ocr-1 sh -c 'cd /app && python <script>.py'
```

Each writes its own output under `/data/` and redirects stdout there, because
Paddle's C++ layer writes to stderr and corrupts anything interleaved.

## Folders

| Folder | What it established |
|---|---|
| `header/` | The section line is found by its printed `अनुभाग` label, not by position. Also: the separator is not always printed (Lansdown prints `व नाम`, S24 prints `और नाम :`), and a section *digit* must be read with the English model — the Hindi model renders a printed `1` as `4` on S24. |
| `speed/` | Per-page cost attribution. The decisive one: `uplift.py` showed the Hindi reader upscaled every card 2× for 22× the cost and an identical read. `cpucheck.py` showed WSL2 was giving the container 1 core while `cpu_count()` claimed 16. |
| `age/` | Why a card printing `आयु : 41` returned `88`: `AGE_REGION` was calibrated on S24-53 and landed in blank white on Lansdown, Paddle read noise, and `18 <= n <= 120` accepted it. `narrow.py` shows `_age_value_span` returns a sliver of the shirorekha on that template. |
| `house/` | Rendering `HOUSE_REGION` so the disputed house numbers could be read by eye instead of argued about. Card 1 genuinely prints 7. |
| `cards/` | Whether the card lattice can be detected rather than hardcoded. The pitch infers to 18.86% by autocorrelation on both rolls; phase is unproven and a 523px empty column inside a card defeats any "empty run = gutter" heuristic. Deferred. |

## Why these are not tests

They were written to answer a question that was open at the time, and they answer
it against code that has since changed. Several now fail or print nothing for
exactly that reason. The durable assertions live in `tests/test_*.py` instead.