from flask import Blueprint, request, jsonify, current_app
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import requests
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from app.models import db, ExpiringNameWatch, GlobalNameState, Listing, NameIndexerProgress, PendingListing
from app.utils import (
    fixed_price_listing_fields,
    validate_shakedex_proof,
    pin_to_ipfs,
    sanitize_html,
    send_gfavip_webhook,
)
from app.watchers import watcher_counts_for_names
import os
import json
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from urllib.parse import urljoin

api_bp = Blueprint('api', __name__)
limiter = Limiter(key_func=get_remote_address, default_limits=["200 per day"])

SHAKEDEX_TRANSFER_LOCKUP = 288
PENDING_TERMINAL_STATUSES = {'active', 'cancelled', 'expired', 'failed'}


@api_bp.route('/v2/watchers/counts', methods=['GET'])
def watcher_counts():
    raw_names = request.args.getlist('names')
    if not raw_names and request.args.get('name'):
        raw_names = request.args.getlist('name')
    names = []
    for value in raw_names:
        names.extend(str(value or '').split(','))
    names = [
        str(name or '').strip().lower().rstrip('/')
        for name in names
        if str(name or '').strip()
    ]
    if len(names) > 100:
        return jsonify({'error': 'At most 100 names can be requested'}), 400
    return jsonify({'counts': watcher_counts_for_names(names)})


def _bob_auction_from_listing(listing):
    proof = listing.proof_json
    bids = proof.get('data', [])
    expires_at = listing.effective_expires_at()
    return {
        "id": listing.id,
        "name": proof["name"],
        "lockingTxHash": proof["lockingTxHash"],
        "lockingOutputIdx": proof["lockingOutputIdx"],
        "publicKey": proof["publicKey"],
        "paymentAddr": proof["paymentAddr"],
        "feeAddr": proof.get("feeAddr"),
        "bids": bids,
        "data": bids,
        "version": proof.get("version", 2),
        "description": listing.description,
        "createdAt": listing.created_at.isoformat() if listing.created_at else None,
        "expiresAt": expires_at.isoformat() if expires_at else None,
        "url": f"/listing/{listing.name}",
    }


def _pending_listing_status(pending):
    if pending.is_expired():
        return {
            "status": "expired",
            "pendingReason": "Pending listing expired",
            "blocksUntilFinalize": None,
            "chainHeight": None,
            "transferHeight": None,
        }

    if pending.status in PENDING_TERMINAL_STATUSES:
        return {
            "status": pending.status,
            "pendingReason": "Pending listing is no longer awaiting finalization",
            "blocksUntilFinalize": None,
            "chainHeight": None,
            "transferHeight": None,
        }

    tx, tx_error = _fetch_hsd_tx(pending.transfer_tx_hash)
    if tx_error:
        status_code = tx_error[1]
        if status_code == 404:
            chain_payload, chain_status = get_hsd_status_payload()
            indexers = {}
            if chain_status == 200:
                indexers = ((chain_payload.get('chain') or {}).get('indexers') or {})
            if indexers.get('indexTX') is False:
                return _pending_status_from_name_info(pending, chain_payload, chain_status)
            return {
                "status": "pending-submitted",
                "pendingReason": "Waiting for transfer transaction to appear",
                "blocksUntilFinalize": None,
                "chainHeight": None,
                "transferHeight": None,
            }
        return {
            "status": pending.status or "pending-submitted",
            "pendingReason": "Could not refresh chain status",
            "blocksUntilFinalize": None,
            "chainHeight": None,
            "transferHeight": None,
        }

    tx_height = tx.get('height') if isinstance(tx, dict) else None
    if not isinstance(tx_height, int) or tx_height < 0:
        return {
            "status": "transfer-unconfirmed",
            "pendingReason": "Waiting for transfer confirmation",
            "blocksUntilFinalize": None,
            "chainHeight": None,
            "transferHeight": tx_height,
        }

    chain_payload, chain_status = get_hsd_status_payload()
    chain_height = chain_payload.get('height') if chain_status == 200 else None
    if not isinstance(chain_height, int):
        return {
            "status": "transfer-lockup",
            "pendingReason": "Transfer confirmed; waiting for chain status",
            "blocksUntilFinalize": None,
            "chainHeight": None,
            "transferHeight": tx_height,
        }

    active_transfer = _pending_name_info_matches_transfer(pending)
    if active_transfer is False:
        return {
            "status": "pending-submitted",
            "pendingReason": "Pending record received; no active transfer lockup is visible for this pending transfer.",
            "blocksUntilFinalize": None,
            "chainHeight": chain_height,
            "transferHeight": None,
        }

    blocks_since_transfer = max(chain_height - tx_height, 0)
    blocks_until_finalize = max((SHAKEDEX_TRANSFER_LOCKUP + 1) - blocks_since_transfer, 0)
    if blocks_until_finalize > 0:
        return {
            "status": "transfer-lockup",
            "pendingReason": "Waiting for Shakedex lock finalization",
            "blocksUntilFinalize": blocks_until_finalize,
            "chainHeight": chain_height,
            "transferHeight": tx_height,
        }

    return {
        "status": "ready-to-finalize",
        "pendingReason": "Ready for seller to finalize the Shakedex lock",
        "blocksUntilFinalize": 0,
        "chainHeight": chain_height,
        "transferHeight": tx_height,
    }


def _pending_name_info_matches_transfer(pending):
    name_info, name_error = _fetch_hsd_name_info(pending.name)
    if name_error:
        return None

    info = name_info.get('info') if isinstance(name_info, dict) else None
    if not isinstance(info, dict):
        return None

    owner = info.get('owner') if isinstance(info.get('owner'), dict) else {}
    owner_hash = str(owner.get('hash') or '').lower()
    if not owner_hash:
        return None
    if owner_hash != pending.transfer_tx_hash:
        return False

    if pending.transfer_output_idx is not None:
        try:
            if int(owner.get('index')) != int(pending.transfer_output_idx):
                return False
        except (TypeError, ValueError):
            return None

    return True


def _pending_status_from_name_info(pending, chain_payload=None, chain_status=None):
    name_info, name_error = _fetch_hsd_name_info(pending.name)
    chain_height = chain_payload.get('height') if chain_status == 200 else None

    if name_error:
        return {
            "status": "pending-submitted",
            "pendingReason": "Pending record received; this channel is not transaction-indexed, and name-state lookup is not available yet.",
            "blocksUntilFinalize": None,
            "chainHeight": chain_height,
            "transferHeight": None,
            "nameState": None,
        }

    info = name_info.get('info') if isinstance(name_info, dict) else None
    if not isinstance(info, dict):
        return {
            "status": "pending-submitted",
            "pendingReason": "Pending record received; no active on-chain name state is visible yet.",
            "blocksUntilFinalize": None,
            "chainHeight": chain_height,
            "transferHeight": None,
            "nameState": None,
        }

    stats = info.get('stats') if isinstance(info.get('stats'), dict) else {}
    transfer_height = info.get('transfer')
    if not isinstance(transfer_height, int) or transfer_height <= 0:
        return {
            "status": "pending-submitted",
            "pendingReason": "Pending record received; no active transfer lockup is visible for this name yet.",
            "blocksUntilFinalize": None,
            "chainHeight": chain_height,
            "transferHeight": None,
            "nameState": info.get('state'),
        }

    owner = info.get('owner') if isinstance(info.get('owner'), dict) else {}
    owner_hash = str(owner.get('hash') or '').lower()
    if owner_hash and owner_hash != pending.transfer_tx_hash:
        return {
            "status": "pending-submitted",
            "pendingReason": "Pending record received; no active transfer lockup is visible for this pending transfer.",
            "blocksUntilFinalize": None,
            "chainHeight": chain_height,
            "transferHeight": None,
            "nameState": info.get('state'),
        }

    if pending.transfer_output_idx is not None:
        try:
            owner_index_matches = int(owner.get('index')) == int(pending.transfer_output_idx)
        except (TypeError, ValueError):
            owner_index_matches = False
        if not owner_index_matches:
            return {
                "status": "pending-submitted",
                "pendingReason": "Pending record received; no active transfer lockup is visible for this pending transfer.",
                "blocksUntilFinalize": None,
                "chainHeight": chain_height,
                "transferHeight": None,
                "nameState": info.get('state'),
            }

    blocks_until_finalize = stats.get('blocksUntilValidFinalize')
    if not isinstance(blocks_until_finalize, int) and isinstance(chain_height, int):
        blocks_since_transfer = max(chain_height - transfer_height, 0)
        blocks_until_finalize = max(SHAKEDEX_TRANSFER_LOCKUP - blocks_since_transfer, 0)

    if isinstance(blocks_until_finalize, int) and blocks_until_finalize <= 0:
        return {
            "status": "ready-to-finalize",
            "pendingReason": "Transfer lockup is complete; seller can finalize the Shakedex lock and upload the proof.",
            "blocksUntilFinalize": 0,
            "chainHeight": chain_height,
            "transferHeight": transfer_height,
            "nameState": info.get('state'),
        }

    return {
        "status": "transfer-lockup",
        "pendingReason": "Transfer detected; waiting for Shakedex lockup before seller finalizes the proof.",
        "blocksUntilFinalize": blocks_until_finalize,
        "chainHeight": chain_height,
        "transferHeight": transfer_height,
        "nameState": info.get('state'),
    }


def _name_transfer_status(name):
    chain_payload, chain_status = get_hsd_status_payload()
    chain_height = chain_payload.get('height') if chain_status == 200 else None
    name_info, name_error = _fetch_hsd_name_info(name)
    if name_error:
        message, status = name_error[:2]
        return {
            "available": False,
            "status": "unknown",
            "message": message,
            "statusCode": status,
            "chainHeight": chain_height,
        }

    info = name_info.get('info') if isinstance(name_info, dict) else None
    if not isinstance(info, dict):
        return {
            "available": False,
            "status": "unknown",
            "message": "Name state is not available from HSD yet.",
            "chainHeight": chain_height,
        }

    owner = info.get('owner') if isinstance(info.get('owner'), dict) else {}
    stats = info.get('stats') if isinstance(info.get('stats'), dict) else {}
    transfer_height = info.get('transfer')
    owner_hash = str(owner.get('hash') or '').lower() or None
    owner_index = owner.get('index')

    payload = {
        "available": True,
        "status": "finalized",
        "message": "No active transfer lockup is visible for this name.",
        "chainHeight": chain_height,
        "transferHeight": transfer_height if isinstance(transfer_height, int) and transfer_height > 0 else None,
        "unlockHeight": None,
        "blocksUntilFinalize": None,
        "ownerTxHash": owner_hash,
        "ownerOutputIndex": owner_index if isinstance(owner_index, int) else None,
        "nameState": info.get('state'),
    }

    if not isinstance(transfer_height, int) or transfer_height <= 0:
        return payload

    unlock_height = transfer_height + SHAKEDEX_TRANSFER_LOCKUP
    blocks_until_finalize = stats.get('blocksUntilValidFinalize')
    if not isinstance(blocks_until_finalize, int) and isinstance(chain_height, int):
        blocks_until_finalize = max(unlock_height - chain_height, 0)

    payload.update({
        "status": "transfer-lockup",
        "message": "Transfer detected; waiting for the 288-block Handshake transfer lockup.",
        "unlockHeight": unlock_height,
        "blocksUntilFinalize": blocks_until_finalize,
    })

    if isinstance(blocks_until_finalize, int) and blocks_until_finalize <= 0:
        payload.update({
            "status": "ready-to-finalize",
            "message": "Transfer lockup is complete; the buyer can now finalize the transfer.",
            "blocksUntilFinalize": 0,
        })

    return payload


