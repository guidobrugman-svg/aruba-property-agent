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
