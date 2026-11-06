"""Bounded smoke check for compose.regtest.yaml; no production URLs or funds."""
import base64
import json
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

NODE = 'http://127.0.0.1:14037/'
MARKET = 'http://127.0.0.1:18000/api/v2/hsd/status'
AUTH = 'Basic ' + base64.b64encode(b'x:local-regtest-only').decode()


def read(url, body=None):
    req = Request(url, data=json.dumps(body).encode() if body else None,
                  headers={'Authorization': AUTH, 'Content-Type': 'application/json'})
    with urlopen(req, timeout=3) as response:
        return json.load(response)


def ready(url):
    for attempt in range(30):
        try:
            result = read(url)
            if result.get('network') == 'regtest':
                return result
            raise AssertionError('Sandbox must report regtest')
        except URLError:
            time.sleep(1)
    raise RuntimeError('Regtest sandbox did not become ready')


ready(NODE)
result = read(NODE, {'method': 'generatetoaddress', 'params': [1, 'rs1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqn6kda'], 'id': 1})
assert not result.get('error'), result
status = ready(MARKET)
assert status['reachable'] and status['progress'] == 1, status
print('Local Docker market and hsd are reachable on regtest with progress 1')
