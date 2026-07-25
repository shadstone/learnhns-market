# LearnHNS Market Load Performance Remediation

Status: In progress
Created: 2026-07-25
Repository: `shadstone/learnhns-market`
Production site: `https://market.learnhns.com`
Draft pull request: `https://github.com/shadstone/learnhns-market/pull/1`

## Problem statement

Users report that the LearnHNS Market initial page load can take 20–30 seconds. The production application was inspected on 2026-07-25 to determine whether the delay comes from hosting capacity, database access, HSD node access, or frontend delivery.

The core problem is architectural rather than a lack of hosting capacity: read-only browse requests synchronously verify every active listing against HSD before returning HTML.

## Production evidence

Measurements taken from Bangkok on 2026-07-25:

| Route | Approximate TTFB | Response size | Notes |
| --- | ---: | ---: | --- |
| `/` | 6.8–8.0 seconds | 530,230 bytes uncompressed; about 24 KB gzip | Renders about 290 listings |
| `/pending` | 7.4 seconds | 52,939 bytes | Performs the same active-listing scan |
| `/stats` | 7.2 seconds | 38,451 bytes | Performs the same active-listing scan |
| `/sold` | 0.6 seconds | 144,392 bytes | Does not perform live verification for every row |
| `/docs` | 0.4 seconds | 15,790 bytes | Database/HSD-independent comparison |
| `/status` | 0.4 seconds | 8,516 bytes | One HSD status request |

The homepage response did not include an HTML cache policy. Static assets were returned with `Cache-Control: no-cache`.

Gunicorn runs two workers. Two concurrent slow requests can therefore occupy both workers, causing later visitors to queue. A base request time of roughly seven seconds can plausibly become 20–30 seconds under modest concurrency.

## Verified root cause

The homepage calls `_active_listings_unique_by_name()`. That helper loads all active listings and calls `_listing_lock_coin_is_spent()` for each one.

For each active Shakedex listing, the verification path calls:

1. HSD status to obtain chain state.
2. HSD `getnameinfo` for the listing name.

With approximately 290 active listings, one homepage request can perform approximately 580 sequential HSD requests, plus readiness and pending-listing checks. The same helper is used by `/pending` and `/stats`.

The read path can also update a listing's status and commit the database transaction while handling a GET request. This makes page rendering expensive, stateful, and difficult to cache safely.

Relevant code:

- `app/blueprints/main.py`: `index()`, `pending()`, and `stats()`
- `app/blueprints/api.py`: `_active_listings_unique_by_name()`
- `app/blueprints/api.py`: `_listing_lock_coin_is_spent()`
- `app/blueprints/api.py`: `_name_transfer_status()`
- `start.sh`: two Gunicorn workers

## Assessment of the proposed solution

The recommendation to keep marketplace state in a database and update it using a background indexer is correct.

The repository already contains much of this architecture:

- Listings and marketplace state are stored in PostgreSQL through SQLAlchemy.
- `app/marketplace_indexer.py` scans blocks and records marketplace covenant events.
- `scripts/watch-marketplace-covenants.py` provides a polling worker.
- `PROCESS_TYPE=marketplace-indexer` starts the worker.

However, the production indexer was stale when inspected:

| Metric | Value |
| --- | --- |
| HSD chain height | 339,665 |
| Last indexed height | 338,218 |
| Indexer lag | 1,447 blocks |
| Indexer `updatedAt` | 2026-07-14 18:45:33 UTC |
| Reported status | `watching` |

The indexer's reported status therefore cannot currently be treated as proof that it is healthy. Database-only browsing should be enabled only after the worker is caught up and freshness monitoring is in place.

Railway inspection identified the failure:

- The `learnhns-marketplace-indexer` deployment was `CRASHED` and stopped.
- On 2026-07-14, a decoded covenant name containing a NUL byte reached a PostgreSQL query.
- PostgreSQL rejected the query with `ValueError: A string literal cannot contain NUL (0x00) characters`.
- Railway restarted the process ten times and then stopped it under the configured restart policy.

## Target architecture

### Read-only browsing

Homepage, pending, stats, search, and listing feeds read marketplace state from PostgreSQL only. They do not synchronously contact HSD for every listing.

### Background reconciliation

A continuously running marketplace indexer:

