import json
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from bs4 import BeautifulSoup
import collectors as c
import monitor as m

ROOT = Path(__file__).parent / 'fixtures'


class PartialCoverage(unittest.TestCase):
    def source(self, name):
        return next(s for s in json.loads(Path(m.SOURCES_FILE).read_text())['sources'] if s['name'] == name)

    def test_page_one_alias_does_not_refetch_first_inventory(self):
        for name, html in [('Ben Real Estate', '<a data-request-data="{ page: 1 }"></a><a data-request-data="{ page: 2 }"></a>'), ('MPG Aruba', '<div class="pagination"><a onclick="changePage(1)"></a><a onclick="changePage(2)"></a></div>')]:
            links = c.page_links(BeautifulSoup(html, 'html.parser'), self.source(name)['url'], self.source(name))
            self.assertEqual(len(links), 1)
            self.assertIn('page=2', links[0])

    def test_explicit_exclusions_are_not_unknown_types(self):
        source = dict(name='B', type='direct_broker', card_selector='article', type_selector='.type')
        html = '<article><h2><a href="/commercial">Office 7</a></h2><span class="type">Commercial</span>$450,000</article><article><h2><a href="/residence">Paradise Residences</a></h2>$400,000</article>'
        items, obs, _, _ = c.parse_page(html, 'https://b.test/', source, m)
        self.assertEqual(items, [])
        self.assertTrue(all(o['status']=='ineligible' and not o.get('needs_type_review') for o in obs))

    def test_scoped_description_recovers_house_and_caches_without_guessing_beds(self):
        source = self.source('Bluefin Realtors')
        card = (ROOT / 'type_bluefin_card.html').read_text()
        detail = (ROOT / 'type_bluefin_detail.html').read_text()
        # Navigation and neighboring unit descriptions must not decide type.
        detail = '<nav>Condominium</nav>' + detail + '<aside>Apartment for rent</aside>'
        def get(url, **kwargs):
            return (card if url==source['url'] else detail), url
        with patch.object(m, 'get_page', side_effect=get) as http:
            items, obs, health = c.scrape(source, 'deep', m)
            self.assertEqual(http.call_count, 2)
        self.assertEqual([(p['name'],p['type'],p['beds']) for p in items], [('Balorian 2, Jaburibari','House','')])
        self.assertEqual(health['status'], 'ok')
        self.assertEqual((items[0]['building_area'], items[0]['land_area']), ('52.2 m²', '285 m²'))
        with patch.object(m, 'get_page', side_effect=get) as http:
            again = c.scrape(source, 'deep', m, health)
            self.assertEqual(http.call_count, 1)
        self.assertEqual(again[2]['type_review_reads'], 0)

    def test_detail_excludes_individual_unit_without_inventing_rental_status(self):
        source = self.source('Keller Williams Aruba')
        source = dict(source, url='https://kw-aruba.com/listings/for-sale/condominium-townhouse', urls=['https://kw-aruba.com/listings/for-sale/condominium-townhouse'])
        card = (ROOT / 'type_kw_card.html').read_text()
        detail = (ROOT / 'type_kw_detail.html').read_text()
        with patch.object(m, 'get_page', side_effect=lambda url: ((card if url==source['url'] else detail),url)):
            items, obs, health = c.scrape(source, 'deep', m)
        self.assertEqual(items, [])
        self.assertEqual(obs[0]['status'], 'ineligible')
        self.assertEqual(health['unclassified_cards'], 0)

    def test_unavailable_detail_stays_unclassified_and_is_cached(self):
        source = self.source('Bluefin Realtors')
        card = (ROOT / 'type_bluefin_card.html').read_text()
        def get(url):
            if url==source['url']:return card,url
            raise TimeoutError('Unavailable')
        with patch.object(m, 'get_page', side_effect=get):
            items, obs, health = c.scrape(source, 'deep', m)
            again = c.scrape(source, 'deep', m, health)
        self.assertEqual(items, [])
        self.assertEqual(health['status'], 'partial')
        self.assertEqual(again[2]['type_review_reads'], 0)

    def test_home_timeout_setting_reaches_http_client(self):
        fake = Mock(); fake.get.return_value = Mock(text='ok',url='https://homeforeveryonearuba.com/')
        with patch.object(m._local, 'session', fake, create=True):
            m.get_page('https://homeforeveryonearuba.com/', timeout=tuple(self.source('Home 4 Everyone')['request_timeout']))
        self.assertEqual(fake.get.call_args.kwargs['timeout'], (8,12))

    def test_expanded_inventory_baselines_before_real_new_alerts(self):
        source = dict(name='B', type='direct_broker')
        first = m.build_property('B','House 12','https://b.test/12',450000,'For Sale House')
        fresh = dict(first,name='House 13',url='https://b.test/13')
        health = dict(status='ok',source_revision='expanded',coverage_limited=False,cards_seen=1,properties_found=1,pages_fetched=1,duration_seconds=0)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            path.write_text(json.dumps(dict(schema_version=2,properties={},baselined_sources=['B'],source_health={'B':{'source_revision':'old'}})))
            with patch.object(m,'STATE_FILE',str(path)), patch.object(m,'DRY_RUN',True), patch.dict(os.environ,{'DEFER_DELIVERY':'1'}), patch.object(m,'should_send_daily_digest',return_value=False), patch('development.enrich',side_effect=lambda p,*args:p):
                for mode,items,expected in [('fast',[first],0),('deep',[first],0),('fast',[first,fresh],1)]:
                    with patch.dict(os.environ,{'SCAN_MODE':mode}), patch.object(c,'scan',return_value=[(source,(items,[],copy.deepcopy(health)))]):
                        m.main()
                    self.assertEqual(json.loads(path.read_text())['last_scan']['new'],expected)


if __name__ == '__main__':
    unittest.main()
