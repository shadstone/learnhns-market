import json
import math
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import quote
from xml.sax.saxutils import escape

from flask import Blueprint, Response, current_app, make_response, jsonify, redirect, render_template, request, url_for
from app.blueprints.api import get_hsd_status_payload
from app.blueprints.api import _active_listing_for_name
from app.blueprints.api import _active_listings_unique_by_name
from app.blueprints.api import _fetch_explorer_tx
from app.blueprints.api import _fetch_hsd_tx
from app.blueprints.api import _fetch_hsd_name_info
from app.blueprints.api import _name_transfer_status
from app.blueprints.api import _mark_listing_sold_if_spent
from app.blueprints.api import _pending_listing_payload
from app.blueprints.api import _refreshable_listing_for_spend
from app.blueprints.api import _resolve_sale_pending_listing
from app.blueprints.api import _validate_hex_hash
from app.blueprints.api import SHAKEDEX_TRANSFER_LOCKUP
from app.marketplace_indexer import event_for_tx
from app.models import Listing, PendingListing
from app.watchers import top_watched_names, total_watcher_count, watched_name_count, watcher_count_for_name, watcher_counts_for_names

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    listings = _active_listings_unique_by_name()
    active_total = len(listings)
    active_names = {listing.name for listing in listings}
    pending_listings = [
        pending for pending in PendingListing.query.order_by(PendingListing.created_at.desc()).all()
        if pending.status not in {'active', 'cancelled', 'expired', 'failed'}
        and not pending.is_expired()
        and pending.name not in active_names
    ]
    pending_total = len(pending_listings)
    featured_listings = sorted(
        (listing for listing in listings if listing.is_featured_on_web()),
        key=lambda listing: (
            listing.featured_rank,
            -_datetime_sort_value(listing.created_at or datetime.min),
        ),
    )[:6]
    watcher_counts = watcher_counts_for_names(
        [listing.name for listing in listings] + [pending.name for pending in pending_listings]
    )
    listings, pending_listings, pagination = _paginated_market_rows(
        listings,
        pending_listings,
        watcher_counts,
    )
    return render_template(
        'index.html',
        listings=listings,
        pending_listings=[_pending_listing_payload(pending, refresh=False) for pending in pending_listings],
        watcher_counts=watcher_counts,
        hsd_readiness=_hsd_readiness(),
        pagination=pagination,
        active_total=active_total,
        pending_total=pending_total,
        featured_listings=featured_listings,
        show_featured=_show_featured_market_section(),
    )


def _paginated_market_rows(listings, pending_listings, watcher_counts, per_page=48):
    rows = []
    for listing in listings:
        rows.append({
            'kind': 'available',
            'record': listing,
            'name': listing.name,
            'price': Decimal(listing.price_hns),
            'created': listing.created_at or datetime.min,
        })
    for pending in pending_listings:
        rows.append({
            'kind': 'pending',
            'record': pending,
            'name': pending.name,
            'price': (
                Decimal(pending.expected_price) / Decimal(1_000_000)
                if pending.expected_price is not None
                else None
            ),
            'created': pending.created_at or datetime.min,
        })

    query = request.args.get('q', '').strip().lower()
    status = request.args.get('status', 'available')
    collection = request.args.get('collection', '').strip().lower()
    punycode = request.args.get('punycode', 'all')
    min_price = _optional_decimal_arg('min')
    max_price = _optional_decimal_arg('max')
    min_chars = _optional_int_arg('min_chars')
    max_chars = _optional_int_arg('max_chars')

    def matches(row):
        name = row['name'].lower()
        if query and query not in name and query not in _decoded_market_name(name).lower():
            return False
        if status in {'available', 'pending'} and row['kind'] != status:
            return False
        if collection == 'short' and len(name) > 3:
            return False
        if collection == 'under-500' and (row['price'] is None or row['price'] > Decimal('500')):
            return False
        if collection == 'emoji' and not name.startswith('xn--'):
            return False
        if collection == 'watched' and watcher_counts.get(row['name'], 0) < 1:
            return False
        if min_price is not None and (row['price'] is None or row['price'] < min_price):
            return False
        if max_price is not None and (row['price'] is None or row['price'] > max_price):
            return False
        if min_chars is not None and len(name) < min_chars:
            return False
        if max_chars is not None and len(name) > max_chars:
            return False
        is_punycode = name.startswith('xn--')
        if punycode == 'only' and not is_punycode:
            return False
        if punycode == 'hide' and is_punycode:
            return False
        return True

    rows = [row for row in rows if matches(row)]
    sort = request.args.get('sort', 'newest')
    if sort == 'watchers-desc':
        rows.sort(key=lambda row: (
            -watcher_counts.get(row['name'], 0),
            -_datetime_sort_value(row['created']),
            row['name'],
        ))
    elif sort == 'price-asc':
        rows.sort(key=lambda row: (row['price'] is None, row['price'] or Decimal(0), row['name']))
    elif sort == 'price-desc':
        rows.sort(key=lambda row: (row['price'] is None, -(row['price'] or Decimal(0)), row['name']))
    elif sort == 'length-asc':
        rows.sort(key=lambda row: (len(row['name']), row['name']))
    elif sort == 'length-desc':
        rows.sort(key=lambda row: (-len(row['name']), row['name']))
    elif sort == 'name-asc':
        rows.sort(key=lambda row: row['name'])
    else:
        rows.sort(key=lambda row: (row['created'], row['name']), reverse=True)

    total = len(rows)
    pages = max(1, math.ceil(total / per_page))
    page = min(max(_optional_int_arg('page') or 1, 1), pages)
    start = (page - 1) * per_page
    page_rows = rows[start:start + per_page]
    page_listings = [row['record'] for row in page_rows if row['kind'] == 'available']
    page_pending = [row['record'] for row in page_rows if row['kind'] == 'pending']

    return page_listings, page_pending, {
        'page': page,
        'pages': pages,
        'per_page': per_page,
        'total': total,
        'first': start + 1 if total else 0,
        'last': min(start + per_page, total),
        'previous_url': _market_page_url(page - 1) if page > 1 else None,
        'next_url': _market_page_url(page + 1) if page < pages else None,
    }


