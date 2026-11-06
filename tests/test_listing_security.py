"""Actual hsd signatures, isolated SQLite and mocked chain/storage boundaries."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from app.models import db, Listing, PendingListing
from app.proof_verifier import verify_proof_signatures

ROOT = Path(__file__).resolve().parent.parent


def fixture(network='main', price=2000000):
    result = subprocess.run(['node', str(ROOT / 'tests/shakedex-fixture.js'), network, str(price)],
                            env={**os.environ, 'NODE_BACKEND': 'js'}, check=True,
                            capture_output=True, text=True)
    return json.loads(result.stdout)


class ListingSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = {price: fixture(price=price) for price in (1000000, 2000000, 3000000)}
        cls.regtest_fixture = fixture('regtest')

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
                               'UPLOAD_FOLDER': self.folder, 'RATELIMIT_ENABLED': True,
                               'ALLOWED_PROOF_NETWORKS': ['main'], 'HSD_HTTP_URL': None})
        self.stack.enter_context(self.app.app_context())
        db.create_all()
        self.addCleanup(db.session.remove)
        self.client = self.app.test_client()
        self.proof = deepcopy(self.fixtures[2000000]['proof'])
        self.coin = deepcopy(self.fixtures[2000000]['coin'])
        self.owner = {'hash': self.proof['lockingTxHash'], 'index': 0}
        self.info = {'info': {'owner': self.owner, 'height': 10, 'transfer': 0,
                              'state': 'CLOSED', 'stats': {'blocksUntilExpire': 10000}}}
        self.chain = {'network': 'main', 'chain': {'height': 500, 'tip': 'f'*64,
                                                 'progress': 1, 'options': {'spv': False}}}
        self.hsd = self.stack.enter_context(patch('app.blueprints.api.get_hsd_status_payload', return_value=(self.chain,200)))
        self.fetch_coin = self.stack.enter_context(patch('app.blueprints.api._fetch_hsd_coin', return_value=(self.coin,None)))
        self.fetch_name = self.stack.enter_context(patch('app.blueprints.api._fetch_hsd_name_info', return_value=(self.info,None)))
        self.pin = self.stack.enter_context(patch('app.blueprints.api.pin_to_ipfs', return_value='fixture-cid'))
        self.stack.enter_context(patch('app.blueprints.api.send_gfavip_webhook'))
        # Any accidentally unmocked HTTP attempt is a test failure.
        self.stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected HTTP')))

    def seed(self, status='active'):
        listing = Listing(name=self.proof['name'], price_hns=Decimal('2'),
                          seller_hns_address=self.proof['paymentAddr'], ipfs_cid='existing',
                          proof_json=deepcopy(self.proof), expires_at=datetime.utcnow()+timedelta(days=1),
                          status=status, description='Original seller text')
        if status == 'sold':
            listing.sale_tx_hash = 'b'*64
            listing.transfer_start_tx_hash = 'b'*64
            listing.sold_at = datetime.utcnow()
        if status == 'cancelled':
            listing.cancel_tx_hash = 'b'*64
            listing.cancelled_at = datetime.utcnow()
        db.session.add(listing); db.session.commit()
        return listing

    def upload(self, proof=None, **fields):
        return self.client.post('/api/upload-proof', data={
            'proof': (BytesIO(json.dumps(proof or self.proof).encode()), '../../untrusted.json'), **fields})

    def refresh(self, body=None):
        return self.client.post('/api/v2/listings/security-fixture/refresh-status', json=body or {})

    def pending_chain(self):
        self.info['info']['transfer'] = 100
        self.coin['covenant']['type'] = 9

    def pending(self, **fields):
        return self.client.post('/api/v2/pending-listings', json={
            'name': self.proof['name'], 'network': 'main', 'transferTxHash': self.owner['hash'],
            'transferOutputIdx': 0, **fields})

    def spend(self, sold=True, tx_hash='c'*64):
        self.fetch_coin.return_value = (None, ('spent', 404, None))
        self.info['info']['owner'] = {'hash':tx_hash, 'index':0}
        tx = {'hash':tx_hash, 'height':400, 'block':'d'*64,
              'inputs':[{'prevout':self.owner}],
              'outputs':[{'value':1000000, 'address':self.coin['address'],
                          'covenant':{'type':9,'items':[hashlib.sha3_256(b'security-fixture').hexdigest()]}},
                         {'value':2000000 if sold else 1, 'address':self.proof['paymentAddr'], 'covenant':{'type':0}}]}
        tx_mock = self.stack.enter_context(patch('app.blueprints.api._fetch_hsd_tx', return_value=(tx,None)))
        block_mock = self.stack.enter_context(patch('app.blueprints.api._hsd_rpc_request', return_value=('d'*64,None)))
        return tx, tx_mock, block_mock

    def test_postgres_url_selects_installed_driver_without_connecting(self):
        for scheme in ('postgres', 'postgresql'):
            app=create_app({'TESTING':True,'UPLOAD_FOLDER':self.folder,
                            'SQLALCHEMY_DATABASE_URI':scheme+'://test@127.0.0.1/market_security_test'})
            self.assertTrue(app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgresql+psycopg2://'))

    def test_real_signed_proof_is_accepted_and_filename_is_not_used(self):
        response = self.upload()
        self.assertEqual(response.status_code,201,response.json)
        self.assertEqual(Listing.query.count(),1)
        self.assertEqual(os.listdir(self.folder),[])
        self.assertTrue(verify_proof_signatures(self.proof,self.coin,'main')[0])

    def test_invalid_signature_cannot_archive_listing_or_pin(self):
        listing = self.seed()
        self.proof['data'][0]['signature'] = '00'*65
        response = self.upload()
        self.assertEqual(response.status_code,400,response.json)
        self.assertEqual(db.session.get(Listing,listing.id).status,'active')
        self.assertEqual(Listing.query.count(),1)
        self.pin.assert_not_called()

    def test_tampered_price_address_key_locktime_hash_type_and_coin_fail(self):
        for field in ('price','address','key','locktime','hash_type','coin_address','coin_name','coin_type','coin_index','coin_value'):
            with self.subTest(field=field):
                proof, coin = deepcopy(self.proof), deepcopy(self.coin)
                if field=='price': proof['data'][0]['price']+=1
                if field=='address': proof['paymentAddr']=coin['address']
                if field=='key': proof['publicKey']='03'+proof['publicKey'][2:]
                if field=='locktime': proof['data'][0]['lockTime']=1024
                if field=='hash_type': proof['data'][0]['signature']=proof['data'][0]['signature'][:-2]+'01'
                if field=='coin_address': coin['address']=proof['paymentAddr']
                if field=='coin_name': coin['covenant']['items'][2]='616263'
                if field=='coin_type': coin['covenant']['type']=9
                if field=='coin_index': coin['index']=1
                if field=='coin_value': coin['value']+=1
                self.assertFalse(verify_proof_signatures(proof,coin,'main')[0])

    def test_verifier_timeout_missing_binary_and_crash_fail_closed(self):
        for error in (FileNotFoundError(),subprocess.TimeoutExpired('node',5),subprocess.CalledProcessError(1,'node')):
            with patch('app.proof_verifier.subprocess.run',side_effect=error):
                self.assertEqual(self.upload().status_code,503)
        self.pin.assert_not_called()
        self.assertEqual(Listing.query.count(),0)

    def test_wrong_network_is_rejected(self):
        self.assertFalse(verify_proof_signatures(self.regtest_fixture['proof'],self.regtest_fixture['coin'],'main')[0])
        self.chain['network']='regtest'
        self.assertEqual(self.upload().status_code,503)
        self.pin.assert_not_called()

    def test_unsigned_fee_mutation_cannot_replace_a_valid_proof(self):
        row=self.seed()
        altered=deepcopy(self.proof)
        altered['feeAddr']=self.proof['paymentAddr']
        altered['data'][0]['fee']=999999
        # Demonstrate why signature verification alone is insufficient for fees.
        self.assertTrue(verify_proof_signatures(altered,self.coin,'main')[0])
        self.assertEqual(self.upload(altered).status_code,400)
        self.assertEqual(row.status,'active')
        self.pin.assert_not_called()

    def test_configured_fee_policy_and_actual_signature_are_both_required(self):
        self.app.config.update(MARKETPLACE_FEE_RATE=100,MARKETPLACE_FEE_ADDRESS=self.proof['paymentAddr'])
        self.proof['feeAddr']=self.proof['paymentAddr']
        self.proof['data'][0]['fee']=20000
        self.assertEqual(self.upload().status_code,201)

    def test_owner_mismatch_rejected(self):
        self.info['info']['owner']={'hash':'b'*64,'index':0}
        self.assertEqual(self.upload().status_code,409)
        self.pin.assert_not_called()

    def test_signed_lower_price_replaces_and_preserves_unsigned_metadata(self):
        original=self.seed()
        lower=deepcopy(self.fixtures[1000000]['proof'])
        lower['expiresAt']=int(datetime.now().timestamp()) + 60
        response=self.upload(lower,description='Forged text',gfavip_user_id='forged')
        self.assertEqual(response.status_code,201,response.json)
        self.assertEqual(original.status,'archived')
        active=Listing.query.filter_by(status='active').one()
        self.assertEqual(active.description,'Original seller text')
        self.assertEqual(active.expires_at,original.expires_at)
        self.assertIsNone(active.gfavip_user_id)
        self.assertEqual(active.proof_json['expiresAt'],original.proof_json['expiresAt'])

    def test_replay_is_noop_and_cannot_raise_price(self):
        original=self.seed()
        response=self.upload(description='Forged')
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['replayed'])
        self.assertEqual(self.upload(self.fixtures[3000000]['proof']).status_code,409)
        self.assertEqual(Listing.query.count(),1)
        self.assertEqual(original.status,'active')
        self.pin.assert_not_called()

    def test_chain_move_and_unsynced_node_reject_upload(self):
        moved=deepcopy(self.chain); moved['chain']['tip']='e'*64
        self.hsd.side_effect=[(self.chain,200),(moved,200)]
        self.assertEqual(self.upload().status_code,503)
        self.hsd.side_effect=None; self.chain['chain']['progress']=0.99
        self.assertEqual(self.upload().status_code,503)
        self.pin.assert_not_called()

    def test_pending_wrong_transfer_rejected_without_row(self):
        self.pending_chain()
        self.assertEqual(self.pending(transferTxHash='b'*64).status_code,409)
        self.assertEqual(self.pending(transferOutputIdx=1).status_code,409)
        self.assertEqual(PendingListing.query.count(),0)

    def test_pending_requires_confirmed_transfer_and_correct_network(self):
        self.assertEqual(self.pending().status_code,409)
        self.pending_chain(); self.coin['height']=-1
        self.assertEqual(self.pending().status_code,409)
        self.coin['height']=100
        self.assertEqual(self.pending(network='regtest').status_code,409)

    def test_pending_accepts_only_chain_facts_and_replay_is_immutable(self):
        self.pending_chain()
        response=self.pending(expectedPrice=1,sellerNote='spoofed',lockScriptAddr='spoofed',listingMode='spoofed')
        self.assertEqual(response.status_code,201,response.json)
        row=PendingListing.query.one()
        self.assertIsNone(row.expected_price)
        self.assertFalse(row.seller_note)
        self.assertIsNone(row.lock_script_addr)
        created=row.created_at; row.status='cancelled'; row.expected_price=222; db.session.commit()
        response=self.pending(expectedPrice=333)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(row.status,'cancelled')
        self.assertEqual(row.expected_price,222)
        self.assertEqual(row.created_at,created)

    def test_retired_pending_can_be_replaced_only_by_verified_new_transfer(self):
        self.pending_chain()
        self.assertEqual(self.pending().status_code, 201)
        row = PendingListing.query.one()
        row.status = 'superseded'; db.session.commit()
        # An arbitrary caller-supplied hash still cannot replace the record.
        self.assertEqual(self.pending(transferTxHash='b'*64).status_code, 409)
        self.owner['hash'] = 'b'*64
        self.coin['hash'] = 'b'*64
        response = self.pending(transferTxHash='b'*64, expectedPrice=999)
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(PendingListing.query.count(), 1)
        self.assertEqual(row.transfer_tx_hash, 'b'*64)
        self.assertEqual(row.status, 'pending-submitted')
        self.assertIsNone(row.expected_price)

    def test_pending_different_existing_transfer_is_not_overwritten(self):
        self.pending_chain()
        row=PendingListing(name=self.proof['name'],network='main',transfer_tx_hash='b'*64,transfer_output_idx=0)
        db.session.add(row); db.session.commit()
        self.assertEqual(self.pending().status_code,409)
        self.assertEqual(row.transfer_tx_hash,'b'*64)

    def test_pending_node_failure_or_chain_move_does_not_write(self):
        self.pending_chain()
        self.fetch_name.return_value=(None,('offline',503,None))
        self.assertEqual(self.pending().status_code,503)
        self.assertEqual(PendingListing.query.count(),0)

    def test_expiry_null_info_and_elapsed_stats(self):
        row=self.seed()
        self.fetch_name.return_value=({'info':None},None)
        response=self.refresh({'outcome':'expired'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(row.status,'expired')
        row.status='active';db.session.commit()
        self.info['info']['stats']['blocksUntilExpire']=0
        self.fetch_name.return_value=(self.info,None)
        self.assertEqual(self.refresh({'outcome':'expired'}).status_code,200)
        self.assertEqual(row.status,'expired')

    def test_expiry_failure_missing_info_and_live_name_preserve_active(self):
        row=self.seed()
        for result in [(None,('offline',503,None)),({},None)]:
            self.fetch_name.return_value=result
            self.assertEqual(self.refresh({'outcome':'expired'}).status_code,503)
            self.assertEqual(row.status,'active')
        self.fetch_name.return_value=(self.info,None)
        self.assertEqual(self.refresh({'outcome':'expired'}).status_code,409)
        self.assertEqual(row.status,'active')

    def test_reorg_reopens_sold_or_cancelled_only_with_valid_current_lock(self):
        row=self.seed('sold')
        response=self.refresh()
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['reopened'])
        self.assertIsNone(row.sale_tx_hash)
        self.assertIsNone(row.sold_at)
        row.status='cancelled';row.cancel_tx_hash='b'*64;row.cancelled_at=datetime.utcnow();db.session.commit()
        self.assertTrue(self.refresh().json['reopened'])
        self.assertIsNone(row.cancel_tx_hash)
        self.assertIsNone(row.cancelled_at)

    def test_reorg_does_not_reopen_invalid_expired_or_superseded_proofs(self):
        row=self.seed('sold')
        bad=deepcopy(row.proof_json);bad['data'][0]['signature']='00'*65;row.proof_json=bad;db.session.commit()
        self.assertEqual(self.refresh().status_code,400)
        self.assertEqual(row.status,'sold')
        row.proof_json=self.proof;row.expires_at=datetime.utcnow()-timedelta(days=1);db.session.commit()
        self.assertEqual(self.refresh().json['status'],'expired')
        row.status='archived';db.session.commit()
        self.assertEqual(self.refresh().status_code,404)
        self.assertEqual(row.status,'archived')

    def test_correct_winning_transaction_and_outcome_ignore_client_label(self):
        row=self.seed('cancelled')
        self.spend(sold=True)
        response=self.refresh({'cancelTxHash':'c'*64,'outcome':'cancelled'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(row.status,'sold')
        self.assertEqual(row.sale_tx_hash,'c'*64)
        self.assertIsNone(row.cancel_tx_hash)

    def test_unindexed_node_uses_current_transfer_height_and_canonical_block(self):
        row=self.seed('cancelled')
        tx,tx_mock,_=self.spend(sold=True)
        tx_mock.return_value=(None,('not indexed',404,None))
        self.info['info']['transfer']=400
        with patch('app.blueprints.api._hsd_request',return_value=(
                {'height':400,'hash':'d'*64,'txs':[tx]},None)) as block:
            response=self.refresh({'saleTxHash':'c'*64})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(row.status,'sold')
        block.assert_called_once_with('/block/400')

    def test_unindexed_node_does_not_trust_orphaned_block(self):
        row=self.seed('cancelled')
        tx,tx_mock,canonical=self.spend(sold=True)
        tx_mock.return_value=(None,('not indexed',404,None))
        self.info['info']['transfer']=400
        canonical.return_value=('e'*64,None)
        with patch('app.blueprints.api._hsd_request',return_value=(
                {'height':400,'hash':'d'*64,'txs':[tx]},None)):
            self.assertEqual(self.refresh({'saleTxHash':'c'*64}).status_code,409)
        self.assertEqual(row.status,'cancelled')

    def test_correct_sale_to_cancellation(self):
        row=self.seed('sold')
        self.spend(sold=False)
        response=self.refresh({'saleTxHash':'c'*64,'outcome':'sold'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(row.status,'cancelled')
        self.assertIsNone(row.sale_tx_hash)
        self.assertEqual(row.cancel_tx_hash,'c'*64)

    def test_orphan_mempool_wrong_spend_and_missing_index_cannot_change_record(self):
        row=self.seed('sold')
        tx,tx_mock,block_mock=self.spend()
        block_mock.return_value=('e'*64,None)
        self.assertEqual(self.refresh({'saleTxHash':'c'*64}).status_code,409)
        block_mock.return_value=('d'*64,None);tx['height']=-1
        self.assertEqual(self.refresh({'saleTxHash':'c'*64}).status_code,409)
        tx['height']=400;tx['inputs'][0]['prevout']={'hash':'e'*64,'index':0}
        self.assertEqual(self.refresh({'saleTxHash':'c'*64}).status_code,409)
        tx_mock.return_value=(None,('not indexed',404,None))
        self.assertEqual(self.refresh({'saleTxHash':'c'*64}).status_code,503)
        self.assertEqual(row.status,'sold')
        self.assertEqual(row.sale_tx_hash,'b'*64)

    def test_reorg_chain_move_preserves_terminal_record(self):
        row=self.seed('cancelled')
        moved=deepcopy(self.chain);moved['chain']['tip']='e'*64
        self.hsd.side_effect=[(self.chain,200),(moved,200)]
        self.assertEqual(self.refresh().status_code,503)
        self.assertEqual(row.status,'cancelled')

    def test_unknown_outcome_rejected(self):
        self.seed()
        self.assertEqual(self.refresh({'outcome':'invented'}).status_code,400)
