# Dynamic OCR Robustness Tasks

- [x] Task 1: Land exact duplicate discovery guard
- [x] Task 2: Add pure occupancy classifier
- [x] Task 3: Add page occupancy shadow instrumentation

## Checkpoint: occupancy safety

- [x] Unit tests pass
- [x] S24 and Lansdown smoke record counts unchanged
- [x] No page-level skip changed; blank-slot skipping is conservative
- [x] Disagreements are reported by the shadow collector
- [ ] Full 161-roll shadow audit complete
- [ ] OCR container restarted with the final shadow code and real logs verified

- [ ] Task 4: Implement shadow lattice inference
- [x] Task 5: Add per-card shadow occupancy comparison
- [ ] Task 6: Corpus shadow audit

## Checkpoint: sparse-page rescue

- [x] Legacy page probe remains unchanged
- [x] Rejected pages are rescanned using all 30 occupancy slots
- [x] Any occupied/uncertain slot rescues the page
- [x] Real 14-card Lansdown tail page returns 14 records
- [ ] Corpus audit verifies zero false-blank pages

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