def _show_featured_market_section():
    ignored = {'page', 'view'}
    meaningful = {
        key: value
        for key, value in request.args.items()
        if key not in ignored and value not in {'', 'available', 'newest', 'all'}
    }
    return not meaningful and (_optional_int_arg('page') or 1) == 1


def _optional_decimal_arg(name):
    value = request.args.get(name, '').strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (ValueError, ArithmeticError):
        return None


def _optional_int_arg(name):
    value = request.args.get(name, '').strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _decoded_market_name(name):
    if not name.startswith('xn--'):
        return name
    try:
        return name.encode('ascii').decode('idna')
    except UnicodeError:
        return name


def _datetime_sort_value(value):
    return (
        value.toordinal() * 86400
        + value.hour * 3600
        + value.minute * 60
        + value.second
    )


def _market_page_url(page):
    args = request.args.to_dict()
    if page > 1:
        args['page'] = page
    else:
        args.pop('page', None)
    return url_for('main.index', **args)

@main_bp.route('/sold')
def sold():
    historical_statuses = ('sale-pending', 'sold', 'completed', 'archived', 'cancelled')
    listings = (
        Listing.query
        .filter(Listing.status.in_(historical_statuses))
        .order_by(Listing.created_at.desc())
        .all()
    )
    chain_height = _current_chain_height()
    for listing in listings:
        listing.sale_transfer_status = _sale_transfer_status(
            listing,
            chain_height=chain_height,
            allow_network=False,
        )
    return render_template('sold.html', listings=listings)


@main_bp.route('/pending')
def pending():
    active_names = {
        listing.name
        for listing in _active_listings_unique_by_name()
    }
    pending_listings = [
        pending for pending in PendingListing.query.order_by(PendingListing.created_at.desc()).all()
        if pending.status not in {'active', 'cancelled', 'expired', 'failed'}
        and not pending.is_expired()
        and pending.name not in active_names
    ]
    return render_template(
        'pending_list.html',
        pending_listings=[_pending_listing_payload(pending, refresh=False) for pending in pending_listings],
    )


@main_bp.route('/stats')
def stats():
    listings = _active_listings_unique_by_name()
    active_names = {listing.name for listing in listings}
    pending_listings = [
        pending for pending in PendingListing.query.order_by(PendingListing.created_at.desc()).all()
        if pending.status not in {'active', 'cancelled', 'expired', 'failed'}
        and not pending.is_expired()
        and pending.name not in active_names
    ]
    sold_listings = (
        Listing.query
        .filter(Listing.status.in_(('sold', 'completed')))
        .order_by(Listing.sold_at.desc(), Listing.created_at.desc())
        .all()
    )
    stats_payload = _market_stats_payload(listings, pending_listings, sold_listings)
    return render_template('stats.html', stats=stats_payload)


