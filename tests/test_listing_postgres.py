"""Opt-in concurrency regression against a disposable PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from io import BytesIO
import json
import os
import tempfile
from threading import Event, Lock
import unittest
from unittest.mock import patch
from sqlalchemy.engine import make_url

from app import create_app
from app.blueprints.api import _lock_listing_name
from app.models import db, Listing
from test_listing_security import fixture
from test_listing_renewal import signed_renewal
from app.blueprints.api import _expiry_timestamp


@unittest.skipUnless(os.environ.get('TEST_POSTGRES_URL'), 'Disposable PostgreSQL URL not configured')
class PostgreSQLListingLockTests(unittest.TestCase):
    def test_concurrent_upload_and_renewal_serialize_and_pin_once(self):
        url = os.environ['TEST_POSTGRES_URL']
        # Never allow this destructive test to point to a real application DB.
        self.assertEqual(make_url(url).database, 'market_security_test')
        with ExitStack() as stack:
            folder = stack.enter_context(tempfile.TemporaryDirectory())
            app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': url,
                              'UPLOAD_FOLDER': folder, 'ALLOWED_PROOF_NETWORKS': ['main'],
                              'MARKETPLACE_FEE_RATE': 0, 'HSD_HTTP_URL': None})
            with app.app_context():
                db.create_all()
                self.assertEqual(Listing.query.count(), 0)
            signed = fixture()
            proof, coin = signed['proof'], signed['coin']
            chain = {'network':'main','chain':{'height':500,'tip':'f'*64,'progress':1}}
            name = {'info':{'owner':{'hash':proof['lockingTxHash'],'index':0}}}
            stack.enter_context(patch('app.blueprints.api.get_hsd_status_payload',return_value=(chain,200)))
            stack.enter_context(patch('app.blueprints.api._fetch_hsd_coin',return_value=(coin,None)))
            stack.enter_context(patch('app.blueprints.api._fetch_hsd_name_info',return_value=(name,None)))
            stack.enter_context(patch('app.blueprints.api.send_gfavip_webhook'))
            pin_started, second_at_lock, release_pin = Event(), Event(), Event()
            count_lock = Lock()
            count = 0
            def enter_lock(name):
                nonlocal count
                with count_lock:
                    count += 1
                    if count == 2:
                        second_at_lock.set()
                _lock_listing_name(name)
            stack.enter_context(patch('app.blueprints.api._lock_listing_name', side_effect=enter_lock))
            def pin(path):
                pin_started.set()
                if not release_pin.wait(10):
                    raise RuntimeError('Test synchronization timed out')
                return 'concurrency-fixture-cid'
            pin_mock = stack.enter_context(patch('app.blueprints.api.pin_to_ipfs',side_effect=pin))
            def upload():
                with app.test_client() as client:
                    response=client.post('/api/upload-proof',data={
                        'proof':(BytesIO(json.dumps(proof).encode()),'proof.json')})
                    return response.status_code, response.json
            with ThreadPoolExecutor(max_workers=2) as executor:
                first=executor.submit(upload)
                try:
                    self.assertTrue(pin_started.wait(10))
                    second=executor.submit(upload)
                    self.assertTrue(second_at_lock.wait(10))
                    self.assertFalse(second.done())
                finally:
                    release_pin.set()
                results=[first.result(timeout=15),second.result(timeout=15)]
            self.assertEqual(sorted(item[0] for item in results),[200,201],results)
            self.assertEqual(pin_mock.call_count,1)
            with app.app_context():
                self.assertEqual(Listing.query.filter_by(status='active').count(),1)
                self.assertEqual(Listing.query.count(),1)

            with app.app_context():
                row = Listing.query.one()
                renewal = signed_renewal(proof, previous=_expiry_timestamp(row))
            pin_started.clear(); second_at_lock.clear(); release_pin.clear()
            count = 0
            pin_mock.reset_mock()
            def renew():
                with app.test_client() as client:
                    response = client.post('/api/v2/listings/security-fixture/renew', json=renewal)
                    return response.status_code, response.json
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(renew)
                try:
                    self.assertTrue(pin_started.wait(10))
                    second = executor.submit(renew)
                    self.assertTrue(second_at_lock.wait(10))
                    self.assertFalse(second.done())
                finally:
                    release_pin.set()
                results = [first.result(timeout=15), second.result(timeout=15)]
            self.assertEqual([item[0] for item in results], [200, 200], results)
            self.assertEqual(sorted(item[1]['renewed'] for item in results), [False, True])
            self.assertEqual(pin_mock.call_count, 1)
            with app.app_context():
                self.assertEqual(Listing.query.count(), 1)
                self.assertEqual(_expiry_timestamp(Listing.query.one()), renewal['expiresAt'])
