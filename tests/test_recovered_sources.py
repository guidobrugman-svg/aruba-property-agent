import json
import unittest
from pathlib import Path
from unittest.mock import patch
import copy
import os
import tempfile
import development

import collectors as c
import monitor as m

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {s['name']: s for s in json.loads((ROOT / 'SOURCES.json').read_text())['sources']}
HTML = (ROOT / 'tests/fixtures/realestatearuba.html').read_text()
API_CASES = json.loads((ROOT / 'tests/fixtures/reallinkr.json').read_text())


class RecoveredSourceTests(unittest.TestCase):
    def parse(self, html=HTML):
        source = SOURCES['Real Estate Aruba']
        return c.parse_page(html, source['url'], source, m)

    def test_live_records_preserve_house_apartments_and_land_fields(self):
        items, observed, count, _ = self.parse()
        self.assertEqual((len(observed), count), (8, 8))
        house = next(p for p in items if p['name'] == 'Versatile Home in Morgenster')
        self.assertEqual((house['price'], house['beds'], house['baths']), (648900, '6', '6'))
        self.assertEqual((house['building_area'], house['land_area']), ('422 m²', '885 m²'))
        self.assertEqual(house['location'], 'Morgenster, Oranjestad')
        self.assertTrue(house['image'].startswith('https://'))
        self.assertIn('Home with Apartments for Sale', house['description'])
        land = next(p for p in items if p['name'] == 'Prime lot in Noord')
        self.assertEqual((land['beds'], land['baths'], land['building_area'], land['land_area']), ('', '', '', '449 m²'))

    def test_sold_on_hold_residence_units_and_over_budget_are_excluded(self):
        items, observed, _, _ = self.parse()
        names = {p['name'] for p in items}
        self.assertNotIn('Single 1 Residence', names)
        self.assertNotIn('Santa Cruz Apartments', names)
        self.assertNotIn('Stunning Estate Complex', names)
        self.assertNotIn('Safir Estate in Noord', names)
        self.assertTrue(any(o['status'] == 'sold' for o in observed))
        self.assertTrue(any(o['status'] == 'on hold' for o in observed))

    def test_layout_currency_and_loading_shell_fail_closed(self):
        for html in ('<html>Loading...</html>', HTML.replace('USD\u00a0648,900', 'USD\u00a0640,000'), HTML.replace('items', 'unknown')):
            with self.subTest(fragment=html[:20]), self.assertRaises(c.ParserError):
                self.parse(html)
        items, _, _, _ = self.parse(HTML.replace('USD\u00a0648,900', 'EUR\u00a0648,900'))
        self.assertNotIn('Versatile Home in Morgenster', {p['name'] for p in items})

    def test_missing_description_reference_is_a_failure(self):
        with self.assertRaises(c.ParserError):
            self.parse(HTML.replace(':T', ':Unknown'))

    def test_ben_alias_does_not_create_a_duplicate_collector(self):
        catalog = json.loads((ROOT / 'SOURCE_CATALOG.json').read_text())['sources']
        ben = next(s for s in catalog if s['name'] == 'Ben Real Estate')
        self.assertIn('Aruba Real Estate (ME)', ben['aliases'])
        self.assertNotIn('Aruba Real Estate (ME)', SOURCES)
        self.assertFalse(SOURCES['Caribmedia Property Assistant']['enabled'])
        self.assertTrue(SOURCES['Real Estate Aruba']['enabled'])

    def test_reallinkr_whole_apartments_and_conflicting_fields(self):
        source = SOURCES['RealLinkr / Aruba MLS']
        for case in API_CASES:
            prop = c.reallinkr_property(source, case['row'], case['detail'], m)
            self.assertIsNotNone(prop)
            self.assertEqual(prop['url'], case['detail']['external_listing_url'])
            if case['row']['id'] == 801:
                self.assertEqual((prop['type'], prop['price'], prop['building_area'], prop['land_area']), ('Apartment Complex', 545000, '230 m²', '985 m²'))
            else:
                self.assertEqual(prop['price'], 421349)
                self.assertEqual((prop['building_area'], prop['baths']), ('', ''))

    def test_reallinkr_currency_contract_projects_and_units_fail_closed(self):
        source = SOURCES['RealLinkr / Aruba MLS']
        case = next(x for x in API_CASES if x['row']['id'] == 801)
        for change in ({'currency':'AWG'}, {'status':'UNDER_CONTRACT'}, {'price_period':'MONTH'}, {'external_listing_url':''}, {'title':'Condo 1','description':'One condo in a residential development'}):
            self.assertIsNone(c.reallinkr_property(source, case['row'], dict(case['detail'], **change), m))
        with self.assertRaises(c.ParserError):
            c.reallinkr_property(source, case['row'], dict(case['detail'], price='540000'), m)
        self.assertIsNone(c.reallinkr_property(source, dict(case['row'], country_display='Curacao'), case['detail'], m))
        self.assertIsNone(c.reallinkr_property(source, dict(case['row'], search_project_id=1), case['detail'], m))

    def test_reallinkr_bounds_cache_and_unavailable_original_url(self):
        source = dict(SOURCES['RealLinkr / Aruba MLS'], fast_type_reviews=1)
        rows = [case['row'] for case in API_CASES]
        search = dict(count=720, page=1, page_size=24, num_pages=30, results=rows)
        def get(url, **kwargs):
            if '/search/' in url:
                return json.dumps(search), url
            detail = next(case['detail'] for case in API_CASES if case['row']['search_slug'] in url)
            return json.dumps(detail), url
        with patch.object(m,'get_page',side_effect=get) as fetch:
            items, obs, health = c.scrape(source,'fast',m,{})
            self.assertEqual((health['pages_fetched'],health['type_review_reads'],health['status']), (1,1,'partial'))
            self.assertEqual(fetch.call_count,2)
        self.assertEqual(len(items),1)
        with patch.object(m,'get_page',side_effect=get) as fetch:
            _, _, second = c.scrape(source,'fast',m,health)
            self.assertEqual(second['type_review_reads'],1)
            self.assertEqual(fetch.call_count,2)
        closed = copy.deepcopy(search)
        closed['results'][0]['search_status'] = 'SALE_IN_PROGRESS'
        with patch.object(m,'get_page',return_value=(json.dumps(closed),source['url'])):
            _, observed, _ = c.scrape(source,'fast',m,second)
        self.assertEqual(observed[0]['status'],'sale in progress')
        self.assertEqual(observed[0]['url'],API_CASES[0]['detail']['external_listing_url'])
        with patch.object(m,'get_page',return_value=('<html>Loading</html>',source['url'])):
            items, _, bad = c.scrape(source,'fast',m,{})
        self.assertFalse(items)
        self.assertEqual(bad['status'],'parser_failed')

    def test_reallinkr_baseline_allows_new_inventory_without_alerting_old_reviews(self):
        source = SOURCES['RealLinkr / Aruba MLS']
        case = next(x for x in API_CASES if x['row']['id'] == 801)
        recovered = c.reallinkr_property(source, case['row'], case['detail'], m)
        health = dict(status='partial', source_revision=c.source_revision(source), pagination_limited=True,
                      cards_seen=1, pages_fetched=1, duration_seconds=0, mode='deep')
        unknown = dict(url=recovered['listing_api_url'], name=recovered['name'], status='', needs_type_review=True)
        observed = dict(url=recovered['url'], name=recovered['name'], status='', type=recovered['type'])
        fresh = m.build_property(source['name'],'New house in Rooi Koochi 99','https://broker.test/rooi-koochi-99',400000,'For Sale')
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            state = Path(folder)/'state.json'; sources = Path(folder)/'sources.json'
            state.write_text(json.dumps(dict(schema_version=2,properties={})))
            sources.write_text(json.dumps({'sources':[source]}))
            results = [(source,([], [unknown], health))]
            with patch.object(m,'STATE_FILE',str(state)), patch.object(m,'SOURCES_FILE',str(sources)), patch.object(m,'DRY_RUN',True), patch.dict(os.environ,{'SCAN_MODE':'deep','DEFER_DELIVERY':'1'}), patch.object(c,'scan',side_effect=lambda *args:copy.deepcopy(results)), patch.object(development,'enrich',side_effect=lambda props,*args:props):
                m.main()
                self.assertEqual(json.loads(state.read_text())['last_scan']['new'],0)
                results[:] = [(source,([recovered],[observed],health))]
                m.main()
                self.assertEqual(json.loads(state.read_text())['last_scan']['new'],0)
                results[:] = [(source,([recovered,fresh],[observed,dict(url=fresh['url'],name=fresh['name'],status='')],health))]
                m.main()
                self.assertEqual(json.loads(state.read_text())['last_scan']['new'],1)


if __name__ == '__main__':
    unittest.main()
