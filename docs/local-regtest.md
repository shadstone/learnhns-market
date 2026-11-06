# Local wallet compatibility sandbox

The public staging website is not a persistent regtest service. On November 6,
2026 its status endpoint reported mainnet. Use this isolated local setup for
fake-name and fake-fund tests. It never connects to a mainnet node.

Requirements: Docker with Compose, and your own test Pinata credentials for
successful proof uploads/renewals. The current production storage implementation
still requires Pinata; starting the containers does not remove that dependency.
No storage-provider migration is included in the renewal change.

```sh
cp .env.regtest.example .env.regtest
# Fill in your test Pinata key/secret in .env.regtest (ignored by Git/Docker).
docker compose --env-file .env.regtest -f compose.regtest.yaml up --build -d
```

Market: http://127.0.0.1:18000. Node RPC: http://127.0.0.1:14037.
Wallet RPC: http://127.0.0.1:14039. The local-only node and wallet API key is
`local-regtest-only`. All published ports bind to loopback. SQLite and chain
state use dedicated named Docker volumes. No webhook is configured.

Generate the first block so the node reports current chain time and progress 1:

```sh
docker compose -f compose.regtest.yaml exec hsd-regtest node -e '
const C = require("hsd/lib/client/node");
const c = new C({port: 14037, apiKey: process.env.HSD_API_KEY});
c.execute("generatetoaddress", [1, "rs1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqn6kda"])
  .then(() => c.getInfo()).then(x => console.log(x.network, x.chain))
  .catch(e => { console.error(e.message); process.exitCode = 1; });'
curl --fail http://127.0.0.1:18000/api/v2/hsd/status
```

Wait for both services to start if the first request fails. Confirm `regtest`
and `progress: 1` before creating/funding test wallets. Connect the wallet to
this node and select the local market base URL. Namehold v0.6.1 releases restrict
market writes to mainnet, so its developer must use a test build/adapter for
regtest; do not change the market's allowed network to work around that guard.

Exercise pending publication before/after confirmation, final proof upload,
download/buyer verification, purchase/cancellation and reorg recovery. For the
new expiry API, follow [listing-renewal.md](listing-renewal.md). Check the returned
`expiresAt`, then download the proof again to confirm it changed.

A storage failure must leave the previous listing and CID unchanged. Pinata 403
can mean a test account has hit its plan limit even if authentication succeeds.
The automated suite mocks storage failures; successful manual uploads need real
working test credentials.

Stop with `docker compose -f compose.regtest.yaml down`. State remains in the
named volumes. Add `--volumes` only when deliberately discarding this sandbox's
fake wallets, names and marketplace database.
