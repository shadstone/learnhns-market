'use strict';
// Offline verifier. hsd is used only for consensus primitives; no node, wallet,
// network client, or user-controlled module is loaded.
const assert = require('assert');
const Coin = require('hsd/lib/primitives/coin');
const MTX = require('hsd/lib/primitives/mtx');
const Output = require('hsd/lib/primitives/output');
const Address = require('hsd/lib/primitives/address');
const Script = require('hsd/lib/script/script');
const Opcode = require('hsd/lib/script/opcode');
const Witness = require('hsd/lib/script/witness');
const rules = require('hsd/lib/covenants/rules');
const {hashType} = require('hsd/lib/script/common');
const HASH_TYPE = hashType.ANYONECANPAY | hashType.SINGLEREVERSE;

// Shakedex's standard lock script (MIT, kurumiimari/shakedex src/script.js).
function lockScript(publicKey) {
  return new Script([
    Opcode.fromSymbol('type'), Opcode.fromInt(rules.types.TRANSFER),
    Opcode.fromSymbol('equal'), Opcode.fromSymbol('if'),
    Opcode.fromPush(publicKey), Opcode.fromSymbol('checksig'),
    Opcode.fromSymbol('else'), Opcode.fromSymbol('type'),
    Opcode.fromInt(rules.types.FINALIZE), Opcode.fromSymbol('equal'),
    Opcode.fromSymbol('endif')
  ]);
}

function proofMTX(proof, coinJSON, network) {
  assert(['main', 'testnet', 'regtest', 'simnet'].includes(network));
  assert(proof.version === 2 && rules.verifyName(proof.name));
  assert(Array.isArray(proof.data) && proof.data.length === 1);
  const coin = Coin.fromJSON(coinJSON, network);
  assert(coin.hash.toString('hex') === proof.lockingTxHash);
  assert(coin.index === proof.lockingOutputIdx && coin.height >= 0);
  assert(coin.covenant.type === rules.types.FINALIZE);
  assert(coin.covenant.items[0].equals(rules.hashName(proof.name)));
  assert(coin.covenant.items[2].equals(Buffer.from(proof.name, 'ascii')));
  assert(/^(02|03)[0-9a-f]{64}$/i.test(proof.publicKey));
  const script = lockScript(Buffer.from(proof.publicKey, 'hex'));
  assert(coin.address.equals(Address.fromScript(script)));
  const bid = proof.data[0];
  assert(Number.isSafeInteger(bid.price) && bid.price > 0);
  assert(Number.isSafeInteger(bid.lockTime) && bid.lockTime >= 0);
  const fee = bid.fee === undefined ? 0 : bid.fee;
  assert(Number.isSafeInteger(fee) && fee >= 0);
  const mtx = new MTX();
  mtx.addCoin(coin);
  mtx.addOutput(new Output({covenant: {type: rules.types.TRANSFER, items: []}}));
  if (fee > 0)
    mtx.addOutput({address: Address.fromString(proof.feeAddr, network), value: fee});
  mtx.addOutput({address: Address.fromString(proof.paymentAddr, network), value: bid.price});
  mtx.setLocktime(bid.lockTime, true);
  return {mtx, coin, script};
}

function verify(proof, coinJSON, network) {
  const {mtx, coin, script} = proofMTX(proof, coinJSON, network);
  const signature = proof.data[0].signature;
  assert(typeof signature === 'string' && /^[0-9a-f]{130}$/i.test(signature));
  const sig = Buffer.from(signature, 'hex');
  assert(sig[64] === HASH_TYPE);
  mtx.inputs[0].witness = new Witness([sig, script.encode()]);
  mtx.inputs[0].witness.compile();
  // Use standard script verification flags, not signature hash flags.
  mtx.checkInput(0, coin);
  return true;
}

module.exports = {verify, proofMTX, lockScript, HASH_TYPE};
if (require.main === module) {
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', chunk => {
    input += chunk;
    if (input.length > 1024 * 1024) process.exit(1);
  });
  process.stdin.on('end', () => {
    let valid = false;
    try {
      const {proof, coin, network} = JSON.parse(input);
      valid = verify(proof, coin, network);
    } catch (_) { /* Invalid data must never turn into acceptance. */ }
    process.stdout.write(JSON.stringify({valid}));
  });
}
