"""Renewal authorization uses real ECDSA signatures; network/storage are isolated."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from app.models import db, Listing
from app.listing_renewal import renewal_message, renewal_digest
from app.blueprints.api import _expiry_timestamp
import test_listing_security as fixtures
ROOT = fixtures.ROOT


def signed_renewal(proof, previous=None, expiry=None, network='main', **extra):
    body = {'name':proof['name'], 'network':network, 'lockingTxHash':proof['lockingTxHash'],
            'lockingOutputIdx':proof['lockingOutputIdx'],
            'previousExpiresAt':previous or proof['expiresAt'],
            'expiresAt':expiry or proof['expiresAt'] + 86400, **extra}
    result = subprocess.run(['node',str(ROOT/'tests/sign-renewal-fixture.js')],
                            input=json.dumps(body),capture_output=True,text=True,check=True,
                            env={**os.environ,'NODE_BACKEND':'js'})
    signed = json.loads(result.stdout)
    return {**body,'signature':signed['signature']}


# Reuse the security fixture/setup without inheriting its test methods.
class ListingRenewalTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ListingSecurityTests.setUpClass.__func__)
    setUp = fixtures.ListingSecurityTests.setUp
    seed = fixtures.ListingSecurityTests.seed
    upload = fixtures.ListingSecurityTests.upload

    def renew(self, body):
        return self.client.post('/api/v2/listings/security-fixture/renew',json=body)

    def test_owner_can_extend_without_new_lock_and_retry_pins_once(self):
        row=self.seed();before=deepcopy(row.proof_json)
        body=signed_renewal(self.proof,previous=_expiry_timestamp(row))
        response=self.renew(body)
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['renewed'])
        self.assertEqual(row.proof_json,{**before,'expiresAt':body['expiresAt']})
        self.assertEqual(row.description,'Original seller text')
        self.assertEqual(row.ipfs_cid,'fixture-cid')
        self.assertEqual(Listing.query.count(),1)
        retry=self.renew(body)
        self.assertEqual(retry.status_code,200,retry.json)
        self.assertTrue(retry.json['replayed'])
        self.assertEqual(self.pin.call_count,1)
        self.assertEqual(os.listdir(self.folder),[])

    def test_wrong_key_and_changed_signed_fields_rejected(self):
        row=self.seed();expiry=row.expires_at
        body=signed_renewal(self.proof,previous=_expiry_timestamp(row))
        for field,value in [('expiresAt',body['expiresAt']+1),
                            ('previousExpiresAt',body['previousExpiresAt']-1),
                            ('network','regtest'),('lockingTxHash','b'*64),
                            ('lockingOutputIdx',1),('signature','00'*64)]:
            with self.subTest(field=field):
                self.assertIn(self.renew({**body,field:value}).status_code,(400,409))
        wrong=signed_renewal(self.proof,previous=_expiry_timestamp(row),wrongKey=True)
        self.assertEqual(self.renew(wrong).status_code,400)
        self.assertEqual(row.expires_at,expiry);self.pin.assert_not_called()

    def test_stale_authorization_cannot_undo_a_later_renewal(self):
        row=self.seed();first=signed_renewal(self.proof,previous=_expiry_timestamp(row))
        self.assertEqual(self.renew(first).status_code,200)
        second=signed_renewal(self.proof,previous=first['expiresAt'],expiry=first['expiresAt']+86400)
        self.assertEqual(self.renew(second).status_code,200)
        self.assertEqual(self.renew(first).status_code,409)
        self.assertEqual(_expiry_timestamp(row),second['expiresAt'])

    def test_expired_board_listing_needs_signature_and_can_be_renewed(self):
        row=self.seed('expired');row.expires_at=datetime.utcnow()-timedelta(days=1)
        row.proof_json={**row.proof_json,'expiresAt':_expiry_timestamp(row)};db.session.commit()
        body=signed_renewal(self.proof,previous=_expiry_timestamp(row))
        self.assertEqual(self.upload().status_code,409)
        self.assertEqual(self.renew(body).status_code,200)
        self.assertEqual(row.status,'active')

    def test_elapsed_active_deadline_cannot_be_bypassed_by_unsigned_upload(self):
        row=self.seed();row.expires_at=datetime.utcnow()-timedelta(seconds=1);db.session.commit()
        self.assertEqual(self.upload().status_code,409);self.pin.assert_not_called()

    def test_sold_cancelled_archived_and_superseded_rows_cannot_renew(self):
        row=self.seed();body=signed_renewal(self.proof,previous=_expiry_timestamp(row))
        for status in ('sold','cancelled','archived'):
            row.status=status;db.session.commit()
            self.assertEqual(self.renew(body).status_code,409)
        row.status='active';db.session.commit()
        newer=self.seed();newer.proof_json={**newer.proof_json,'lockingTxHash':'b'*64};db.session.commit()
        self.assertEqual(self.renew(body).status_code,409);self.pin.assert_not_called()

    def test_spent_owner_changed_chain_expired_and_unavailable_chain_fail_closed(self):
        row=self.seed();body=signed_renewal(self.proof,previous=_expiry_timestamp(row))
        for result,error in [(None,('unavailable',503,None)),(None,('spent',404,None))]:
            self.fetch_coin.return_value=(result,error)
            self.assertIn(self.renew(body).status_code,(404,503))
        self.fetch_coin.return_value=(self.coin,None)
        self.info['info']['owner']={'hash':'b'*64,'index':0}
        self.assertEqual(self.renew(body).status_code,409)
        self.fetch_name.return_value=({'info':None},None)
        self.assertNotEqual(self.renew(body).status_code,200)
        self.pin.assert_not_called()

    def test_storage_failure_and_chain_move_during_pin_preserve_listing(self):
        row=self.seed();old=row.expires_at;body=signed_renewal(self.proof,previous=_expiry_timestamp(row))
        self.pin.side_effect=RuntimeError('test storage failure')
        self.assertEqual(self.renew(body).status_code,503)
        self.assertEqual(row.expires_at,old);self.assertEqual(row.ipfs_cid,'existing')
        def spent_after_pin(path):
            self.fetch_coin.return_value=(None,('spent',404,None));return 'orphaned-test-pin'
        self.pin.side_effect=spent_after_pin
        self.assertEqual(self.renew(body).status_code,404)
        self.assertEqual(row.expires_at,old);self.assertEqual(row.ipfs_cid,'existing')
        self.assertEqual(os.listdir(self.folder),[])

    def test_malformed_lifetime_and_missing_verifier(self):
        row=self.seed();body=signed_renewal(self.proof,previous=_expiry_timestamp(row))
        for change in [{'expiresAt':True},{'previousExpiresAt':None},{'lockingOutputIdx':True},
                       {'network':[]},{'expiresAt':body['previousExpiresAt']},
                       {'expiresAt':int(datetime.now().timestamp())+366*86400}]:
            self.assertEqual(self.renew({**body,**change}).status_code,400,change)
        with patch('app.proof_verifier.subprocess.run',side_effect=FileNotFoundError):
            self.assertEqual(self.renew(body).status_code,503)
        self.pin.assert_not_called()

    def test_replay_response_reports_effective_expiry_and_ignored_request(self):
        row=self.seed();later={**self.proof,'expiresAt':self.proof['expiresAt']+86400}
        response=self.upload(later)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['expiresAt'],_expiry_timestamp(row))
        self.assertTrue(response.json['expiryChangeIgnored'])
        self.assertTrue(response.json['renewalRequired']);self.pin.assert_not_called()

    def test_signing_format_matches_independent_node_crypto(self):
        body=signed_renewal(self.proof,previous=1800000000,expiry=1800086400)
        message=renewal_message('main','security-fixture','a'*64,0,1800000000,1800086400)
        self.assertEqual(message,'LearnHNS Market listing renewal v1\nmain\nsecurity-fixture\n'+'a'*64+'\n0\n1800000000\n1800086400')
        from app.proof_verifier import verify_renewal_signature
        digest=renewal_digest('main','security-fixture','a'*64,0,1800000000,1800086400)
        self.assertTrue(verify_renewal_signature(digest,body['signature'],self.proof['publicKey'])[0])
