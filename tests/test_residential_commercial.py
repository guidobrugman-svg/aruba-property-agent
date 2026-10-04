import json
import unittest
from pathlib import Path
from unittest.mock import patch
import collectors as c
import monitor as m


class ResidentialCommercial(unittest.TestCase):
    def test_house_with_apartments_is_eligible_in_commercial_category(self):
        for title,text in [('Commercial House with 3 Apartments','For Sale Commercial property'), ('Commercial Apartment Complex','For Sale Apartment Complex'), ('House and three apartments','For Sale Commercial property')]:
            with self.subTest(title=title):
                self.assertIsNotNone(m.build_property('B',title,'https://b.test/mixed',600000,text,type_hint='Commercial'))

    def test_ordinary_businesses_and_individual_units_remain_excluded(self):
        for title,text in [('Commercial Office Building','For Sale'), ('Hotel with House and 3 Apartments','For Sale'), ('Warehouse','For Sale House with 3 Apartments'), ('Apartment 7','For Sale Commercial Apartment Complex'), ('Commercial House','For Sale Commercial property')]:
            with self.subTest(title=title):
                self.assertIsNone(m.build_property('B',title,'https://b.test/unit',400000,text,type_hint='Commercial'))

    def test_budget_and_sold_filters_remain(self):
        self.assertIsNotNone(m.build_property('B','House with 3 Apartments','https://b.test/mixed',695000,'For Sale Commercial property'))
        self.assertIsNone(m.build_property('B','House with 3 Apartments','https://b.test/mixed',800001,'For Sale Commercial property'))
        self.assertIsNone(m.build_property('B','House with 3 Apartments','https://b.test/mixed',600000,'Sold Commercial property'))

    def test_commercial_card_does_not_override_residential_income_evidence(self):
        source=dict(name='B',type='direct_broker',card_selector='article',type_selector='.type')
        html='<article><h2><a href="/12">House with 3 Apartments</a></h2><span class="type">Commercial</span>For Sale $600,000</article>'
        items,obs,_,_=c.parse_page(html,'https://b.test/',source,m)
        self.assertEqual(len(items),1)
        self.assertNotEqual(obs[0]['status'],'ineligible')

    def test_scoped_description_recovers_mixed_house_and_apartments(self):
        source=next(s for s in json.loads(Path(m.SOURCES_FILE).read_text())['sources'] if s['name']=='Aruba Happy Homes')
        url='https://arubahappyhomes.com/listings/for-sale/commercial'
        source=dict(source,url=url,urls=[url])
        card='<div class="rent-sec"><a class="link-cover" href="/listings/mixed"></a><div class="rent-contain"><div class="position-relative">Investment Property</div><div class="price">USD 600,000</div>For Sale Commercial property</div></div>'
        description='<div class="property-d-contain"><div class="richeditor"><p>This property consists of a main house with two spacious bedrooms, one of which is currently used as an office. In addition, there are three apartments: one two-bedroom unit and two one-bedroom units.</p></div></div>'
        with patch.object(m,'get_page',side_effect=lambda u,**kwargs: (card if u==url else description,u)):
            items,obs,health=c.scrape(source,'deep',m)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['type'],'House')
        self.assertEqual(health['status'],'ok')


if __name__=='__main__':
    unittest.main()