- Tracks HSD height and its own indexed height.
- Stores recent block-height/hash checkpoints and verifies the indexed tip each cycle.
- Scans new blocks.
- Detects spends of listing lock coins.
- Records TRANSFER and FINALIZE events.
- Rewinds orphaned events and listing state to the last common checkpoint before replaying a chain reorganization.
- Updates listing status transactionally.
- Exposes honest health and lag metrics.
- Alerts when stopped, stale, or behind.

### Security-sensitive actions

Live HSD verification remains mandatory for:

- Creating or accepting a listing.
- Buying.
- Cancelling.
- Recording a sale.
- Finalizing a transfer.
- Explicit manual status refreshes.

A cached browse result must never be treated as sufficient authorization to perform a transaction.

## Implementation goals

### Goal 1 — Restore and observe the indexer

Status: In progress — production restored and caught up; 24-hour observation window active

- Confirm the Railway marketplace-indexer service exists and is running.
- Determine why it stopped updating after 2026-07-14.
- Catch up from height 338,219 to the current HSD height without skipping blocks.
- Correct the health endpoint so `watching` is not reported when `updatedAt` is stale.
- Report at least current HSD height, indexed height, lag, last successful cycle, and last error.
- Add an alert or external monitor for stale timestamp and excessive block lag.

Done when:

- Indexed height remains within an agreed threshold of HSD height for 24 hours.
- A stopped worker becomes visibly unhealthy within five minutes.
- Restarting the worker resumes from the stored height without manual repair.

### Goal 2 — Make browse routes database-only

Status: Completed in production on 2026-07-25

- Split database selection from live chain reconciliation.
- Remove per-listing HSD calls from `/`, `/pending`, `/stats`, and read-only listing feeds.
- Ensure GET routes do not change listing status or commit database transactions.
- Use the indexer's stored status as the browse snapshot.
- Preserve live verification in transaction-changing endpoints.
- Add query-count and HSD-call-count tests for browse routes.

Done when:

- A homepage request makes zero per-listing HSD calls.
- Repeated GET requests cause no database writes.
- Homepage server TTFB is below one second under normal production load.
- A spent listing disappears after the indexer processes the relevant block.

### Goal 3 — Add safe response caching

Status: Completed in production on 2026-07-25

- Define separate behavior for anonymous and authenticated responses.
- Add a short cache policy for anonymous marketplace browse responses.
- Consider a 15–30 second freshness window with `stale-while-revalidate`.
- Ensure account navigation, watch buttons, and session state are not leaked through a shared cache.
- Invalidate or naturally expire cached results after marketplace state changes.
- Avoid adding Redis unless measurement shows PostgreSQL/CDN caching is insufficient.

Done when:

- Anonymous repeat requests are served from cache where expected.
- Authenticated content is never shared between accounts.
- Listing updates become visible within the documented freshness window.

### Goal 4 — Reduce initial render cost

Status: Completed in production on 2026-07-25

- Paginate or progressively load listings instead of rendering all listings into the first HTML response.
- Preserve search, price, length, status, watcher, and punycode filters using server-side query parameters or a paginated API.
- Set a practical initial page size, such as 30–50 listings.
- Precompile Tailwind CSS instead of using `cdn.tailwindcss.com` in production.
- Add long-lived immutable caching for versioned static assets.
- Optimize the 138 KB header logo if visual quality can be preserved.

Done when:

- Initial HTML is substantially smaller than the current 530 KB uncompressed response.
- The page remains usable while later results load.
- Filters and sorting produce correct results across the full dataset.
- Production no longer relies on Tailwind's browser-side compiler.

### Goal 5 — Verify scale, cost, and correctness

Status: In progress — production load test passed; observation window and cost review remain

- Record p50, p95, and p99 TTFB for the primary routes.
- Load-test concurrent anonymous browsing without performing marketplace actions.
- Track application CPU, HSD CPU, database load, request volume, and error rate.
- Test indexer restart, HSD unavailability, stale data, and reorganization handling. Completed in the container suite.
- Document operational recovery steps.

Done when:

- Homepage p95 TTFB is below one second at the agreed concurrency target.
- Browse traffic does not generate per-listing HSD requests.
- Worker failure is detected automatically.
- Marketplace actions retain live chain verification.
- Hosting spend and resource usage are measured before and after the change.

