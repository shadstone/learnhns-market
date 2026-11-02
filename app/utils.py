import requests
import bleach
from flask import current_app
from decimal import Decimal
from datetime import datetime
import re

HNS_BASE_UNITS = Decimal("1000000")
HEX_RE = re.compile(r'^[a-f0-9]+$', re.IGNORECASE)
ADDRESS_RE = re.compile(r'^(hs|rs|ts|ss)1[a-zA-HJ-NP-Z0-9]{25,39}$', re.IGNORECASE)
NETWORK_PREFIXES = {
    'main': 'hs',
    'regtest': 'rs',
    'testnet': 'ts',
    'simnet': 'ss',
}

def sanitize_html(text):
    if not text:
        return ""
    return bleach.clean(text, tags=['b', 'i', 'a', 'br'], strip=True)

def send_gfavip_webhook(title: str, message: str, color: str = "#10b981"):
    url = current_app.config.get('GFAVIP_WEBHOOK_URL')
    if not url:
        return
    payload = {
        "title": title,
        "message": message,
        "color": color
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except:
        pass  # fail gracefully

def pin_to_ipfs(file_path: str) -> str:
    url = "https://api.pinata.cloud/pinning/pinFileToIPFS"
    headers = {
        "pinata_api_key": current_app.config['PINATA_API_KEY'],
        "pinata_secret_api_key": current_app.config['PINATA_SECRET_KEY']
    }
    with open(file_path, 'rb') as f:
        response = requests.post(url, files={'file': f}, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()['IpfsHash']

def _is_hex(value, length):
    return isinstance(value, str) and len(value) == length and bool(HEX_RE.match(value))

def _address_network(address):
    match = ADDRESS_RE.match(address or '')
    if not match:
        return None
    prefix = match.group(1).lower()
    for network, network_prefix in NETWORK_PREFIXES.items():
        if prefix == network_prefix:
            return network
    return None

def _validate_allowed_network(address, label):
    network = _address_network(address)
    if not network:
        return False, f"Invalid {label}"

    allowed_networks = current_app.config.get('ALLOWED_PROOF_NETWORKS', ['main'])
    if network not in allowed_networks:
        allowed = ', '.join(allowed_networks)
        return False, f"{label} uses {network}, but this environment only accepts: {allowed}"

    return True, network

def validate_shakedex_proof(proof_data: dict) -> tuple[bool, str]:
    try:
        required = [
            'version',
            'name',
            'lockingTxHash',
            'lockingOutputIdx',
            'publicKey',
            'paymentAddr',
            'data',
        ]
        if not all(k in proof_data for k in required):
            return False, "Missing required Shakedex proof fields"
        if proof_data['version'] != 2:
            return False, "Unsupported Shakedex proof version"
        if (not isinstance(proof_data['name'], str)
                or not re.fullmatch(r'[a-z0-9_-]{1,63}', proof_data['name'])):
            return False, "Invalid name"
        if not _is_hex(proof_data['lockingTxHash'], 64):
            return False, "Invalid locking transaction hash"
        if not isinstance(proof_data['lockingOutputIdx'], int) or proof_data['lockingOutputIdx'] < 0:
            return False, "Invalid locking output index"
        if not _is_hex(proof_data['publicKey'], 66):
            return False, "Invalid public key"
        ok, seller_network = _validate_allowed_network(
            proof_data['paymentAddr'],
            'Seller payment address',
        )
        if not ok:
            return False, seller_network
        if proof_data.get('feeAddr'):
            ok, fee_network = _validate_allowed_network(
                proof_data['feeAddr'],
                'Fee address',
            )
            if not ok:
                return False, fee_network
            if fee_network != seller_network:
                return False, "Fee address network must match seller payment address network"
        if not isinstance(proof_data['data'], list) or len(proof_data['data']) != 1:
            return False, "Fixed-price listings must contain exactly one proof entry"

        bid = proof_data['data'][0]
        bid_required = ['price', 'lockTime', 'signature']
        if not all(k in bid for k in bid_required):
            return False, "Missing fixed-price proof entry fields"
        if not isinstance(bid['price'], int) or bid['price'] <= 0:
            return False, "Invalid fixed price"
        if not isinstance(bid['lockTime'], int) or bid['lockTime'] < 0:
            return False, "Invalid lock time"
        if bid['lockTime'] > int(datetime.utcnow().timestamp()) + 7200:
            return False, "Fixed-price bid lock time is in the future. Create a new proof with the current MTP as the bid lockTime and use expiresAt for listing expiration."
        if not _is_hex(bid['signature'], 130):
            return False, "Invalid proof signature"
        if 'fee' in bid and (not isinstance(bid['fee'], int) or bid['fee'] < 0):
            return False, "Invalid fee"
        if 'expiresAt' in proof_data and (type(proof_data['expiresAt']) is not int or not 0 < proof_data['expiresAt'] <= 253402300799):
            return False, "Invalid listing expiration"

        return True, "Valid"
    except Exception as e:
        return False, str(e)

def fixed_price_listing_fields(proof_data: dict) -> dict:
    bid = proof_data['data'][0]
    expires_at = None
    expires_at_timestamp = proof_data.get('expiresAt')
    if isinstance(expires_at_timestamp, int) and expires_at_timestamp > 0:
        expires_at = datetime.utcfromtimestamp(expires_at_timestamp)

    return {
        'name': proof_data['name'],
        'price_hns': Decimal(bid['price']) / HNS_BASE_UNITS,
        'seller_hns_address': proof_data['paymentAddr'],
        'expires_at': expires_at,
    }
