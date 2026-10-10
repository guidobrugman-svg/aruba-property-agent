import unittest
from bs4 import BeautifulSoup
import monitor as m


def property_record(**fields):
    result = dict(name='Ponton house', url='https://broker.test/house', source='Broker',
                  price=500000, type='House', beds='3', baths='2', location='Ponton',
                  status='available', building_area='180 m²', land_area='500 m²')
    result.update(fields)
    return result


class AlertChangeDetails(unittest.TestCase):
    def test_event_and_digest_show_previous_and_current_before_general_details(self):
        prop = property_record(beds='4')
        changes = {'Bedrooms': {'old': '3', 'new': '4'},
                   'Location': {'old': '<old & address>', 'new': 'Ponton'}}
        event = m.send_event_email([], [], [(prop, changes)], render_only=True)[1]
        activity = m.empty_daily_activity()
        m.record_daily_activity(activity, [], [], [(prop, changes)])
        digest = m.send_daily_digest(activity, 1, {}, render_only=True)[1]
        for html in (event, digest):
            soup = BeautifulSoup(html, 'html.parser')
            table = soup.find('table')
            self.assertEqual([x.get_text() for x in table.select('thead th')], ['Detail', 'Previous', 'Current'])
            self.assertEqual([x.get_text() for x in table.select('tbody tr')[0].find_all(['th', 'td'])], ['Bedrooms', '3', '4'])
            self.assertIn('&lt;old &amp; address&gt;', html)
            self.assertLess(html.index('What changed:'), html.index('Development checks'))

    def test_areas_need_two_matching_observations_and_retain_old_values(self):
        old = property_record()
        observed = property_record(building_area='210 m²', land_area='550 m²')
        history, _, _, changes = m.reconcile({'old': old}, [observed])
        self.assertEqual(changes, [])
        self.assertEqual(history['old']['building_area'], '180 m²')
        history, _, _, changes = m.reconcile(history, [observed])
        self.assertEqual(changes[0][1], {
            'Building / built-up area': {'old': '180 m²', 'new': '210 m²'},
            'Land area': {'old': '500 m²', 'new': '550 m²'}})

    def test_digest_preserves_earliest_value_and_removes_reverted_changes(self):
        activity = m.empty_daily_activity()
        prop = property_record()
        for before, after in [('3', '4'), ('4', '5')]:
            m.record_daily_activity(activity, [], [], [(prop, {'Bedrooms': {'old': before, 'new': after}})])
        self.assertEqual(activity['major_changes'][0]['changes']['Bedrooms'], {'old': '3', 'new': '5'})
        m.record_daily_activity(activity, [], [], [(prop, {'Bedrooms': {'old': '5', 'new': '3'}})])
        self.assertEqual(activity['major_changes'], [])

    def test_legacy_alert_without_values_is_explicit(self):
        html = m.property_block(property_record(), 'major', changes={})
        self.assertIn('exact changes are unavailable', html)


if __name__ == '__main__':
    unittest.main()
