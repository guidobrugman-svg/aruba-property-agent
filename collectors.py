"""Bounded source-specific public HTML collectors. No guessed pagination."""
import hashlib
import json
import re
import xml.etree.ElementTree as ET
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
from bs4 import BeautifulSoup


class ParserError(ValueError):
    pass


class AccessBlocked(ParserError):
    pass


def source_revision(source):
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()[:16]


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
                if match[1] == '1':
                    continue
                url = base.split('?')[0] + '?page=' + match[1]
                if url not in result:
                    result.append(url)
    if source.get('name') == 'MPG Aruba':
        # The public page accepts GET page numbers advertised by changePage controls.
        for a in soup.select('.pagination a[onclick]'):
            match = re.fullmatch(r'changePage\((\d+)\)', a['onclick'])
            if match:
                if match[1] == '1':
                    continue
                parts = urlparse(base)
                query = dict(parse_qsl(parts.query))
                query['page'] = match[1]
                url = urlunparse(parts._replace(query=urlencode(query)))
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
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    for meta in soup.select('meta[http-equiv]'):
        if meta.get('http-equiv', '').lower() == 'refresh':
            parts = meta.get('content', '').split(';', 1)
            if len(parts) == 2:
                target = re.sub(r'^url\s*=\s*', '', parts[1].strip(), flags=re.I).strip('"\x27 ')
                path = urlparse(urljoin(url, target)).path
                if path.startswith('/.well-known/sgcaptcha/'):
                    raise AccessBlocked('Public page returned a SiteGround access challenge instead of listings')
    if title in ('just a moment...', 'attention required! | cloudflare', 'access denied', '403 forbidden'):
        raise AccessBlocked('Public page returned an access challenge instead of listings')
    if source.get('adapter') == 'myhome':
        match = re.search(r'var MyHomeListing\d+ = (\{.*?\});', html, re.S)
        if match:
            data = json.loads(match[1])
        else:
            try:
                raw = json.loads(html)
            except ValueError as exc:
                raise ParserError('Missing MyHome listing records') from exc
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
            p = api.build_property(name, r['name'], r['link'], price, text, r.get('image', ''), meta, fields.get('Property type', ''))
            if p:
                p['source_type'] = source['type']
                building, land = api.explicit_areas(' '.join(k + ': ' + v for k,v in fields.items()))
                # These named API fields are the broker's building and lot areas.
                building = api.area_m2(fields.get('Property size m²', '')) or building
                land = api.area_m2(fields.get('Lot size m²', '')) or land
                p.update(building_area=building if p['type'] != 'Land' else '', land_area=land)
                p['location'] = ', '.join(dict.fromkeys(v for v in (r.get('address', ''), fields.get('Neighborhood', ''), fields.get('City', '')) if v)) or p['location']
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
        for sup in node.select('sup'):
            if sup.get_text(strip=True) == '2' and str(sup.previous_sibling).strip().endswith(('m', 'ft')):
                sup.previous_sibling.replace_with(str(sup.previous_sibling) + '²')
                sup.decompose()
        for icon, label in (('.lucide-bed', 'Beds'), ('.lucide-bath', 'Baths')):
            for svg in node.select(icon):
                svg.insert_after(label + ': ')
        text = api.clean_text(node.get_text(' ', strip=True))
        context = source.get('listing_context', '')
        for fragment, hint in source.get('url_contexts', {}).items():
            if fragment in url:
                context += ' ' + hint
        if name == 'Keller Williams Aruba':
            context += ' Vacant Land' if '/land' in url else ' House' if '/residential' in url else ''
        if name == 'RE/MAX Aruba':
            context += ' Vacant Land' if '/land-for-sale' in url else ' House'
        text = context + ' ' + text
        type_node = node.select_one(source.get('type_selector', '.property-type, .item-type, .h-type'))
        declared = api.clean_text(type_node.get_text(' ', strip=True)) if type_node else ''
        declared = source.get('declared_type_map', {}).get(declared, declared)
        text = f'{declared} {text}'
        status = api.extract_status(text)
        observations.append({'url': href, 'name': title, 'status': status, 'type': declared})
        price_node = node.select_one(source.get('price_selector', '.property-price, .item-price, .price'))
        price_text = api.clean_text(price_node.get_text(' ', strip=True)) if price_node else text
        price, meta = api.parse_price_details(price_text)
        if api.extract_per_m2_rate(price_text):
            price, meta = api.parse_price_details(price_text + ' ' + text)
        p = api.build_property(name, title, href, price, text, api.image_from(node, url), meta, declared)
        excluded = bool(re.search(r'\bresidences\b|\bresidence (?:complex|development|project)\b', api.normalize(title + ' ' + text)))
        excluded = excluded or api.normalize(declared) in ('commercial', 'commercial building')
        if excluded:
            observations[-1].update(excluded=True, status='ineligible')
        if not p and not excluded and price is not None and 0 < price <= api.PRICE_LIMIT and not status and not api.infer_property_type(title, text):
            observations[-1]['needs_type_review'] = True
            observations[-1]['_candidate'] = dict(price=price, text=text, image=api.image_from(node, url), meta=meta, declared=declared)
            address = node.select_one(source.get('address_selector', '.item-address, .property-location, .card__address, address'))
            observations[-1]['_candidate']['location'] = api.clean_text(address.get_text(' ', strip=True)) if address else ''
            for key, selector in (('building_area', '.h-area'), ('land_area', '.h-land-area')):
                area = node.select_one(selector)
                observations[-1]['_candidate'][key] = api.area_m2(area.get_text(' ', strip=True)) if area else ''
        if p:
            p['source_type'] = source['type']
            address = node.select_one(source.get('address_selector', '.item-address, .property-location, .card__address, address'))
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