## Recommended execution order

1. Goal 1: restore and monitor the indexer.
2. Goal 2: remove synchronous reconciliation from browse requests.
3. Goal 3: introduce safe short-duration caching.
4. Goal 4: paginate and optimize frontend delivery.
5. Goal 5: load-test, validate correctness, and document operations.

Goal 4 planning can run alongside Goals 1–2, but the read-path and indexer changes provide the largest performance and cost improvement.

## Guardrails

- Do not hide a stale or failed indexer behind page caching.
- Do not trust cached state when authorizing a transaction.
- Do not increase Gunicorn workers or Railway resources as the primary fix.
- Do not run chain reconciliation inside normal GET requests.
- Do not expose authenticated HTML through a shared public cache.
- Do not skip unindexed blocks while catching up.

## Overall completion criteria

This remediation is complete when:

- The production marketplace indexer is continuously healthy and monitored.
- Browse routes use PostgreSQL without per-listing HSD calls or writes.
- Homepage p95 TTFB is below one second at the agreed traffic level.
- Initial HTML and browser rendering costs are materially reduced.
- Anonymous caching is safe and documented.
- Buying, listing, cancellation, sale, and finalization actions still perform live HSD verification.
- Before/after performance and infrastructure-cost measurements are recorded in this document.

## Production verification

Post-deployment measurements from Bangkok on 2026-07-25:

| Check | Before | After |
| --- | ---: | ---: |
| Homepage TTFB, individual requests | 6.8–8.0 seconds | 0.40–0.58 seconds |
| Homepage HTML | 530,230 bytes | 105,253 bytes |
| Listings rendered initially | About 290 | 48 |
| Marketplace index lag | 1,447 blocks | 0 blocks |
| Indexer health endpoint | HTTP 200 but misleading `watching` | HTTP 200 with `healthy: true`, zero lag, and fresh heartbeat |

Production load test:

- Requests: 100
- Concurrency: 10
- Failures: 0
- Wall time: 4.984 seconds
- TTFB p50: 329 ms
- TTFB p95: 647 ms
- TTFB p99: 876 ms
- Maximum TTFB: 903 ms
- Total response time p95: 878 ms
- Total response time p99: 1,128 ms

Additional live checks:

- `/pending`: 0.50-second TTFB
- `/stats`: 0.46-second TTFB
- `/sold`: 0.71-second TTFB
- `/api/v2/pending-listings`: 0.53-second TTFB with 22 rows and no live row refresh
- Anonymous browse responses: `public, max-age=15, s-maxage=30, stale-while-revalidate=60`
- Requests carrying an account cookie: `private, no-store`
- Versioned static assets: `public, max-age=31536000, immutable`
- Tailwind is served as a compiled 26,600-byte stylesheet.
- Pagination, full-dataset search, status filtering, and price sorting returned HTTP 200 in production.
- The 19-test container suite proves that listing uploads validate the live owner coin, buying re-fetches the listing coin, sale and cancellation records validate the spending transaction, and transfer-finalization status reads live chain and name state.
- Regression coverage also bounds homepage SQL statements independently of listing count and proves that an indexed lock-coin spend removes the listing from the active snapshot.
- Draft PR CI rebuilds the production image, runs all 19 tests, and migrates an empty database to the current Alembic head. Run `30142314984` passed without warnings.

### Railway resource and cost evidence

Railway service metrics provide the infrastructure baseline. The before window is the 2026-07-24 complaint period from 09:00:00–11:46:40 UTC. The first post-deployment sample is from 2026-07-25 after 03:08 UTC.

| Service and metric | Before | Early after | Change |
| --- | ---: | ---: | ---: |
| Web CPU average | 0.0907 vCPU | 0.0257 vCPU | -71.7% |
| Web memory average | 186.5 MB | 174.9 MB | -6.2% |
| HSD CPU average | 0.0248 vCPU | 0.00057 vCPU | -97.7% |
| HSD memory average | 2,898.7 MB | 3,157.8 MB | +8.9% |
| Indexer CPU average | 0 vCPU because the worker was stopped | 0.00009 vCPU | Worker restored at negligible steady CPU |
| Indexer memory average | 0 MB because the worker was stopped | 89.6 MB | Cost of restored continuous indexing |

