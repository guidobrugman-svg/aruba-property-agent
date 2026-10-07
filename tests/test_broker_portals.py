import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import collectors as c
import monitor as m

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {s['name']: s for s in json.loads((ROOT/'SOURCES.json').read_text())['sources']}
FIXTURE = json.loads((ROOT/'tests/fixtures/broker_portals.json').read_text())

class BrokerPortalTests(unittest.TestCase):
    def scan(self, name, replacements=None, source=None):
        data = copy.deepcopy(FIXTURE)
        data.update(replacements or {})
        source = source or SOURCES[name]
        def get(url, **kwargs):
            return json.dumps(data[url]), url
        with patch.object(m, 'get_page', side_effect=get) as reader:
            result = c.scrape(source, 'deep', m)
        return result, reader.call_count

    def test_evertsz_explicit_usd_land_and_under_contract_exclusion(self):
        (items, observations, health), reads = self.scan('Evertsz Real Estate')
        self.assertEqual(reads, 3)
        self.assertEqual(health['reported_records'], 4)
        self.assertEqual(len(items), 1)
        land = items[0]
        self.assertEqual((land['price'], land['type'], land['land_area']), (220000, 'Land', '416 m²'))
        self.assertEqual((land['beds'], land['baths'], land['building_area']), ('', '', ''))
        self.assertEqual(land['location'], '') # Purun prose conflicts with imported address/region.
        self.assertTrue(land['url'].startswith('https://arubalistings.com/sale/'))
        self.assertTrue(any(o['status'] == 'under contract' for o in observations))

    def test_blue_aruba_individual_condo_and_sothebys_over_budget_excluded(self):
        for name, count in [('Blue Aruba Realty', 2), ('Aruba Sotheby’s International Realty', 6)]:
            with self.subTest(name=name):
                (items, observations, health), reads = self.scan(name)
                self.assertEqual(items, [])
                self.assertEqual(health['cards_seen'], count)
                self.assertLessEqual(reads, 3)

    def test_identity_change_and_external_pagination_fail_closed(self):
        s = SOURCES['Evertsz Real Estate']
        identity = dict(FIXTURE[s['organization_url']], country_name='Curacao')
        (items, _, health), _ = self.scan(s['name'], {s['organization_url']: identity})
        self.assertEqual(items, [])
        self.assertEqual(health['pages_fetched'], 0)
        feed = dict(FIXTURE[s['url']], next='https://foreign.test/page=2')
        (items, _, health), reads = self.scan(s['name'], {s['url']: feed})
        self.assertEqual(items, [])
        self.assertEqual(reads, 2)
        self.assertTrue(health['errors'])

    def test_non_usd_asking_price_never_qualifies(self):
        s = SOURCES['Evertsz Real Estate']
        feed = copy.deepcopy(FIXTURE[s['url']])
        for r in feed['results']: r['currency'] = 'AWG'
        (items, _, _), reads = self.scan(s['name'], {s['url']: feed})
        self.assertEqual(items, [])
        self.assertEqual(reads, 2)

    def test_whole_apartment_complex_is_not_rejected_by_portal_unit_category(self):
        s = SOURCES['Blue Aruba Realty']
        feed = copy.deepcopy(FIXTURE[s['url']])
        row = feed['results'][1]
        detail_url = s['detail_api_url'] + row['slug'] + '/'
        detail = dict(FIXTURE[detail_url], title='Whole 7 apartment complex',
            description='Entire apartment building for sale with seven apartments, no separate house. USD 515000.')
        row['title'] = detail['title']
        (items, _, _), _ = self.scan(s['name'], {s['url']: feed, detail_url: detail})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['type'], 'Apartment Complex')

    def test_broker_pagination_is_bounded(self):
        s = dict(SOURCES['Evertsz Real Estate'], deep_pages=1)
        feed = dict(FIXTURE[s['url']], count=200, next=s['url'].replace('page=1', 'page=2'))
        (_, _, health), reads = self.scan(s['name'], {s['url']: feed}, s)
        self.assertEqual(health['pages_fetched'], 1)
        self.assertTrue(health['pagination_limited'])
        self.assertEqual(reads, 3)

    def test_north_side_scopes_broker_cards_and_excludes_current_offers(self):
        s = SOURCES['North Side Realty Aruba']
        html = (ROOT/'tests/fixtures/north_side.html').read_text()
        # An unrelated recommended card outside the broker tab must not count.
        html += '<div class="item-listing-wrap"><h2 class="item-title">Unrelated house</h2></div>'
        items, observations, count, soup = c.parse_page(html, s['url'], s, m)
        self.assertEqual((items, count), ([], 2))
        self.assertEqual(len(observations), 2)
        self.assertTrue(all('/property/' in o['url'] for o in observations))
        self.assertEqual(c.page_links(soup, s['url'], s), ['https://bluefinrealtors.com/agency/north/page/2/'])