@main_bp.route('/upload')
def upload():
    return render_template('upload.html')

@main_bp.route('/docs')
def docs():
    return render_template('docs.html')


@main_bp.route('/how-it-works')
def how_it_works():
    return render_template('how_it_works.html')


@main_bp.route('/admin')
def admin():
    if not _has_admin_page_access():
        response = make_response(render_template('404.html'), 404)
        response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
        return response

    response = make_response(render_template('admin.html'))
    response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    return response


@main_bp.route('/robots.txt')
def robots_txt():
    body = "\n".join([
        "User-agent: *",
        "Disallow: /admin",
        "",
        "Sitemap: https://market.learnhns.com/sitemap.xml",
        "",
    ])
    return Response(body, mimetype='text/plain')


@main_bp.route('/sitemap.xml')
def sitemap_xml():
    base_url = 'https://market.learnhns.com'
    urls = [
        (url_for('main.index'), 'daily', '1.0'),
        (url_for('main.sold'), 'hourly', '0.8'),
        (url_for('main.pending'), 'hourly', '0.8'),
        (url_for('main.stats'), 'hourly', '0.8'),
        (url_for('main.how_it_works'), 'monthly', '0.6'),
        (url_for('main.docs'), 'monthly', '0.5'),
        (url_for('main.status'), 'daily', '0.4'),
        (url_for('main.llms_txt'), 'weekly', '0.4'),
        (url_for('main.skill_md'), 'weekly', '0.4'),
    ]
    for listing in Listing.query.order_by(Listing.created_at.desc()).limit(500).all():
        urls.append((url_for('main.listing_detail', name=listing.name), 'daily', '0.7'))
    for pending in PendingListing.query.order_by(PendingListing.created_at.desc()).limit(200).all():
        urls.append((url_for('main.listing_detail', name=pending.name), 'daily', '0.6'))

    entries = []
    seen = set()
    for path, changefreq, priority in urls:
        loc = f"{base_url}{path}"
        if loc in seen:
            continue
        seen.add(loc)
        entries.append(
            "  <url>\n"
            f"    <loc>{escape(loc)}</loc>\n"
            f"    <changefreq>{changefreq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            "  </url>"
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )
    return Response(body, mimetype='application/xml')


@main_bp.route('/llms.txt')
def llms_txt():
    return Response(_llms_txt(), mimetype='text/plain')


@main_bp.route('/skill.md')
@main_bp.route('/SKILL.md')
def skill_md():
    return Response(_skill_md(), mimetype='text/plain')


@main_bp.route('/status')
def status():
    status_data, _ = get_hsd_status_payload()
    progress = status_data.get('progress')
    progress_percent = None
    if isinstance(progress, (int, float)):
        progress_percent = max(0, min(100, progress * 100))

    return render_template(
        'status.html',
        status=status_data,
        progress_percent=progress_percent,
    )

@main_bp.route('/listing/<name>')
def listing_detail(name):
    normalized_name = name.lower().rstrip('/')
    listing_history = _listing_history(normalized_name)
    listing = _active_listing_for_name(normalized_name)
    if not listing:
        pending = (
            PendingListing.query
            .filter_by(name=normalized_name)
            .order_by(PendingListing.created_at.desc())
            .first()
        )
        if pending and pending.status not in {'active', 'cancelled', 'expired', 'failed'} and not pending.is_expired():
            return render_template(
                'pending.html',
                pending=_pending_listing_payload(pending),
                listing_history=listing_history,
                watcher_count=watcher_count_for_name(normalized_name),
                hsd_readiness=_hsd_readiness(),
            )

        listing = (
            Listing.query
            .filter_by(name=normalized_name)
            .order_by(Listing.created_at.desc())
            .first()
        )
        if not listing:
            return render_template(
                'name_profile.html',
                profile=_name_profile_payload(normalized_name),
                listing_history=listing_history,
                watcher_count=watcher_count_for_name(normalized_name),
            )

    listing_is_expired = listing.is_expired()
    listing_display_status = 'expired' if listing_is_expired else listing.status
    listing_expires_at = listing.effective_expires_at()

    bob_deep_link = None
    if listing.status == 'active' and not listing_is_expired:
        proof_json = json.dumps(listing.proof_json, separators=(',', ':'))
        bob_deep_link = (
            f"bob-learnhns://x/fulfillauction?name={quote(listing.name, safe='')}"
            f"&presign={quote(proof_json, safe='')}"
        )

    return render_template(
        'listing.html',
        listing=listing,
        listing_display_status=listing_display_status,
        listing_expires_at=listing_expires_at,
        sale_transfer_status=_name_transfer_status(normalized_name),
        bob_deep_link=bob_deep_link,
        listing_history=listing_history,
        watcher_count=watcher_count_for_name(normalized_name),
        hsd_readiness=_hsd_readiness(),
    )