def _pending_listing_payload(pending, refresh=True):
    status_info = _pending_listing_status(pending) if refresh else {
        "status": pending.status,
        "pendingReason": None,
        "blocksUntilFinalize": None,
        "chainHeight": None,
        "transferHeight": None,
        "nameState": None,
    }

    return {
        "id": f"pending-{pending.id}",
        "name": pending.name,
        "network": pending.network,
        "status": status_info["status"],
        "buyable": False,
        "pending": True,
        "pendingReason": status_info["pendingReason"],
        "transferTxHash": pending.transfer_tx_hash,
        "transferOutputIdx": pending.transfer_output_idx,
        "lockScriptAddr": pending.lock_script_addr,
        "listingMode": pending.listing_mode,
        "expectedPrice": pending.expected_price,
        "blocksUntilFinalize": status_info["blocksUntilFinalize"],
        "chainHeight": status_info["chainHeight"],
        "transferHeight": status_info["transferHeight"],
        "nameState": status_info.get("nameState"),
        "sellerNote": pending.seller_note,
        "createdAt": pending.created_at.isoformat() if pending.created_at else None,
        "updatedAt": pending.updated_at.isoformat() if pending.updated_at else None,
        "url": f"/listing/{pending.name}",
    }


def _bob_auction_from_pending(pending, refresh=False):
    payload = _pending_listing_payload(pending, refresh=refresh)
    return {
        **payload,
        "bids": [],
        "data": [],
        "version": 2,
        "description": payload["pendingReason"],
        "url": f"/listing/{pending.name}",
    }


def _listing_is_available(listing):
    return listing and listing.status == 'active' and not listing.is_expired()


def _active_listings_unique_by_name():
    """Return the database snapshot used by read-only marketplace routes."""
    listings = (
        Listing.query
        .filter_by(status='active')
        .order_by(Listing.created_at.desc())
        .all()
    )
    unique = []
    seen_names = set()
    for listing in listings:
        if listing.is_expired() or listing.name in seen_names:
            continue
        unique.append(listing)
        seen_names.add(listing.name)
    return unique


def _active_listing_for_name(name):
    return (
        Listing.query
        .filter_by(name=name, status='active')
        .order_by(Listing.created_at.desc())
        .first()
    )


def _refreshable_listing_for_name(name):
    return _refreshable_listings_for_name(name).first()


def _refreshable_listings_for_name(name):
    return (
        Listing.query
        .filter(Listing.name == name, Listing.status.in_(('active', 'sale-pending')))
        .order_by(Listing.created_at.desc())
    )


def _refreshable_listing_for_spend(name, tx_hash):
    fallback_listing = None
    last_payload = None
    last_status = 400
    for listing in _refreshable_listings_for_name(name).all():
        if listing.is_expired():
            continue
        _, payload, status = _verified_listing_spend(tx_hash, listing)
        if status == 200:
            return listing, None, 200
        if fallback_listing is None and _tx_is_finalized_name_owner(tx_hash, listing):
            fallback_listing = listing
        last_payload = payload
        last_status = status

    if fallback_listing:
        return fallback_listing, None, 200

    return None, last_payload or {"error": "Listing not found"}, last_status


def _listing_coin_ref(listing):
    proof = listing.proof_json if isinstance(listing.proof_json, dict) else {}
    tx_hash = proof.get("lockingTxHash")
    output_index = proof.get("lockingOutputIdx")
    if not tx_hash or output_index is None:
        return None, None

    try:
        output_index = int(output_index)
    except (TypeError, ValueError):
        return None, None

    tx_hash = str(tx_hash).strip().lower()
    if len(tx_hash) != 64 or any(c not in '0123456789abcdef' for c in tx_hash):
        return None, None

    return tx_hash, output_index


def _listing_has_unverified_sale_claim(listing):
    proof = listing.proof_json if isinstance(listing.proof_json, dict) else {}
    return bool(proof.get("saleClaimedWithoutTx"))


def _restore_listing_to_active(listing, reason):
    listing.status = 'active'
    listing.sold_at = None
    listing.sale_tx_hash = None
    listing.transfer_start_tx_hash = None
    db.session.commit()
    current_app.logger.info(reason, listing.name)
    return listing


def _listing_lock_is_current_owner(listing, transfer_status=None):
    lock_tx_hash, lock_output_index = _listing_coin_ref(listing)
    if not lock_tx_hash:
        return False

    transfer_status = transfer_status or _name_transfer_status(listing.name)
    if transfer_status.get('status') != 'finalized':
        return False

    owner_tx_hash, hash_error = _validate_hex_hash(
        transfer_status.get('ownerTxHash'),
        'ownerTxHash',
    )
    if hash_error or owner_tx_hash != lock_tx_hash:
        return False

    try:
        owner_output_index = int(transfer_status.get('ownerOutputIndex'))
    except (TypeError, ValueError):
        return False

    return owner_output_index == lock_output_index


def _tx_spends_listing_coin(tx, listing):
    tx_hash, output_index = _listing_coin_ref(listing)
    if not tx_hash:
        return False

    inputs = tx.get('inputs', []) if isinstance(tx, dict) else []

    for tx_input in inputs:
        prevout = tx_input.get('prevout') if isinstance(tx_input, dict) else None
        if not isinstance(prevout, dict):
            continue

        prev_hash = prevout.get('hash')
        prev_index = prevout.get('index')
        try:
            prev_index = int(prev_index)
        except (TypeError, ValueError):
            continue

        if prev_hash == tx_hash and prev_index == int(output_index):
            return True

    return False


def _validate_hex_hash(value, field):
    tx_hash = str(value or '').strip().lower()
    if not tx_hash:
        return None, None
    if len(tx_hash) != 64 or any(c not in '0123456789abcdef' for c in tx_hash):
        return None, (f"{field} must be a 64-character hex string", 400)
    return tx_hash, None


def _verified_listing_spend(tx_hash, listing):
    lock_tx_hash, _lock_output_index = _listing_coin_ref(listing)
    if not lock_tx_hash:
        return None, {
            "error": "Listing does not have a Shakedex locking coin reference",
            "url": f"/listing/{listing.name}",
        }, 400

    tx, error = _fetch_hsd_tx(tx_hash)
    if error:
        message, status = error[:2]
        if status != 404:
            return None, {"error": message}, status

        tx, explorer_error = _fetch_explorer_tx(tx_hash)
        if explorer_error:
            explorer_message, explorer_status = explorer_error[:2]
            return None, {
                "error": message,
                "fallbackError": explorer_message,
            }, explorer_status

    if not _tx_spends_listing_coin(tx, listing):
        return None, {
            "error": "Transaction does not spend this listing's Shakedex locking coin",
            "url": f"/listing/{listing.name}",
        }, 400

    return tx, None, 200


def _tx_is_finalized_name_owner(tx_hash, listing):
    transfer_status = _name_transfer_status(listing.name)
    if transfer_status.get('status') != 'finalized':
        return False

    owner_tx_hash, hash_error = _validate_hex_hash(
        transfer_status.get('ownerTxHash'),
        'ownerTxHash',
    )
    return not hash_error and owner_tx_hash == tx_hash


def _tx_is_current_name_owner_transfer(name, tx_hash):
    transfer_status = _name_transfer_status(name)
    if transfer_status.get('status') not in {'transfer-lockup', 'ready-to-finalize', 'finalized'}:
        return False, transfer_status

    owner_tx_hash, hash_error = _validate_hex_hash(
        transfer_status.get('ownerTxHash'),
        'ownerTxHash',
    )
    if hash_error or owner_tx_hash != tx_hash:
        return False, transfer_status

    return True, transfer_status


def _candidate_listing_sale_tx_hashes(listing):
    tx_hash, _output_index = _listing_coin_ref(listing)
    if not tx_hash:
        return []

    candidates = []
    seen = set()

    def add_candidate(value):
        tx_hash, hash_error = _validate_hex_hash(value, 'txHash')
        if hash_error or not tx_hash or tx_hash in seen:
            return
        seen.add(tx_hash)
        candidates.append(tx_hash)

    transfer_status = _name_transfer_status(listing.name)
    if transfer_status.get('status') in {'transfer-lockup', 'ready-to-finalize'}:
        add_candidate(transfer_status.get('ownerTxHash'))

    try:
        from app.marketplace_indexer import events_for_name

        for event in events_for_name(listing.name):
            if event.covenant_action == 'TRANSFER':
                add_candidate(event.tx_hash)
    except Exception as exc:
        current_app.logger.warning(
            "Could not read marketplace transfer events for %s: %s",
            listing.name,
            exc,
        )

    return candidates


def _find_verified_listing_sale_tx_hash(listing):
    for tx_hash in _candidate_listing_sale_tx_hashes(listing):
        _tx, _payload, status = _verified_listing_spend(tx_hash, listing)
        if status == 200:
            return tx_hash
    return None


def _index_marketplace_sale_txs(listing):
    try:
        from app.marketplace_indexer import index_tx_hash

        for tx_hash_value in (listing.transfer_start_tx_hash, listing.sale_tx_hash):
            if isinstance(tx_hash_value, str) and len(tx_hash_value) == 64:
                index_tx_hash(tx_hash_value)
    except Exception as exc:
        current_app.logger.warning(
            "Could not index marketplace sale txs for %s: %s",
            getattr(listing, 'name', 'unknown'),
            exc,
        )


