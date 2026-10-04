import json
import unittest
import collectors
import monitor as m


class WholeApartments(unittest.TestCase):
    def test_multi_apartment_offer_does_not_require_house(self):
        for title in ('7 Studio Apartments in Moko', 'Seven studio apartments in Moko', '3 Apartments in Noord'):
            with self.subTest(title=title):
                p = m.build_property('B',title,'https://b.test/complex',600000,'For Sale Apartment 7 Bedrooms 7 Bathrooms')
                self.assertEqual(p['type'],'Apartment Complex')

    def test_explicit_entire_building_overrides_generic_apartment_label(self):
        p = m.build_property('B','Moko 56','https://b.test/complex',600000,'For Sale Apartment. The property consists of seven studio apartments.')
        self.assertEqual(p['type'],'Apartment Complex')

    def test_individual_units_and_remaining_project_units_stay_excluded(self):
        for title,text in [('Apartment 7','Entire apartment complex nearby'), ('Apartment 7 in an apartment complex','For Sale'), ('Studio 7 in Moko','Property contains seven apartments'), ('7 studio apartments available','Starting at $250000 per apartment'), ('3 Apartments in Paradise Residences','For Sale'), ('7 Apartments in Moko','Price per unit $100000')]:
            with self.subTest(title=title):
                self.assertIsNone(m.build_property('B',title,'https://b.test/unit',250000,text))

    def test_price_limit_and_unavailable_status_still_apply(self):
        self.assertIsNone(m.build_property('B','7 Studio Apartments in Moko','https://b.test/complex',695000,'For Sale'))
        self.assertIsNone(m.build_property('B','7 Studio Apartments in Moko','https://b.test/complex',600000,'Sold'))

    def test_aggregator_apartment_label_preserves_whole_complex(self):
        record=dict(title='7 Studio Apartments in Moko',seo='/sale/moko/complex',contract='for_sale',type='Apartment',price='$600,000',beds=7,baths=7,size=252)
        html='<script id="lx-mapdata" type="application/json">'+json.dumps([record])+'</script>'
        items,_,_,_=collectors.parse_page(html,'https://arubalistings.com/',dict(name='Aruba Listings',type='aggregator',adapter='aruba_listings'),m)
        self.assertEqual(items[0]['type'],'Apartment Complex')


if __name__=='__main__':
    unittest.main()
