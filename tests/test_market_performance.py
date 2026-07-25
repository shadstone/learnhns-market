from io import BytesIO
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import create_app
from app.models import (
    Listing,
    MarketplaceBlockCheckpoint,
    MarketplaceCovenantEvent,
    MarketplaceIndexerProgress,
    PendingListing,
    db,
)
from app.blueprints.api import _market_index_health
from app import marketplace_indexer


class MarketIndexHealthTests(unittest.TestCase):
    def test_fresh_caught_up_worker_is_healthy(self):
        now = datetime(2026, 7, 25, 12, 0, 0)
        progress = SimpleNamespace(
            status='watching',
            last_indexed_height=100,
            updated_at=now - timedelta(seconds=30),
        )

        health = _market_index_health(progress, 102, now=now)

        self.assertTrue(health['healthy'])
        self.assertEqual(health['status'], 'healthy')
        self.assertEqual(health['lagBlocks'], 2)
        self.assertEqual(health['heartbeatAgeSeconds'], 30)

    def test_stale_lagging_worker_is_unhealthy(self):
        now = datetime(2026, 7, 25, 12, 0, 0)
        progress = SimpleNamespace(
            status='watching',
            last_indexed_height=80,
            updated_at=now - timedelta(minutes=10),
        )

        health = _market_index_health(progress, 100, now=now)

        self.assertFalse(health['healthy'])
        self.assertEqual(health['status'], 'stale')
        self.assertIn('stale-heartbeat', health['reasons'])
        self.assertIn('block-lag', health['reasons'])

    def test_not_started_status_is_unhealthy(self):
        now = datetime(2026, 7, 25, 12, 0, 0)
        progress = SimpleNamespace(
            status='not-started',
            last_indexed_height=None,
            updated_at=now,
        )

        health = _market_index_health(progress, 100, now=now)

        self.assertFalse(health['healthy'])
        self.assertIn('worker-not-running', health['reasons'])
        self.assertIn('lag-unknown', health['reasons'])

    def test_unreachable_node_is_unhealthy(self):
        now = datetime(2026, 7, 25, 12, 0, 0)
        progress = SimpleNamespace(
            status='watching',
            last_indexed_height=100,
            updated_at=now,
        )

        health = _market_index_health(
            progress,
            None,
            now=now,
            node_reachable=False,
        )

        self.assertFalse(health['healthy'])
        self.assertIn('node-unreachable', health['reasons'])
        self.assertIn('lag-unknown', health['reasons'])


class MarketplaceIndexerTests(unittest.TestCase):
    def test_normalize_name_rejects_control_characters(self):
        self.assertIsNone(marketplace_indexer.normalize_name('bad\x00name'))
        self.assertIsNone(marketplace_indexer.normalize_name('\x00'))
        self.assertEqual(marketplace_indexer.normalize_name('Valid-Name'), 'valid-name')

    def test_caught_up_worker_records_heartbeat_without_rescanning(self):
        now = datetime.utcnow() - timedelta(minutes=10)
        progress = SimpleNamespace(
            status='watching',
            last_indexed_height=100,
            target_height=100,
            finished_at=now,
            updated_at=now,
            last_error='old error',
        )
        fake_db = SimpleNamespace(session=SimpleNamespace(commit=Mock()))

        with (
            patch.object(marketplace_indexer, 'get_chain_height', return_value=100),
            patch.object(marketplace_indexer, 'progress_for', return_value=progress),
            patch.object(
                marketplace_indexer,
                'reconcile_chain_reorganization',
                return_value={'detected': False, 'checkpointInitialized': False},
            ),
            patch.object(marketplace_indexer, 'db', fake_db),
            patch.object(marketplace_indexer, 'get_block_hash') as get_block_hash,
        ):
            result = marketplace_indexer.scan_market_blocks()

        self.assertEqual(result['blocksProcessed'], 0)
        self.assertEqual(result['endHeight'], 100)
        self.assertEqual(progress.status, 'watching')
        self.assertEqual(progress.target_height, 100)
        self.assertIsNone(progress.last_error)
        self.assertGreater(progress.updated_at, now)
        fake_db.session.commit.assert_called_once_with()
        get_block_hash.assert_not_called()


class BrowseSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        database_path = f"{self.tempdir.name}/test.db"
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f"sqlite:///{database_path}",
            'HSD_HTTP_URL': None,
            'UPLOAD_FOLDER': self.tempdir.name,
            'WTF_CSRF_ENABLED': False,
        })
        with self.app.app_context():
            db.create_all()
            db.session.add(Listing(
                name='fastmarket',
                price_hns=Decimal('25.0'),
                description='Performance test listing',
                seller_hns_address='hs1qtest',
                ipfs_cid='test-cid',
                proof_json={
                    'name': 'fastmarket',
                    'lockingTxHash': 'a' * 64,
                    'lockingOutputIdx': 0,
                    'publicKey': 'test-key',
                    'paymentAddr': 'hs1qtest',
                    'data': [],
                    'version': 2,
                },
                status='active',
            ))
            db.session.add(PendingListing(
                name='pendingmarket',
                network='main',
                transfer_tx_hash='b' * 64,
                transfer_output_idx=0,
                status='pending-submitted',
            ))
            db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        self.tempdir.cleanup()

    def test_browse_routes_do_not_reconcile_each_listing(self):
        ready = ({
            'reachable': True,
            'progress': 1,
            'height': 100,
        }, 200)
        with (
            patch('app.blueprints.api._name_transfer_status', side_effect=AssertionError('unexpected live listing check')),
            patch('app.blueprints.api._fetch_hsd_tx', side_effect=AssertionError('unexpected live transaction check')),
            patch('app.blueprints.api._fetch_hsd_name_info', side_effect=AssertionError('unexpected live name check')),
            patch('app.blueprints.api.get_hsd_status_payload', side_effect=AssertionError('unexpected live chain check')),
            patch('app.blueprints.main.get_hsd_status_payload', return_value=ready),
        ):
            for path in (
                '/',
                '/pending',
                '/stats',
                '/api/v2/auctions',
                '/api/v2/pending-listings',
            ):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertIn('public', response.headers['Cache-Control'], path)

        with self.app.app_context():
            listing = Listing.query.filter_by(name='fastmarket').one()
            self.assertEqual(listing.status, 'active')

    def test_authenticated_browse_response_is_not_shared(self):
        self.client.set_cookie('learnhns_session', 'invalid-session')
        with patch('app.blueprints.main.get_hsd_status_payload', return_value=({
            'reachable': True,
            'progress': 1,
            'height': 100,
        }, 200)):
            response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')

    def test_homepage_paginates_and_filters_the_full_snapshot(self):
        with self.app.app_context():
            for index in range(59):
                name = f"market-{index:02d}"
                db.session.add(Listing(
                    name=name,
                    price_hns=Decimal(index + 1),
                    seller_hns_address='hs1qtest',
                    ipfs_cid=f"test-cid-{index}",
                    proof_json={
                        'name': name,
                        'lockingTxHash': f"{index + 1:064x}",
                        'lockingOutputIdx': 0,
                        'publicKey': 'test-key',
                        'paymentAddr': 'hs1qtest',
                        'data': [],
                        'version': 2,
                    },
                    status='active',
                ))
            db.session.commit()

        ready = ({'reachable': True, 'progress': 1, 'height': 100}, 200)
        with patch('app.blueprints.main.get_hsd_status_payload', return_value=ready):
            first_page = self.client.get('/')
            filtered = self.client.get('/?q=market-58')

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.data.count(b'\n         data-market-card'), 48)
        self.assertLess(len(first_page.data), 150_000)
        self.assertIn(b'Page 1 of 2', first_page.data)
        self.assertIn(b'market-58', filtered.data)
        self.assertEqual(filtered.data.count(b'\n         data-market-card'), 1)

    def test_precompiled_assets_are_versioned_and_cacheable(self):
        response = self.client.get('/')
        self.assertNotIn(b'cdn.tailwindcss.com', response.data)
        self.assertIn(b'/static/css/tailwind.css?v=', response.data)

        asset = self.client.get('/static/css/tailwind.css?v=test')
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.headers['Cache-Control'], 'public, max-age=31536000, immutable')
        asset.close()

    def test_coin_action_still_verifies_against_hsd(self):
        coin = {'hash': 'a' * 64, 'index': 0, 'value': 1}
        with patch('app.blueprints.api._fetch_hsd_coin', return_value=(coin, None)) as fetch_coin:
            response = self.client.get('/api/v2/listings/fastmarket/coin')

        self.assertEqual(response.status_code, 200)
        fetch_coin.assert_called_once_with('a' * 64, 0)

    def test_listing_upload_verifies_live_owner_coin(self):
        lock_hash = '8' * 64
        proof = {
            'version': 2,
            'name': 'verifiedlisting',
            'lockingTxHash': lock_hash,
            'lockingOutputIdx': 0,
            'publicKey': '02' + ('9' * 64),
            'paymentAddr': 'hs1q' + ('a' * 35),
            'data': [{
                'price': 1_000_000,
                'lockTime': int(datetime.utcnow().timestamp()),
                'signature': 'a' * 130,
            }],
            'expiresAt': int((datetime.utcnow() + timedelta(days=1)).timestamp()),
        }
        coin = {'hash': lock_hash, 'index': 0, 'value': 1}
        name_info = {
            'info': {
                'owner': {
                    'hash': lock_hash,
                    'index': 0,
                },
            },
        }

        with (
            patch('app.blueprints.api._fetch_hsd_coin', return_value=(coin, None)) as fetch_coin,
            patch('app.blueprints.api._fetch_hsd_name_info', return_value=(name_info, None)) as fetch_name,
            patch('app.blueprints.api.pin_to_ipfs', return_value='verified-cid'),
            patch('app.blueprints.api.send_gfavip_webhook'),
        ):
            response = self.client.post(
                '/api/upload-proof',
                data={
                    'proof': (
                        BytesIO(json.dumps(proof).encode()),
                        'verified-proof.json',
                    ),
                },
                content_type='multipart/form-data',
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        fetch_coin.assert_called_once_with(lock_hash, 0)
        fetch_name.assert_called_once_with('verifiedlisting')

    def test_sale_recording_verifies_spending_transaction(self):
        sale_tx_hash = '9' * 64
        tx = {
            'hash': sale_tx_hash,
            'inputs': [{
                'prevout': {
                    'hash': 'a' * 64,
                    'index': 0,
                },
            }],
        }
        with (
            patch('app.blueprints.api._fetch_hsd_tx', return_value=(tx, None)) as fetch_tx,
            patch('app.blueprints.api._index_marketplace_sale_txs'),
        ):
            response = self.client.post(
                '/api/v2/listings/fastmarket/refresh-status',
                json={'saleTxHash': sale_tx_hash, 'outcome': 'sold'},
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['sold'])
        self.assertGreaterEqual(fetch_tx.call_count, 1)
        with self.app.app_context():
            self.assertEqual(
                Listing.query.filter_by(name='fastmarket').one().status,
                'sold',
            )

    def test_cancellation_recording_verifies_spending_transaction(self):
        cancel_tx_hash = 'b' * 64
        tx = {
            'hash': cancel_tx_hash,
            'inputs': [{
                'prevout': {
                    'hash': 'a' * 64,
                    'index': 0,
                },
            }],
        }
        with patch('app.blueprints.api._fetch_hsd_tx', return_value=(tx, None)) as fetch_tx:
            response = self.client.post(
                '/api/v2/listings/fastmarket/refresh-status',
                json={'cancelTxHash': cancel_tx_hash, 'outcome': 'cancelled'},
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['cancelled'])
        self.assertGreaterEqual(fetch_tx.call_count, 1)
        with self.app.app_context():
            self.assertEqual(
                Listing.query.filter_by(name='fastmarket').one().status,
                'cancelled',
            )

    def test_transfer_status_verifies_live_chain_and_name_state(self):
        with (
            patch(
                'app.blueprints.api.get_hsd_status_payload',
                return_value=({'height': 200, 'reachable': True}, 200),
            ) as fetch_chain,
            patch(
                'app.blueprints.api._fetch_hsd_name_info',
                return_value=({
                    'info': {
                        'owner': {'hash': 'c' * 64, 'index': 0},
                        'transfer': 0,
                        'stats': {},
                        'state': 'CLOSED',
                    },
                }, None),
            ) as fetch_name,
        ):
            response = self.client.get('/api/v2/names/fastmarket/transfer-status')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'finalized')
        fetch_chain.assert_called_once_with()
        fetch_name.assert_called_once_with('fastmarket')

    def test_indexer_restart_resumes_after_stored_checkpoint(self):
        checkpoint_hash = '1' * 64
        next_hash = '2' * 64
        with self.app.app_context():
            db.session.add(MarketplaceIndexerProgress(
                network='main',
                status='watching',
                last_indexed_height=100,
                target_height=100,
                events_indexed=0,
            ))
            db.session.add(MarketplaceBlockCheckpoint(
                network='main',
                block_height=100,
                block_hash=checkpoint_hash,
            ))
            db.session.commit()

            with (
                patch.object(marketplace_indexer, 'get_chain_height', return_value=101),
                patch.object(
                    marketplace_indexer,
                    'get_block_hash',
                    side_effect=lambda height: {
                        100: checkpoint_hash,
                        101: next_hash,
                    }[height],
                ),
                patch.object(marketplace_indexer, 'get_block', return_value={'tx': []}),
            ):
                result = marketplace_indexer.scan_market_blocks()

            progress = MarketplaceIndexerProgress.query.filter_by(network='main').one()
            checkpoint = MarketplaceBlockCheckpoint.query.filter_by(
                network='main',
                block_height=101,
            ).one()
            self.assertEqual(result['startHeight'], 101)
            self.assertEqual(result['blocksProcessed'], 1)
            self.assertEqual(progress.last_indexed_height, 101)
            self.assertEqual(checkpoint.block_hash, next_hash)

    def test_indexer_rewinds_and_replays_a_chain_reorganization(self):
        ancestor_hash = '3' * 64
        orphan_hash = '4' * 64
        replacement_hash = '5' * 64
        next_hash = '6' * 64
        orphan_tx_hash = '7' * 64
        with self.app.app_context():
            listing = Listing.query.filter_by(name='fastmarket').one()
            listing.status = 'sold'
            listing.sold_at = datetime.utcnow()
            listing.sale_tx_hash = orphan_tx_hash
            listing.transfer_start_tx_hash = orphan_tx_hash
            db.session.add(MarketplaceIndexerProgress(
                network='main',
                status='watching',
                last_indexed_height=100,
                target_height=100,
                events_indexed=1,
            ))
            db.session.add_all([
                MarketplaceBlockCheckpoint(
                    network='main',
                    block_height=99,
                    block_hash=ancestor_hash,
                ),
                MarketplaceBlockCheckpoint(
                    network='main',
                    block_height=100,
                    block_hash=orphan_hash,
                ),
                MarketplaceCovenantEvent(
                    network='main',
                    name='fastmarket',
                    covenant_action='TRANSFER',
                    tx_hash=orphan_tx_hash,
                    output_index=0,
                    block_height=100,
                    block_hash=orphan_hash,
                    source='hsd-block',
                ),
            ])
            db.session.commit()

            with (
                patch.object(marketplace_indexer, 'get_chain_height', return_value=101),
                patch.object(
                    marketplace_indexer,
                    'get_block_hash',
                    side_effect=lambda height: {
                        99: ancestor_hash,
                        100: replacement_hash,
                        101: next_hash,
                    }[height],
                ),
                patch.object(marketplace_indexer, 'get_block', return_value={'tx': []}),
            ):
                result = marketplace_indexer.scan_market_blocks()

            db.session.refresh(listing)
            progress = MarketplaceIndexerProgress.query.filter_by(network='main').one()
            self.assertTrue(result['reorg']['detected'])
            self.assertEqual(result['reorg']['ancestorHeight'], 99)
            self.assertEqual(result['reorg']['depth'], 1)
            self.assertEqual(result['reorg']['eventsRemoved'], 1)
            self.assertEqual(result['reorg']['listingsReverted'], 1)
            self.assertEqual(result['blocksProcessed'], 2)
            self.assertEqual(progress.last_indexed_height, 101)
            self.assertEqual(listing.status, 'active')
            self.assertIsNone(listing.sale_tx_hash)
            self.assertEqual(
                MarketplaceCovenantEvent.query.filter_by(tx_hash=orphan_tx_hash).count(),
                0,
            )
            self.assertEqual(
                MarketplaceBlockCheckpoint.query.filter_by(
                    network='main',
                    block_height=100,
                ).one().block_hash,
                replacement_hash,
            )


if __name__ == '__main__':
    unittest.main()