def _mark_listing_sold_if_spent(listing, sale_tx_hash=None, transfer_start_tx_hash=None):
    tx_hash, output_index = _listing_coin_ref(listing)
    if not tx_hash:
        return False, {
            "error": "Listing does not have a Shakedex locking coin reference",
            "url": f"/listing/{listing.name}",
        }, 400

    if sale_tx_hash:
        tx, payload, status = _verified_listing_spend(sale_tx_hash, listing)
        verification_source = tx.get("source") if isinstance(tx, dict) else None
        if status != 200:
            return False, payload, status

        listing.status = 'sold'
        listing.sold_at = datetime.utcnow()
        listing.sale_tx_hash = sale_tx_hash
        if transfer_start_tx_hash:
            listing.transfer_start_tx_hash = transfer_start_tx_hash
        elif not listing.transfer_start_tx_hash and verification_source != "name-owner":
            listing.transfer_start_tx_hash = sale_tx_hash
        db.session.commit()
        _index_marketplace_sale_txs(listing)
        return True, {
            "sold": True,
            "status": listing.status,
            "soldAt": listing.sold_at.isoformat(),
            "saleTxHash": listing.sale_tx_hash,
            "transferStartTxHash": listing.transfer_start_tx_hash,
            "verificationSource": verification_source,
            "url": f"/listing/{listing.name}",
        }, 200

    coin, error = _fetch_hsd_coin(tx_hash, output_index)

    if error:
        message, status = error[:2]
        if status != 404:
            return False, {"error": message}, status

        verified_sale_tx_hash = _find_verified_listing_sale_tx_hash(listing)
        if verified_sale_tx_hash:
            return _mark_listing_sold_if_spent(
                listing,
                verified_sale_tx_hash,
                verified_sale_tx_hash,
            )
        return False, {
            "sold": False,
            "status": listing.status,
            "message": "The Shakedex locking coin is not currently available, but no verified buyer transfer was found. The listing status was left unchanged.",
            "url": f"/listing/{listing.name}",
        }, 200

    return False, {
        "sold": False,
        "status": listing.status,
        "coin": coin,
        "url": f"/listing/{listing.name}",
    }, 200


def _resolve_sale_pending_listing(listing):
    if not listing or listing.status != 'sale-pending' or listing.sale_tx_hash:
        return listing

    transfer_status = _name_transfer_status(listing.name)
    if _listing_lock_is_current_owner(listing, transfer_status=transfer_status):
        if _listing_has_unverified_sale_claim(listing):
            current_app.logger.info(
                "Keeping listing %s sale-pending: seller reported a sale but no verified transfer tx is visible yet.",
                listing.name,
            )
            return listing

        return _restore_listing_to_active(
            listing,
            "Restored listing %s to active: name owner is still the Shakedex lock output.",
        )

    verified_sale_tx_hash = _find_verified_listing_sale_tx_hash(listing)
    if verified_sale_tx_hash:
        _mark_listing_sold_if_spent(
            listing,
            verified_sale_tx_hash,
            verified_sale_tx_hash,
        )
        return listing

    if (
        not _listing_has_unverified_sale_claim(listing)
        and transfer_status.get('status') in {'transfer-lockup', 'ready-to-finalize'}
    ):
        return _restore_listing_to_active(
            listing,
            "Restored listing %s to active: active transfer state does not spend the Shakedex lock output.",
        )

    if transfer_status.get('status') != 'finalized':
        return listing

    owner_tx_hash, hash_error = _validate_hex_hash(
        transfer_status.get('ownerTxHash'),
        'ownerTxHash',
    )
    if hash_error:
        return listing

    existing_sale = Listing.query.filter_by(
        name=listing.name,
        sale_tx_hash=owner_tx_hash,
    ).first()
    if existing_sale:
        return listing

    current_app.logger.info(
        "Sale-pending listing %s has finalized owner tx %s but no verified Shakedex sale tx.",
        listing.name,
        owner_tx_hash,
    )

    return listing


def _repair_sold_listing_sale_tx_hash(listing):
    if (
        not listing
        or listing.status not in {'sold', 'completed'}
    ):
        return listing

    if listing.sale_tx_hash:
        _tx, _payload, status = _verified_listing_spend(listing.sale_tx_hash, listing)
        if status == 200:
            return listing

    verified_sale_tx_hash = _find_verified_listing_sale_tx_hash(listing)
    if not verified_sale_tx_hash or verified_sale_tx_hash == listing.sale_tx_hash:
        return listing

    listing.sale_tx_hash = verified_sale_tx_hash
    listing.transfer_start_tx_hash = verified_sale_tx_hash
    if isinstance(listing.proof_json, dict):
        proof_json = dict(listing.proof_json)
        proof_json["transferStartTxHash"] = verified_sale_tx_hash
        listing.proof_json = proof_json
    db.session.commit()
    _index_marketplace_sale_txs(listing)
    return listing


def _mark_listing_cancelled_if_spent(listing, cancel_tx_hash):
    if not cancel_tx_hash:
        return False, {"error": "cancelTxHash is required to record a cancelled listing"}, 400

    _, payload, status = _verified_listing_spend(cancel_tx_hash, listing)
    if status != 200:
        return False, payload, status

    listing.status = 'cancelled'
    listing.cancelled_at = datetime.utcnow()
    listing.cancel_tx_hash = cancel_tx_hash
    db.session.commit()
    return True, {
        "cancelled": True,
        "status": listing.status,
        "cancelledAt": listing.cancelled_at.isoformat(),
        "cancelTxHash": listing.cancel_tx_hash,
        "url": f"/listing/{listing.name}",
    }, 200


def _hsd_api_style():
    style = current_app.config.get('HSD_API_STYLE', 'raw')
    return style.lower()


def _hsd_endpoint(endpoint):
    if _hsd_api_style() != 'firehsd':
        return endpoint

    normalized = endpoint.strip('/')
    if not normalized:
        return '/api/v1/status'
    return f"/api/v1/{normalized}"


def _hsd_request(endpoint, method='GET', payload=None):
    hsd_url = current_app.config.get('HSD_HTTP_URL')
    api_key = current_app.config.get('HSD_API_KEY')

    if not hsd_url:
        return None, ("HSD HTTP URL is not configured", 503, None)

    mapped_endpoint = _hsd_endpoint(endpoint)
    url = urljoin(hsd_url.rstrip('/') + '/', mapped_endpoint.lstrip('/'))
    auth = ('x', api_key) if api_key and _hsd_api_style() != 'firehsd' else None

    try:
        response = requests.request(
            method,
            url,
            auth=auth,
            json=payload,
            timeout=current_app.config.get('HSD_HTTP_TIMEOUT', 5),
        )
    except requests.RequestException as exc:
        current_app.logger.warning("Failed HSD request %s %s: %s", method, mapped_endpoint, exc)
        return None, ("Could not reach HSD node", 503, str(exc))

    if response.status_code == 404:
        return None, ("HSD resource not found", 404, response.text[:500])
    if response.status_code == 401:
        return None, ("HSD API key is invalid", 503, response.text[:500])
    if response.status_code >= 400:
        current_app.logger.warning(
            "HSD request failed for %s %s with status %s: %s",
            method,
            mapped_endpoint,
            response.status_code,
            response.text[:500],
        )
        return None, ("HSD request failed", 503, response.text[:500])

    try:
        return response.json(), None
    except ValueError:
        return None, ("HSD returned invalid JSON", 503, response.text[:500])


def _hsd_rpc_request(method, params=None):
    if _hsd_api_style() == 'firehsd':
        return None, ("HSD RPC is not available for Fire HSD endpoints", 503, None)

    hsd_url = current_app.config.get('HSD_HTTP_URL')
    api_key = current_app.config.get('HSD_API_KEY')

    if not hsd_url:
        return None, ("HSD HTTP URL is not configured", 503, None)

    url = hsd_url.rstrip('/') + '/'
    auth = ('x', api_key) if api_key else None
    body = {
        "jsonrpc": "2.0",
        "id": "learnhns-market",
        "method": method,
        "params": params or [],
    }

    try:
        response = requests.post(
            url,
            auth=auth,
            json=body,
            timeout=current_app.config.get('HSD_HTTP_TIMEOUT', 5),
        )
    except requests.RequestException as exc:
        current_app.logger.warning("Failed HSD RPC %s: %s", method, exc)
        return None, ("Could not reach HSD node", 503, str(exc))

    if response.status_code == 401:
        return None, ("HSD API key is invalid", 503, response.text[:500])
    if response.status_code >= 400:
        current_app.logger.warning(
            "HSD RPC failed for %s with status %s: %s",
            method,
            response.status_code,
            response.text[:500],
        )
        return None, ("HSD RPC request failed", 503, response.text[:500])

    try:
        data = response.json()
    except ValueError:
        return None, ("HSD returned invalid JSON", 503, response.text[:500])

    if isinstance(data, dict) and data.get('error'):
        error = data.get('error') or {}
        message = error.get('message') or "HSD RPC returned an error"
        return None, (message, 404 if "not found" in message.lower() else 503, error)

    return data.get('result') if isinstance(data, dict) else data, None


def _fetch_hsd_coin(tx_hash, output_index):
    coin, error = _hsd_request(f"/coin/{tx_hash}/{output_index}")
    if error and error[1] == 404:
        return None, ("Listing coin was not found or is already spent", 404, error[2])
    return coin, error


def _fetch_hsd_tx(tx_hash):
    tx, error = _hsd_request(f"/tx/{tx_hash}")
    if error and error[1] == 404:
        return None, ("Transaction was not found", 404, error[2])
    return tx, error


