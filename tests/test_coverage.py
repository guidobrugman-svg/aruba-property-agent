import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
import collectors as c
import monitor as m

ROOT = Path(__file__).parent / 'fixtures'


class CoverageTests(unittest.TestCase):
    def case(self, filename):
        case = next(x for x in json.loads((ROOT / 'cases.json').read_text()) if x['file'] == filename)
        return c.parse_page((ROOT / filename).read_text(), case['source']['url'], case['source'], m)[0]

    def test_bhhs_explicit_building_size(self):
        house = next(p for p in self.case('bhhs_coverage.html') if p['name'] == 'Pavia 182')
        self.assertEqual((house['beds'], house['baths']), ('4', '3'))
        self.assertEqual((house['building_area'], house['land_area']), ('230 m²', '540 m²'))

    def test_mpg_superscript_area_is_land_only(self):
        house = next(p for p in self.case('mpg_res_page2_coverage.html') if p['name'] == 'Villa Baranca 19')
        self.assertEqual((house['beds'], house['baths']), ('3', '2'))
        self.assertEqual((house['building_area'], house['land_area']), ('', '333 m²'))
        self.assertEqual(house['location'], 'Paradera, Aruba')

    def test_objective_icons_are_labeled_not_card_numbers(self):
        house = next(p for p in self.case('objective_all_coverage.html') if 'Rooi Taki 3' in p['name'])
        self.assertEqual((house['beds'], house['baths']), ('2', '2'))
        self.assertTrue(house['image'].startswith('https://www.objective-realty.com/'))

    def test_whole_house_with_apartments(self):
        house = self.case('happyhomes_res_coverage.html')[0]
        self.assertEqual((house['name'], house['type'], house['price'], house['beds']), ('Matadera 9-U', 'House', 435000, '5'))
        self.assertIsNone(m.build_property('B', 'Apartment 12', 'https://b.test/12', 300000, 'Family home with two apartments'))

    def test_smiley_usd_api_filters_sold_and_individual_units(self):
        source = dict(name='Smiley Real Estate', type='direct_broker', adapter='myhome')
        items, observations, total, _ = c.parse_page((ROOT / 'smiley_api.json').read_text(), 'https://smileyaruba.com/', source, m)
        self.assertEqual([(p['name'], p['price']) for p in items], [('Ruby 23', 528089)])
        self.assertEqual(total, 4)
        self.assertEqual((items[0]['building_area'], items[0]['land_area'], items[0]['beds']), ('243 m²', '525 m²', ''))
        self.assertTrue(any(o['status'] == 'sold' for o in observations))

    def test_access_challenge_is_blocked_not_empty(self):
        source = dict(name='B', type='direct_broker', url='https://b.test', card_selector='article')
        with patch.object(m, 'get_page', return_value=('<title>Just a moment...</title><p>Enable JavaScript and cookies</p>', source['url'])):
            items, _, health = c.scrape(source, 'fast', m)
        self.assertEqual(items, [])
        self.assertEqual(health['status'], 'blocked')
        self.assertEqual(health['pages_fetched'], 0)

    def test_mpg_only_follows_advertised_page_numbers(self):
        soup = BeautifulSoup('<ul class="pagination"><a onclick="changePage(2)" href="javascript:void(0)">2</a><a onclick="changePage(17)">17</a></ul>', 'html.parser')
        links = c.page_links(soup, 'https://www.mpgaruba.com/houses-for-sale-aruba', {'name': 'MPG Aruba'})
        self.assertEqual(links, ['https://www.mpgaruba.com/houses-for-sale-aruba?page=2', 'https://www.mpgaruba.com/houses-for-sale-aruba?page=17'])

    def test_added_source_baseline_then_real_new_listing(self):
        source = dict(name='Objective Realty Aruba', type='direct_broker')
        first = self.case('objective_all_coverage.html')[0]
        fresh = dict(first, name='New standalone house 77', url='https://www.objective-realty.com/properties/new-house-77')
        health = dict(status='ok', checked_at=m.iso_now(), properties_found=1, cards_seen=1, pages_fetched=1, duration_seconds=0)
        result = lambda items: [(source, (items, [], copy.deepcopy(health)))]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            path.write_text(json.dumps(dict(schema_version=2, properties={'legacy': dict(first, name='Unrelated history', url='https://old.test/keep')}, baselined_sources=[])))
            with patch.object(m, 'STATE_FILE', str(path)), patch.object(m, 'DRY_RUN', True), patch.dict(os.environ, {'SCAN_MODE': 'deep', 'DEFER_DELIVERY': '1'}), patch.object(m, 'should_send_daily_digest', return_value=False), patch('development.enrich', side_effect=lambda items, *args: items):
                for items, expected in [([first], 0), ([first], 0), ([first, fresh], 1)]:
                    with patch.object(c, 'scan', return_value=result(items)):
                        m.main()
                    state = json.loads(path.read_text())
                    self.assertEqual(state['last_scan']['new'], expected)
                    self.assertIn('legacy', state['properties'])

    def test_conflicting_aliases_cannot_confirm_twice_in_one_scan(self):
        a = self.case('objective_all_coverage.html')[0]
        b = dict(a, name='Alternate broker title', url='https://www.objective-realty.com/properties/alternate', beds='3', baths='3')
        original = dict(a, beds='4', baths='4', aliases=[a['url'], b['url']])
        history = {'permanent': original}
        for expected in (0, 1, 0):
            history, new, reductions, major = m.reconcile(history, [a, b])
            self.assertEqual(len(major), expected)
            self.assertEqual(new, [])
            self.assertEqual(reductions, [])
            self.assertEqual(set(history), {'permanent'})

    def test_shallow_baseline_waits_for_deep_inventory(self):
        source = dict(name='Objective Realty Aruba', type='direct_broker')
        first = self.case('objective_all_coverage.html')[0]
        deeper = dict(first, name='Existing standalone house 99', url='https://www.objective-realty.com/properties/old-house-99')
        fresh = dict(first, name='New standalone house 77', url='https://www.objective-realty.com/properties/new-house-77')
        health = dict(status='partial', checked_at=m.iso_now(), properties_found=1, cards_seen=1, pages_fetched=1, duration_seconds=0, coverage_limited=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            path.write_text(json.dumps(dict(schema_version=2, properties={})))
            with patch.object(m, 'STATE_FILE', str(path)), patch.object(m, 'DRY_RUN', True), patch.dict(os.environ, {'DEFER_DELIVERY': '1'}), patch.object(m, 'should_send_daily_digest', return_value=False), patch('development.enrich', side_effect=lambda items, *args: items):
                for mode, items, expected in [('fast', [first], 0), ('deep', [first, deeper], 0), ('fast', [first, deeper, fresh], 1)]:
                    with patch.dict(os.environ, {'SCAN_MODE': mode}), patch.object(c, 'scan', return_value=[(source, (items, [], health))]):
                        m.main()
                    state = json.loads(path.read_text())
                    self.assertEqual(state['last_scan']['new'], expected)
                    self.assertEqual(source['name'] in state['baselined_sources'], mode != 'fast' or expected == 1)
