'use strict';
// Test-only deterministic key. Never used outside synthetic fixtures.
const secp256k1 = require('bcrypto/lib/secp256k1');
const Coin = require('hsd/lib/primitives/coin');
const Address = require('hsd/lib/primitives/address');
const rules = require('hsd/lib/covenants/rules');
const {lockScript, proofMTX, HASH_TYPE, verify} = require('../scripts/verify-shakedex-proof');
const privateKey = Buffer.alloc(32); privateKey[31] = 1;
const publicKey = secp256k1.publicKeyCreate(privateKey, true);
const network = process.argv[2] || 'main';
const coin = new Coin();
coin.version = 1; coin.height = 100; coin.value = 1000000;
coin.hash = Buffer.alloc(32, 0xaa); coin.index = 0;
coin.address = Address.fromScript(lockScript(publicKey));
coin.covenant.setFinalize(rules.hashName('security-fixture'), 10,
  Buffer.from('security-fixture'), 0, 0, 0, Buffer.alloc(32));
const proof = {version: 2, name: 'security-fixture', lockingTxHash: coin.hash.toString('hex'),
  lockingOutputIdx: 0, publicKey: publicKey.toString('hex'),
  paymentAddr: Address.fromHash(Buffer.alloc(20, 1), 0).toString(network),
  expiresAt: Math.floor(Date.now()/1000)+86400,
  data: [{price: Number(process.argv[3] || 2000000), lockTime: 0, signature: null}]};
const coinJSON = coin.getJSON(network);
const {mtx, script} = proofMTX(proof, coinJSON, network);
proof.data[0].signature = mtx.signature(0, script, coin.value, privateKey, HASH_TYPE).toString('hex');
verify(proof, coinJSON, network);
console.log(JSON.stringify({proof,coin:coinJSON}));
