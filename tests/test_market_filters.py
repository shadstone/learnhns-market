from contextlib import ExitStack
from datetime import datetime, timedelta
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from app.models import db, Listing, PendingListing
from app.market_filters import filter_auctions, estimated_price


class MarketFilterTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
                              'UPLOAD_FOLDER': folder})
        self.stack.enter_context(self.app.app_context())
        db.create_all(); self.addCleanup(db.session.remove)
        self.stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Browse must not call HSD')))
        # More than two Bob pages, deliberately inserted in the opposite order.
        for n in range(45, 0, -1):
            self.seed(f'name{n:02}', n)
        self.seed('alpha', 80); self.seed('a-b', 0); self.seed('123', 90)
        self.seed('xn--ip8h', 100)  # A single Unicode code point; prefix hyphens are not name hyphens.
        db.session.add(PendingListing(name='pending', network='main', transfer_tx_hash='a'*64,
                                     status='pending-submitted', expected_price=None))
        db.session.add(PendingListing(name='retired', network='main', transfer_tx_hash='b'*64,
                                     status='superseded'))
        db.session.commit()
        self.client = self.app.test_client()

    def seed(self, name, price):
        db.session.add(Listing(name=name, price_hns=Decimal(price), status='active',
                               seller_hns_address='fixture', ipfs_cid='fixture',
                               created_at=datetime(2026, 1, 1)+timedelta(minutes=price),
                               expires_at=datetime.utcnow()+timedelta(days=1),
                               proof_json={'name':name, 'lockingTxHash':'a'*64, 'lockingOutputIdx':0,
                                           'publicKey':'fixture', 'paymentAddr':'fixture',
                                           'data':[{'price':price*1000000, 'lockTime':0}]}))

    def get(self, query):
        response = self.client.get('/api/v2/auctions?'+query)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_global_sort_and_filter_precede_page_boundaries(self):
        pages = [self.get(f'q=name&sort=name-desc&per_page=20&page={page}') for page in (1,2,3)]
        self.assertTrue(all(page['total'] == 45 for page in pages))
        self.assertEqual([row['name'] for page in pages for row in page['auctions']],
                         [f'name{n:02}' for n in range(45,0,-1)])
        self.assertEqual(pages[0]['filterVersion'], 1)
        self.assertEqual(self.get('q=name01')['total'],1)

    def test_character_numbers_hyphens_compose_and_use_decoded_names(self):
        data=self.get('min_length=3&max_length=3&numbers=exclude&hyphens=include')
        self.assertEqual([row['name'] for row in data['auctions']],['a-b'])
        self.assertEqual(self.get('numbers=only')['auctions'][0]['name'],'123')
        self.assertEqual(self.get('max_length=1&hyphens=exclude&numbers=exclude')['auctions'][0]['name'],'xn--ip8h')
        self.assertEqual(self.get('numbers=include&hyphens=exclude&q=name')['total'],45)
        self.assertEqual(self.get('q=doesnotexist')['total'],0)

    def test_price_zero_unknown_and_newest_order(self):
        data=self.get('sort=price-asc&per_page=100')
        self.assertEqual(data['auctions'][0]['name'],'a-b')
        self.assertEqual(data['auctions'][-1]['name'],'pending')
        self.assertEqual(self.get('sort=price-desc')['auctions'][0]['name'],'xn--ip8h')
        self.assertEqual(self.get('availability=available&sort=newest')['auctions'][0]['name'],'xn--ip8h')
        self.assertNotIn('retired', [row['name'] for row in data['auctions']])

    def test_invalid_filters_are_errors_not_partial_results(self):
        for query in ['min_length=0','max_length=64','min_length=4&max_length=2','min_length=1.5',
                      'numbers=bad','hyphens=only','mode=bad','sort=bad']:
            self.assertEqual(self.client.get('/api/v2/auctions?'+query).status_code,400,query)

    def test_reverse_price_uses_eligible_step_not_future_lowest(self):
        row={'id':1,'name':'reverse','bids':[{'price':90,'lockTime':1024},{'price':10,'lockTime':4096}]}
        self.assertEqual(estimated_price(row,2048),90)
        self.assertIsNone(estimated_price(row,1024))
        self.assertEqual(estimated_price(row,4097),10)
        fixed={'id':2,'name':'fixed','bids':[{'price':50,'lockTime':0}]}
        self.assertEqual(filter_auctions([row,fixed],{'sort':'price-asc'},now=2048),[fixed,row])
        self.assertEqual(filter_auctions([row,fixed],{'mode':'reverse'}),[row])

    def test_length_ties_are_stable_and_pending_availability_is_separate(self):
        rows=self.get('sort=length-asc&per_page=100')['auctions']
        self.assertEqual(rows[0]['name'],'xn--ip8h')
        self.assertEqual([row['name'] for row in rows[1:3]],['123','a-b'])
        self.assertEqual(self.get('availability=pending')['total'],1)
        self.assertEqual(self.get('availability=available')['total'],49)