def resolve_types(source, mode, api, observations, items, old_health, start, budget):
    """Resolve ambiguous cards from scoped public descriptions, with durable caching."""
    selector = source.get('detail_description_selector')
    old_cache = old_health.get('type_review_cache', {})
    cache, reads = {}, 0
    limit = source.get('deep_type_reviews', 12) if mode == 'deep' else source.get('fast_type_reviews', 2)
    for obs in observations:
        candidate = obs.pop('_candidate', None)
        if not candidate or not selector:
            continue
        key = hashlib.sha256(json.dumps([obs['url'], obs['name'], candidate['text'], selector], sort_keys=True).encode()).hexdigest()[:24]
        result = old_cache.get(key)
        checked = api.parse_datetime(result.get('checked_at', '')) if result else None
        if not checked or (api.now_utc()-checked).total_seconds() > (604800 if result.get('type') or result.get('excluded') else 3600):
            result = None
        if not result and reads < limit and time.monotonic()-start < budget:
            reads += 1
            result = {'checked_at': api.iso_now()}
            try:
                kwargs = {'timeout': tuple(source['request_timeout'])} if source.get('request_timeout') else {}
                html, final = api.get_page(obs['url'], **kwargs)
                if api.canonical_url(final) != api.canonical_url(obs['url']):
                    raise ParserError('Type detail redirected away from listing')
                soup = BeautifulSoup(html, 'html.parser')
                nodes = soup.select(selector)
                if not nodes:
                    raise ParserError('No recognized listing description for type review')
                for node in nodes:
                    for unrelated in node.select('nav, form, script, style, .related-properties, .item-listing-wrap, .card'):
                        unrelated.decompose()
                text = api.clean_text(' '.join(n.get_text(' ', strip=True) for n in nodes))
                inferred = api.infer_property_type(obs['name'], text)
                result.update(type=inferred, excluded=bool(re.search(r'\bresidences\b|\bresidence (?:complex|development|project)\b', api.normalize(text))) or inferred in api.EXCLUDED_RESIDENTIAL_UNIT_TYPES)
            except Exception as exc:
                result['error'] = str(exc)[:160]
        if not result:
            continue
        cache[key] = result
        if result.get('excluded'):
            obs.pop('needs_type_review', None)
            obs.update(type=result.get('type', ''), status='ineligible', excluded=True)
        elif result.get('type'):
            prop = api.build_property(source['name'], obs['name'], obs['url'], candidate['price'], candidate['text'], candidate['image'], candidate['meta'], result['type'])
            if prop:
                prop['source_type'] = source['type']
                for field in ('location', 'building_area', 'land_area'):
                    if candidate.get(field) and not (field == 'building_area' and prop['type'] == 'Land'):
                        prop[field] = candidate[field]
                items.append(prop)
                obs.pop('needs_type_review', None)
                obs['type'] = result['type']
    return cache, reads


def scrape(source, mode, api, old_health=None):
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
            kwargs = {'timeout': tuple(source['request_timeout'])} if source.get('request_timeout') else {}
            html, final = api.get_page(url, request_data, **kwargs) if request_data else api.get_page(url, **kwargs)
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
    cache, review_reads = resolve_types(source, mode, api, observations, items, old_health or {}, start, budget)
    unclassified = sum(bool(o.get('needs_type_review')) for o in observations)
    if errors and not pages:
        status = errors[0]['status']
    elif errors or repeated or unclassified or truncated:
        status = 'partial'
    else:
        status = 'ok' if items else 'empty'
    return api.dedupe_properties(items), observations, {
        'status': status, 'checked_at': api.iso_now(), 'properties_found': len(api.dedupe_properties(items)),
        'source_revision': source_revision(source),
        'cards_seen': cards, 'unclassified_cards': unclassified, 'pages_fetched': pages, 'mode': mode, 'coverage_limited': truncated,
        'duration_seconds': round(time.monotonic()-start, 2), 'repeated_pages': repeated, 'errors': errors,
        'type_review_cache': cache, 'type_review_reads': review_reads,
    }


def failure_status(exc):
    import requests
    if isinstance(exc, AccessBlocked): return 'blocked'
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


def retry_delay_minutes(source, status, failures):
    # Some brokers' hosts intermittently challenge shared runner IPs. Recheck on
    # the ordinary schedule without attempting the challenge or changing identity.
    if status == 'blocked' and source.get('blocked_retry_minutes'):
        return max(15, min(60, int(source['blocked_retry_minutes'])))
    if status in ('blocked', 'url_changed', 'ssl_error', 'parser_failed'):
        return 60 * min(24, 2 ** min(failures - 1, 5))
    return 15


def scan(sources, mode, health, api):
    due = []
    for source in sources:
        old = health.get(source['name'], {})
        retry = api.parse_datetime(old.get('retry_after', ''))
        if retry and retry > api.now_utc() and old.get('source_revision') == source_revision(source):
            continue
        due.append(source)
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for source, result in zip(due, pool.map(lambda s: scrape(s, mode, api, health.get(s['name'], {})), due)):
            results.append((source, result))
    return results
