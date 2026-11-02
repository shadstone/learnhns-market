# Listing integrity API changes

Status: patch under local validation; not deployed. Based on production commit
`3d117361166c806e587752f56b44b9ab13abefc3`.

## Proof uploads

Uploads now require a synchronized full HSD node on the allowed network, the
current owner outpoint, and a valid Shakedex signature. The offline verifier
uses pinned `hsd@8.0.0` primitives and the standard Shakedex lock script. It checks
FINALIZE/name binding, script address, coin value, network address checksums,
locktime, seller payment and exact ANYONECANPAY|SINGLEREVERSE signature mode.
It has no wallet or network access and receives no node credentials. Missing
Node/dependencies, subprocess failures and timeouts fail closed with HTTP 503.

SINGLEREVERSE does not sign the separate fee output. Uploads therefore enforce
MARKETPLACE_FEE_RATE (basis points, floored) and MARKETPLACE_FEE_ADDRESS. This
can reject old proofs made under a different fee policy; inspect compatibility
before deploying. It does not change fee settings or stored proofs.

For the same live lock, replay is a no-op and price increases are rejected to
prevent old higher-price proofs from undoing reductions. New lower-price signed
proofs preserve the existing description, account association and expiresAt,
which are not signed by this protocol. Raising a price or changing these fields
requires a new lock until a separate signed metadata protocol is added.

PostgreSQL transaction advisory locks serialize same-name writes. SQLite is
used for isolated tests, not a production concurrency guarantee. Uploads are
bounded to 1 MiB; temporary paths are server-generated and IPFS requests have
a timeout. Invalid proofs never pin or archive an existing listing.

## Pending listings

Unauthenticated callers may publish confirmed, current-owner TRANSFER facts
only. Unconfirmed transfers return 409 and must be retried after confirmation.
The output index can be inferred from getnameinfo when omitted. HSD's current
state is read with `safe=false`; its lagging safe tree cannot establish the
current owner.

Existing rows accept only the same outpoint as an idempotent replay (200), and
cannot have their status, creation time, expected price or notes changed by a
public transaction reference. New rows ignore expectedPrice, sellerNote,
listingMode and lockScriptAddr supplied by callers. The actual price becomes
available with the signed final proof. A different pre-existing TRANSFER row
returns 409; old stale or poisoned rows require deliberate reconciliation, not
an unrestricted upsert.

Wallet impact: Bob/Namehold may attempt pending publication before confirmation.
Those requests must retry later; this change does not broadcast, cancel, or
modify a wallet transaction. Real wallet UX remains a staging acceptance gate.

## Refresh status

Supported outcomes: refresh, sold, cancel/cancelled/canceled, expired. Unknown
outcomes return 400. GET and POST remain supported, with a per-IP rate limit.
Only the latest non-superseded listing may be reconciled; archived/flagged rows
are never revived.

Expiry requires a successful getnameinfo response on a synchronized full node.
Raw hsd returns `info: null` for expired names. Missing/malformed responses and
RPC/HTTP failures are not expiry evidence. An unspent coin from an earlier
name epoch cannot reopen a listing in a new auction cycle.

A returned unspent current-owner lock reopens a sold/cancelled/expired listing
only after verifying its proof and expiry again. All stale sale/cancel references
are cleared. A spent lock requires a confirmed transaction spending that exact
outpoint, a matching canonical block hash, and a TRANSFER for this name. The
seller payment at the SINGLEREVERSE output position determines sale versus
cancellation; the client outcome label is a hint, not authority. A corrected
transaction replaces the previous record; unchanged refreshes retain dates.
Unavailable canonical transaction/block evidence, orphaned transactions, mempool-only evidence and
chain movement during checks leave records unchanged with a retryable error.

Production's HSD currently reports indexTX=false. Returned-lock reconciliation
works without that index. For spent locks, current name transfer height or a
stored event height can locate one block directly on HSD. Its transaction
contents and canonical hash are rechecked; cached event JSON is not trusted.
If no usable height or indexed transaction is available, the API returns 503. No node index, node restart or deployment
setting is changed by this patch. Explorer HTML is not used for this API's
canonical-chain decisions. Existing background indexer behavior is separate.

## Runtime and checks

PostgreSQL URLs explicitly select the installed psycopg2 driver, including
under SQLAlchemy 2.1. The container includes Node 22 and Python 3.12; hsd native install scripts are
not run because the verifier uses NODE_BACKEND=js. Only consensus/script
primitives are loaded. npm audit reports bsock's upstream weak-hash advisory
through hsd's unused networking dependencies; the verifier imports no bsock,
bcurl or bweb and opens no listener. This patch does not run an hsd server in
the web container. Keep that boundary if extending the helper.

Run `npm ci --ignore-scripts`, install requirements.txt under Python 3.12, then
`python -m unittest discover -s tests -v`. Tests generate real deterministic
signatures for synthetic coins, use isolated SQLite databases and mock network
boundaries. They do not prove live regtest/mainnet wallet compatibility.

Before release: build/test the production container, verify PostgreSQL migration
and same-name write serialization, smoke-test Bob/Namehold on isolated regtest,
verify fee-policy compatibility, back up the production database, and deploy
the reviewed commit only. No schema migration is added. Roll back the image if
needed, recognizing the old image reintroduces the overwrite vulnerabilities;
do not bulk-delete listings as a rollback step.

## Validation recorded 2026-11-02

- Local production Docker build succeeded on linux/arm64.
- Container regression suite: 53 passed; PostgreSQL-only test skipped there and
  then run separately against an isolated PostgreSQL 18 database: 1 passed.
- Fresh SQLite and PostgreSQL migrations reached `9d5a7c1e4b20` (unchanged head).
- Concurrent same-lock uploads returned 201/200, pinned once and left one active
  row in PostgreSQL.
- Existing Shakedex SwapProof verified the synthetic fixture; its own signing
  method produced a signature accepted by the new verifier. This was a direct
  local source compatibility check, not a live wallet session.
- Read-only samples from production (`m-ent`, `kkk15`, `3fmc`) all passed the
  offline signature/lock verifier against their public coin responses. Their
  fees matched the advertised zero-basis-point policy. No uploads or status
  writes were made to production. This sample is not a whole-market audit.
- `git diff --check` and JavaScript syntax validation passed.

Release acceptance completed on November 2 before production rollout:

- Native Linux GitHub CI and local linux/amd64 regression/migration/concurrency
  checks passed (54 tests across the suite and PostgreSQL-specific run).
- Production and staging PostgreSQL snapshots restored into isolated databases;
  all 15 table counts matched. The new image accepts the restored production
  database at the unchanged migration head.
- Patched Railway staging passed 15 real hsd regtest checks: confirmed pending
  facts, spoof/replay rejection, signed proof publication and download, valid
  price reduction, tamper preservation, buyer verification/purchase, cancellation,
  reopening after sale/cancel rollbacks, and actual name expiry after mining.
- The run used Bob's Shakedex signer/buyer source and Namehold v0.6.1 API response
  contracts. It was not a full desktop UI test; released Namehold restricts
  market writes to mainnet. Early pending publication still requires a retry
  after confirmation.
- Staging's old Pinata account was blocked by its plan file limit. Acceptance
  used the working current market storage configuration temporarily. The
  production account accepted the diagnostic and two regtest proof pins.
  No old pins were deleted and no storage-provider migration was included.

Deployment IDs, backup checksums, cleanup, rollback instructions and live
verification are recorded in hub-learnhns/temp-specs/
LEARNHNS-MARKET-LISTING-SECURITY-2026-11-02.md. No message to the reporter or
compatible-wallet catalog endorsement is part of this release.
