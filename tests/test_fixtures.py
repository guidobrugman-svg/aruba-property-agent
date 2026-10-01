import json
import unittest
from pathlib import Path
import collectors
import monitor

class LiveFixtures(unittest.TestCase):
    def test_saved_listing_cards(self):
        root=Path(__file__).parent/'fixtures'
        for case in json.loads((root/'cases.json').read_text()):
            with self.subTest(source=case['source']['name']):
                items,observations,cards,_=collectors.parse_page((root/case['file']).read_text(),case['source']['url'],case['source'],monitor)
                self.assertEqual([[p['name'],p['type'],p['price']] for p in items],case['expected'])
                self.assertEqual(cards,case['cards'])
                self.assertTrue(all(p['type'] not in monitor.EXCLUDED_RESIDENTIAL_UNIT_TYPES for p in items))

    def test_bluefin_image_address_and_separate_areas(self):
        root=Path(__file__).parent/'fixtures'
        case=next(c for c in json.loads((root/'cases.json').read_text()) if c['source']['name']=='Bluefin Realtors')
        items,_,_,_=collectors.parse_page((root/case['file']).read_text(),case['source']['url'],case['source'],monitor)
        house=next(p for p in items if 'Pos Chiquito 19F' in p['name'])
        self.assertEqual((house['beds'],house['baths']),('3','2'))
        self.assertEqual((house['building_area'],house['land_area']),('167 m²','630 m²'))
        self.assertTrue(house['image'].startswith('https://bluefinrealtors.com/wp-content/'))
        self.assertIn('19F',house['location'])
        land=next(p for p in items if p['type']=='Land')
        self.assertEqual((land['building_area'],land['land_area']),('','1,466 m²'))
