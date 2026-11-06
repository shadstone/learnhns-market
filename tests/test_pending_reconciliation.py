from contextlib import ExitStack
from datetime import datetime, timedelta
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from app.models import db, PendingListing
from app.pending_status import pending_status, reconcile_pending_listings


class PendingReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
                               'UPLOAD_FOLDER': folder, 'HSD_HTTP_URL': None})
        self.stack.enter_context(self.app.app_context())
        db.create_all()
        self.addCleanup(db.session.remove)
        self.row = PendingListing(name='example', network='main', transfer_tx_hash='a'*64,
                                  transfer_output_idx=2, status='pending-submitted')
        db.session.add(self.row); db.session.commit()
        self.info = {'info': {'owner': {'hash': 'a'*64, 'index': 2}, 'transfer': 100, 'stats': {}}}
        self.coin = {'hash': 'a'*64, 'index': 2, 'height': 100,
                     'covenant': {'type': 9, 'items': [hashlib.sha3_256(b'example').hexdigest()]}}
        self.chain = {'height': 200, 'tip': 'f'*64, 'network': 'main'}
        self.verify = self.stack.enter_context(patch('app.blueprints.api._verified_chain', return_value=(self.chain, None)))
        self.name = self.stack.enter_context(patch('app.blueprints.api._fetch_hsd_name_info', return_value=(self.info, None)))
        self.coin_mock = self.stack.enter_context(patch('app.blueprints.api._fetch_hsd_coin', return_value=(self.coin, None)))
        self.unchanged = self.stack.enter_context(patch('app.blueprints.api._chain_unchanged', return_value=None))
        self.block = self.stack.enter_context(patch('app.blueprints.api._hsd_request'))
        self.stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected HTTP')))

    def change_owner(self, covenant_type=10):
        self.info['info']['owner'] = {'hash': 'b'*64, 'index': 0}
        self.info['info']['transfer'] = 0
        self.coin.update(hash='b'*64, index=0, height=150)
        self.coin['covenant']['type'] = covenant_type
        self.block.return_value = ({'height': 150, 'txs': [{'hash': 'b'*64,
            'inputs': [{'prevout': {'hash': 'a'*64, 'index': 2}}]}]}, None)

    def test_transfer_progress_and_finalize_proof_step_without_tx_index(self):
        self.assertEqual(pending_status(self.row)['blocksUntilFinalize'], 188)
        self.chain['height'] = 388
        self.assertEqual(pending_status(self.row)['status'], 'ready-to-finalize')
        self.change_owner()
        self.assertEqual(pending_status(self.row)['status'], 'awaiting-proof')
        reconcile_pending_listings()
        payload = self.app.test_client().get('/api/v2/pending-listings').json['pending'][0]
        self.assertEqual(payload['status'], 'awaiting-proof')
        self.assertIn('proof', payload['nextAction'])
        self.assertNotIn('finalize', payload['nextAction'])

    def test_cancelled_or_later_owner_retires_pending_on_every_browse_surface(self):
        self.change_owner()
        self.block.return_value[0]['txs'][0]['inputs'][0]['prevout']['hash'] = 'c'*64
        result = reconcile_pending_listings()
        self.assertEqual(result['changed'], 1)
        self.assertEqual(db.session.get(PendingListing, self.row.id).status, 'superseded')
        client = self.app.test_client()
        self.assertEqual(client.get('/api/v2/pending-listings').json['total'], 0)
        self.assertEqual(client.get('/api/v2/auctions?availability=pending').json['total'], 0)
        self.assertNotIn(b'example', client.get('/pending').data)
        detail = client.get('/listing/example').data
        self.assertIn(b'Previous pending record retired', detail)
        self.assertNotIn(b'Pending Shakedex Listing', detail)

    def test_transfer_to_new_owner_retires_without_claiming_cancelled(self):
        self.change_owner(9)
        self.assertEqual(pending_status(self.row)['status'], 'superseded')
        self.block.assert_not_called()

    def test_failures_and_mempool_or_wrong_name_coins_do_not_retire(self):
        self.change_owner()
        for failure in ['offline', 'unconfirmed', 'wrong-name', 'reorg', 'missing-tx']:
            with self.subTest(failure=failure):
                self.coin['height'] = -1 if failure == 'unconfirmed' else 150
                self.coin['covenant']['items'][0] = '0'*64 if failure == 'wrong-name' else hashlib.sha3_256(b'example').hexdigest()
                self.unchanged.return_value = ('chain changed', 503) if failure == 'reorg' else None
                self.block.return_value = (None, ('unavailable', 503)) if failure == 'offline' else ({'height': 150, 'txs': []}, None)
                payload = pending_status(self.row)
                self.assertFalse(payload['verified'])
                self.assertEqual(payload['status'], 'pending-submitted')

    def test_reorg_restores_current_transfer_after_retirement(self):
        self.row.status = 'superseded'; db.session.commit()
        reconcile_pending_listings()
        self.assertEqual(db.session.get(PendingListing, self.row.id).status, 'transfer-lockup')

    def test_upload_race_cannot_overwrite_active_pending_record(self):
        original = pending_status
        def uploaded(row, **kwargs):
            payload = original(row, **kwargs)
            PendingListing.query.filter_by(id=row.id).update({'status': 'active'}, synchronize_session=False)
            db.session.commit()
            return payload
        with patch('app.pending_status.pending_status', side_effect=uploaded):
            reconcile_pending_listings()
        self.assertEqual(db.session.get(PendingListing, self.row.id).status, 'active')

    def test_sweep_is_bounded_and_expired_records_retire(self):
        self.row.expires_at = datetime.utcnow() - timedelta(days=1); db.session.commit()
        self.assertEqual(reconcile_pending_listings(limit=0)['checked'], 0)
        reconcile_pending_listings(limit=1)
        self.assertEqual(db.session.get(PendingListing, self.row.id).status, 'expired')
        self.verify.assert_not_called()