def _fetch_explorer_tx(tx_hash):
    explorer_base_url = current_app.config.get('TX_EXPLORER_BASE_URL')
    if not explorer_base_url:
        return None, ("Transaction explorer fallback is not configured", 503, None)

    try:
        response = requests.get(
            f"{explorer_base_url.rstrip('/')}/{tx_hash}",
            timeout=current_app.config.get('HSD_HTTP_TIMEOUT', 5),
        )
    except requests.RequestException as exc:
        current_app.logger.warning("Failed tx explorer lookup for %s: %s", tx_hash, exc)
        return None, ("Could not reach transaction explorer fallback", 503, str(exc))

    if response.status_code == 404:
        return None, ("Transaction was not found by explorer fallback", 404, response.text[:500])
    if response.status_code >= 400:
        current_app.logger.warning(
            "Tx explorer lookup failed for %s with status %s: %s",
            tx_hash,
            response.status_code,
            response.text[:500],
        )
        return None, ("Transaction explorer fallback request failed", 503, response.text[:500])

    html = response.text or ''
    if tx_hash not in html:
        return None, ("Transaction explorer fallback returned an unexpected page", 503, html[:500])

    height = None
    height_match = re.search(r'data-height=["\'](\d+)["\']', html, re.IGNORECASE)
    if not height_match:
        height_match = re.search(r'href=["\']/block/(\d+)(?:["\'/])', html, re.IGNORECASE)
    if height_match:
        height = int(height_match.group(1))

    inputs = []
    for match in re.finditer(
        r'<a\b(?=[^>]*data-tooltip=["\']input["\'])(?=[^>]*href=["\'][^"\']*/transaction/([a-f0-9]{64})#output-(\d+)["\'])[^>]*>',
        html,
        re.IGNORECASE,
    ):
        inputs.append({
            "prevout": {
                "hash": match.group(1).lower(),
                "index": int(match.group(2)),
            },
        })

    outputs = []
    for match in re.finditer(
        r'<div\b[^>]*id=["\']output-(\d+)["\'][^>]*>.*?<button[^>]*>\s*([^<]+?)\s*</button>\s*<a\b[^>]*href=["\']/name/([^"\']+)["\']',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        outputs.append({
            "index": int(match.group(1)),
            "covenant": {
                "action": match.group(2).strip().upper(),
                "name": match.group(3).strip().lower().rstrip('/'),
            },
        })

    if not inputs:
        return None, ("Transaction explorer fallback returned no transaction inputs", 503, html[:500])

    return {
        "hash": tx_hash,
        "height": height,
        "inputs": inputs,
        "outputs": outputs,
        "source": "tx-explorer",
    }, None


def _fetch_hsd_name_info(name):
    info, error = _hsd_rpc_request('getnameinfo', [name, True])
    if error and error[1] == 404:
        return None, ("Name was not found", 404, error[2])
    return info, error


def _observed_market_names(limit):
    names = []
    seen = set()
    rows = [
        *Listing.query.order_by(Listing.created_at.desc()).all(),
        *PendingListing.query.order_by(PendingListing.created_at.desc()).all(),
    ]

    for row in rows:
        name = getattr(row, 'name', None)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= limit:
            break

    return names


def _community_expiring_watch_names(limit, network='main'):
    rows = (
        ExpiringNameWatch.query
        .filter_by(network=network, source='community-import')
        .order_by(
            ExpiringNameWatch.blocks_until_expire.is_(None),
            ExpiringNameWatch.blocks_until_expire.asc(),
            ExpiringNameWatch.name.asc(),
        )
        .limit(limit)
        .all()
    )
    return [row.name for row in rows]


def _expiring_source_counts(network='main'):
    rows = ExpiringNameWatch.query.filter_by(network=network).all()
    counts = {}
    imported_count = 0
    refreshed_count = 0
    not_found_count = 0
    last_import_at = None
    last_refresh_at = None

    for row in rows:
        source = row.source or 'unknown'
        counts[source] = counts.get(source, 0) + 1
        if source == 'community-import':
            imported_count += 1
            if row.created_at and (last_import_at is None or row.created_at > last_import_at):
                last_import_at = row.created_at
        if row.last_checked_at:
            refreshed_count += 1
            if last_refresh_at is None or row.last_checked_at > last_refresh_at:
                last_refresh_at = row.last_checked_at
        if row.found is False:
            not_found_count += 1

    return {
        "sourceCounts": counts,
        "importedCount": imported_count,
        "refreshedCount": refreshed_count,
        "notFoundCount": not_found_count,
        "lastImportAt": last_import_at.isoformat() if last_import_at else None,
        "lastRefreshAt": last_refresh_at.isoformat() if last_refresh_at else None,
    }


def _merge_expiring_name_lists(limit, *name_lists):
    names = []
    seen = set()
    for name_list in name_lists:
        for name in name_list:
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
            if len(names) >= limit:
                return names
    return names


def _require_market_admin():
    token = current_app.config.get('MARKET_ADMIN_TOKEN')
    if not token:
        return None

    body = request.get_json(silent=True)
    submitted = (
        request.headers.get('X-Market-Admin-Token')
        or request.args.get('adminToken')
        or (body.get('adminToken') if isinstance(body, dict) else None)
    )
    if submitted != token:
        return jsonify({"error": "Admin token is required"}), 401

    return None



HANDSHAKE_IMPORT_NAME_RE = re.compile(r'^[a-z0-9-]{1,63}$')
EXPIRING_IMPORT_MAX_NAMES = 1000
EXPIRING_IMPORT_REFRESH_MAX_NAMES = 100
EXPIRING_IMPORT_SKIP_TOKENS = {'name', 'names', 'domain', 'domains', 'tld', 'tlds'}


def _normalize_expiring_import_name(raw_name):
    if raw_name is None:
        return None, 'empty name'

    name = str(raw_name).strip().lower().rstrip('/').strip()
    if name in EXPIRING_IMPORT_SKIP_TOKENS:
        return None, 'header token'
    if not name:
        return None, 'empty name'
    if '/' in name or '.' in name or any(char.isspace() for char in name):
        return None, 'use root name only, without slash, dot, or spaces'
    if not HANDSHAKE_IMPORT_NAME_RE.match(name):
        return None, 'name must be 1-63 lowercase letters, numbers, or hyphens'

    return name, None


def _split_expiring_import_text(text):
    if not text:
        return []
    return [token for token in re.split(r'[\s,;]+', text) if token]


def _expiring_import_values_from_request(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        raw_names = data.get('names', data.get('domains'))
        if isinstance(raw_names, list):
            return raw_names
        if isinstance(raw_names, str):
            return _split_expiring_import_text(raw_names)

        raw_text = data.get('text', data.get('csv'))
        if isinstance(raw_text, str):
            return _split_expiring_import_text(raw_text)

        raw_name = data.get('name', data.get('domain'))
        if raw_name:
            return [raw_name]

    raw_body = request.get_data(as_text=True) or ''
    return _split_expiring_import_text(raw_body)


def _refresh_named_expiring_watches(names, source='community-import', network='main', limit=EXPIRING_IMPORT_REFRESH_MAX_NAMES):
    chain_payload, chain_status = get_hsd_status_payload()
    chain_height = chain_payload.get('height') if chain_status == 200 else None
    refreshed = []

    for name in names[:limit]:
        payload = _expiring_name_payload(name, chain_height)
        watch = _store_expiring_watch(
            payload,
            chain_height=chain_height,
            network=network,
            source=source,
        )
        refreshed.append(_expiring_watch_payload(watch))

    db.session.commit()
    return {
        "count": len(refreshed),
        "chainHeight": chain_height,
        "names": refreshed,
    }

def _expiring_name_payload(name, chain_height=None):
    info, error = _fetch_hsd_name_info(name)
    if error:
        message, status = error[:2]
        return {
            "name": name,
            "found": False,
            "error": message,
            "statusCode": status,
        }

    name_info = info.get('info') if isinstance(info, dict) else {}
    stats = name_info.get('stats') if isinstance(name_info, dict) and isinstance(name_info.get('stats'), dict) else {}
    blocks_until_expire = stats.get('blocksUntilExpire')
    expiration_height = None
    if isinstance(chain_height, int) and isinstance(blocks_until_expire, int):
        expiration_height = chain_height + blocks_until_expire

    return {
        "name": name,
        "found": bool(name_info),
        "state": name_info.get('state') if isinstance(name_info, dict) else None,
        "renewalHeight": name_info.get('renewal') if isinstance(name_info, dict) else None,
        "expirationHeight": expiration_height,
        "blocksUntilExpire": blocks_until_expire,
        "daysUntilExpire": stats.get('daysUntilExpire'),
        "hoursUntilExpire": stats.get('hoursUntilExpire'),
        "expired": isinstance(blocks_until_expire, int) and blocks_until_expire <= 0,
        "stats": stats,
    }


def _seed_expiring_watches(names, source='market-observed', network='main'):
    created = 0
    for name in names:
        existing = ExpiringNameWatch.query.filter_by(name=name, network=network).first()
        if existing:
            continue
        db.session.add(ExpiringNameWatch(name=name, network=network, source=source))
        created += 1

    if created:
        db.session.commit()

    return created


def _store_expiring_watch(payload, chain_height=None, network='main', source='market-observed'):
    watch = ExpiringNameWatch.query.filter_by(name=payload['name'], network=network).first()
    if watch is None:
        watch = ExpiringNameWatch(name=payload['name'], network=network, source=source)

    watch.source = source
    watch.state = payload.get('state')
    watch.renewal_height = payload.get('renewalHeight')
    watch.expiration_height = payload.get('expirationHeight')
    watch.blocks_until_expire = payload.get('blocksUntilExpire')
    watch.days_until_expire = payload.get('daysUntilExpire')
    watch.hours_until_expire = payload.get('hoursUntilExpire')
    watch.expired = bool(payload.get('expired'))
    watch.found = bool(payload.get('found'))
    watch.error = payload.get('error')
    watch.source_height = chain_height
    watch.last_checked_at = datetime.utcnow()
    watch.updated_at = datetime.utcnow()
    db.session.add(watch)
    return watch


def _expiring_watch_payload(watch):
    return {
        "name": watch.name,
        "found": watch.found,
        "state": watch.state,
        "renewalHeight": watch.renewal_height,
        "expirationHeight": watch.expiration_height,
        "blocksUntilExpire": watch.blocks_until_expire,
        "daysUntilExpire": float(watch.days_until_expire) if watch.days_until_expire is not None else None,
        "hoursUntilExpire": float(watch.hours_until_expire) if watch.hours_until_expire is not None else None,
        "expired": watch.expired,
        "error": watch.error,
        "source": watch.source,
        "sourceHeight": watch.source_height,
        "lastCheckedAt": watch.last_checked_at.isoformat() if watch.last_checked_at else None,
    }


def _global_name_payload(row):
    return {
        "name": row.name,
        "found": True,
        "state": row.state,
        "renewalHeight": row.renewal_height,
        "expirationHeight": row.expiration_height,
        "blocksUntilExpire": row.blocks_until_expire,
        "daysUntilExpire": float(row.days_until_expire) if row.days_until_expire is not None else None,
        "hoursUntilExpire": float(row.hours_until_expire) if row.hours_until_expire is not None else None,
        "expired": row.expired,
        "error": None,
        "source": "global-index",
        "sourceHeight": row.source_height,
        "lastCheckedAt": row.last_checked_at.isoformat() if row.last_checked_at else None,
    }


def _store_global_name_state(payload, chain_height=None, network='main'):
    if not payload.get('found'):
        return None

    row = GlobalNameState.query.filter_by(name=payload['name'], network=network).first()
    if row is None:
        row = GlobalNameState(name=payload['name'], network=network)

    row.state = payload.get('state')
    row.renewal_height = payload.get('renewalHeight')
    row.expiration_height = payload.get('expirationHeight')
    row.blocks_until_expire = payload.get('blocksUntilExpire')
    row.days_until_expire = payload.get('daysUntilExpire')
    row.hours_until_expire = payload.get('hoursUntilExpire')
    row.expired = bool(payload.get('expired'))
    row.source_height = chain_height
    row.last_checked_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    db.session.add(row)
    return row


def _name_indexer_status_payload(network='main'):
    progress = NameIndexerProgress.query.filter_by(network=network).first()
    if progress is None:
        return {
            "status": "not-started",
            "network": network,
            "mode": "forward-only",
            "lastIndexedHeight": None,
            "targetHeight": None,
            "namesIndexed": 0,
            "lastError": None,
            "startedAt": None,
            "finishedAt": None,
            "updatedAt": None,
            "ready": False,
            "complete": False,
        }

    status = progress.status or "not-started"
    return {
        "status": status,
        "network": progress.network,
        "mode": "forward-only",
        "lastIndexedHeight": progress.last_indexed_height,
        "targetHeight": progress.target_height,
        "namesIndexed": progress.names_indexed,
        "lastError": progress.last_error,
        "startedAt": progress.started_at.isoformat() if progress.started_at else None,
        "finishedAt": progress.finished_at.isoformat() if progress.finished_at else None,
        "updatedAt": progress.updated_at.isoformat() if progress.updated_at else None,
        "ready": status in {"watching", "ready"} and not progress.last_error,
        "complete": status == "ready",
    }


def _global_name_status_query(query, status):
    if status == 'expired':
        return query.filter(or_(
            GlobalNameState.expired.is_(True),
            GlobalNameState.blocks_until_expire <= 0,
        ))
    if status == 'all':
        return query
    return query.filter(
        GlobalNameState.expired.is_(False),
        GlobalNameState.blocks_until_expire.isnot(None),
        GlobalNameState.blocks_until_expire > 0,
    )


def _global_name_status_order(status):
    if status == 'expired':
        return (
            GlobalNameState.blocks_until_expire.is_(None),
            GlobalNameState.blocks_until_expire.desc(),
            GlobalNameState.name.asc(),
        )
    return (
        GlobalNameState.blocks_until_expire.is_(None),
        GlobalNameState.blocks_until_expire.asc(),
        GlobalNameState.name.asc(),
    )


def _cached_global_expiring_names(limit, network='main', status='active'):
    query = _global_name_status_query(
        GlobalNameState.query.filter_by(network=network),
        status,
    )
    rows = query.order_by(*_global_name_status_order(status)).limit(limit).all()
    return [_global_name_payload(row) for row in rows]


def _refresh_global_expiring_names(limit=100, stale_only=True, network='main', status='active'):
    refresh_minutes = current_app.config.get('EXPIRING_GLOBAL_REFRESH_MINUTES', 60)
    cutoff = datetime.utcnow() - timedelta(minutes=refresh_minutes)
    query = _global_name_status_query(
        GlobalNameState.query.filter_by(network=network),
        status,
    )
    if stale_only:
        query = query.filter(
            (GlobalNameState.last_checked_at.is_(None))
            | (GlobalNameState.last_checked_at < cutoff)
        )

    rows = (
        query
        .order_by(*_global_name_status_order(status))
        .limit(limit)
        .all()
    )

    chain_payload, chain_status = get_hsd_status_payload()
    chain_height = chain_payload.get('height') if chain_status == 200 else None
    if not isinstance(chain_height, int):
        return {
            "refreshed": 0,
            "removed": 0,
            "node": {
                "reachable": chain_payload.get('reachable', False),
                "height": chain_height,
                "progress": chain_payload.get('progress'),
            },
        }

    refreshed = 0
    removed = 0
    for row in rows:
        payload = _expiring_name_payload(row.name, chain_height)
        if payload.get('found') and isinstance(payload.get('blocksUntilExpire'), int):
            _store_global_name_state(payload, chain_height=chain_height, network=network)
            refreshed += 1
        elif not payload.get('error'):
            db.session.delete(row)
            removed += 1

    if rows:
        db.session.commit()

    return {
        "refreshed": refreshed,
        "removed": removed,
        "node": {
            "reachable": chain_payload.get('reachable', False),
            "height": chain_height,
            "progress": chain_payload.get('progress'),
        },
    }


def _global_rows_are_fresh(rows, network='main'):
    refresh_minutes = current_app.config.get('EXPIRING_GLOBAL_REFRESH_MINUTES', 60)
    cutoff = datetime.utcnow() - timedelta(minutes=refresh_minutes)
    return all(row.get('lastCheckedAt') and datetime.fromisoformat(row['lastCheckedAt']) >= cutoff for row in rows)


def _refresh_global_expiring_window(limit=100, network='main', status='active'):
    refresh_limit = min(max(limit * 25, 50), 500)
    result = {
        "refreshed": 0,
        "removed": 0,
        "node": {},
    }

    for _ in range(5):
        batch = _refresh_global_expiring_names(
            limit=refresh_limit,
            stale_only=True,
            network=network,
            status=status,
        )
        result["refreshed"] += batch.get("refreshed", 0)
        result["removed"] += batch.get("removed", 0)
        result["node"] = batch.get("node", {})

        if batch.get("refreshed", 0) == 0 and batch.get("removed", 0) == 0:
            break

        rows = _cached_global_expiring_names(limit, network=network, status=status)
        if _global_rows_are_fresh(rows, network=network):
            break

    return result


def _cached_expiring_watches(limit, network='main'):
    return _cached_expiring_watches_for_sources(limit, network=network)


def _cached_expiring_watches_for_sources(limit, sources=None, network='main'):
    query = ExpiringNameWatch.query.filter_by(network=network)
    if sources:
        query = query.filter(ExpiringNameWatch.source.in_(sources))

    rows = (
        query.order_by(
            ExpiringNameWatch.blocks_until_expire.is_(None),
            ExpiringNameWatch.blocks_until_expire.asc(),
            ExpiringNameWatch.name.asc(),
        )
        .limit(limit)
        .all()
    )
    return [_expiring_watch_payload(row) for row in rows]


def _refresh_expiring_watches(limit=100, stale_only=True, network='main'):
    _seed_expiring_watches(_observed_market_names(limit), network=network)

    refresh_minutes = current_app.config.get('EXPIRING_WATCH_REFRESH_MINUTES', 60)
    cutoff = datetime.utcnow() - timedelta(minutes=refresh_minutes)
    query = ExpiringNameWatch.query.filter_by(network=network)
    if stale_only:
        query = query.filter(
            (ExpiringNameWatch.last_checked_at.is_(None))
            | (ExpiringNameWatch.last_checked_at < cutoff)
        )

    watches = (
        query
        .order_by(
            ExpiringNameWatch.last_checked_at.isnot(None),
            ExpiringNameWatch.blocks_until_expire.is_(None),
            ExpiringNameWatch.blocks_until_expire.asc(),
            ExpiringNameWatch.name.asc(),
        )
        .limit(limit)
        .all()
    )

    chain_payload, chain_status = get_hsd_status_payload()
    chain = chain_payload.get('chain', {}) if chain_status == 200 else {}
    chain_height = chain_payload.get('height') if chain_status == 200 else None

    refreshed = []
    for watch in watches:
        payload = _expiring_name_payload(watch.name, chain_height)
        refreshed_watch = _store_expiring_watch(
            payload,
            chain_height=chain_height,
            network=network,
            source=watch.source or 'market-observed',
        )
        refreshed.append(_expiring_watch_payload(refreshed_watch))

    if refreshed:
        db.session.commit()

    return {
        "refreshed": len(refreshed),
        "names": refreshed,
        "node": {
            "reachable": chain_payload.get('reachable', False),
            "height": chain_height,
            "progress": chain_payload.get('progress'),
            "spv": ((chain.get('options') or {}).get('spv') if isinstance(chain, dict) else None),
        },
    }


@api_bp.route('/v1/fee_info', methods=['GET'])
@api_bp.route('/v2/fee_info', methods=['GET'])
def fee_info():
    rate = current_app.config['MARKETPLACE_FEE_RATE']
    address = current_app.config['MARKETPLACE_FEE_ADDRESS']
    if rate < 0 or rate > 10000:
        return jsonify({"error": "Marketplace fee rate is invalid"}), 503
    if rate > 0 and not address:
        return jsonify({"error": "Marketplace fee address is not configured"}), 503

    return jsonify({
        "rate": rate,
        "address": address,
        "addr": address,
    })


@api_bp.route('/v2/auctions', methods=['GET'])
def auctions():
    try:
        page = max(int(request.args.get('page', 1)), 1)
        per_page = min(max(int(request.args.get('per_page', 20)), 1), 100)
    except ValueError:
        return jsonify({"error": "Invalid pagination"}), 400

    active_listings = _active_listings_unique_by_name()
    active_names = {listing.name for listing in active_listings}
    pending_listings = [
        pending for pending in PendingListing.query.order_by(PendingListing.created_at.desc()).all()
        if pending.status not in PENDING_TERMINAL_STATUSES
        and not pending.is_expired()
        and pending.name not in active_names
    ]
    all_rows = [
        *[_bob_auction_from_pending(pending) for pending in pending_listings],
        *[_bob_auction_from_listing(listing) for listing in active_listings],
    ]
    total = len(all_rows)
    start = (page - 1) * per_page
    end = start + per_page

    return jsonify({
        "total": total,
        "auctions": all_rows[start:end],
    })


@api_bp.route('/v2/sales', methods=['GET'])
def sales():
    sale_statuses = ('sale-pending', 'sold', 'completed', 'archived')
    listings = (
        Listing.query
        .filter(Listing.status.in_(sale_statuses))
        .order_by(Listing.created_at.desc())
        .all()
    )
    return jsonify({
        "total": len(listings),
        "sales": [
            {
                "id": listing.id,
                "name": listing.name,
                "priceHns": float(listing.price_hns),
                "status": listing.status,
                "statusLabel": _sale_status_label(listing.status),
                "pending": listing.status == 'sale-pending',
                "createdAt": listing.created_at.isoformat() if listing.created_at else None,
                "soldAt": listing.sold_at.isoformat() if listing.sold_at else None,
                "saleTxHash": listing.sale_tx_hash,
                "transferStartTxHash": listing.transfer_start_tx_hash,
                "expiresAt": listing.expires_at.isoformat() if listing.expires_at else None,
                "url": f"/listing/{listing.name}",
            }
            for listing in listings
        ],
    })


@api_bp.route('/v2/sales/private', methods=['POST'])
@limiter.limit("20 per hour")
def record_private_sale():
    auth_error = _require_market_admin()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    name = str(data.get('name', request.args.get('name', ''))).strip().lower().rstrip('/')
    if not name:
        return jsonify({"error": "name is required"}), 400

    sale_tx_hash, sale_hash_error = _validate_hex_hash(
        data.get('saleTxHash', request.args.get('saleTxHash')),
        'saleTxHash',
    )
    if sale_hash_error:
        message, status = sale_hash_error
        return jsonify({"error": message}), status

    try:
        price_hns = Decimal(str(data.get('priceHns', request.args.get('priceHns'))))
    except (InvalidOperation, TypeError):
        return jsonify({"error": "priceHns must be a decimal HNS amount"}), 400
    if price_hns <= 0:
        return jsonify({"error": "priceHns must be greater than zero"}), 400

    matches_owner, transfer_status = _tx_is_current_name_owner_transfer(name, sale_tx_hash)
    if not matches_owner:
        return jsonify({
            "error": "saleTxHash is not the current owner transfer transaction for this name",
            "transferStatus": transfer_status,
        }), 400

    existing_sale = Listing.query.filter_by(name=name, sale_tx_hash=sale_tx_hash).first()
    if existing_sale:
        return jsonify({
            "success": True,
            "created": False,
            "id": existing_sale.id,
            "name": existing_sale.name,
            "priceHns": float(existing_sale.price_hns),
            "status": existing_sale.status,
            "saleTxHash": existing_sale.sale_tx_hash,
            "transferStartTxHash": existing_sale.transfer_start_tx_hash,
            "url": f"/listing/{existing_sale.name}",
        })

    listing = (
        Listing.query
        .filter_by(name=name, status='sale-pending')
        .filter(Listing.sale_tx_hash.is_(None))
        .filter(Listing.price_hns == price_hns)
        .order_by(Listing.created_at.desc())
        .first()
    )

    created = False
    if listing is None:
        listing = Listing(
            name=name,
            price_hns=price_hns,
            description='Private Shakedex sale recorded from on-chain transfer history.',
            seller_hns_address=str(data.get('sellerHnsAddress') or 'private-sale'),
            ipfs_cid=f"private-sale:{sale_tx_hash[:46]}",
            proof_json={
                "version": 2,
                "name": name,
                "privateSale": True,
                "saleTxHash": sale_tx_hash,
                "transferStartTxHash": sale_tx_hash,
                "ownerOutputIndex": transfer_status.get('ownerOutputIndex'),
            },
            status='sold',
            created_at=datetime.utcnow(),
        )
        db.session.add(listing)
        created = True

    listing.status = 'sold'
    listing.sold_at = datetime.utcnow()
    listing.sale_tx_hash = sale_tx_hash
    listing.transfer_start_tx_hash = sale_tx_hash
    db.session.commit()

    return jsonify({
        "success": True,
        "created": created,
        "id": listing.id,
        "name": listing.name,
        "priceHns": float(listing.price_hns),
        "status": listing.status,
        "soldAt": listing.sold_at.isoformat(),
        "saleTxHash": listing.sale_tx_hash,
        "transferStartTxHash": listing.transfer_start_tx_hash,
        "verificationSource": "name-owner",
        "url": f"/listing/{listing.name}",
    }), 201 if created else 200


@api_bp.route('/v2/sales/transfer-start', methods=['POST'])
@limiter.limit("40 per hour")
def update_sale_transfer_start():
    auth_error = _require_market_admin()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    request_values = {**request.args.to_dict(), **data}
    transfer_start_tx_hash, transfer_hash_error = _validate_hex_hash(
        request_values.get('transferStartTxHash', request_values.get('transfer_start_tx_hash')),
        'transferStartTxHash',
    )
    if transfer_hash_error:
        message, status = transfer_hash_error
        return jsonify({"error": message}), status
    if not transfer_start_tx_hash:
        return jsonify({"error": "transferStartTxHash is required"}), 400

    sale_tx_hash, sale_hash_error = _validate_hex_hash(
        request_values.get('saleTxHash', request_values.get('sale_tx_hash')),
        'saleTxHash',
    )
    if sale_hash_error:
        message, status = sale_hash_error
        return jsonify({"error": message}), status

    record_sale_tx_hash, record_sale_hash_error = _validate_hex_hash(
        request_values.get('recordSaleTxHash', request_values.get('record_sale_tx_hash')),
        'recordSaleTxHash',
    )
    if record_sale_hash_error:
        message, status = record_sale_hash_error
        return jsonify({"error": message}), status

    listing = None
    listing_id = request_values.get('listingId', request_values.get('listing_id'))
    if listing_id:
        try:
            listing = Listing.query.get(int(listing_id))
        except (TypeError, ValueError):
            return jsonify({"error": "listingId must be an integer"}), 400
    else:
        name = str(request_values.get('name', '')).strip().lower().rstrip('/')
        if not name:
            return jsonify({"error": "name or listingId is required"}), 400

        query = Listing.query.filter(
            Listing.name == name,
            Listing.status.in_(('sold', 'completed')),
        )
        if sale_tx_hash:
            query = query.filter(Listing.sale_tx_hash == sale_tx_hash)
        listing = query.order_by(Listing.sold_at.desc(), Listing.created_at.desc()).first()

    if not listing:
        return jsonify({"error": "Listing not found"}), 404
    if listing.status not in {'sold', 'completed'}:
        return jsonify({"error": "Only sold listings can store a transfer start transaction"}), 409
    if sale_tx_hash and listing.sale_tx_hash != sale_tx_hash:
        return jsonify({"error": "saleTxHash does not match this listing"}), 409

    if record_sale_tx_hash:
        listing.sale_tx_hash = record_sale_tx_hash
    listing.transfer_start_tx_hash = transfer_start_tx_hash
    if isinstance(listing.proof_json, dict):
        proof_json = dict(listing.proof_json)
        proof_json["transferStartTxHash"] = transfer_start_tx_hash
        listing.proof_json = proof_json
    db.session.commit()
    _index_marketplace_sale_txs(listing)

    return jsonify({
        "success": True,
        "id": listing.id,
        "name": listing.name,
        "priceHns": float(listing.price_hns),
        "status": listing.status,
        "saleTxHash": listing.sale_tx_hash,
        "transferStartTxHash": listing.transfer_start_tx_hash,
        "url": f"/listing/{listing.name}",
    })


@api_bp.route('/v2/listings/archive', methods=['POST'])
@limiter.limit("20 per hour")
def archive_listing():
    auth_error = _require_market_admin()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    listing_id = data.get('listingId', request.args.get('listingId'))
    if not listing_id:
        return jsonify({"error": "listingId is required"}), 400

    try:
        listing_id = int(listing_id)
    except (TypeError, ValueError):
        return jsonify({"error": "listingId must be an integer"}), 400

    listing = Listing.query.get(listing_id)
    if not listing:
        return jsonify({"error": "Listing not found"}), 404
    if listing.status in {'sold', 'completed'}:
        return jsonify({"error": "Sold listings cannot be archived"}), 409

    listing.status = 'archived'
    listing.flagged_reason = str(data.get('reason') or 'Archived by market admin.').strip()
    db.session.commit()

    return jsonify({
        "success": True,
        "id": listing.id,
        "name": listing.name,
        "priceHns": float(listing.price_hns),
        "status": listing.status,
        "reason": listing.flagged_reason,
        "url": f"/listing/{listing.name}",
    })


def _sale_status_label(status):
    return {
        'sale-pending': 'Sale pending',
        'sold': 'Sold',
        'completed': 'Completed',
        'archived': 'Archived',
    }.get(status, status.replace('-', ' ').title())


@api_bp.route('/v2/pending-listings', methods=['GET'])
def pending_listings():
    pending = [
        row for row in PendingListing.query.order_by(PendingListing.created_at.desc()).all()
        if row.status not in PENDING_TERMINAL_STATUSES and not row.is_expired()
    ]
    return jsonify({
        "total": len(pending),
        "pending": [_pending_listing_payload(row) for row in pending],
    })


@api_bp.route('/v2/pending-listings', methods=['POST'])
@limiter.limit("20 per hour")
def create_pending_listing():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip().lower().rstrip('/')
    network = str(data.get('network', 'main')).strip().lower()
    transfer_tx_hash = str(data.get('transferTxHash', data.get('transfer_tx_hash', ''))).strip().lower()

    if not name:
        return jsonify({"error": "name is required"}), 400
    if network not in {'main', 'testnet', 'regtest', 'simnet'}:
        return jsonify({"error": "network is invalid"}), 400
    if len(transfer_tx_hash) != 64 or any(c not in '0123456789abcdef' for c in transfer_tx_hash):
        return jsonify({"error": "transferTxHash must be a 64-character hex string"}), 400

    transfer_output_idx = data.get('transferOutputIdx', data.get('transfer_output_idx'))
    if transfer_output_idx is not None:
        try:
            transfer_output_idx = int(transfer_output_idx)
        except (TypeError, ValueError):
            return jsonify({"error": "transferOutputIdx must be an integer"}), 400
        if transfer_output_idx < 0:
            return jsonify({"error": "transferOutputIdx must be non-negative"}), 400

    expected_price = data.get('expectedPrice', data.get('expected_price'))
    if expected_price in ('', None):
        expected_price = None
    else:
        try:
            expected_price = int(expected_price)
        except (TypeError, ValueError):
            return jsonify({"error": "expectedPrice must be an integer in base HNS units"}), 400
        if expected_price < 0:
            return jsonify({"error": "expectedPrice must be non-negative"}), 400

    pending = PendingListing.query.filter_by(name=name, network=network).first()
    if pending is None:
        pending = PendingListing(name=name, network=network, transfer_tx_hash=transfer_tx_hash)

    pending.transfer_tx_hash = transfer_tx_hash
    pending.transfer_output_idx = transfer_output_idx
    pending.lock_script_addr = data.get('lockScriptAddr', data.get('lock_script_addr'))
    pending.listing_mode = data.get('listingMode', data.get('listing_mode')) or 'fixed-price'
    pending.expected_price = expected_price
    pending.seller_note = sanitize_html(data.get('sellerNote', data.get('seller_note', '')))
    pending.status = 'pending-submitted'

    try:
        db.session.add(pending)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "A pending listing with that transferTxHash already exists"}), 409

    return jsonify({
        "success": True,
        "pending": _pending_listing_payload(pending),
    }), 201


