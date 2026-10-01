import unittest
from unittest.mock import patch
import development as d
import monitor as m


def listing(n=1):
    return {'name':f'Land at Ponton {n}', 'type':'Land', 'source':'Aruba Brokers',
            'url':f'https://broker.test/land/{n}', 'price':100000, 'description':''}


class DevelopmentEvidence(unittest.TestCase):
    def test_scoped_evidence_excludes_neighbor_and_navigation(self):
        html = '''<nav>Zoned commercial, apartments permitted</nav>
        <div class="property-description">Freehold land. Zoning: residential.
        HOA restrictions apply. Building apartments is prohibited.
        <div class="related-properties">Leasehold land. Building area: 500 m²</div></div>
        <div class="detail-list">Lot size: 630 m² Building area: 167 m²</div>'''
        result=d.detail_fields(html,'https://broker.test/land/1',m)
        self.assertEqual((result['building_area'],result['land_area']),('167 m²','630 m²'))
        excerpts=' '.join(x['excerpt'] for x in result['development_evidence'])
        self.assertIn('prohibited',excerpts)
        self.assertNotIn('Leasehold',excerpts)
        self.assertNotIn('commercial',excerpts)
        self.assertEqual({x['label'] for x in result['development_evidence']},
                         {'Ownership','Zoning','Building restrictions','Apartment construction'})

    def test_no_claim_means_unknown_not_permission(self):
        p=listing()
        p['development_evidence']=d.evidence('Great residential land with investment potential',p['url'],m)
        self.assertEqual(p['development_evidence'],[])
        html=m.property_block(p,'new')
        self.assertIn('Apartment construction:</strong> Unknown',html)
        self.assertIn('permission remains unverified',html)
        with self.assertRaises(ValueError):d.detail_fields('<main>Search results</main>',p['url'],m)

    def test_bounded_reads_cache_failures_and_land_area_safety(self):
        items=[listing(n) for n in range(8)]
        html='<div class="property-description">Freehold. Lot size: 900 m² Building area: 200 m²</div>'
        with patch.object(m,'get_page',side_effect=lambda u:(html,u)) as get:
            d.enrich(items,{},'fast',m)
            self.assertEqual(get.call_count,2)
        self.assertFalse(items[0].get('building_area'))
        self.assertEqual(items[0]['land_area'],'900 m²')
        previous={m.property_key(p):p for p in items[:2]}
        with patch.object(m,'get_page') as get:
            d.enrich([listing(0),listing(1)],previous,'fast',m)
            self.assertEqual(get.call_count,0)
        with patch.object(m,'get_page',side_effect=ValueError('Blocked')) as get:
            broken=d.enrich([listing(90)],{},'deep',m)[0]
            self.assertEqual(broken['detail_status'],'unavailable')
            d.enrich([listing(90)],{m.property_key(broken):broken},'deep',m)
            self.assertEqual(get.call_count,1)

    def test_residence_membership_not_nearby_reference(self):
        found=d.detail_fields('<div class="property-description">This project is part of Paradera Private Residences.</div>','https://broker.test/1',m)
        self.assertTrue(found['residence_complex'])
        nearby=d.detail_fields('<div class="property-description">House near Paradera Private Residences.</div>','https://broker.test/1',m)
        self.assertFalse(nearby['residence_complex'])
