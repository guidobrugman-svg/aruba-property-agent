"""Bounded source-specific public HTML collectors. No guessed pagination."""
import hashlib
import json
import re
import xml.etree.ElementTree as ET
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup


class ParserError(ValueError):
    pass


def page_links(soup, base, source):
    selector = source.get('pagination_selector', '.pagination a[href], a.next[href], a[rel="next"]')
    result = []
    for a in soup.select(selector):
        if not a.get('href'):
            continue
        url = urljoin(base, a['href'])
        if urlparse(url).netloc == urlparse(base).netloc and url not in result:
            result.append(url)
    if source.get('name') == 'Ben Real Estate':
        # Source advertises these AJAX page numbers; its GET page endpoint was verified.
        for a in soup.select('[data-request-data]'):
            match = re.search(r'page:\s*(\d+)', a['data-request-data'])
            if match:
                url = base.split('?')[0] + '?page=' + match[1]
                if url not in result:
                    result.append(url)
    return result


def parse_page(html, url, source, api):
    items, observations = [], []
    name = source['name']
    if '<rss ' in html[:300]:
        root = ET.fromstring(html)
        entries = root.findall('./channel/item')
        if not entries:
            raise ParserError('RSS feed has no listing entries')
        for entry in entries:
            raw_title = entry.findtext('title', '')
            title = raw_title.split(' :: ')[0].strip()
            href = entry.findtext('link', '')
            desc = BeautifulSoup(entry.findtext('description', ''), 'html.parser').get_text(' ', strip=True)
            text = 'For Sale ' + desc
            status = api.extract_status(title + ' ' + text)
            observations.append({'url': href, 'name': title, 'status': status})
            price, meta = api.parse_price_details(raw_title)
            p = api.build_property(name, title, href, price, text, '', meta)
            if p:
                p.update(source_type=source['type'], published_or_updated_at=entry.findtext('pubDate', ''))
                items.append(p)
        return items, observations, len(entries), BeautifulSoup('', 'html.parser')
    soup = BeautifulSoup(html, 'html.parser')
    if source.get('adapter') == 'myhome':
        match = re.search(r'var MyHomeListing\d+ = (\{.*?\});', html, re.S)
        if match:
            data = json.loads(match[1])
        else:
            raw = json.loads(html)
            if not isinstance(raw.get('results'), list):
                raise ParserError('Missing MyHome listing records')
            data = {'results': {'estates': raw['results'], 'totalResults': raw['found_results']}}
        records = data['results']['estates']
        for r in records:
            fields = {f['name']: ' '.join(str(v['name']) for v in f.get('values', [])) for f in r.get('attributes', [])}
            text = ' '.join(k + ': ' + v for k,v in fields.items()) + ' ' + r.get('excerpt', '')
            status = api.extract_status(text)
            observations.append({'url': r['link'], 'name': r['name'], 'status': status, 'type': fields.get('Property type', '')})
            dollar = next((x['price'] for x in r.get('price', []) if x['price'].startswith('$')), '')
            price, meta = api.parse_price_details(dollar + ' ' + fields.get('Lot size m²', ''))
            p = api.build_property(name, r['name'], r['link'], price, text, r.get('image', ''), meta)
            if p:
                p['source_type'] = source['type']
                building, land = api.explicit_areas(' '.join(k + ': ' + v for k,v in fields.items()))
                p.update(building_area=building if p['type'] != 'Land' else '', land_area=land)
                items.append(p)
        return items, observations, data['results'].get('totalResults', len(records)), soup

    if source.get('adapter') == 'aruba_listings':
        script = soup.select_one('#lx-mapdata')
        if not script:
            raise ParserError('Missing lx-mapdata listing data')
        records = json.loads(script.string or script.get_text())
        for r in records:
            title, link = r.get('title', ''), urljoin(url, r.get('seo') or r.get('slug', ''))
            contract = r.get('contract', '')
            text = f"{r.get('type', '')} {r.get('statusLabel', '')} {r.get('raw', '')}"
            status = api.extract_status(text) or ('for rent' if contract != 'for_sale' else '')
            observations.append({'url': link, 'name': title, 'status': status, 'type': r.get('type', '')})
            if status:
                continue
            # The map description is truncated; never derive asking price or status from it.
            price, meta = api.parse_price_details(r.get('price', ''))
            p = api.build_property(name, title, link, price, text, r.get('img', ''), meta, r.get('type', ''))
            if p:
                p.update(location=r.get('area', ''), beds=str(r.get('beds') or ''),
                         baths=str(r.get('baths') or ''), size=f"{r['size']} m²" if r.get('size') else '',
                         description=api.clean_text(BeautifulSoup(r.get('desc', ''), 'html.parser').get_text())[:300],
                         broker=r.get('broker', ''), source_type=source['type'])
                if p['type'] == 'Land':
                    p.update(beds='', baths='', building_area='', land_area=api.area_m2(p['size']))
                # Aggregator AW reference is its own ID, not the direct broker's MLS.
                items.append(p)
        return items, observations, len(records), soup

    nodes = soup.select(source['card_selector'])
    if not nodes:
        if soup.select_one(source.get('empty_selector', '.no-results, .no-properties')) or re.search(r'\bNo listing found\.', soup.get_text(' ', strip=True)):
            return [], [], 0, soup
        raise ParserError('No recognized listing cards; inventory cannot be assumed empty')
    for node in nodes:
        heading = node.select_one(source.get('title_selector', '.property-title, .item-title, h2, h3, h4'))
        if not heading:
            continue
        title = api.clean_text(heading.get_text(' ', strip=True))
        link = node if node.name == 'a' and node.get('href') else heading if heading.name == 'a' else heading.find('a', href=True)
        if not link:
            link = node.select_one(source.get('link_selector', 'a[href]'))
        if not link:
            continue
        href = urljoin(url, link['href'])
        # Only one card is ever used, never a parent containing other listings.
        for img in node.select('img[alt]'):
            if img.get('alt', '').lower() in ('beds', 'baths', 'sq mt'):
                img.insert_after(' ' + img['alt'] + ': ')
        text = api.clean_text(node.get_text(' ', strip=True))
        context = source.get('listing_context', '')
        if name == 'Keller Williams Aruba':
            context += ' Vacant Land' if '/land' in url else ' House' if '/residential' in url else ''
        if name == 'RE/MAX Aruba':
            context += ' Vacant Land' if '/land-for-sale' in url else ' House'
        text = context + ' ' + text
        type_node = node.select_one(source.get('type_selector', '.property-type, .item-type, .h-type'))
        declared = api.clean_text(type_node.get_text(' ', strip=True)) if type_node else ''
        text = f'{declared} {text}'
        status = api.extract_status(text)
        observations.append({'url': href, 'name': title, 'status': status, 'type': declared})
        price_node = node.select_one(source.get('price_selector', '.property-price, .item-price, .price'))
        price_text = api.clean_text(price_node.get_text(' ', strip=True)) if price_node else text
        price, meta = api.parse_price_details(price_text)
        if api.extract_per_m2_rate(price_text):
            price, meta = api.parse_price_details(price_text + ' ' + text)
        p = api.build_property(name, title, href, price, text, api.image_from(node, url), meta, declared)
        if not p and price is not None and 0 < price <= api.PRICE_LIMIT and not status and not api.infer_property_type(title, text):
            observations[-1]['needs_type_review'] = True
        if p:
            p['source_type'] = source['type']
            address = node.select_one('.item-address, .property-location, .card__address, address')
            if address:
                p['location'] = api.clean_text(address.get_text(' ', strip=True)) or p['location']
            # Houzez explicitly separates building and plot areas in its card markup.
            building = node.select_one('.h-area')
            land = node.select_one('.h-land-area')
            if building and p['type'] != 'Land':
                p['building_area'] = api.area_m2(building.get_text(' ', strip=True))
            if land:
                p['land_area'] = api.area_m2(land.get_text(' ', strip=True))
            elif building and p['type'] == 'Land':
                p['land_area'] = api.area_m2(building.get_text(' ', strip=True))
            items.append(p)
    return api.dedupe_properties(items), observations, len(nodes), soup