@main_bp.route('/listing/<name>/success')
def listing_success(name):
    normalized_name = name.lower().rstrip('/')
    tx_hash, tx_hash_error = _validate_hex_hash(request.args.get('tx'), 'tx')
    listing = (
        Listing.query
        .filter_by(name=normalized_name)
        .order_by(Listing.created_at.desc())
        .first()
    )
    market_sync = None

    if not listing:
        return redirect(url_for('main.listing_detail', name=normalized_name))

    if tx_hash_error:
        message, _status = tx_hash_error
        market_sync = {
            'status': 'error',
            'message': message,
        }
    elif tx_hash:
        refresh_listing, payload, status = _refreshable_listing_for_spend(
            normalized_name,
            tx_hash,
        )
        if refresh_listing:
            _sold, payload, status = _mark_listing_sold_if_spent(
                refresh_listing,
                tx_hash,
                tx_hash,
            )
            listing = refresh_listing

        if status == 200 and payload and payload.get('sold'):
            market_sync = {
                'status': 'success',
                'message': 'This sale has been recorded in LearnHNS Market.',
            }
        else:
            current_app.logger.warning(
                "Could not record purchase success tx for %s (%s): %s",
                normalized_name,
                tx_hash,
                payload,
            )
            market_sync = {
                'status': 'pending',
                'message': 'The purchase transaction was accepted, but LearnHNS Market has not verified the sale record yet.',
            }

    return render_template(
        'purchase_success.html',
        listing=listing,
        tx_hash=tx_hash,
        market_sync=market_sync,
    )


@main_bp.route('/listing/<name>/proof.json')
def listing_proof(name):
    normalized_name = name.lower().rstrip('/')
    listing = _active_listing_for_name(normalized_name)
    if listing is None:
        return jsonify({"error": "Active listing not found"}), 404
    return jsonify(listing.proof_json)


@main_bp.route('/pending/<name>')
def pending_detail(name):
    return redirect(url_for('main.listing_detail', name=name), code=301)


def _listing_history(name):
    historical_statuses = ('sale-pending', 'sold', 'completed', 'archived', 'cancelled')
    return (
        Listing.query
        .filter(Listing.name == name, Listing.status.in_(historical_statuses))
        .order_by(Listing.created_at.desc())
        .all()
    )


def _market_stats_payload(active_listings, pending_listings, sold_listings):
    now = datetime.utcnow()
    cutoff_30d = now - timedelta(days=30)
    sales_30d = [
        listing for listing in sold_listings
        if _sale_date(listing) and _sale_date(listing) >= cutoff_30d
    ]
    active_inventory_value = sum((listing.price_hns for listing in active_listings), Decimal('0'))
    all_time_volume = sum((listing.price_hns for listing in sold_listings), Decimal('0'))
    volume_30d = sum((listing.price_hns for listing in sales_30d), Decimal('0'))
    monthly_volume = _monthly_sales_volume(sold_listings)
    most_watched = _most_watched_payload(active_listings, pending_listings)
    most_watched_active = _most_watched_payload(active_listings, pending_listings, scope='active')
    most_watched_inactive = _most_watched_payload(active_listings, pending_listings, scope='inactive')

    return {
        'activeListingCount': len(active_listings),
        'pendingListingCount': len(pending_listings),
        'activeInventoryValue': active_inventory_value,
        'completedSaleCount': len(sold_listings),
        'allTimeVolume': all_time_volume,
        'volume30d': volume_30d,
        'totalWatcherCount': total_watcher_count(),
        'watchedNameCount': watched_name_count(),
        'topSalesAllTime': _top_sales_payload(sold_listings, limit=10),
        'topSales30d': _top_sales_payload(sales_30d, limit=10),
        'mostWatched': most_watched,
        'mostWatchedActive': most_watched_active,
        'mostWatchedInactive': most_watched_inactive,
        'monthlyVolume': monthly_volume,
    }


