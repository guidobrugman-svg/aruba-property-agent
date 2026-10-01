import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, Mock
import requests
import monitor as m
import collectors as c


def prop(**fields):
    p={'name':'House at Ponton 12','url':'https://broker.test/house/12','price':500000,
       'type':'House','location':'Ponton','source':'Aruba Brokers','beds':'3','baths':'2',
       'status':'available','detected_at':'2026-01-01T00:00:00+00:00'}
    p.update(fields)
    return p


class Rules(unittest.TestCase):
    def build(self,title,text,price=500000):
        return m.build_property('broker',title,'https://broker.test/12',price,text)

    def test_prices(self):
        for raw,value in [('USD 650,000',650000),('$650.000',650000),('US$ 650,000.50',650000.5),('USD 650.000,50',650000.5)]:
            with self.subTest(raw=raw):self.assertEqual(m.parse_price(raw),value)
        self.assertIsNone(m.parse_price('AWG 650000'))

    def test_land_rate(self):
        self.assertIsNone(m.parse_price('$98/M2'))
        self.assertEqual(m.parse_price('US$ 98 per m² Lot size: 1,000 m²'),98000)
        self.assertIsNone(m.parse_price('$98/M2 area 1000 m2 and 2000 m2'))

    def test_include_full_homes(self):
        for title,text in [('Land lot','For Sale land 1000 m2 nearby villa'),('3-bedroom residence','For Sale residential 3 beds 2 baths land details'),('Villa in resort','For Sale Residential near a hotel'),('Townhouse in community','For Sale Condominium category'),('Apartment complex','For Sale 4 apartments residential investment')]:
            with self.subTest(title=title):self.assertIsNotNone(self.build(title,text))

    def test_exclusions(self):
        for title,text in [('Condo 14','For Sale'),('Apartment 4','For Sale'),('Generic residence','For Sale Condominium'),('Commercial building','For Sale'),('House 14','For Rent $2000 per month'),('House 14','For Sale Sold'),('House 14','Under Contract'),('House 14','Sale in Progress'),('House 14','Withdrawn'),('House 14','On Hold'),('Timeshare villa','For Sale $14000 each week'),('L’Aquila | 1 Bedroom','New Development For Sale')]:
            with self.subTest(title=title,text=text):self.assertIsNone(self.build(title,text))
        self.assertIsNone(self.build('House 14','For Sale',650001))

    def test_broad_hint_cannot_promote_condo(self):
        self.assertIsNone(m.build_property('B','Gated Community Living','https://b.test/unit',463000,'For Sale Houses in Aruba Condominiums in Aruba 3 Bedrooms 2 Bathrooms',type_hint='Houses in Aruba, Condominiums in Aruba'))

    def test_generic_residential_labeled_beds(self):
        self.assertIsNotNone(self.build('Ponton 12','For Sale Residential Beds: 3 Baths: 2'))
        self.assertEqual(m.infer_property_type('Ponton 12','Residential Beds: 3 Baths: 2'),'House')
        self.assertIsNone(self.build('Ponton 12','For Sale Condominium Beds: 3 Baths: 2'))

    def test_rental_income_sale(self):
        self.assertIsNotNone(self.build('House 14','For Sale $500000 with potential rental income'))

    def test_identity_type_price_independent(self):
        p=prop(); self.assertEqual(m.property_key(p),m.property_key(dict(p,type='Villa',price=400000)))

    def test_lots_not_merged(self):
        self.assertEqual(len(m.cross_source_dedupe([prop(name='Palm Estates Lot 1'),prop(name='Palm Estates Lot 2')])),2)

    def test_marketing_titles_same_address(self):
        direct=prop(name='Ponton 12 Family Home')
        aggregate=prop(name='Spacious House in Ponton 12',source='Bluefin Realtors')
        self.assertEqual(len(m.cross_source_dedupe([direct,aggregate])),1)
        self.assertEqual(m.find_previous_record({'old':direct},aggregate)[0],'old')
        self.assertEqual(len(m.cross_source_dedupe([direct,prop(name='Ponton 13 Family Home')])),2)
        self.assertEqual(len(m.cross_source_dedupe([prop(name='Pavia 12 Unit 1'),prop(name='Pavia 12 Unit 2')])),2)

    def test_direct_preference_aliases(self):
        p=prop(); agg=prop(source='Bluefin Realtors',url='https://agg.test/12')
        result=m.cross_source_dedupe([agg,p]);self.assertEqual(len(result),1)
        self.assertEqual(result[0]['source'],'Aruba Brokers');self.assertIn(agg['url'],result[0]['aliases'])

    def test_legacy_history_reappearance(self):
        p=prop();history={'legacy-key':p}
        saved,new,*_=m.reconcile(history,[dict(p,type='Villa')])
        self.assertEqual(new,[]);self.assertIn('legacy-key',saved)
        self.assertEqual(saved['legacy-key']['detected_at'],p['detected_at'])
        saved,_,_,_=m.reconcile(saved,[]);self.assertIn('legacy-key',saved)
        self.assertEqual(m.reconcile(saved,[p])[1],[])

    def test_url_alias_title_change(self):
        p=prop(aliases=['https://agg.test/12']);history={'old':p}
        changed=prop(name='Renamed home',url='https://agg.test/12?utm_source=email')
        self.assertEqual(m.find_previous_record(history,changed)[0],'old')

    def test_price_reduction_confirmation(self):
        p=prop();history={'old':p};lower=prop(price=450000)
        h,n,r,ch=m.reconcile(history,[lower]);self.assertEqual(r,[]);self.assertEqual(h['old']['price'],500000)
        h,n,r,ch=m.reconcile(h,[lower]);self.assertEqual(len(r),1);self.assertEqual(r[0][1]['price'],500000)
        self.assertEqual(m.reconcile(h,[lower])[2],[])

    def test_basis_source_switch_no_reduction(self):
        for lower in [prop(price=450000,source='Bluefin Realtors'),prop(price=450000,calculated_price=True,price_area_m2=1000)]:
            self.assertEqual(m.reconcile({'old':prop()},[lower])[2],[])

    def test_major_changes_confirmation(self):
        h,n,r,ch=m.reconcile({'old':prop()},[prop(beds='4')]);self.assertEqual(ch,[])
        h,n,r,ch=m.reconcile(h,[prop(beds='4')]);self.assertEqual(len(ch),1)

    def test_failure_classification(self):
        for code,expected in [(403,'blocked'),(404,'url_changed'),(429,'rate_limited')]:
            response=requests.Response();response.status_code=code
            self.assertEqual(c.failure_status(requests.HTTPError(response=response)),expected)
        self.assertEqual(c.failure_status(requests.Timeout()),'timeout')
        self.assertEqual(c.failure_status(c.ParserError()),'parser_failed')

    def test_403_no_retry(self):
        response=requests.Response();response.status_code=403
        fake=Mock();fake.get.return_value=response
        with patch.object(m._local,'session',fake,create=True):
            with self.assertRaises(requests.HTTPError):m.get_page('https://broker.test/')
        self.assertEqual(fake.get.call_count,1)

    def test_transient_retry(self):
        good=Mock(text='ok',url='https://broker.test/')
        fake=Mock();fake.get.side_effect=[requests.Timeout(),good]
        with patch.object(m._local,'session',fake,create=True),patch.object(m.time,'sleep'):
            self.assertEqual(m.get_page('https://broker.test/')[0],'ok')
        self.assertEqual(fake.get.call_count,2)

    def test_rss_listing_scope(self):
        xml='<rss version="2.0"><channel><item><title>House Ponton 12 :: $450,000 US</title><link>https://b.test/12</link><description>3 bedrooms 2 baths rental income potential</description></item></channel></rss>'
        items,_,_,_=c.parse_page(xml,'https://feed.test/',{'name':'Coldwell Banker Aruba','type':'direct_broker'},m)
        self.assertEqual(items[0]['price'],450000)
        self.assertEqual(items[0]['type'],'House')

    def test_unclassified_sale_not_silent_zero(self):
        source={'name':'B','type':'direct_broker','url':'https://b.test/','card_selector':'article'}
        html='<article><h2><a href="/12">Ponton 12</a></h2>For Sale $450000 Beds: 3 Baths: 2</article>'
        with patch.object(m,'get_page',return_value=(html,source['url'])):
            items,_,health=c.scrape(source,'fast',m)
        self.assertEqual(items,[]);self.assertEqual(health['status'],'partial');self.assertEqual(health['unclassified_cards'],1)

    def test_parser_failure_not_empty(self):
        with self.assertRaises(c.ParserError):c.parse_page('<nav>House $500000</nav>','https://b.test',{'name':'B','card_selector':'.listing'},m)

    def test_scoped_card(self):
        html='<nav>Condo For Rent $2000</nav><article><h2><a href="/house">Land lot</a></h2><p>For Sale $100000 1000 m2</p></article><footer>Villa</footer>'
        source={'name':'B','type':'direct_broker','card_selector':'article'}
        items,_,_,_=c.parse_page(html,'https://b.test',source,m)
        self.assertEqual(items[0]['type'],'Land');self.assertEqual(items[0]['price'],100000)

    def test_one_download_next_links_only(self):
        source={'name':'B','type':'direct_broker','url':'https://b.test/','card_selector':'article','fast_pages':1,'deep_pages':2}
        html='<article><h2><a href="/lot">Land lot</a></h2>For Sale $100000</article><div class="pagination"><a href="/?page=2">2</a></div>'
        with patch.object(m,'get_page',return_value=(html,'https://b.test/')) as get:
            items,obs,health=c.scrape(source,'fast',m)
        self.assertEqual(get.call_count,1);self.assertTrue(health['coverage_limited'])

    def test_digest_catchup(self):
        with patch.object(m,'aruba_now',return_value=datetime(2026,9,30,9,tzinfo=m.ARUBA_TZ)):
            self.assertTrue(m.should_send_daily_digest({'last_digest_date':'2026-09-29'}))
            self.assertFalse(m.should_send_daily_digest({'last_digest_date':'2026-09-30'}))

    def test_digest_activity_only(self):
        subject,html=m.send_daily_digest(m.empty_daily_activity(),38,{},render_only=True)
        self.assertIn('38 qualifying',html);self.assertIn('since the previous',html)
        self.assertNotIn('VIEW PROPERTY',html)

    def test_email_quotes_and_schemes(self):
        self.assertIn('&#x27;',m.html_escape("a'b"))
        self.assertIsNone(m.build_property('B','House 12','javascript:alert(1)',500000,'For Sale'))
        self.assertEqual(m.build_property('B','House 12','https://b.test/12',500000,'For Sale',image='javascript:bad')['image'],'')

    def test_one_image(self):
        self.assertEqual(m.property_block(prop(image='https://b.test/a.jpg'),'new').count('<img'),1)

    def test_residence_projects_excluded_standalone_new_build_retained(self):
        for title in ('Paradera Private Residences', 'Reina Sophia Residences', 'New residence complex'):
            self.assertIsNone(self.build(title, 'For Sale New Construction Houses 2 bedrooms'))
        self.assertIsNotNone(self.build('Modern New Homes in Paradera', 'For Sale New Construction House 3 bedrooms'))
        self.assertIsNotNone(self.build('Standalone residential development', 'For Sale New Development'))
        self.assertIsNotNone(self.build('Charming residence in Noord', 'For Sale House 3 bedrooms'))
        self.assertEqual(m.property_block(prop(name='Paradera Private Residences'),'new'), '')

    def test_attached_email_bedroom_regressions(self):
        self.assertEqual(m.extract_details('Noord 40', '$559,000 Noord 40 Beds: 2 Baths: 2')[0:2], ('2','2'))
        self.assertEqual(m.extract_details('Caya Juan Pablo II No 61', '$178,850 Beds: 4 Baths: 2')[0], '4')
        land = self.build('Eigendom Land Ponton', '$142,040 Bed: 0 Bath: 0 Land 1000 m²')
        self.assertEqual((land['beds'], land['baths'], land['building_area']), ('','',''))
        old = dict(land, beds='40', baths='1')
        history, _, _, _ = m.reconcile({'existing':old}, [land])
        self.assertEqual(history['existing']['beds'], '')
        self.assertNotIn('40 beds', m.property_block(old, 'new'))

    def test_lazy_image_skips_data_placeholder(self):
        from bs4 import BeautifulSoup
        node = BeautifulSoup('<div><img alt="Beds" src="/bed.svg"><img src="data:image/svg+xml,blank" data-src="/house.jpg"></div>', 'html.parser')
        self.assertEqual(m.image_from(node,'https://b.test/'), 'https://b.test/house.jpg')

    def test_building_and_land_areas_remain_distinct(self):
        self.assertEqual(m.explicit_areas('Building area: 167 m² Lot size: 630 m²'), ('167 m²','630 m²'))
        self.assertEqual(m.area_m2('1 m²'), '')
        self.assertEqual(m.area_m2('1000 sqft'), '92.9 m²')
        html = m.property_block(prop(building_area='167 m²',land_area='630 m²'), 'new')
        self.assertIn('Building / built-up area:', html)
        self.assertIn('Land area:', html)
        missing = m.property_block(prop(location='', image='',size='422 m²'), 'new')
        self.assertIn('Location:</strong> Not provided', missing)
        self.assertIn('Building / built-up area:</strong> Not provided', missing)
        self.assertIn('scope unspecified', missing)
        self.assertIn('Image:</strong> Not provided', missing)

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';p.write_text('{bad')
            with self.assertRaises(json.JSONDecodeError):m.load_json(str(p),{})

    def test_durable_outbox_failure_then_success(self):
        p=prop(queued_at='2026-09-30T00:00:00+00:00')
        state={'properties':{'old':p},'pending_new':[p],'daily_activity':m.empty_daily_activity()}
        m.queue_message(state,'new',([p],[],[]),{'pending_new':[m.event_token(p)]})
        payload=copy.deepcopy(state['outbox'])
        with tempfile.TemporaryDirectory() as d,patch.object(m,'STATE_FILE',str(Path(d)/'state.json')):
            m.save_json(m.STATE_FILE,state)
            with patch.object(m,'send_email',return_value=False):m.flush_outbox(state)
            self.assertEqual(state['outbox'],payload);self.assertEqual(len(state['pending_new']),1)
            with patch.object(m,'send_email',return_value=True):m.flush_outbox(state)
            self.assertEqual(state['outbox'],[]);self.assertEqual(state['pending_new'],[])

    def test_digest_clears_only_snapshot_after_success(self):
        old=prop();fresh=prop(name='House Ponton 14',url='https://b.test/14')
        state={'daily_activity':{'new':[old],'reductions':[],'major_changes':[]},'properties':{}}
        snapshot={'date':'2026-09-30','activity':{'new':[m.event_token(old)],'reductions':[],'major_changes':[]}}
        m.queue_message(state,'digest',(state['daily_activity'],1,{}),snapshot)
        state['daily_activity']['new'].append(fresh)
        with tempfile.TemporaryDirectory() as d,patch.object(m,'STATE_FILE',str(Path(d)/'state.json')):
            with patch.object(m,'send_email',return_value=False):m.flush_outbox(state)
            self.assertEqual(len(state['daily_activity']['new']),2)
            with patch.object(m,'send_email',return_value=True):m.flush_outbox(state)
        self.assertEqual(state['daily_activity']['new'],[fresh])
        self.assertEqual(state['last_digest_date'],'2026-09-30')

    def test_source_nonfatal(self):
        source={'name':'B','type':'direct_broker','url':'https://b.test','card_selector':'article'}
        with patch.object(m,'get_page',side_effect=requests.Timeout()):
            records=c.scan([source],'fast',{},m)
        self.assertEqual(records[0][1][2]['status'],'timeout')

    def test_cooldown_skips(self):
        source={'name':'B','type':'direct_broker','url':'https://b.test','card_selector':'article'}
        health={'B':{'retry_after':(m.now_utc()+timedelta(hours=1)).isoformat(), 'source_revision':c.source_revision(source)}}
        with patch.object(m,'get_page') as get:self.assertEqual(c.scan([source],'fast',health,m),[])
        get.assert_not_called()


if __name__=='__main__':unittest.main()
