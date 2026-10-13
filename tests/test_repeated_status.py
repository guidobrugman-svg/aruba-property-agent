import copy
import unittest
import monitor as m


def prop(**fields):
    p = dict(name='House', url='https://broker.test/house', source='Broker',
             price=278090, type='House', status='available', beds='3', location='Noord')
    p.update(fields)
    return p


class RepeatedStatus(unittest.TestCase):
    def test_filter_oscillation_never_generates_major_alerts(self):
        for name, price in [('Turibana 35c', 599000), ('Cunucu Abao 112', 278090),
                            ('DE VEERSTRAAT 3', 228572)]:
            history = {'old': prop(name=name, price=price, status='ineligible')}
            for _ in range(4):
                for _ in range(2):
                    history, new, reductions, major = m.reconcile(history, [prop(name=name, price=price)])
                    self.assertEqual((new, reductions, major), ([], [], []))
                history['old']['status'] = 'ineligible'

    def test_real_availability_and_other_fields_still_confirm(self):
        for status in ['sold', 'under contract']:
            history = {'old': prop(status=status)}
            history, _, _, major = m.reconcile(history, [prop(beds='4')])
            self.assertEqual(major, [])
            history, _, _, major = m.reconcile(history, [prop(beds='4')])
            self.assertEqual(set(major[0][1]), {'Status', 'Bedrooms'})
        history = {'old': prop(status='ineligible')}
        history, _, _, _ = m.reconcile(history, [prop(beds='4', price=250000)])
        history, _, reductions, major = m.reconcile(history, [prop(beds='4', price=250000)])
        self.assertEqual(len(reductions), 1)
        self.assertEqual(set(major[0][1]), {'Bedrooms'})

    def test_pending_and_digest_cleanup_rebuilds_only_affected_outbox(self):
        correction = {'Status': {'old': 'ineligible', 'new': 'available'}}
        event = {'property': prop(), 'changes': correction, 'queued_at': '2026-10-13'}
        mixed = {'property': prop(url='https://broker.test/other'),
                 'changes': dict(correction, Bedrooms={'old': '3', 'new': '4'})}
        tokens = [m.event_token(event), m.event_token(mixed)]
        state = {'pending_major_changes': [copy.deepcopy(event), copy.deepcopy(mixed)],
                 'daily_activity': {'major_changes': [copy.deepcopy(event), copy.deepcopy(mixed)]},
                 'outbox': [{'kind': 'changes', 'snapshot': {'pending_major_changes': tokens}},
                            {'kind': 'digest', 'snapshot': {'activity': {'major_changes': tokens}}},
                            {'kind': 'new', 'snapshot': {}},
                            {'kind': 'changes', 'snapshot': {'pending_major_changes': ['unrelated']}}]}
        m.prune_internal_change_alerts(state)
        self.assertEqual(len(state['pending_major_changes']), 1)
        self.assertEqual(set(state['pending_major_changes'][0]['changes']), {'Bedrooms'})
        self.assertEqual(len(state['daily_activity']['major_changes']), 1)
        self.assertEqual([x['kind'] for x in state['outbox']], ['new', 'changes'])
        before = copy.deepcopy(state)
        m.prune_internal_change_alerts(state)
        self.assertEqual(state, before)


if __name__ == '__main__':
    unittest.main()