def _sale_date(listing):
    return listing.sold_at or listing.created_at


def _top_sales_payload(listings, limit=10):
    sorted_sales = sorted(
        listings,
        key=lambda listing: (listing.price_hns or Decimal('0'), _sale_date(listing) or datetime.min),
        reverse=True,
    )
    return [
        {
            'name': listing.name,
            'priceHns': listing.price_hns,
            'soldAt': _sale_date(listing),
            'status': listing.status,
            'url': url_for('main.listing_detail', name=listing.name),
        }
        for listing in sorted_sales[:limit]
    ]


def _monthly_sales_volume(listings):
    buckets = {}
    for listing in listings:
        sale_date = _sale_date(listing)
        if not sale_date:
            continue
        key = sale_date.strftime('%Y-%m')
        bucket = buckets.setdefault(key, {'month': key, 'saleCount': 0, 'volumeHns': Decimal('0')})
        bucket['saleCount'] += 1
        bucket['volumeHns'] += listing.price_hns or Decimal('0')

    return [
        buckets[key]
        for key in sorted(buckets.keys(), reverse=True)[:12]
    ]


def _most_watched_payload(active_listings, pending_listings, scope='all'):
    active_by_name = {listing.name: listing for listing in active_listings}
    pending_by_name = {pending.name: pending for pending in pending_listings}
    active_names = set(active_by_name.keys())
    if scope == 'active':
        watched = top_watched_names(limit=10, names=active_names)
    elif scope == 'inactive':
        watched = top_watched_names(limit=10, exclude_names=active_names)
    else:
        watched = top_watched_names(limit=10)
    rows = []
    for row in watched:
        name = row['name']
        active = active_by_name.get(name)
        pending = pending_by_name.get(name)
        latest = None
        if not active and not pending:
            latest = (
                Listing.query
                .filter_by(name=name)
                .order_by(Listing.created_at.desc())
                .first()
            )
        status = 'active' if active else 'pending' if pending else latest.status if latest else 'profile'
        price = active.price_hns if active else None
        if pending and pending.expected_price:
            price = Decimal(pending.expected_price) / Decimal('1000000')
        if latest and price is None:
            price = latest.price_hns
        rows.append({
            'name': name,
            'watcherCount': row['watcherCount'],
            'status': status,
            'priceHns': price,
            'url': url_for('main.listing_detail', name=name),
        })
    return rows


def _has_admin_page_access():
    token = current_app.config.get('MARKET_ADMIN_TOKEN')
    if not token:
        return True

    submitted = (
        request.headers.get('X-Market-Admin-Token')
        or request.args.get('adminToken')
    )
    return submitted == token


def _llms_txt():
    return """# LearnHNS Market

LearnHNS Market is a public Shakedex channel for fixed-price Handshake name listings.

Canonical site: https://market.learnhns.com/
Browse listings: https://market.learnhns.com/
How it works: https://market.learnhns.com/how-it-works
Sale history: https://market.learnhns.com/sold
Pending listings: https://market.learnhns.com/pending
Human login: https://market.learnhns.com/login
Agent skill: https://market.learnhns.com/skill.md
Shakedex channels: https://shakedex.org/markets

Agents can read public listings, pending listings, sale history, and listing detail pages without logging in. Buying requires a compatible Handshake wallet that can validate and fill Shakedex proofs, such as Bob LearnHNS or another wallet with HSD access.

Use the public API only for read or wallet-preparation tasks unless the user explicitly authorizes an action. Never submit a purchase, listing, cancellation, or watchlist action without the user's explicit instruction and wallet confirmation.
"""