@api_bp.route('/v2/hsd/status', methods=['GET'])
def hsd_status():
    payload, status = get_hsd_status_payload()
    return jsonify(payload), status


def get_hsd_status_payload():
    hsd_url = current_app.config.get('HSD_HTTP_URL')
    configured = bool(hsd_url)
    api_style = _hsd_api_style()

    if not configured:
        return {
            "configured": False,
            "reachable": False,
            "error": "HSD HTTP URL is not configured",
        }, 503

    info, error = _hsd_request('/')
    if error:
        message, status, detail = error
        return {
            "configured": True,
            "reachable": False,
            "url": hsd_url,
            "apiStyle": api_style,
            "error": message,
            "detail": detail,
        }, status

    if api_style == 'firehsd':
        chain_payload, chain_error = _hsd_request('/chain')
        if chain_error:
            message, status, detail = chain_error
            return {
                "configured": True,
                "reachable": False,
                "url": hsd_url,
                "apiStyle": api_style,
                "error": message,
                "detail": detail,
            }, status
        chain = chain_payload.get('chain', {}) if isinstance(chain_payload, dict) else {}
    else:
        chain = info.get('chain', {}) if isinstance(info, dict) else {}

    network = 'main' if api_style == 'firehsd' else (
        info.get('network') if isinstance(info, dict) else None
    )

    return {
        "configured": True,
        "reachable": True,
        "url": hsd_url,
        "apiStyle": api_style,
        "network": network,
        "version": info.get('version') if isinstance(info, dict) else None,
        "chain": chain,
        "height": chain.get('height'),
        "progress": chain.get('progress'),
    }, 200


