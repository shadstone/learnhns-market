"""Filter and order the complete channel snapshot before slicing a page."""
import re
import time


def display_label(name):
    try:
        return name.encode('ascii').decode('idna') if name.startswith('xn--') else name
    except UnicodeError:
        return name


def auction_mode(row):
    if row.get('pending'):
        return {'fixed-price': 'fixed', 'reverse-auction': 'reverse'}.get(row.get('listingMode'), 'unknown')
    return 'fixed' if len(row.get('bids') or []) == 1 else 'reverse'


def estimated_price(row, now):
    if row.get('pending'):
        return row.get('expectedPrice')
    bids = row.get('bids') or []
    if len(bids) == 1:
        return bids[0].get('price')
    # Proof lockTimes are seconds. HSD encodes them in 512-second units.
    # This browse estimate uses wall time; Bob verifies against MTP before buying.
    eligible = [bid['price'] for bid in bids
                if type(bid.get('price')) is int and type(bid.get('lockTime')) is int
                and (bid['lockTime'] // 512) * 512 < now]
    return min(eligible) if eligible else None


def filter_auctions(rows, args, now=None):
    sort = args.get('sort', 'default')
    if sort not in {'default', 'name', 'name-asc', 'name-desc', 'price-asc', 'price-desc',
                    'length-asc', 'length-desc', 'newest', 'mode'}:
        raise ValueError('Invalid sort')
    mode = args.get('mode', 'all')
    numbers = args.get('numbers', 'any')
    hyphens = args.get('hyphens', 'any')
    if mode not in {'all', 'fixed', 'reverse'}:
        raise ValueError('Invalid listing mode')
    if numbers not in {'any', 'exclude', 'include', 'only'}:
        raise ValueError('Invalid numbers filter')
    if hyphens not in {'any', 'exclude', 'include'}:
        raise ValueError('Invalid hyphens filter')
    limits = []
    for key in ('min_length', 'max_length'):
        value = args.get(key, '')
        if value == '':
            limits.append(None)
        elif not re.fullmatch(r'[0-9]{1,2}', str(value)) or not 1 <= int(value) <= 63:
            raise ValueError(f'{key} must be between 1 and 63')
        else:
            limits.append(int(value))
    minimum, maximum = limits
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError('Minimum length must not exceed maximum length')
    query = args.get('q', '').strip().lower().rstrip('/')
    now = time.time() if now is None else now
    result = []
    for row in rows:
        label = display_label(row['name'])
        has_numbers = bool(re.search('[0-9]', label))
        if query and query not in row['name'].lower() and query not in label.lower():
            continue
        if minimum is not None and len(label) < minimum:
            continue
        if maximum is not None and len(label) > maximum:
            continue
        if mode != 'all' and auction_mode(row) != mode:
            continue
        if (numbers == 'exclude' and has_numbers or numbers == 'include' and not has_numbers
                or numbers == 'only' and not re.fullmatch('[0-9]+', label)):
            continue
        if hyphens == 'exclude' and '-' in label or hyphens == 'include' and '-' not in label:
            continue
        result.append(row)
    # Stable secondary ordering prevents equal-price/length rows jumping pages.
    name_key = lambda row: (display_label(row['name']).lower(), row['name'], str(row['id']))
    if sort != 'default':
        result.sort(key=name_key)
    if sort in {'name-desc'}:
        result.reverse()
    elif sort in {'length-asc', 'length-desc'}:
        result.sort(key=lambda row: len(display_label(row['name'])), reverse=sort == 'length-desc')
    elif sort in {'price-asc', 'price-desc'}:
        def price_key(row):
            price = estimated_price(row, now)
            return (price is None, (price if sort == 'price-asc' else -price) if price is not None else 0)
        result.sort(key=price_key)
    elif sort == 'newest':
        result.sort(key=lambda row: row.get('createdAt') or '', reverse=True)
    elif sort == 'mode':
        result.sort(key=auction_mode)
    return result
