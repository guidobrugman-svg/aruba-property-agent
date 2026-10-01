"""Listing evidence about development constraints; never legal feasibility advice."""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import urlparse
from bs4 import BeautifulSoup


def evidence(text, url, api):
    text = api.clean_text(text)
    records = []
    patterns = {
        'Ownership': r'\b(?:freehold|leasehold|long lease|property land|ownership land|eigendom|erfpacht)\b',
        'Zoning': r'\b(?:zoning|zoned|bestemming|bestemmingsplan)\b',
        'Building restrictions': r'\b(?:building restrictions?|construction restrictions?|restrictive covenants?|covenants?|homeowners? association|HOA|maximum (?:height|units|floors)|building height|building coverage|setbacks?|building permit|construction permit)\b',
        'Apartment construction': r'\b(?:build|construct|permit|allow|prohibit|restrict)\w*\b[^.!?]{0,100}\bapartments?\b|\bapartments?\b[^.!?]{0,100}\b(?:build|construct|permit|allow|prohibit|restrict)\w*\b',
    }
    for label, pattern in patterns.items():
        for match in list(re.finditer(pattern, text, re.I))[:2]:
            start = max(text.rfind('. ', 0, match.start()) + 2, match.start()-100, 0)
            end = text.find('. ', match.end())
            end = min(end if end >= 0 else len(text), match.end()+170)
            records.append({'label': label, 'excerpt': text[start:end].strip(), 'url': url})
    return records


def detail_fields(html, url, api):
    soup = BeautifulSoup(html, 'html.parser')
    # Listing-specific sections only. Related listings, navigation, and search forms
    # must never contribute an ownership claim or a neighboring property's area.
    nodes = soup.select('.property-description, #property-description, .property-detail-description, .listing-description, .property-details, .detail-list, .property-features, .property-overview')
    if not nodes:
        raise ValueError('No recognized property-detail sections')
    for node in nodes:
        for unrelated in node.select('.related-properties, .item-listing-wrap, nav, form, script, style'):
            unrelated.decompose()
    text = api.clean_text(' '.join(n.get_text(' ', strip=True) for n in nodes))
    result = {'development_evidence': evidence(text, url, api)}
    building, land = api.explicit_areas(text)
    if building:
        result['building_area'] = building
    if land:
        result['land_area'] = land
    address = soup.select_one('.property-address, .property-location, .property-title-wrap .item-address')
    if address:
        result['location'] = api.clean_text(address.get_text(' ', strip=True))
    image = soup.select_one('meta[property="og:image"]')
    if image and api.looks_like_url(api.absolute_url(url, image.get('content', ''))):
        result['image'] = api.absolute_url(url, image['content'])
    result['residence_complex'] = bool(re.search(
        r'\b(?:part of|within|located in|in the)\b[^.!?]{0,70}\bresidences\b|\bresidence (?:complex|development|project)\b', text, re.I))
    return result


def enrich(properties, previous, mode, api):
    """Two fast / four deep detail reads per scan, rotated using durable timestamps."""
    due = []
    for prop in properties:
        _, old = api.find_previous_record(previous, prop)
        if old and api.canonical_url(old.get('url', '')) == api.canonical_url(prop['url']):
            for key in ('development_evidence', 'detail_checked_at', 'detail_status', 'residence_complex', 'building_area', 'land_area'):
                if not prop.get(key) and old.get(key):
                    prop[key] = old[key]
        # Card descriptions can already contain useful claims without another request.
        card_evidence = evidence(prop.get('description', ''), prop['url'], api)
        if card_evidence and not prop.get('development_evidence'):
            prop['development_evidence'] = card_evidence
        checked = api.parse_datetime(prop.get('detail_checked_at', ''))
        interval = timedelta(days=7 if prop.get('detail_status') == 'ok' else 1)
        if not checked or api.now_utc()-checked >= interval:
            due.append(prop)
    due.sort(key=lambda p: p.get('detail_checked_at', ''))
    def read(prop):
        prop['detail_checked_at'] = api.iso_now()
        try:
            html, final = api.get_page(prop['url'])
            if urlparse(final).netloc != urlparse(prop['url']).netloc:
                raise ValueError('Detail redirected to another host')
            fields = detail_fields(html, final, api)
            if prop.get('type') == 'Land':
                fields.pop('building_area', None)
            for key, value in fields.items():
                if key in ('location', 'image') and prop.get(key):
                    continue
                prop[key] = value
            prop['detail_status'] = 'ok'
        except Exception as exc:
            prop['detail_status'] = 'unavailable'
            prop['detail_error'] = str(exc)[:180]
        return prop
    limit = 4 if mode == 'deep' else 2
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(read, due[:limit]))
    return properties
