"""Versioned authorization for extending an existing listing's board expiry."""
import hashlib

DOMAIN = 'LearnHNS Market listing renewal v1'
MAX_RENEWAL_SECONDS = 365 * 24 * 60 * 60


def renewal_message(network, name, tx_hash, output_index, previous_expiry, expiry):
    # Fixed ASCII fields, decimal integers, LF separators, no trailing LF.
    return '\n'.join((DOMAIN, network, name, tx_hash, str(output_index),
                      str(previous_expiry), str(expiry)))


def renewal_digest(network, name, tx_hash, output_index, previous_expiry, expiry):
    return hashlib.sha256(renewal_message(network, name, tx_hash, output_index,
                                         previous_expiry, expiry).encode('ascii')).hexdigest()
