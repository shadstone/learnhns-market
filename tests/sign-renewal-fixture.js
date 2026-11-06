'use strict';
// Test key only. Production wallets keep their lock key inside the wallet.
const crypto = require('crypto');
const secp256k1 = require('bcrypto/lib/secp256k1');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const message = ['LearnHNS Market listing renewal v1', input.network, input.name,
  input.lockingTxHash, String(input.lockingOutputIdx),
  String(input.previousExpiresAt), String(input.expiresAt)].join('\n');
const digest = crypto.createHash('sha256').update(message, 'ascii').digest();
const key = Buffer.alloc(32); key[31] = input.wrongKey ? 2 : 1;
console.log(JSON.stringify({message, digest: digest.toString('hex'),
  signature: secp256k1.sign(digest, key).toString('hex')}));
