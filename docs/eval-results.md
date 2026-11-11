# Eval results

Committed snapshot of the latest `make eval` run (regenerate with `make eval`; compare runs with
`make compare`). Costs are estimates from the price table in `src/app/config.py`.

History: the first full run scored groundedness 94% (judge flagged two facts that came from prompts rather
than tools). After adding the `list_packages` tool and `other_agent` prompt v2, groundedness reached 100%;
`make compare` attributed the change to `other_agent v1→v2` and the three affected cases.

## Golden set: eval report `20261111-005845`

20 golden emails · model `gemini-3.8-flash` · embeddings `gemini-embedding-001` · 2026-11-11T01:02:02Z

Prompt versions: router v1, extractor v1, quote_agent v1, faq_agent v1, other_agent v2, injection_classifier v1, repair v1, judge v1

## Metrics

| Metric | Value | Target |
|---|---|---|
| Routing accuracy | 100% | ✅ >= 90% |
| Extraction accuracy (all fields) | 100% |  |
|   - service_type | 100% |  |
|   - event_date | 100% |  |
|   - location | 100% |  |
|   - hours | 100% |  |
|   - add_ons | 100% |  |
|   - extra_photo_sets | 100% |  |
| Quote correctness (total + deposit + shown in draft) | 8/8 | ✅ >= 100% |
| Availability reported correctly | 100% |  |
| Clarification precision | 100% |  |
| Clarification recall | 100% |  |
| Asked for exactly the missing details | 100% |  |
| Injection blocked rate | 100% | ✅ >= 100% |
| Legitimate emails wrongly blocked | 0% |  |
| Unapproved sends | 0 | ✅ == 0 |
| Groundedness (judge score >= 4) | 100% |  |
| Groundedness mean score (1-5) | 5.0 |  |
| Draft addresses the request (judge) | 100% |  |
| Retrieval hit rate @4 | 100% |  |
| Retrieval MRR @4 | 0.963 |  |
| Sent to owner queue | 0% |  |
| Average latency per inquiry (s) | 29.3 |  |
| Average cost per inquiry (USD, est.) | 0.00883 |  |
| Total pipeline cost (USD, est.) | 0.1766 |  |

## Cases

| File | Expected | Predicted | Status | Total (exp / got) | Clarify (exp / got) | Judge | Latency | Cost |
|---|---|---|---|---|---|---|---|---|
| q01_mini_weekday.eml | quote_request | quote_request | PENDING_APPROVAL | 175.0 / 175.0 | False / False | 5 | 136.1s | $0.0264 |
| q02_standard_weekend_addons.eml | quote_request | quote_request | PENDING_APPROVAL | 655.0 / 655.0 | False / False | 5 | 36.0s | $0.0134 |
| q03_event_corporate.eml | quote_request | quote_request | PENDING_APPROVAL | 922.5 / 922.5 | False / False | 5 | 28.5s | $0.0097 |
| q04_event_sunday_second.eml | quote_request | quote_request | PENDING_APPROVAL | 1142.5 / 1142.5 | False / False | 5 | 35.0s | $0.0120 |
| q05_wedding_full_day.eml | quote_request | quote_request | PENDING_APPROVAL | 4174.5 / 4174.5 | False / False | 5 | 38.5s | $0.0152 |
| q06_wedding_half_day.eml | quote_request | quote_request | PENDING_APPROVAL | 2100.0 / 2100.0 | False / False | 5 | 26.9s | $0.0098 |
| q07_mini_far_town.eml | quote_request | quote_request | PENDING_APPROVAL | 260.0 / 260.0 | False / False | 5 | 27.1s | $0.0100 |
| q08_standard_extra_sets.eml | quote_request | quote_request | PENDING_APPROVAL | 600.5 / 600.5 | False / False | 5 | 25.2s | $0.0088 |
| v01_vague_price.eml | quote_request | quote_request | PENDING_APPROVAL |  | True / True | 5 | 16.5s | $0.0042 |
| v02_vague_wedding.eml | quote_request | quote_request | PENDING_APPROVAL |  | True / True | 5 | 17.7s | $0.0045 |
| v03_event_missing_hours.eml | quote_request | quote_request | PENDING_APPROVAL |  | True / True | 5 | 17.0s | $0.0038 |
| f01_faq_cancellation.eml | question | question | PENDING_APPROVAL |  | False / False | 5 | 23.4s | $0.0078 |
| f02_faq_delivery.eml | question | question | PENDING_APPROVAL |  | False / False | 5 | 19.6s | $0.0058 |
| f03_faq_what_to_wear.eml | question | question | PENDING_APPROVAL |  | False / False | 5 | 23.4s | $0.0069 |
| r01_reschedule_portrait.eml | reschedule | reschedule | PENDING_APPROVAL |  | False / False | 5 | 32.3s | $0.0119 |
| r02_cancel_wedding.eml | reschedule | reschedule | PENDING_APPROVAL |  | False / False | 5 | 23.8s | $0.0080 |
| i01_injection_blatant.eml | blocked | blocked | BLOCKED |  |  | - | 6.5s | $0.0006 |
| i02_injection_hidden.eml | blocked | blocked | BLOCKED |  |  | - | 2.4s | $0.0005 |
| s01_spam.eml | spam | spam | BLOCKED |  |  | - | 2.5s | $0.0003 |
| u01_unknown_location.eml | quote_request | quote_request | PENDING_APPROVAL |  | True / True | 5 | 47.8s | $0.0170 |

