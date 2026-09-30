# Aruba Property Agent

Monitors publicly advertised Aruba homes, villas, townhouses, land, and whole multi-unit residential properties offered for sale at no more than USD 650,000. Individual apartments/condos, rentals, unavailable listings, commercial buildings, timeshares, and construction-only modular-home offers are excluded.

## Monitoring

GitHub Actions requests a run every five minutes. GitHub schedules can be delayed; this is a best-effort polling interval, not a five-minute delivery guarantee. Every hour, the same job upgrades to a deep scan using the same permanent property history. Four workers check separate sources concurrently. Per-source limits, short HTTP timeouts, one transient retry, and persistent failure cooldowns prevent one broker from stalling the job. Pagination follows advertised links or explicitly verified source endpoints. Repeated listing pages are detected and reported as partial coverage.

`SOURCES.json` contains selectors, category endpoints, and fast/deep page budgets. The added direct brokers are Century 21 Aruba, Coldwell Banker Aruba, and Keller Williams Aruba. Smiley uses embedded listing data and its advertised public listing-query API. Aruba Listings uses listing-specific structured map data. Site access varies by network; a successful local test does not imply GitHub runner access.

## Events, history, and migration

Existing history keys and first-detected dates are retained. URLs from alternate brokers/aggregators become aliases; changes to type, price, or source do not define identity. Separate numbered lots are not automatically merged. Development consolidation requires an explicit project identity; this intentionally favors avoiding false merges.

A source’s first successful check also baselines newly uncovered inventory rather than labeling it NEW. Coldwell Banker’s advertised public updated-listing RSS feeds are checked in the fast layer.

The first upgraded run establishes a baseline for newly uncovered inventory without generating historical NEW alerts. It archives the old unsent short-alert queues in `legacy_queues` for audit rather than releasing a backlog of potentially misclassified listings. All old history and daily activity remain in state. Subsequent runs detect new opportunities. A source/parser switch establishes a new price baseline instead of reporting a reduction. Same-source price reductions and major field changes need two agreeing successful observations. Missing listings on shallow/failed scans remain in permanent history; only explicit unavailable status changes availability. The daily count includes available qualifying records seen in the past seven days, so it is an approximate recent monitoring count, not a claim that every historical listing is still available.

## Email

Resend, the existing verified sender, recipient, and `RESEND_API_KEY` are preserved. New-listing emails are queued immediately after detection. Confirmed reductions/major changes are batched after 30 minutes. This may increase email frequency compared with batching every event. GitHub commits discoveries and frozen outbox payloads before sending; accepted-email acknowledgements are committed afterward. Retries reuse the same payload and Resend idempotency key. Resend acceptance is not proof of inbox delivery; an ambiguous failure retried after Resend's 24-hour idempotency retention can still duplicate a message.

Daily digests target 08:00 Aruba (12:00 UTC). A delayed run catches up later the same local day. Digests contain activity since the last successfully sent digest and an approximate monitoring count, never a full inventory dump. Digest activity clears only after API acceptance. The email footer links to the GitHub monitoring workflow; disable it there to stop alerts. No hosted unsubscribe service is added.

## Test and investigate

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
DRY_RUN=1 python tools/validate_live.py deep
```

The validator uses temporary state copies, never production state or email credentials. It measures live coverage, preserves all history keys, and replays identical inventory to check false NEW events. It writes `live-validation.json` locally and the verification workflow uploads it as an artifact. `SCAN_MODE=fast|deep|auto` overrides cadence for investigation. `STATE_FILE` chooses a separate state file. `DEFER_DELIVERY=1` checkpoints without sending; `DELIVER_ONLY=1` processes a previously checkpointed outbox. Never run production state experiments against `state.json`.

Production workflow concurrency serializes monitoring. The verification workflow runs on this development branch and pull requests with read-only repository permission and no Resend secret. Review the PR and its validation evidence before merging; scheduled production behavior changes only after merge.
