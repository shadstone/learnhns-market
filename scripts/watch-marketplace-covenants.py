#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.marketplace_indexer import index_listing_hashes, mark_progress_failed, scan_market_blocks


def main():
    parser = argparse.ArgumentParser(description="Index marketplace-relevant Handshake TRANSFER and FINALIZE covenants.")
    parser.add_argument("--lookback", type=int, default=720)
    parser.add_argument("--max-blocks", type=int, default=720)
    parser.add_argument("--start-height", type=int)
    parser.add_argument("--end-height", type=int)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--hash-refresh-seconds", type=int, default=3600)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        next_hash_refresh_at = 0
        while True:
            cycle_error = None
            try:
                now = time.monotonic()
                hash_results = []
                if now >= next_hash_refresh_at:
                    hash_results = index_listing_hashes()
                    next_hash_refresh_at = now + max(args.hash_refresh_seconds, args.poll_seconds)
                block_result = scan_market_blocks(
                    start_height=args.start_height,
                    end_height=args.end_height,
                    lookback=args.lookback,
                    max_blocks=args.max_blocks,
                )
                print(json.dumps({
                    "success": True,
                    "hashes": {
                        "checked": len(hash_results),
                        "eventsIndexed": sum(result.get("indexed", 0) for result in hash_results),
                        "errors": [
                            result
                            for result in hash_results
                            if result.get("error")
                        ],
                    },
                    "blocks": block_result,
                }, indent=2, sort_keys=True), flush=True)
            except Exception as exc:
                cycle_error = exc
                mark_progress_failed(exc)
                print(json.dumps({
                    "success": False,
                    "error": str(exc),
                }, sort_keys=True), file=sys.stderr, flush=True)
                traceback.print_exc()

            if args.once:
                if cycle_error:
                    raise cycle_error
                break

            args.start_height = None
            args.end_height = None
            args.lookback = max(12, min(args.lookback, 120))
            args.max_blocks = max(12, min(args.max_blocks, 120))
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
