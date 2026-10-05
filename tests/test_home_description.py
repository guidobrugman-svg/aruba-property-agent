import json
import unittest
from pathlib import Path
from unittest.mock import patch
import collectors as c
import monitor as m


class HomeDescription(unittest.TestCase):
    def test_elementor_description_resolves_commercial_card(self):
        source=next(s for s in json.loads(Path(m.SOURCES_FILE).read_text())['sources'] if s['name']=='Home 4 Everyone')
        card='<div class="item-listing-wrap"><h2 class="item-title"><a href="/property/example/">Bubali 1-D</a></h2>For Sale Commercial $600,000</div>'
        for description,expected in [
            ('A multifunctional commercial property. Potential for an Apartment complex, Condominium, or as it is now with three warehouses and an apartment.',0),
            ('This property consists of a main house and three apartments.',1),
        ]:
            with self.subTest(description=description):
                detail='<nav>House with 3 apartments</nav><div class="elementor-widget-houzez-property-content">'+description+'</div><aside>Condominium for sale</aside>'
                with patch.object(m,'get_page',side_effect=lambda url,**kwargs: (card if url==source['url'] else detail,url)):
                    items,obs,health=c.scrape(source,'deep',m)
                self.assertEqual(len(items),expected)
                self.assertEqual(health['unclassified_cards'],0)
                self.assertIn(health['status'],('ok','empty'))


if __name__=='__main__':
    unittest.main()
