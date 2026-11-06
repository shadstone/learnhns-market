'use strict';
// Offline secp256k1 ECDSA verification of a domain-separated renewal digest.
const secp256k1 = require('bcrypto/lib/secp256k1');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
  input += chunk;
  if (input.length > 2048) process.exit(1);
});
process.stdin.on('end', () => {
  let valid = false;
  try {
    const {digest, signature, publicKey} = JSON.parse(input);
    if (/^[0-9a-f]{64}$/.test(digest) && /^[0-9a-f]{128}$/.test(signature)
        && /^(02|03)[0-9a-f]{64}$/.test(publicKey)) {
      const sig = Buffer.from(signature, 'hex');
      valid = secp256k1.isLowS(sig) && secp256k1.verify(
        Buffer.from(digest, 'hex'), sig, Buffer.from(publicKey, 'hex'));
    }
  } catch (_) { /* Malformed input is never authorization. */ }
  process.stdout.write(JSON.stringify({valid}));
});
