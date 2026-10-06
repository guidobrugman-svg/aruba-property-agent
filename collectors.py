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
    if source.get('pagination_adapter') == 'wpl':
        # WPL publishes its current and total scroll pages in the public page.
        html = str(soup)
        total = re.search(r'var wpl_listing_total_pages\s*=\s*(\d+)', html)
        current = re.search(r'var wpl_listing_current_page\s*=\s*(\d+)', html)
        if total and current:
            parts = urlparse(base)
            for page in range(int(current[1]) + 1, min(int(total[1]), 20) + 1):
                query = dict(parse_qsl(parts.query))
                query['wplpage'] = str(page)
                result.append(urlunparse(parts._replace(query=urlencode(query))))
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
    if source.get('adapter') == 'october':
        # Only the advertised listing fragment is parsed; other AJAX fragments
        # (forms, messages, navigation) never become property cards.
        try:
            fragments = json.loads(html)
            html = fragments[source['fragment_key']]
        except (ValueError, KeyError, TypeError) as exc:
            raise ParserError('Missing public October listing fragment') from exc
        soup = BeautifulSoup(html, 'html.parser')
    if source.get('adapter') == 'realtor':
        return parse_realtor(soup, url, source, api)
    if source.get('adapter') == 'qobrix':
        for card in soup.select(source['card_selector']):
            fields = card.select('.et_pb_text')
            if len(fields) != 7:
                raise ParserError('Qobrix public card field layout changed')
            for index, label in [(3, 'property-price'), (4, 'property-title'), (5, 'property-location')]:
                fields[index]['class'] = fields[index].get('class', []) + [label]
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
        title = api.clean_text(heading.get(source['title_attribute'], '') if source.get('title_attribute') else heading.get_text(' ', strip=True))
        if source.get('title_suffix_pattern'):
            title = re.sub(source['title_suffix_pattern'], '', title).strip()
        link = node if node.name == 'a' and node.get('href') else heading if heading.name == 'a' else heading.find('a', href=True)
        if not link:
            link = node.select_one(source.get('link_selector', 'a[href]'))
        if not link:
            continue
        href = urljoin(url, link['href'])
        if source.get('same_host_links') and urlparse(href).netloc.removeprefix('www.') != urlparse(url).netloc.removeprefix('www.'):
            continue
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
        for selector, label in source.get('field_labels', {}).items():
            for field in node.select(selector):
                field.insert_before(label + ': ')
        text = api.clean_text(node.get_text(' ', strip=True))
        context = source.get('listing_context', '')
        for selector, hint in source.get('card_contexts', {}).items():
            if node.css.match(selector):
                context += ' ' + hint
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
        excluded = api.residence_unit_offer(title, text)
        commercial = api.normalize(declared) in ('commercial', 'commercial building') or '/for-sale/commercial' in url
        income = api.residential_income_offer(title, text, api.infer_property_type(title, text))
        review_commercial = commercial and not income and bool(source.get('detail_description_selector'))
        excluded = excluded or (commercial and not income and not review_commercial)
        if excluded:
            observations[-1].update(excluded=True, status='ineligible')
        if not p and not excluded and not status and price is None and source.get('review_missing_price'):
            observations[-1]['needs_type_review'] = True
            observations[-1]['_candidate'] = dict(price=None, text=text, image=api.image_from(node, url), meta=meta, declared=declared, commercial=commercial)
        if not p and not excluded and price is not None and 0 < price <= api.PRICE_LIMIT and not status and (not api.infer_property_type(title, text) or review_commercial):
            observations[-1]['needs_type_review'] = True
            observations[-1]['_candidate'] = dict(price=price, text=text, image=api.image_from(node, url), meta=meta, declared=declared, commercial=commercial)
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
            apply_card_fields(p, node, source, api)
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


