import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import create_app
from app.models import Listing, db
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
            patch('app.blueprints.main.get_hsd_status_payload', return_value=ready),
        ):
            for path in ('/', '/pending', '/stats', '/api/v2/auctions'):
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


if __name__ == '__main__':
    unittest.main()
