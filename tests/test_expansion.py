import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
import collectors as c
import monitor as m

ROOT=Path(__file__).resolve().parents[1]
SOURCES={s['name']:s for s in json.loads((ROOT/'SOURCES.json').read_text())['sources']}
CASES={x['name']:x for x in json.loads((ROOT/'tests/fixtures/expansion_cases.json').read_text())}

class ExpansionTests(unittest.TestCase):
    def parse(self,name):
        case=CASES[name]
        return c.parse_page(case['html'],case['url'],SOURCES[name],m)

    def test_verified_card_layouts_are_recognized(self):
        for name in CASES:
            if name.endswith(' detail'):continue
            with self.subTest(source=name):
                items,observations,count,_=self.parse(name)
                self.assertGreater(count,0)
                self.assertTrue(observations)
                for p in items:
                    self.assertLessEqual(p['price'],800000)
                    self.assertNotIn(p['type'],('Apartment','Condominium'))
                    self.assertFalse(m.residence_unit_offer(p['name'],p['description']))

    def test_kermit_sold_reserved_and_explicit_building_area(self):
        items,obs,_,_=self.parse('Kermit Real Estate')
        house=next(p for p in items if p['name']=='Montaña 23, Noord, Aruba')
        self.assertEqual((house['price'],house['type'],house['building_area']),(699000,'House','342 m²'))
        self.assertTrue(house['image'].startswith('https://kermitaruba.com/'))
        self.assertTrue(any(o['status']=='sold' for o in obs))
        self.assertNotIn('Casa Solara, The San Miguel Retreat',[p['name'] for p in items])

    def test_able_houses_with_apartments_and_sold_home(self):
        items,obs,_,_=self.parse('ABLE Realty')
        house=next(p for p in items if p['name']=='Boegoeroei 36i')
        self.assertEqual((house['price'],house['type'],house['beds'],house['baths']),(450000,'House','6','4'))
        self.assertEqual(house['building_area'],'260 m²')
        self.assertNotIn('Hooiberg Home',[p['name'] for p in items])
        self.assertTrue(any(o['status']=='sold' for o in obs))

    def test_id_realty_actual_price_and_residential_fields(self):
        house=self.parse('ID Realty Group')[0][0]
        self.assertEqual((house['name'],house['price'],house['type']),('Caya Luna 40, San Barbola',365168,'House'))
        self.assertEqual((house['beds'],house['baths']),('4','3'))
        self.assertEqual(house['building_area'],'') # bare 492 has no explicit unit

    def test_maurer_spaced_suffix_usd_prices(self):
        items=self.parse('Maurer Real Estate')[0]
        house=next(p for p in items if 'Primavera' in p['name'])
        self.assertEqual(house['price'],510000)
        self.assertEqual(m.parse_price('897 000 fl / 503 000 $'),503000)
        self.assertIsNone(m.parse_price('897 000 fl'))

    def test_qobrix_title_is_property_not_status_and_follows_links(self):
        _,obs,_,soup=self.parse('XCLSV Aruba Realty')
        self.assertEqual(obs[0]['name'],'Blue Residences Corner Unit with Ocean Views')
        self.assertEqual(obs[1]['status'],'reserved')
        with self.assertRaises(c.ParserError):
            c.parse_page('<div class="qobrix-property property-item"><div class="et_pb_text">Broken</div></div>',SOURCES['XCLSV Aruba Realty']['url'],SOURCES['XCLSV Aruba Realty'],m)
        links=c.page_links(BeautifulSoup('<a data-page="2" href="/properties/?sale_rent=for_sale&page_num=2">2</a>','html.parser'),SOURCES['XCLSV Aruba Realty']['url'],SOURCES['XCLSV Aruba Realty'])
        self.assertEqual(len(links),1)

    def test_named_residence_units_and_harbour_house_unit_are_excluded(self):
        for title in ['Silent Listing - House + Apartment in Monte Verde Residence','ORA Residence in Savaneta - 1 Bedroom','Luca Model - Reina Sophia Residence','Harbour House Unit 610']:
            with self.subTest(title=title):
                self.assertIsNone(m.build_property('B',title,'https://b.test/1',500000,'For Sale House'))
        self.assertIsNotNone(m.build_property('B','Charming residence in Noord','https://b.test/2',500000,'For Sale House 3 bedrooms'))
        self.assertIsNotNone(m.build_property('B','Standalone new house','https://b.test/3',500000,'For Sale New Construction'))

    def test_capital_detail_resolves_whole_12_apartment_commercial_offer(self):
        source=SOURCES['Capital Reliance Aruba']
        items,obs,_,_=self.parse(source['name'])
        target=copy.deepcopy(obs[:1]);self.assertTrue(target[0]['needs_type_review'])
        with patch.object(m,'get_page',return_value=(CASES['Capital Reliance Aruba detail']['html'],target[0]['url'])):
            cache,reads=c.resolve_types(source,'deep',m,target,items,{},time.monotonic(),55)
        self.assertEqual(reads,1)
        self.assertEqual((items[0]['price'],items[0]['type']),(730337,'Apartment Complex'))
        self.assertNotIn('needs_type_review',target[0])
        # An explicitly numbered individual unit cannot be promoted by a hint.
        self.assertIsNone(m.build_property('B','Apartment 12','https://b.test/12',300000,'Commercial','',{},'Apartment Complex',residential_income_evidence=True))
        self.assertIsNone(m.build_property('B','Apartment in Noord','https://b.test/apartment',300000,'Family home with two apartments'))

    def test_verified_whole_complex_stays_available_across_history_scans(self):
        source=SOURCES['Capital Reliance Aruba']
        items,obs,_,_=self.parse(source['name'])
        target=copy.deepcopy(obs[:1])
        with patch.object(m,'get_page',return_value=(CASES['Capital Reliance Aruba detail']['html'],target[0]['url'])):
            c.resolve_types(source,'deep',m,target,items,{},time.monotonic(),55)
        health=dict(status='partial',cards_seen=1,pages_fetched=1,duration_seconds=0,pagination_limited=False,coverage_limited=True,source_revision=c.source_revision(source))
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';path.write_text(json.dumps(dict(schema_version=2,properties={})))
            with patch.object(m,'STATE_FILE',str(path)),patch.object(m,'DRY_RUN',True),patch.dict(os.environ,{'SCAN_MODE':'deep','DEFER_DELIVERY':'1'}),patch.object(m,'should_send_daily_digest',return_value=False),patch('development.enrich',side_effect=lambda rows,*args:rows):
                for _ in range(4):
                    with patch.object(c,'scan',return_value=[(source,(copy.deepcopy(items),copy.deepcopy(target),copy.deepcopy(health)))]):m.main()
                    state=json.loads(path.read_text())
                    self.assertEqual(state['last_scan']['major_changes'],0)
                    self.assertEqual(state['last_scan']['new'],0)
                    self.assertTrue(all(p['status']=='available' for p in state['properties'].values()))

    def test_partial_public_subset_alerts_fresh_listings_without_review_backfill(self):
        source=SOURCES['All Property Aruba']
        house=m.build_property(source['name'],'Standalone house 1','https://www.allpropertyaruba.com/house-1',400000,'For Sale House')
        old_lead=dict(house,name='Standalone house 2',url='https://www.allpropertyaruba.com/house-2')
        fresh=dict(house,name='Standalone house 3',url='https://www.allpropertyaruba.com/house-3')
        unresolved=dict(name=old_lead['name'],url=old_lead['url'],status='',needs_type_review=True)
        health=dict(status='partial',cards_seen=2,pages_fetched=1,duration_seconds=0,pagination_limited=False,coverage_limited=True,unclassified_cards=1,source_revision=c.source_revision(source))
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';path.write_text(json.dumps(dict(schema_version=2,properties={})))
            with patch.object(m,'STATE_FILE',str(path)),patch.object(m,'DRY_RUN',True),patch.dict(os.environ,{'SCAN_MODE':'deep','DEFER_DELIVERY':'1'}),patch.object(m,'should_send_daily_digest',return_value=False),patch('development.enrich',side_effect=lambda rows,*args:rows):
                for rows,observed,expected in [([house],[unresolved],0),([house],[unresolved],0),([house,old_lead],[],0),([house,old_lead,fresh],[],1)]:
                    with patch.object(c,'scan',return_value=[(source,(copy.deepcopy(rows),copy.deepcopy(observed),copy.deepcopy(health)))]):m.main()
                    state=json.loads(path.read_text())
                    self.assertEqual(state['last_scan']['new'],expected)

    def test_realtor_does_not_follow_countryless_pagination(self):
        source=SOURCES['Realtor International Aruba']
        soup=BeautifulSoup('<a href="/international/p2">2</a>','html.parser')
        self.assertEqual(c.page_links(soup,source['url'],source),[])

    def test_ambiguous_card_detail_excludes_prima_residence(self):
        source=SOURCES['Prima Casa Real Estate'];items,obs,_,_=self.parse(source['name'])
        target=copy.deepcopy(obs[:1])
        with patch.object(m,'get_page',return_value=(CASES['Prima Casa Real Estate detail']['html'],target[0]['url'])):
            c.resolve_types(source,'deep',m,target,items,{},time.monotonic(),55)
        self.assertEqual(items,[])
        self.assertTrue(target[0]['excluded'])

    def test_under_contract_description_banner_is_respected(self):
        source=SOURCES['Aruba Real Estate Brokers']
        obs=[dict(name='Fuerte 3',url='https://arubarealestatebrokers.com/property/for-sale-fuerte-3',needs_type_review=True,_candidate=dict(price=429000,text='For Sale',image='',meta={},declared='',commercial=False))]
        items=[]
        with patch.object(m,'get_page',return_value=(CASES['Aruba Real Estate Brokers detail']['html'],obs[0]['url'])):
            c.resolve_types(source,'deep',m,obs,items,{},time.monotonic(),55)
        self.assertEqual(items,[])
        self.assertEqual(obs[0]['status'],'under contract')

    def test_awg_only_detail_is_unconfirmed_not_a_usd_match(self):
        source=SOURCES['Casnan Real Estate']
        obs=[dict(name='House – Marawiel',url='https://casnan.com/property/house-for-sale-marawiel/',needs_type_review=True,_candidate=dict(price=None,text='For Sale AWG 2,314,000 House',image='',meta={},declared='',commercial=False))]
        items=[]
        with patch.object(m,'get_page',return_value=(CASES['Casnan Real Estate detail']['html'],obs[0]['url'])):
            c.resolve_types(source,'deep',m,obs,items,{},time.monotonic(),55)
        self.assertEqual(items,[])
        self.assertTrue(obs[0]['needs_type_review'])

    def test_realtor_original_usd_country_and_rental_guards(self):
        items=self.parse('Realtor International Aruba')[0]
        self.assertTrue(any(p['price']==557000 and p['type']=='House' for p in items))
        case=CASES['Realtor International Aruba'];soup=BeautifulSoup(case['html'],'html.parser')
        data=json.loads(soup.select_one('#__NEXT_DATA__').get_text())
        records=data['props']['apolloState']
        for k,v in records.items():
            if k.startswith('ListingDetail:'):v['country']='us'
        soup.select_one('#__NEXT_DATA__').string=json.dumps(data)
        self.assertEqual(c.parse_page(str(soup),case['url'],SOURCES[case['name']],m)[0],[])
        soup=BeautifulSoup(case['html'],'html.parser');data=json.loads(soup.select_one('#__NEXT_DATA__').get_text())
        for key,price in data['props']['apolloState'].items():
            if '.price(' in key:price['displayListingPrice']='AWG 700,000';price['displayConsumerPrice']='USD $393,258'
        soup.select_one('#__NEXT_DATA__').string=json.dumps(data)
        self.assertEqual(c.parse_page(str(soup),case['url'],SOURCES[case['name']],m)[0],[])

    def test_advertised_october_pagination_is_bounded(self):
        source=dict(SOURCES['Prima Casa Real Estate'],fast_pages=1,detail_description_selector='')
        fragment=json.loads(CASES[source['name']]['html'])
        fragment['#partial-properties']+='<a data-request="Listings::onFilter" data-request-data="{ page: 2 }">2</a>'
        with patch.object(m,'get_page',return_value=(json.dumps(fragment),source['url'])) as get:
            _,_,health=c.scrape(source,'fast',m)
        self.assertEqual(get.call_count,1)
        self.assertEqual(health['pages_fetched'],1)
        self.assertTrue(health['coverage_limited'])
        self.assertEqual(get.call_args.kwargs['request_headers']['X-OCTOBER-REQUEST-HANDLER'],'Listings::onFilter')

    def test_wpl_only_publishes_advertised_scroll_pages(self):
        source=SOURCES['Alto Vista Real Estate']
        soup=BeautifulSoup('<script>var wpl_listing_total_pages = 2; var wpl_listing_current_page = 1;</script>','html.parser')
        self.assertEqual(c.page_links(soup,'https://altovistarealestate.com/house-for-sale/',source),['https://altovistarealestate.com/house-for-sale/?wplpage=2'])

    def test_catalog_unavailable_entries_never_make_network_requests(self):
        disabled=[s for s in SOURCES.values() if s.get('enabled') is False]
        self.assertEqual(len(disabled),20)
        with patch.object(m,'get_page') as get:
            self.assertEqual(c.scan(disabled,'deep',{},m),[])
        get.assert_not_called()
        for s in disabled:self.assertTrue(s['disabled_reason'])
        self.assertEqual(m.source_priority('ABLE Realty'),0)
        self.assertEqual(m.source_priority('Realtor International Aruba'),2)

    def test_explicit_usd_formats_and_budget_boundary(self):
        self.assertEqual(m.parse_price('U$D: 730,337'),730337)
        self.assertEqual(m.parse_price('USD 450.000.00'),450000)
        self.assertIsNone(m.parse_price('Afl. 450.000'))
        self.assertIsNone(m.parse_price('€ 450,000'))
        self.assertIsNotNone(m.build_property('B','Family house','https://b.test/1',800000,'For Sale'))
        self.assertIsNone(m.build_property('B','Family house','https://b.test/1',800001,'For Sale'))

if __name__=='__main__':unittest.main()