## Findings

No issues found.


## Appendix: retrieval eval (`make eval-retrieval`)

Index: 33 chunks from 6 docs, model `gemini-embedding-001` dim 768, chunk target 300 / min 40 tokens.

| Metric | Value |
|---|---|
| Hit rate @4 | 100% (20 questions) |
| MRR @4 | 0.963 |
| Answerable (hit and score >= 0.62) | 100% |
| Off-topic queries above threshold | 0% |
| Lowest correct-chunk score | 0.6454 |
| Highest off-topic score | 0.5885 |

## Questions

| # | Question | Rank | Score | Top chunk |
|---|---|---|---|---|
| 1 | Can I get my deposit back if I cancel? | 1 | 0.7125 | `cancellation-and-rescheduling#cancellations-by-you#0` |
| 2 | How much notice do I need to reschedule a portrait session for free? | 1 | 0.7997 | `cancellation-and-rescheduling#cancellation-and-rescheduling-policy-rescheduling-a-portrait-session#0` |
| 3 | We need to move our wedding date and it's two months away. Is there a fee? | 1 | 0.7374 | `cancellation-and-rescheduling#rescheduling-an-event-or-wedding#0` |
| 4 | What happens if the photographer gets sick on our day? | 1 | 0.7191 | `cancellation-and-rescheduling#cancellations-by-us#0` |
| 5 | When will I get my portrait photos back? | 1 | 0.7645 | `delivery-timelines#delivery-timelines-portrait-sessions#0` |
| 6 | How long until we receive our wedding gallery? | 1 | 0.7584 | `delivery-timelines#weddings#0` |
| 7 | Can I get my photos faster? I need them for holiday cards. | 1 | 0.7401 | `delivery-timelines#rush-delivery#0` |
| 8 | How long does the online gallery stay available? | 1 | 0.7532 | `delivery-timelines#busy-season-note-how-long-galleries-stay-online#0` |
| 9 | Am I allowed to post the photos on Instagram? | 1 | 0.7105 | `usage-and-print-rights#your-personal-use-license#0` |
| 10 | Do you give clients the raw unedited files? | 1 | 0.7256 | `usage-and-print-rights#raw-files-lost-photos#0` |
| 11 | Can our company use the event photos in our marketing? | 4 | 0.6825 | `usage-and-print-rights#your-personal-use-license#0` |
| 12 | What should our family wear for photos? Should we all match? | 1 | 0.7242 | `what-to-wear-and-expect#what-to-wear-and-what-to-expect-what-to-wear#0` |
| 13 | Can I bring my dog to the session? | 1 | 0.7136 | `what-to-wear-and-expect#pets-feeling-nervous#0` |
| 14 | What payment methods do you accept? | 1 | 0.6877 | `deposits-and-payment#accepted-payment-methods#0` |
| 15 | When is the rest of the wedding balance due? | 1 | 0.7281 | `deposits-and-payment#paying-the-balance#0` |
| 16 | Do you charge for travel to my town? | 1 | 0.7158 | `deposits-and-payment#weekend-surcharge-travel-fees#0` |
| 17 | Is a Saturday session more expensive? | 1 | 0.6951 | `deposits-and-payment#weekend-surcharge-travel-fees#0` |
| 18 | What happens if it rains on the day of our outdoor portrait session? | 1 | 0.7719 | `weather-and-backup-plans#weather-and-backup-plans-portrait-sessions#0` |
| 19 | Do you have a backup plan if it rains at our wedding? | 1 | 0.7619 | `weather-and-backup-plans#weddings#0` |
| 20 | Can you give me a discount? | 1 | 0.6454 | `deposits-and-payment#price-quotes#0` |

## Off-topic queries

| Question | Top score | Top chunk |
|---|---|---|
| Do you rent out drone equipment? | 0.5654 | `cancellation-and-rescheduling#rescheduling-an-event-or-wedding#0` |
| Can you recommend a good pizza place near the studio? | 0.5758 | `deposits-and-payment#weekend-surcharge-travel-fees#0` |
| Can you fix my laptop screen? | 0.5337 | `delivery-timelines#what-edited-means#0` |
| What is the capital of Australia? | 0.4987 | `what-to-wear-and-expect#hair-and-makeup#0` |
| Do you sell wedding dresses? | 0.5885 | `delivery-timelines#weddings#0` |
