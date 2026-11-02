"""Bounded, fail-closed bridge to hsd's offline Shakedex script verifier."""
import json
import os
from pathlib import Path
import subprocess

VERIFIER = Path(__file__).resolve().parent.parent / 'scripts' / 'verify-shakedex-proof.js'


def verify_proof_signatures(proof, coin, network):
    try:
        result = subprocess.run(
            ['node', str(VERIFIER)],
            input=json.dumps({'proof': proof, 'coin': coin, 'network': network}),
            text=True, capture_output=True, timeout=5, check=True,
            env={'PATH': os.environ.get('PATH', ''), 'NODE_BACKEND': 'js'},
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False, 'Proof verifier is unavailable; please retry later', 503
    if not isinstance(payload, dict) or payload.get('valid') is not True:
        return False, 'Proof signature or Shakedex locking script is invalid', 400
    return True, None, 200
