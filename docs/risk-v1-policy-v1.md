# ReturnGuard Risk-v1 and Policy-v1

Risk-v1 is a deterministic suspiciousness/anomaly index, not a fraud
probability, financial-loss estimate, priority score, or policy decision.
Missing data is neutral. `fraud_labels` and scenario IDs are not inputs.

## Canonical signals and caps

Return Behavior is canonical for duplicated historical-return facts. Customer
Intelligence supplies purchase denominators and coverage. Positive group caps
are Customer Behavior 25, Inspection 40, Network Context 10, Cross Signal 10,
and future Vision 15. Product Context mitigates by at most 10. Economics and
evidence metadata add no suspiciousness.

Customer rules:

- lifetime return rate with at least five items: 0/3/6/9 at 0.10, 0.20, 0.35;
- 30-day returns/items ratio: 0/2/4/6 at 0.15, 0.30, 0.50;
- returned/purchased value ratio: 0/2/4/6 at 0.10, 0.25, 0.40;
- repeat current-product count: 0/2/4/6 for 0, 1, 2, 3+;
- count fallback, only when rate is NULL and history is authoritative:
  0/2/4/5 for 0-1, 2-3, 4-5, 6+.

Product-rate elevation mitigates 0/3/6 for deltas up to 0.05, 0.05-0.15,
and above 0.15. Limited history caps this at 2; absent history gives none.

Inspection rules are serial mismatch 25, missing item 25, weight deviation
0/3/7/12 at 5%, 15%, and 30%, and missing accessories 0/3/6. Weight and
accessory points are suppressed when the item is absent.

Network rules activate only when Customer Behavior is at least 6 or Inspection
is at least 10. Linked return, recent 90-day activity, and shared-identifier
counts use the frozen thresholds and remain capped at 10. No identifiers are
included in risk output.

Cross-signal rules are serial plus repeat product (4), missing item plus
material behavior (3), and strong inspection plus meaningful network context
(3), capped at 10.

Scores are clamped to 0-100. Bands are LOW 0-24, MEDIUM 25-49, HIGH 50-74,
and CRITICAL 75-100. A numeric score requires two evaluable core domains or
one direct inspection observation/comparison; otherwise the band is
UNDETERMINED.

## Pattern limitations

Risk-v1 emits only patterns supported by normalized deterministic facts.
Wardrobing is defined in the contract but not emitted because current
Intelligence lacks a normalized used/worn condition semantic. Condition
misrepresentation, claim/evidence conflict, logistics damage, and visual
mismatch are likewise deferred. `LIKELY_LEGITIMATE_RETURN` is not emitted
because current evidence metadata cannot deterministically establish the
absence of contradictory evidence.

## Policy-v1

Policy-v1 applies rules in descending priority: confirmed serial mismatch,
undetermined coverage, critical physical discrepancy, high/critical review,
medium inspection handling, refund-without-return economics, low/substantial
auto-approval, then standard return. It never rejects solely from risk.

The return-fee action remains in the contract but P30 is deferred because no
normalized buyer-preference/non-defect reason is exposed by current
Intelligence. Money comparisons use `Decimal` rounded to cents.

## Reassessment and persistence

Risk event identity is SHA-256 over `return_id`, `assessment_id`,
`assessment_at`, and `risk-v1`. The `risk_events` repository uses an insert-only
MERGE keyed by that ID: retries are no-ops, while a new assessment identity or
timestamp creates a new immutable event. Previous scores are never inputs.
