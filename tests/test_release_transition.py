"""Offline transition guard checks."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from release_transition import digest, prepare_transition


def signed(obj, key):
    result = dict(obj)
    result[key] = digest(result)
    return result


class TransitionTest(unittest.TestCase):
    def setUp(self):
        self.row = dict(identity_key='bot', account_key='account', account_name='ACCOUNT',
                        character_name='Bot', guid_allocation_key='guid', wave_owner_level=1,
                        wave_ordinal=1, race_id=1, class_id=1, appearance={'face': 0})
        self.old = signed({'rows': [self.row], 'version': 1}, 'manifest_digest')
        self.new = signed({'rows': [self.row], 'version': 2}, 'manifest_digest')
        state = signed({'manifest_digest': self.old['manifest_digest'], 'bindings': {},
                        'admitted_identity_keys': [], 'pending_admission_key': None}, 'state_digest')
        self.snapshot = {'state': state, 'population_account': [dict(account_key='account',
                         account_name='ACCOUNT', account_id=None, expected_slots=1)],
                         'population_identity': [dict(identity_key='bot', account_key='account',
                         guid_allocation_key='guid', wave_key='owner-level-01:0001',
                         account_id=None, character_guid=None, manifest_digest=None, admitted=0)],
                         'auth_accounts': [], 'characters': []}

    def test_transition_and_duplicate_native_rejected(self):
        result, updates = prepare_transition(self.old, self.new, self.snapshot)
        self.assertEqual(result['manifest_digest'], self.new['manifest_digest'])
        self.assertEqual(updates, [])
        broken = copy.deepcopy(self.snapshot)
        broken['auth_accounts'] = [{'id': 1, 'username': 'A'}, {'id': 1, 'username': 'B'}]
        with self.assertRaises(ValueError):
            prepare_transition(self.old, self.new, broken)
        broken = copy.deepcopy(self.snapshot)
        broken['characters'] = [{'guid': 1, 'name': 'Other'}, {'guid': 2, 'name': 'other'}]
        with self.assertRaises(ValueError):
            prepare_transition(self.old, self.new, broken)

    def test_boolean_admitted_is_rejected(self):
        broken = copy.deepcopy(self.snapshot)
        broken['population_identity'][0]['admitted'] = True
        with self.assertRaisesRegex(ValueError, 'invalid admission flag'):
            prepare_transition(self.old, self.new, broken)

    def test_duplicate_identity_and_state_entries_are_rejected(self):
        second = dict(self.row, identity_key='other')
        old = signed({'rows': [self.row, second], 'version': 1}, 'manifest_digest')
        new = signed({'rows': [self.row, second], 'version': 2}, 'manifest_digest')
        with self.assertRaisesRegex(ValueError, 'duplicate identity'):
            prepare_transition(old, new, self.snapshot)
        broken = copy.deepcopy(self.snapshot)
        broken['state'].pop('state_digest')
        broken['state']['admitted_identity_keys'] = ['bot', 'bot']
        broken['state']['state_digest'] = digest(broken['state'])
        with self.assertRaisesRegex(ValueError, 'state admissions'):
            prepare_transition(self.old, self.new, broken)


if __name__ == '__main__':
    unittest.main()