The early sample supports the expected CPU reduction from removing hundreds of request-time HSD calls. It is not yet a final monthly-cost estimate: the post-deployment interval is short, contains the production load test, and HSD memory varies independently of web request handling. The final comparison will use the complete observation window and Railway's current per-minute CPU and memory pricing.

## Monitoring and recovery

Public health check:

```text
https://market.learnhns.com/api/v2/market-index/status
```

Healthy criteria:

- HTTP status is 200.
- `healthy` is `true`.
- `lagBlocks` is at most 6.
- `heartbeatAgeSeconds` is at most 300.
- `lastError` is empty.

Unhealthy responses use HTTP 503 and include machine-readable reasons such as `stale-heartbeat`, `block-lag`, `worker-failed`, or `node-unreachable`.

Railway services:

- Web: `learnhns-market`
- Indexer: `learnhns-marketplace-indexer`
- Node: `learnhns-hsd`

Recovery sequence:

1. Inspect the public health payload and record HSD height, indexed height, lag, heartbeat age, and error.
2. Inspect the Railway indexer deployment status and recent logs.
3. Confirm HSD is reachable and fully synced.
4. Restart or redeploy the indexer only after identifying whether the failure is application, database, or HSD related.
5. Confirm the stored index height advances without gaps.
6. Wait for lag to return to the configured threshold.
7. Confirm the health endpoint returns HTTP 200 and remains fresh.
8. Do not reintroduce request-time reconciliation as a recovery shortcut.

A Codex heartbeat named `LearnHNS market 24h health watch` checks health and homepage performance hourly for 24 runs. It reports failures to this task.

The worker keeps 2,016 recent block checkpoints and searches up to 720 blocks for a common ancestor if the stored tip hash no longer matches HSD. A supported reorganization removes orphaned `hsd-block` events, reverts listings whose recorded sale transaction was orphaned, and deterministically replays from the common ancestor. A deeper reorganization fails visibly rather than silently trusting potentially invalid marketplace state.

## Change log

| Date | Change | Result |
| --- | --- | --- |
| 2026-07-25 | Initial production investigation and remediation plan | Plan accepted; implementation not started |
| 2026-07-25 | Inspected Railway indexer deployment and logs | Found NUL-containing covenant name crash and exhausted restart policy |
| 2026-07-25 | Implemented indexer input validation, resilient polling, heartbeat updates, truthful health reporting, and hourly historical-hash reconciliation | Ten container tests passing |
| 2026-07-25 | Implemented database-only browse snapshots, short anonymous caching, 48-row pagination, server-side filtering, versioned assets, and precompiled Tailwind CSS | Local container verification passing; production deployment pending |
| 2026-07-25 | Deployed the repaired indexer and optimized web application to Railway | Homepage TTFB reduced to 0.40–0.58 seconds; indexer catch-up in progress |
| 2026-07-25 | Completed indexer catch-up and production load test | Zero block lag; 100 requests at concurrency 10 with zero failures and 647 ms p95 TTFB |
| 2026-07-25 | Started hourly 24-run health observation | Automation `learnhns-market-24h-health-watch` active |
| 2026-07-25 | Captured Railway complaint-window and early post-deployment resource metrics | Web CPU down 71.7% and HSD CPU down 97.7%; final cost conclusion deferred until the observation window completes |
| 2026-07-25 | Audited all read-only listing feeds and removed the remaining live refresh from `/api/v2/pending-listings` | Container tests now assert that browse feeds make no transaction, name, or chain calls; production feed TTFB is 0.53 seconds |
| 2026-07-25 | Added durable block checkpoints, reorganization rollback/replay, restart-resumption coverage, and node-unavailable coverage | 13 container tests and a clean Alembic migration pass; production worker initialized its checkpoint at height 339,667 with zero lag |
| 2026-07-25 | Added live owner-coin validation to listing proof uploads and audited all transaction-sensitive paths | 19 container tests cover listing, buying, sale, cancellation, transfer-finalization, bounded browse queries, and indexer-driven listing removal; final web deployment healthy at zero index lag |
| 2026-07-25 | Opened draft pull request 1 against `main` | Deployed changes are reviewable; PR remains draft until the observation and final cost gates complete |
| 2026-07-25 | Added GitHub Actions regression and migration verification | PR run `30142314984` passed the container build, 19 tests, and clean database migration |