def scrape(source, mode, api):
    start = time.monotonic()
    budget = source.get('deep_seconds', 65) if mode == 'deep' else source.get('fast_seconds', 30)
    limit = source.get('deep_pages', 12) if mode == 'deep' else source.get('fast_pages', 1)
    queue = list(source.get('fast_urls', source.get('urls', [source['url']]))) if mode == 'fast' else list(source.get('urls', [source['url']]))
    if mode == 'deep':
        queue.extend(source.get('deep_urls', []))
    seen, fingerprints = set(), set()
    items, observations, errors = [], [], []
    cards = pages = repeated = count = 0
    truncated = False
    while queue and pages < limit:
        if time.monotonic() - start >= budget:
            truncated = True
            break
        entry = queue.pop(0)
        url = entry['url'] if isinstance(entry, dict) else entry
        request_data = entry.get('data') if isinstance(entry, dict) else None
        marker = api.canonical_url(url) + (json.dumps(request_data, sort_keys=True) if request_data else '')
        if marker in seen:
            continue
        seen.add(marker)
        try:
            html, final = api.get_page(url, request_data) if request_data else api.get_page(url)
            batch, observed, count, soup = parse_page(html, final, source, api)
            pages += 1
            fingerprint = hashlib.sha256(json.dumps(sorted(x['url'] for x in observed)).encode()).hexdigest()
            if fingerprint in fingerprints:
                repeated += 1
                continue
            fingerprints.add(fingerprint)
            cards += len(observed) if source.get('adapter') == 'myhome' else count
            items.extend(batch)
            observations.extend(observed)
            if source.get('adapter') == 'myhome' and mode == 'deep' and pages == 1:
                page_size = len(observed)
                if page_size:
                    for page in range(2, min(10, (count + page_size - 1)//page_size) + 1):
                        data = {'data[offer-type][compare]': '=', 'data[offer-type][key]': 'offer-type',
                                'data[offer-type][slug]': 'offer-type', 'data[offer-type][values][0][name]': 'Sale Property',
                                'data[offer-type][values][0][value]': 'for-sale', 'page': str(page),
                                'limit': str(page_size), 'sortBy': 'newest', 'currency': 'price'}
                        queue.append({'url': source['api_url'], 'data': data})
            for next_url in page_links(soup, final, source):
                if api.canonical_url(next_url) not in seen and next_url not in queue:
                    queue.append(next_url)
        except Exception as exc:
            errors.append({'url': url, 'status': failure_status(exc), 'error': str(exc)[:220]})
            if not pages and not queue:
                break
    truncated = truncated or bool(queue)
    if source.get('adapter') == 'myhome':
        # Each API response reports the same total; use the observed distinct rows.
        truncated = truncated or count > len({o['url'] for o in observations})
    unclassified = sum(bool(o.get('needs_type_review')) for o in observations)
    if errors and not pages:
        status = errors[0]['status']
    elif errors or repeated or unclassified or (source.get('adapter') == 'myhome' and truncated):
        status = 'partial'
    else:
        status = 'ok' if items else 'empty'
    return api.dedupe_properties(items), observations, {
        'status': status, 'checked_at': api.iso_now(), 'properties_found': len(api.dedupe_properties(items)),
        'cards_seen': cards, 'unclassified_cards': unclassified, 'pages_fetched': pages, 'mode': mode, 'coverage_limited': truncated,
        'duration_seconds': round(time.monotonic()-start, 2), 'repeated_pages': repeated, 'errors': errors,
    }


def failure_status(exc):
    import requests
    if isinstance(exc, requests.exceptions.SSLError): return 'ssl_error'
    if isinstance(exc, requests.exceptions.Timeout): return 'timeout'
    if isinstance(exc, requests.exceptions.HTTPError):
        code = exc.response.status_code
        if code in (401, 403): return 'blocked'
        if code in (404, 410): return 'url_changed'
        if code == 429: return 'rate_limited'
        return 'http_error'
    if isinstance(exc, (ParserError, ValueError)): return 'parser_failed'
    return 'connection_error'


def scan(sources, mode, health, api):
    due = []
    for source in sources:
        old = health.get(source['name'], {})
        retry = api.parse_datetime(old.get('retry_after', ''))
        if retry and retry > api.now_utc():
            continue
        due.append(source)
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for source, result in zip(due, pool.map(lambda s: scrape(s, mode, api), due)):
            results.append((source, result))
    return results
