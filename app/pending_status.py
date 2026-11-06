"""Reconcile pending transfers with current chain ownership, off the browse path."""
import hashlib
import time
from datetime import datetime

from app.models import PendingListing, db

PENDING_TERMINAL_STATUSES = {'active', 'cancelled', 'expired', 'failed', 'superseded'}
REASONS = {
    'pending-submitted': 'Waiting for verification of the recorded transfer.',
    'transfer-lockup': 'Waiting for the Handshake transfer lockup.',
    'ready-to-finalize': 'Ready for the seller to finalize the listing lock.',
    'awaiting-proof': 'Listing transfer finalized; waiting for the sale proof upload.',
    'superseded': 'The recorded transfer is no longer the current name owner. This pending record has been retired.',
    'expired': 'Pending listing expired.',
}


def status_reason(status):
    return REASONS.get(status, 'This listing is no longer awaiting transfer finalization.')


def pending_status(pending, block_cache=None):
    # Import lazily: the API also uses this module's cached status labels.
    from app.blueprints import api

    def result(status, verified=False, reason=None, **extra):
        return dict(status=status, pendingReason=reason or status_reason(status),
                    blocksUntilFinalize=None, chainHeight=None, transferHeight=None,
                    verified=verified, **extra)

    if pending.is_expired():
        return result('expired', True)
    if pending.status in PENDING_TERMINAL_STATUSES - {'superseded'}:
        return result(pending.status)
    unknown = result(pending.status, reason='Chain status could not be verified; the last known status is retained.')
    snapshot, error = api._verified_chain(pending.network)
    if error:
        return unknown
    data, error = api._fetch_hsd_name_info(pending.name)
    info = (data or {}).get('info') or {}
    owner = info.get('owner') or {}
    if error or not isinstance(owner.get('hash'), str) or type(owner.get('index')) is not int:
        return unknown
    coin, error = api._fetch_hsd_coin(owner['hash'], owner['index'])
    covenant = (coin or {}).get('covenant') or {}
    name_hash = hashlib.sha3_256(pending.name.encode('ascii')).hexdigest()
    if (error or not coin or type(coin.get('height')) is not int or coin['height'] < 0
            or coin['height'] > snapshot['height']
            or coin.get('hash') != owner['hash'] or coin.get('index') != owner['index']
            or (covenant.get('items') or [None])[0] != name_hash):
        return unknown
    same_owner = (owner['hash'] == pending.transfer_tx_hash
                  and pending.transfer_output_idx in (None, owner['index']))
    status = 'superseded'
    remaining = None
    transfer_height = None
    if same_owner:
        transfer_height = info.get('transfer')
        if covenant.get('type') != 9 or type(transfer_height) is not int or transfer_height <= 0:
            return unknown
        remaining = (info.get('stats') or {}).get('blocksUntilValidFinalize')
        if type(remaining) is not int:
            remaining = max(api.SHAKEDEX_TRANSFER_LOCKUP - (snapshot['height'] - transfer_height), 0)
        remaining = max(remaining, 0)
        status = 'ready-to-finalize' if remaining == 0 else 'transfer-lockup'
    elif covenant.get('type') == 10:
        # Without indexTX, fetch only the canonical block containing the current
        # FINALIZE. Directly spending the original transfer means awaiting proof;
        # a later cancellation/retransfer must not revive that original pending row.
        cache_key = (snapshot['tip'], coin['height'])
        if block_cache is not None and cache_key in block_cache:
            block, block_error = block_cache[cache_key]
        else:
            block, block_error = api._hsd_request(f"/block/{coin['height']}")
            if block_cache is not None and not block_error:
                block_cache[cache_key] = (block, block_error)
        if not block_error and isinstance(block, dict) and block.get('height') == coin['height']:
            tx = next((tx for tx in block.get('txs', [])
                       if isinstance(tx, dict) and tx.get('hash') == owner['hash']), None)
            if not tx:
                return unknown
            if any((item.get('prevout') or {}).get('hash') == pending.transfer_tx_hash
                          and pending.transfer_output_idx in (None, (item.get('prevout') or {}).get('index'))
                          for item in tx.get('inputs', []) if isinstance(item, dict)):
                status = 'awaiting-proof'
        else:
            # Don't retire a possibly just-finalized listing on a transient RPC
            # failure. A successful block lookup is needed to disambiguate it.
            return unknown
    if api._chain_unchanged(snapshot):
        return unknown
    payload = result(status, True)
    payload.update(blocksUntilFinalize=remaining, chainHeight=snapshot['height'], transferHeight=transfer_height)
    return payload


def reconcile_pending_listings(limit=50, max_seconds=20, network='main'):
    """Fair, bounded sweep; superseded rows are rechecked for reorg recovery.

    Compare-and-update protects simultaneous proof uploads and pending replacement.
    Reads keep using the database snapshot and do not fan out to HSD.
    """
    rows = (PendingListing.query.filter_by(network=network)
            .filter(~PendingListing.status.in_(PENDING_TERMINAL_STATUSES - {'superseded'}))
            .order_by(PendingListing.updated_at.asc(), PendingListing.id.asc()).limit(limit).all())
    counts = {'checked': 0, 'changed': 0, 'unverified': 0}
    started = time.monotonic()
    block_cache = {}
    for row in rows:
        if time.monotonic() - started >= max_seconds:
            break
        old_status, old_hash = row.status, row.transfer_tx_hash
        payload = pending_status(row, block_cache=block_cache)
        status = payload['status'] if payload['verified'] else old_status
        updated = PendingListing.query.filter_by(id=row.id, status=old_status,
                                                 transfer_tx_hash=old_hash).update(
            {'status': status, 'updated_at': datetime.utcnow()}, synchronize_session=False)
        db.session.commit()
        counts['checked'] += 1
        counts['changed'] += int(bool(updated) and status != old_status)
        counts['unverified'] += int(not payload['verified'])
    return counts