def _market_event_payload(event):
    return {
        "id": event.id,
        "network": event.network,
        "name": event.name,
        "action": event.covenant_action,
        "txHash": event.tx_hash,
        "outputIndex": event.output_index,
        "blockHeight": event.block_height,
        "blockHash": event.block_hash,
        "blockTime": event.block_time.isoformat() if event.block_time else None,
        "source": event.source,
    }


def _market_index_health(
    progress,
    chain_height,
    *,
    now=None,
    stale_seconds=300,
    max_lag_blocks=6,
    node_reachable=True,
):
    now = now or datetime.utcnow()
    worker_status = progress.status if progress else 'not-started'
    updated_at = progress.updated_at if progress else None
    indexed_height = progress.last_indexed_height if progress else None
    heartbeat_age_seconds = None
    if updated_at:
        heartbeat_age_seconds = max(0, int((now - updated_at).total_seconds()))

    lag_blocks = None
    if isinstance(chain_height, int) and isinstance(indexed_height, int):
        lag_blocks = max(chain_height - indexed_height, 0)

    reasons = []
    if not node_reachable:
        reasons.append('node-unreachable')
    if progress is None:
        reasons.append('not-started')
    elif worker_status == 'failed':
        reasons.append('worker-failed')
    elif worker_status not in {'running', 'watching'}:
        reasons.append('worker-not-running')
    if updated_at is None or heartbeat_age_seconds > stale_seconds:
        reasons.append('stale-heartbeat')
    if lag_blocks is None:
        reasons.append('lag-unknown')
    elif lag_blocks > max_lag_blocks:
        reasons.append('block-lag')

    healthy = not reasons
    if healthy:
        status = 'healthy'
    elif 'worker-failed' in reasons:
        status = 'failed'
    elif 'stale-heartbeat' in reasons:
        status = 'stale'
    elif 'block-lag' in reasons:
        status = 'lagging'
    else:
        status = 'unhealthy'

    return {
        'healthy': healthy,
        'status': status,
        'workerStatus': worker_status,
        'reasons': reasons,
        'hsdHeight': chain_height,
        'lastIndexedHeight': indexed_height,
        'lagBlocks': lag_blocks,
        'lastHeartbeatAt': updated_at.isoformat() if updated_at else None,
        'heartbeatAgeSeconds': heartbeat_age_seconds,
        'staleAfterSeconds': stale_seconds,
        'maxLagBlocks': max_lag_blocks,
    }


