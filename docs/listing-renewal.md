# Owner-authorized listing expiry renewal (v1)

Status: proposed API in the renewal follow-up; check the PR deployment status
before using it on the public market. This extends the marketplace display
expiry, not Handshake name ownership or the validity of an on-chain signature.

`POST /api/v2/listings/<name>/renew` accepts a JSON object:

```json
{
  "network": "main",
  "lockingTxHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "lockingOutputIdx": 0,
  "previousExpiresAt": 1800000000,
  "expiresAt": 1800086400,
  "signature": "<128 lowercase hex characters>"
}
```

The lock outpoint and compressed public key come from the current listing proof.
Sign with that lock's private key, which stays inside the wallet. This does not
require an on-chain transaction or disclose the private key to the market.

## Exact signing format

Build the following ASCII text with LF separators and **no trailing newline**:

```text
LearnHNS Market listing renewal v1
main
security-fixture
aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
0
1800000000
1800086400
```

Fields, in order: literal domain/version line, network, lowercase name without
trailing slash, lowercase transaction hash, output index, previous expiry,
requested expiry. Integers are unsigned base-10 without padding. Expiries are
Unix seconds in UTC. `testnet` is spelled `testnet`, not `test`.

Hash those exact bytes with SHA-256. Sign the 32-byte digest with **secp256k1
ECDSA**, compact 64-byte `r || s`, low-S normalization. Send the signature as
128 lowercase hex characters. Do not use DER, recovery bytes, an appended
transaction sighash byte, a wallet-specific message prefix, or Schnorr.

The implementation uses `bcrypto`'s `secp256k1.sign(digest, privateKey)` and
`secp256k1.verify(digest, signature, publicKey)`. Shakedex purchase signatures
are separate and stay unchanged. `tests/sign-renewal-fixture.js` is a runnable
interoperability fixture with an explicitly public test key; never use that key
for a real listing. A language-independent test vector (message, digest, public
key and compact signature) is in `tests/fixtures/listing-renewal-v1.json`.

## Preconditions and responses

- The latest listing must be active or expired only on the market board. A
  superseded, archived, sold or cancelled listing cannot be revived here.
- The name must still belong to the same confirmed, unspent Shakedex FINALIZE
  coin on a synced node of the requested/allowed network. A lapsed name cannot
  be renewed by this endpoint. Reconcile a reorg with refresh-status first.
- Requested expiry must be greater than the stored expiry, in the future and
  no more than 365 days from server time. A listing with no expiry does not
  need extension and is not eligible for this protocol.
- `previousExpiresAt` must match the stored expiry. A stale request returns 409
  with the current expiry; use the returned effective expiry/current proof and sign a new request. A repeat of
  an already-applied, valid request returns success with `replayed: true` and
  does not repin. An older request cannot undo a later renewal.
- Success is HTTP 200 with `success`, `name`, effective `expiresAt`, `renewed`,
  `replayed`, and `cid`. Price, payment address, purchase signature, description
  and account association are preserved. The updated JSON is pinned first;
  the downloadable proof, database expiry and new CID are then updated together.
- Bad fields/signatures return 400, conflicting state 409, spent/missing coins
  can return 404, and unavailable verification/storage returns 503. Failed
  storage or a changed owner during storage leaves the existing row unchanged.
  A successful pin followed by a chain/DB failure may leave an unreferenced pin;
  it is not authority to change the listing. No automatic unpin is performed.

Unsigned uploads remain backward-compatible for ordinary identical retries.
When a requested expiry was ignored, the success response now includes
`expiryChangeIgnored: true`, `renewalRequired: true`, the effective `expiresAt`,
and `renewalEndpoint`. A signed price reduction also reports the retained
expiry. A board-expired same-lock upload returns 409 instead of bypassing renewal
authorization. Wallets must use this signed API to extend an existing listing.

This protocol needs a Namehold client change. Sending the old unsigned
`expiresAt` alone will not renew a listing. Interoperability agreement with the
wallet author is still needed; server tests are not a desktop compatibility
certification.