def apply_card_fields(prop, node, source, api):
    """Read only fields whose meaning was verified in this source's markup."""
    for field, selector in source.get('area_selectors', {}).items():
        area = node.select_one(selector)
        if area and not (field == 'building_area' and prop['type'] == 'Land'):
            prop[field] = api.area_m2(area.get_text(' ', strip=True))
    for field, selector in source.get('count_selectors', {}).items():
        count = node.select_one(selector)
        value = count.get_text(' ', strip=True) if count else ''
        if prop['type'] != 'Land' and re.fullmatch(r'\d+(?:\.\d+)?', value):
            prop[field] = value


def parse_realtor(soup, url, source, api):
    """Public SSR records; original asking currency and Aruba country are required."""
    try:
        data = json.loads(soup.select_one('#__NEXT_DATA__').get_text())['props']['apolloState']
        search = next(v for k, v in data.items() if k.startswith('$ROOT_QUERY.searchListListings(') and '.pageInfo' not in k and isinstance(v, dict) and isinstance(v.get('listings'), list))
        records = [data[ref['id']] for ref in search['listings']]
    except (AttributeError, ValueError, KeyError, StopIteration, TypeError) as exc:
        raise ParserError('Missing Realtor public search records') from exc
    items, observations = [], []
    for record in records:
        if record.get('country') != 'aw':
            continue
        def field(prefix):
            value = next((v for k, v in record.items() if k.startswith(prefix)), None)
            return data.get(value['id'], {}) if isinstance(value, dict) and value.get('id') else value
        asking = field('price(') or {}
        title = record.get('displayAddress', '')
        link = urljoin(url, field('detailPageUrl(') or '')
        if urlparse(link).netloc != urlparse(url).netloc or not urlparse(link).path.startswith('/international/aw/'):
            continue
        declared = ' '.join((field('propertyTypes(') or {}).get('json', []))
        text = declared + ' ' + record.get('tagline', '')
        status = 'for rent' if asking.get('rentPricePeriod') else api.extract_status(title + ' ' + text)
        observations.append(dict(url=link, name=title, status=status, type=declared))
        # Consumer prices may be converted from another currency. Use the
        # broker's original displayed asking price, never the converted amount.
        raw = asking.get('displayListingPrice', '')
        if not re.search(r'\bUSD\b|US\$', raw, re.I) or status or record.get('isProjectProfile'):
            continue
        price, meta = api.parse_price_details(raw)
        anchor = soup.find('a', href=field('detailPageUrl('))
        prop = api.build_property(source['name'], title, link, price, text, api.image_from(anchor, url), meta, declared)
        if prop:
            prop.update(source_type=source['type'], location=title)
            for key, prefix in [('land_area', 'landSize('), ('building_area', 'buildingSize(')]:
                value = field(prefix)
                if value and not (key == 'building_area' and prop['type'] == 'Land'):
                    prop[key] = api.area_m2(str(value) + ' sq ft')
            if prop['type'] != 'Land':
                prop.update(beds=str(record.get('bedrooms') or ''), baths=str(record.get('bathrooms') or ''))
            items.append(prop)
    return api.dedupe_properties(items), observations, len(records), soup


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
        key = hashlib.sha256(json.dumps(['public-detail-v2', obs['url'], obs['name'], candidate['text'], selector], sort_keys=True).encode()).hexdigest()[:24]
        result = old_cache.get(key)
        checked = api.parse_datetime(result.get('checked_at', '')) if result else None
        ttl = 3600 if candidate['price'] is None else 604800 if result and (result.get('type') or result.get('excluded')) else 3600
        if not checked or (api.now_utc()-checked).total_seconds() > ttl:
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
                status_text = soup.title.get_text(' ', strip=True) if soup.title else ''
                for status_node in soup.select(source.get('detail_status_selector', '.property-status')):
                    status_text += ' ' + status_node.get_text(' ', strip=True)
                result['status'] = api.extract_status(status_text)
                # Explicit availability banners at the start of a description
                # are authoritative; ordinary "sold furnished" prose is not.
                banner = re.match(r'^[\s\W]*(UNDER CONTRACT|SOLD|ON HOLD|RESERVED|SALE IN PROGRESS|WITHDRAWN)\b', text, re.I)
                if banner:
                    result['status'] = api.extract_status(banner[1])
                if candidate['price'] is None:
                    result['price'], result['price_meta'] = api.parse_price_details(text)
                result['building_area'], result['land_area'] = api.explicit_areas(text)
                result.update(type=inferred, excluded=api.residence_unit_offer(obs['name'], text) or inferred in api.EXCLUDED_RESIDENTIAL_UNIT_TYPES)
                result['residential_income'] = api.residential_income_offer(obs['name'], text, inferred)
                if candidate.get('commercial') and not result['residential_income']:
                    result['excluded'] = True
            except Exception as exc:
                result['error'] = str(exc)[:160]
        if not result:
            continue
        cache[key] = result
        if result.get('status'):
            obs.pop('needs_type_review', None)
            obs.update(status=result['status'])
        elif result.get('excluded'):
            obs.pop('needs_type_review', None)
            obs.update(type=result.get('type', ''), status='ineligible', excluded=True)
        elif result.get('type'):
            price = candidate['price'] if candidate['price'] is not None else result.get('price')
            meta = candidate['meta'] if candidate['price'] is not None else result.get('price_meta', {})
            prop = api.build_property(source['name'], obs['name'], obs['url'], price, candidate['text'], candidate['image'], meta, result['type'], residential_income_evidence=result.get('residential_income', False))
            if prop:
                prop['source_type'] = source['type']
                for field in ('location', 'building_area', 'land_area'):
                    value = candidate.get(field) or result.get(field)
                    if value and not (field == 'building_area' and prop['type'] == 'Land'):
                        prop[field] = value
                items.append(prop)
                obs.pop('needs_type_review', None)
                obs['type'] = result['type']
            elif price is not None and price > api.PRICE_LIMIT:
                obs.pop('needs_type_review', None)
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
            if source.get('request_headers'):
                kwargs['request_headers'] = source['request_headers']
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
                                'data[offer-type][values][0][value]': source.get('myhome_sale_slug', 'for-sale'), 'page': str(page),
                                'limit': str(page_size), 'sortBy': 'newest', 'currency': 'price'}
                        queue.append({'url': source['api_url'], 'data': data})
            if source.get('adapter') == 'october':
                for anchor in soup.select('a[data-request="Listings::onFilter"][data-request-data]'):
                    page = re.search(r'page\s*:\s*(\d+)', anchor['data-request-data'])
                    if page:
                        data = dict(request_data or source['urls'][0]['data'], page=page[1])
                        next_entry = {'url': source['url'], 'data': data}
                        next_marker = api.canonical_url(source['url']) + json.dumps(data, sort_keys=True)
                        if next_marker not in seen and next_entry not in queue:
                            queue.append(next_entry)
            for next_url in page_links(soup, final, source):
                if api.canonical_url(next_url) not in seen and next_url not in queue:
                    queue.append(next_url)
        except Exception as exc:
            errors.append({'url': url, 'status': failure_status(exc), 'error': str(exc)[:220]})
            if not pages and not queue:
                break
    pagination_limited = truncated or bool(queue)
    truncated = pagination_limited or source.get('known_coverage_limit', False)
    if source.get('adapter') == 'myhome':
        # Each API response reports the same total; use the observed distinct rows.
        pagination_limited = pagination_limited or count > len({o['url'] for o in observations})
        truncated = truncated or pagination_limited
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
        'cards_seen': cards, 'unclassified_cards': unclassified, 'pages_fetched': pages, 'mode': mode, 'coverage_limited': truncated, 'pagination_limited': pagination_limited,
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
        if source.get('enabled') is False:
            continue
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