@api_bp.route('/v2/market-index/status', methods=['GET'])
def market_index_status():
    from app.models import MarketplaceCovenantEvent, MarketplaceIndexerProgress

    progress = MarketplaceIndexerProgress.query.filter_by(network='main').first()
    latest_event = (
        MarketplaceCovenantEvent.query
        .order_by(MarketplaceCovenantEvent.block_height.desc())
        .first()
    )
    chain_payload, chain_status = get_hsd_status_payload()
    chain_height = chain_payload.get('height') if chain_status == 200 else None
    health = _market_index_health(
        progress,
        chain_height,
        stale_seconds=current_app.config.get('MARKET_INDEXER_STALE_SECONDS', 300),
        max_lag_blocks=current_app.config.get('MARKET_INDEXER_MAX_LAG_BLOCKS', 6),
        node_reachable=chain_status == 200 and chain_payload.get('reachable', False),
    )
    payload = {
        "network": "main",
        **health,
        "targetHeight": progress.target_height if progress else None,
        "eventsIndexed": progress.events_indexed if progress else 0,
        "lastError": progress.last_error if progress else None,
        "updatedAt": progress.updated_at.isoformat() if progress and progress.updated_at else None,
        "latestEvent": _market_event_payload(latest_event) if latest_event else None,
        "nodeError": chain_payload.get('error') if chain_status != 200 else None,
    }
    return jsonify(payload), 200 if health['healthy'] else 503


@api_bp.route('/v2/market-index/refresh', methods=['POST'])
@limiter.limit("10 per hour")
def refresh_market_index():
    auth_error = _require_market_admin()
    if auth_error:
        return auth_error

    from app.marketplace_indexer import index_listing_hashes, scan_market_blocks

    data = request.get_json(silent=True) or {}
    try:
        lookback = int(data.get('lookback', request.args.get('lookback', 720)))
        max_blocks = int(data.get('maxBlocks', request.args.get('maxBlocks', lookback)))
    except (TypeError, ValueError):
        return jsonify({"error": "lookback and maxBlocks must be integers"}), 400

    try:
        start_height = data.get('startHeight', request.args.get('startHeight'))
        end_height = data.get('endHeight', request.args.get('endHeight'))
        start_height = int(start_height) if start_height not in (None, '') else None
        end_height = int(end_height) if end_height not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({"error": "startHeight and endHeight must be integers"}), 400

    if lookback < 0 or max_blocks <= 0:
        return jsonify({"error": "lookback must be >= 0 and maxBlocks must be > 0"}), 400
    max_blocks = min(max_blocks, 2000)

    try:
        hash_results = index_listing_hashes()
        block_result = scan_market_blocks(
            start_height=start_height,
            end_height=end_height,
            lookback=lookback,
            max_blocks=max_blocks,
        )
    except Exception as exc:
        current_app.logger.exception("Marketplace index refresh failed")
        return jsonify({"error": str(exc)}), 503

    return jsonify({
        "success": True,
        "hashes": hash_results,
        "blocks": block_result,
    })


@api_bp.route('/v2/market-index/names/<name>', methods=['GET'])
def market_index_name(name):
    from app.marketplace_indexer import events_for_name, name_state

    normalized_name = name.lower().rstrip('/')
    events = events_for_name(normalized_name)
    return jsonify({
        "name": normalized_name,
        "state": name_state(normalized_name),
        "events": [_market_event_payload(event) for event in events],
    })


@api_bp.route('/v2/listings/<name>/coin', methods=['GET'])
def listing_coin(name):
    listing = _active_listing_for_name(name)
    if not _listing_is_available(listing):
        return jsonify({"error": "Listing not found"}), 404

    proof = listing.proof_json
    tx_hash = proof["lockingTxHash"]
    output_index = proof["lockingOutputIdx"]
    coin, error = _fetch_hsd_coin(tx_hash, output_index)

    if error:
        message, status = error[:2]
        return jsonify({"error": message}), status

    return jsonify({
        "name": listing.name,
        "lockingTxHash": tx_hash,
        "lockingOutputIdx": output_index,
        "coin": coin,
    })


@api_bp.route('/v2/coin/<tx_hash>/<int:output_index>', methods=['GET'])
def coin_lookup(tx_hash, output_index):
    tx_hash, hash_error = _validate_hex_hash(tx_hash, "txHash")
    if hash_error:
        message, status = hash_error
        return jsonify({"error": message}), status

    if output_index < 0:
        return jsonify({"error": "Invalid output index"}), 400

    coin, error = _fetch_hsd_coin(tx_hash, output_index)
    if error:
        message, status = error[:2]
        return jsonify({"error": message}), status

    return jsonify({
        "txHash": tx_hash,
        "outputIndex": output_index,
        "coin": coin,
    })


@api_bp.route('/v2/listings/<name>/refresh-status', methods=['GET', 'POST'])
def refresh_listing_status(name):
    normalized_name = name.lower().rstrip('/')

    data = request.get_json(silent=True) or {}
    request_values = {**request.args.to_dict(), **data}
    sale_tx_hash, sale_hash_error = _validate_hex_hash(
        request_values.get('saleTxHash', request_values.get('sale_tx_hash')),
        'saleTxHash',
    )
    if sale_hash_error:
        message, status = sale_hash_error
        return jsonify({"error": message}), status

    transfer_start_tx_hash, transfer_hash_error = _validate_hex_hash(
        request_values.get('transferStartTxHash', request_values.get('transfer_start_tx_hash')),
        'transferStartTxHash',
    )
    if transfer_hash_error:
        message, status = transfer_hash_error
        return jsonify({"error": message}), status

    cancel_tx_hash, cancel_hash_error = _validate_hex_hash(
        request_values.get('cancelTxHash', request_values.get('cancel_tx_hash')),
        'cancelTxHash',
    )
    if cancel_hash_error:
        message, status = cancel_hash_error
        return jsonify({"error": message}), status

    outcome = str(request_values.get('outcome', request_values.get('status', 'sold' if sale_tx_hash else 'refresh'))).strip().lower()
    if outcome in {'cancelled', 'canceled', 'cancel'}:
        if cancel_tx_hash:
            listing, payload, status = _refreshable_listing_for_spend(normalized_name, cancel_tx_hash)
            if not listing:
                return jsonify(payload), status
        else:
            listing = _refreshable_listing_for_name(normalized_name)
        if not listing or listing.is_expired():
            return jsonify({"error": "Listing not found"}), 404
        _, payload, status = _mark_listing_cancelled_if_spent(listing, cancel_tx_hash)
        return jsonify(payload), status

    if sale_tx_hash:
        listing, payload, status = _refreshable_listing_for_spend(normalized_name, sale_tx_hash)
        if not listing:
            return jsonify(payload), status
    else:
        listing = _refreshable_listing_for_name(normalized_name)

    if not listing or listing.is_expired():
        return jsonify({"error": "Listing not found"}), 404

    _, payload, status = _mark_listing_sold_if_spent(
        listing,
        sale_tx_hash or None,
        transfer_start_tx_hash or None,
    )
    return jsonify(payload), status


@api_bp.route('/v2/names/<name>/transfer-status', methods=['GET'])
def name_transfer_status(name):
    return jsonify(_name_transfer_status(name.lower().rstrip('/')))


@api_bp.route('/v2/tx/<tx_hash>/status', methods=['GET'])
def tx_status(tx_hash):
    tx, error = _fetch_hsd_tx(tx_hash)
    if error:
        message, status = error[:2]
        return jsonify({"error": message}), status

    chain_payload, status = get_hsd_status_payload()
    chain_height = chain_payload.get('height') if status == 200 else None
    tx_height = tx.get('height') if isinstance(tx, dict) else None
    confirmations = tx.get('confirmations') if isinstance(tx, dict) else None
    if confirmations is None and isinstance(chain_height, int) and isinstance(tx_height, int) and tx_height >= 0:
        confirmations = max(chain_height - tx_height + 1, 0)

    return jsonify({
        "hash": tx_hash,
        "found": True,
        "confirmed": isinstance(tx_height, int) and tx_height >= 0,
        "height": tx_height,
        "confirmations": confirmations,
        "block": tx.get('block') if isinstance(tx, dict) else None,
        "mtime": tx.get('mtime') if isinstance(tx, dict) else None,
        "fee": tx.get('fee') if isinstance(tx, dict) else None,
        "chainHeight": chain_height,
        "tx": tx,
    })


@api_bp.route('/v2/names/<name>/status', methods=['GET'])
def name_status(name):
    info, error = _fetch_hsd_name_info(name)
    if error:
        message, status = error[:2]
        return jsonify({"error": message}), status

    return jsonify({
        "name": name,
        "found": bool(isinstance(info, dict) and info.get('info')),
        "nameInfo": info,
    })