def _skill_md():
    return """# LearnHNS Market Agent Skill

Use this skill when an AI agent needs to inspect, explain, or help a human use LearnHNS Market, a Shakedex channel for Handshake names.

## What This App Does

- Lists fixed-price Handshake names for sale using Shakedex proofs.
- Shows pending listings whose transfer lockup is not complete yet.
- Shows sold listings and, when available, transfer-start/finalize status using Handshake chain data.
- Lets signed-in humans save watchlists and renewal alerts.

## Public URLs

- Browse: https://market.learnhns.com/
- How it works: https://market.learnhns.com/how-it-works
- Sold history: https://market.learnhns.com/sold
- Pending listings: https://market.learnhns.com/pending
- Docs: https://market.learnhns.com/docs
- Login page: https://market.learnhns.com/login
- Machine-readable overview: https://market.learnhns.com/llms.txt

## Login Flow

When a task requires a watchlist, renewal alert, saved account settings, or another account feature:

1. Send the human to `/login?next=<encoded destination>`.
2. Explain that LearnHNS Market uses GFAVIP SSO for accounts.
3. The human clicks the GFAVIP login button and completes login on GFAVIP.
4. GFAVIP redirects back to LearnHNS Market with an account session cookie.

Do not ask the human for their GFAVIP password or token. Do not try to automate private account login unless the human is present and explicitly asks for browser assistance.

## Buying With a Wallet

An AI agent can help prepare a purchase by finding the listing page and explaining the flow, but the wallet owner must approve the transaction.

If the agent has access to an HSD wallet and the human explicitly asks it to buy:

1. Fetch the listing detail page or proof JSON.
2. Verify the proof network, name, price, lock time, and payment address.
3. Verify the listing is still active and not expired.
4. Ask for explicit human confirmation of the exact name and HNS amount.
5. Use the wallet's Shakedex fill flow to create and broadcast the fill transaction.
6. Record the transaction hash and remind the human that Handshake transfers require a 288-block wait before buyer finalization.

Never buy, cancel, finalize, or submit a listing without explicit user approval.

## Seller/Sale Status

For sold names, the deterministic transfer status is:

`transfer_start_height + 288 - current_height`

If the transfer-start transaction is not recorded, the site should say that plainly instead of guessing. A buyer finalize only counts after the 288-block transfer lockup is complete.
"""


def _current_chain_height():
    chain_payload, chain_status = get_hsd_status_payload()
    chain_height = chain_payload.get('height') if chain_status == 200 else None
    return chain_height if isinstance(chain_height, int) else None


def _sale_transfer_status(listing, chain_height=None, allow_network=True):
    if listing.status not in {'sold', 'completed'}:
        return None

    transfer_start_tx_hash = (listing.transfer_start_tx_hash or '').lower()
    if not transfer_start_tx_hash:
        return {
            "label": "Transfer start tx not recorded",
            "tone": "pending",
        }

    tx_status = _sale_tx_transfer_status(
        transfer_start_tx_hash,
        finalize_tx_hash=(listing.sale_tx_hash or '').lower(),
        name=listing.name,
        chain_height=chain_height,
        allow_network=allow_network,
    )
    if tx_status:
        return tx_status

    return {
        "label": "Checking buyer finalize status",
        "tone": "pending",
    }


def _sale_tx_transfer_status(
    transfer_start_tx_hash,
    finalize_tx_hash=None,
    name=None,
    chain_height=None,
    allow_network=True,
):
    transfer_event = event_for_tx(transfer_start_tx_hash, name=name, action='TRANSFER')
    if transfer_event and isinstance(transfer_event.block_height, int):
        return _sale_transfer_status_from_height(
            transfer_event.block_height,
            transfer_start_tx_hash,
            finalize_tx_hash=finalize_tx_hash,
            name=name,
            chain_height=chain_height,
            allow_network=allow_network,
        )

    if not allow_network:
        return {
            "label": "Transfer start tx not indexed",
            "tone": "pending",
        }

    tx, tx_error = _fetch_hsd_tx(transfer_start_tx_hash)
    if tx_error:
        tx, tx_error = _fetch_explorer_tx(transfer_start_tx_hash)
    if tx_error or not isinstance(tx, dict):
        return None

    tx_height = tx.get('height')
    if not isinstance(tx_height, int) or tx_height < 0:
        return {
            "label": "Sale tx waiting for confirmation",
            "tone": "pending",
        }

    return _sale_transfer_status_from_height(
        tx_height,
        transfer_start_tx_hash,
        finalize_tx_hash=finalize_tx_hash,
        name=name,
        chain_height=chain_height,
        allow_network=allow_network,
    )


