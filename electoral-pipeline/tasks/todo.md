# Dynamic OCR Robustness Tasks

- [ ] Task 1: Land exact duplicate discovery guard
- [ ] Task 2: Add pure occupancy classifier
- [ ] Task 3: Add page occupancy shadow instrumentation

## Checkpoint: occupancy safety
- [ ] Unit tests pass
- [ ] S24 and Lansdown record counts unchanged
- [ ] No page skipped by shadow detector
- [ ] Disagreements visible in logs

- [ ] Task 4: Implement shadow lattice inference
- [ ] Task 5: Add per-card shadow occupancy comparison
- [ ] Task 6: Corpus shadow audit

## Checkpoint: dynamic detector gate
- [ ] Zero missed populated cards across the 161-roll shadow audit
- [ ] Sparse, blank, and partial-tail pages covered
- [ ] Unknown layouts remain uncertain
- [ ] Human review before authority switch

- [ ] Task 7: Add field evidence records
- [ ] Task 8: Harden age and house arbitration
- [ ] Task 9: Enable dynamic occupancy only for proven classes
- [ ] Task 10: Normalize large card renders
- [ ] Task 11: Reduce redundant Paddle calls
- [ ] Task 12: Export review and completeness report

## Final checkpoint
- [ ] Full suite passes
- [ ] No record-count or field-population regression
- [ ] Dynamic path remains fallback-safe
- [ ] Performance measured before and after