@api_bp.route('/v2/expiring-names', methods=['GET'])
def expiring_names():
    try:
        limit = min(max(int(request.args.get('limit', 100)), 1), 500)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    raw_names = request.args.get('names', '')
    requested_scope = request.args.get('scope', '').strip().lower()
    requested_status = request.args.get('status', 'active').strip().lower()
    refresh = request.args.get('refresh', '').lower() in {'1', 'true', 'yes'}
    network = request.args.get('network', 'main').strip().lower() or 'main'
    if requested_status == 'future':
        requested_status = 'active'
    if requested_status not in {'active', 'expired', 'all'}:
        return jsonify({"error": "status must be active, expired, all, or future"}), 400

    if requested_scope == 'global':
        names = []
        scope = 'global'
    elif requested_scope in {'community', 'community-observed', 'community-import'}:
        names = _community_expiring_watch_names(limit, network=network)
        scope = 'community-observed'
    elif requested_scope in {'observed', 'combined'}:
        market_names = _observed_market_names(limit)
        community_names = _community_expiring_watch_names(limit, network=network)
        names = _merge_expiring_name_lists(limit, market_names, community_names)
        scope = 'observed'
        _seed_expiring_watches(market_names, network=network)
    elif raw_names:
        names = []
        seen = set()
        for raw_name in raw_names.split(','):
            name = raw_name.strip().lower().rstrip('/')
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
            if len(names) >= limit:
                break
        scope = 'requested'
    else:
        market_names = _observed_market_names(limit)
        names = market_names
        scope = 'channel-observed'
        _seed_expiring_watches(market_names, network=network)

    chain_payload, chain_status = get_hsd_status_payload()
    chain = chain_payload.get('chain', {}) if chain_status == 200 else {}
    chain_height = chain_payload.get('height') if chain_status == 200 else None
    rows = []

    indexer_status = _name_indexer_status_payload()

    if scope == 'global':
        _refresh_global_expiring_window(limit=limit, network=network, status=requested_status)
        rows = _cached_global_expiring_names(limit, network=network, status=requested_status)
    elif scope == 'requested' or refresh:
        for name in names:
            payload = _expiring_name_payload(name, chain_height)
            source = 'requested' if scope == 'requested' else 'market-observed'
            if scope != 'requested':
                existing_watch = ExpiringNameWatch.query.filter_by(
                    name=name,
                    network=network,
                ).first()
                if existing_watch and existing_watch.source:
                    source = existing_watch.source
            watch = _store_expiring_watch(
                payload,
                chain_height=chain_height,
                network=network,
                source=source,
            )
            rows.append(_expiring_watch_payload(watch))

        db.session.commit()
    elif scope == 'community-observed':
        rows = _cached_expiring_watches_for_sources(limit, sources=['community-import'], network=network)
    elif scope == 'channel-observed':
        rows = _cached_expiring_watches_for_sources(limit, sources=['market-observed'], network=network)
    else:
        rows = _cached_expiring_watches(limit, network=network)

    if scope == 'global' and requested_status == 'expired':
        rows.sort(key=lambda row: (
            row.get('blocksUntilExpire') is None,
            -(row.get('blocksUntilExpire') if row.get('blocksUntilExpire') is not None else -10**18),
            row.get('name') or '',
        ))
    else:
        rows.sort(key=lambda row: (
            row.get('blocksUntilExpire') is None,
            row.get('blocksUntilExpire') if row.get('blocksUntilExpire') is not None else 10**18,
            row.get('name') or '',
        ))

    return jsonify({
        "scope": scope,
        "status": requested_status if scope == 'global' else None,
        "global": scope == 'global' and indexer_status.get('ready', False),
        "globalReason": (
            "Forward-only global discovery is active from the recorded index height. It does not include historical names before the watcher started."
            if scope == 'global' and indexer_status.get('ready', False) and not indexer_status.get('complete')
            else None
            if scope == 'global' and indexer_status.get('ready', False)
            else "Full global discovery needs the name-state indexer to finish before this feed is complete."
        ),
        "indexer": indexer_status,
        **_expiring_source_counts(network=network),
        "node": {
            "reachable": chain_payload.get('reachable', False),
            "height": chain_height,
            "progress": chain_payload.get('progress'),
            "spv": ((chain.get('options') or {}).get('spv') if isinstance(chain, dict) else None),
        },
        "total": len(rows),
        "names": rows,
    })


@api_bp.route('/v2/expiring-names/indexer-status', methods=['GET'])
def expiring_names_indexer_status():
    network = request.args.get('network', 'main').strip().lower() or 'main'
    chain_payload, chain_status = get_hsd_status_payload()
    chain = chain_payload.get('chain', {}) if chain_status == 200 else {}
    return jsonify({
        "success": True,
        "indexer": _name_indexer_status_payload(network=network),
        "node": {
            "reachable": chain_payload.get('reachable', False),
            "height": chain_payload.get('height') if chain_status == 200 else None,
            "progress": chain_payload.get('progress'),
            "spv": ((chain.get('options') or {}).get('spv') if isinstance(chain, dict) else None),
        },
    })



@api_bp.route('/v2/expiring-names/import', methods=['POST'])
@limiter.limit("10 per hour")
def import_expiring_names():
    if not current_app.config.get('MARKET_ADMIN_TOKEN'):
        return jsonify({"error": "MARKET_ADMIN_TOKEN must be configured for imports"}), 503

    auth_error = _require_market_admin()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True)
    raw_values = _expiring_import_values_from_request(data)
    if not raw_values:
        return jsonify({"error": "Provide names as JSON, newline text, or CSV text"}), 400

    network = 'main'
    source = 'community-import'
    refresh_now = str(
        (data or {}).get('refresh', request.args.get('refresh', 'false'))
        if isinstance(data, dict)
        else request.args.get('refresh', 'false')
    ).lower() in {'1', 'true', 'yes'}

    accepted = []
    seen = set()
    rejected = []
    truncated = False

    for raw_name in raw_values:
        if len(accepted) >= EXPIRING_IMPORT_MAX_NAMES:
            truncated = True
            break

        name, reason = _normalize_expiring_import_name(raw_name)
        if reason:
            if reason != 'header token':
                rejected.append({"name": str(raw_name), "reason": reason})
            continue
        if name in seen:
            continue
        seen.add(name)
        accepted.append(name)

    if not accepted:
        return jsonify({
            "error": "No valid names found",
            "rejected": rejected,
        }), 400

    inserted = 0
    updated = 0
    deduped = 0
    for name in accepted:
        watch = ExpiringNameWatch.query.filter_by(name=name, network=network).first()
        if watch is None:
            db.session.add(ExpiringNameWatch(name=name, network=network, source=source))
            inserted += 1
            continue

        if watch.source in {None, '', 'requested'}:
            watch.source = source
            watch.updated_at = datetime.utcnow()
            db.session.add(watch)
            updated += 1
        else:
            deduped += 1

    db.session.commit()

    refresh_result = None
    if refresh_now:
        refresh_result = _refresh_named_expiring_watches(
            accepted,
            source=source,
            network=network,
        )

    rows = (
        ExpiringNameWatch.query
        .filter(ExpiringNameWatch.network == network, ExpiringNameWatch.name.in_(accepted))
        .order_by(ExpiringNameWatch.name.asc())
        .all()
    )

    return jsonify({
        "success": True,
        "scope": "community-observed",
        "source": source,
        "network": network,
        "received": len(raw_values),
        "accepted": len(accepted),
        "inserted": inserted,
        "updated": updated,
        "deduped": deduped,
        "rejected": rejected,
        "truncated": truncated,
        "refresh": refresh_result,
        "names": [_expiring_watch_payload(row) for row in rows],
    })


@api_bp.route('/v2/expiring-names/refresh', methods=['POST'])
@limiter.limit("20 per hour")
def refresh_expiring_names():
    auth_error = _require_market_admin()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    try:
        limit = min(max(int(data.get('limit', request.args.get('limit', 100))), 1), 500)
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    stale_only = str(data.get('staleOnly', request.args.get('staleOnly', 'true'))).lower() not in {'0', 'false', 'no'}
    result = _refresh_expiring_watches(limit=limit, stale_only=stale_only)
    result.update({
        "success": True,
        "scope": "channel-observed",
        "global": False,
        "globalReason": "This refreshes the channel-observed expiring-name index. Broad global discovery still needs a full name-state indexer.",
    })
    return jsonify(result)


@api_bp.route('/upload-proof', methods=['POST'])
@limiter.limit("10 per hour")  # per IP
def upload_proof():
    if 'proof' not in request.files:
        return jsonify({"error": "No proof file"}), 400
    
    file = request.files['proof']
    description = sanitize_html(request.form.get('description', ''))
    gfavip_user_id = request.form.get('gfavip_user_id')  # optional
    
    # Save temp
    temp_path = os.path.join(current_app.config['UPLOAD_FOLDER'], file.filename)
    file.save(temp_path)
    
    try:
        with open(temp_path) as f:
            proof_data = json.load(f)
    except json.JSONDecodeError:
        os.remove(temp_path)
        return jsonify({"error": "Invalid JSON format"}), 400
    
    valid, msg = validate_shakedex_proof(proof_data)
    if not valid:
        os.remove(temp_path)
        return jsonify({"error": msg}), 400
    
    listing_fields = fixed_price_listing_fields(proof_data)
    if listing_fields['expires_at'] and listing_fields['expires_at'] < datetime.utcnow():
        os.remove(temp_path)
        return jsonify({"error": "This proof has already expired. Please create a fresh proof and upload it again."}), 400

    existing_active = (
        Listing.query
        .filter_by(name=listing_fields['name'], status='active')
        .order_by(Listing.created_at.desc())
        .first()
    )
    replacement_listing = None
    if existing_active and not existing_active.is_expired():
        existing_proof = existing_active.proof_json or {}
        same_listing_lock = (
            existing_proof.get('lockingTxHash') == proof_data.get('lockingTxHash')
            and existing_proof.get('lockingOutputIdx') == proof_data.get('lockingOutputIdx')
            and existing_proof.get('publicKey') == proof_data.get('publicKey')
        )
        if not same_listing_lock:
            os.remove(temp_path)
            return jsonify({
                "error": (
                    f"An active listing for {listing_fields['name']} already exists. "
                    "Cancel it or wait for it to expire before uploading a different listing proof."
                )
            }), 409
        replacement_listing = existing_active

    # Pin to IPFS after validation/replacement checks so rejected duplicates do not create pins.
    try:
        cid = pin_to_ipfs(temp_path)
    except Exception as e:
        os.remove(temp_path)
        return jsonify({"error": f"Failed to pin to IPFS: {str(e)}"}), 500
        
    os.remove(temp_path)

    listing = Listing(
        name=listing_fields['name'],
        price_hns=listing_fields['price_hns'],
        description=description,
        seller_hns_address=listing_fields['seller_hns_address'],
        gfavip_user_id=gfavip_user_id,
        ipfs_cid=cid,
        proof_json=proof_data,
        expires_at=listing_fields['expires_at']
    )
    
    try:
        if replacement_listing:
            replacement_listing.status = 'archived'
        db.session.add(listing)
        pending = PendingListing.query.filter_by(name=listing.name).first()
        if pending:
            pending.status = 'active'
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": f"A listing for {listing.name} already exists"}), 409
    
    send_gfavip_webhook(
        title="🎉 New HNS Listing!",
        message=f"**{listing.name}** — {listing.price_hns} HNS\n[View]({request.host_url}listing/{listing.name})"
    )
    
    return jsonify({
        "success": True,
        "name": listing.name,
        "cid": cid,
        "replaced": bool(replacement_listing),
        "previousListingId": replacement_listing.id if replacement_listing else None,
    }), 201