def _sale_transfer_status_from_height(
    tx_height,
    transfer_start_tx_hash,
    finalize_tx_hash=None,
    name=None,
    chain_height=None,
    allow_network=True,
):
    if (
        finalize_tx_hash
        and finalize_tx_hash != transfer_start_tx_hash
        and _tx_has_name_covenant(
            finalize_tx_hash,
            name,
            'FINALIZE',
            min_height=tx_height + SHAKEDEX_TRANSFER_LOCKUP,
            allow_network=allow_network,
        )
    ):
        return {
            "label": "Buyer finalized transfer",
            "tone": "complete",
        }

    if not isinstance(chain_height, int):
        chain_height = _current_chain_height() if allow_network else None
    if not isinstance(chain_height, int):
        return {
            "label": "Checking buyer finalize status",
            "tone": "pending",
        }

    unlock_height = tx_height + SHAKEDEX_TRANSFER_LOCKUP
    blocks = max(unlock_height - chain_height, 0)
    if blocks > 0:
        return {
            "label": f"Buyer finalize in {blocks} blocks",
            "tone": "pending",
        }

    return {
        "label": "288-block transfer wait complete; buyer can finalize",
        "tone": "ready",
    }


def _tx_has_name_covenant(tx_hash, name, covenant_action, min_height=None, allow_network=True):
    if not tx_hash or not name:
        return False

    indexed_event = event_for_tx(tx_hash, name=name, action=covenant_action)
    if indexed_event:
        if min_height is None or (
            isinstance(indexed_event.block_height, int)
            and indexed_event.block_height >= min_height
        ):
            return True
        return False

    if not allow_network:
        return False

    tx, tx_error = _fetch_hsd_tx(tx_hash)
    if tx_error:
        tx, tx_error = _fetch_explorer_tx(tx_hash)
    if tx_error or not isinstance(tx, dict):
        return False

    if min_height is not None:
        tx_height = tx.get('height')
        if not isinstance(tx_height, int) or tx_height < min_height:
            return False

    expected_name = name.lower().rstrip('/')
    expected_action = covenant_action.upper()
    for output in tx.get('outputs', []):
        if not isinstance(output, dict):
            continue

        covenant = output.get('covenant') if isinstance(output.get('covenant'), dict) else {}
        action = str(
            covenant.get('action')
            or covenant.get('type')
            or output.get('action')
            or ''
        ).upper()

        output_name = str(
            covenant.get('name')
            or output.get('name')
            or ''
        ).lower().rstrip('/')

        if action == expected_action and output_name == expected_name:
            return True

    return False


def _name_profile_payload(name):
    info, error = _fetch_hsd_name_info(name)
    if error:
        message, status = error[:2]
        return {
            "name": name,
            "found": False,
            "error": message,
            "statusCode": status,
            "info": {},
            "stats": {},
            "start": {},
        }

    payload = info if isinstance(info, dict) else {}
    name_info = payload.get('info') if isinstance(payload.get('info'), dict) else {}
    start_info = payload.get('start') if isinstance(payload.get('start'), dict) else {}
    stats = name_info.get('stats') if isinstance(name_info.get('stats'), dict) else {}
    return {
        "name": name,
        "found": bool(name_info),
        "info": name_info,
        "start": start_info,
        "stats": stats,
        "state": name_info.get('state'),
        "registered": name_info.get('registered'),
        "expired": name_info.get('expired'),
        "reserved": start_info.get('reserved'),
        "locked": start_info.get('locked'),
        "owner": name_info.get('owner') if isinstance(name_info.get('owner'), dict) else None,
        "renewalHeight": name_info.get('renewal'),
        "transferHeight": name_info.get('transfer'),
        "blocksUntilExpire": stats.get('blocksUntilExpire'),
        "daysUntilExpire": stats.get('daysUntilExpire'),
        "valueHns": name_info.get('value') / 1000000 if isinstance(name_info.get('value'), int) else None,
        "highestHns": name_info.get('highest') / 1000000 if isinstance(name_info.get('highest'), int) else None,
    }


def _hsd_readiness():
    status_data, _ = get_hsd_status_payload()
    progress = status_data.get('progress')
    progress_percent = None
    if isinstance(progress, (int, float)):
        progress_percent = max(0, min(100, progress * 100))

    return {
        "reachable": status_data.get('reachable', False),
        "ready": status_data.get('reachable', False) and isinstance(progress, (int, float)) and progress >= 0.99,
        "height": status_data.get('height'),
        "progress_percent": progress_percent,
        "error": status_data.get('error'),
    }
