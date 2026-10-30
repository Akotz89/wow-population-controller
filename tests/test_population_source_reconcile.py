"""Offline checks for variable roster and gate-bound schedules."""
import json
from dataclasses import replace
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from population_runtime.canonical import digest_file
from population_runtime.config import _load_bands, load_config
from population_runtime.controller import PopulationController
from population_runtime.state import new_state
from population_runtime.errors import ArtifactError
from population_runtime.manifest import SCHEMA, load_manifest


class SourceReconcileTest(unittest.TestCase):
    def test_config_supports_large_target_and_preserves_safe_default(self):
        bands = [dict(name=n, share=s, owner_level_offset=i) for i, (n, s) in enumerate(zip(
            ('pioneer', 'scout', 'forward', 'main', 'trailing'), ('0.05', '0.15', '0.25', '0.35', '0.20')))]
        with tempfile.TemporaryDirectory() as directory:
            raw = {key: f'{directory}/{key}' for key in (
                'manifest_path', 'capabilities_path', 'identity_gate_path', 'decisions_path',
                'schedule_path', 'template_catalogue_path', 'appearance_catalogue_path', 'state_path', 'audit_path')}
            raw.update({key: 'a' * 64 for key in ('manifest_file_sha256', 'capabilities_file_sha256', 'identity_gate_sha256')})
            raw.update(schema_version='cpi-223-python-population-runtime-v1', enabled=True,
                       durable_target=10000, online_cap=10, admission_batch_size=1,
                       admission_interval_seconds=30, bridge_batch_size=1, action_batch_size=1,
                       snapshot_max_age_seconds=30, starter_zone_ids=[1], starter_zone_online_cap=2, bands=bands)
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(raw))
            config = load_config(path)
            self.assertEqual((config.durable_target, config.admission_capacity), (10000, 10000))
            self.assertEqual(replace(config, admission_capacity=None).admission_capacity, 10000)
            self.assertFalse(config.allow_overcap_sessions)
            raw['durable_target'] = True
            path.write_text(json.dumps(raw))
            with self.assertRaises(ArtifactError):
                load_config(path)
            bands[0].update(ceiling_mode='phase_cap', minimum_slots=10001)
            with self.assertRaises(ArtifactError):
                _load_bands(bands, 10000)

    def test_arrivals_prefer_underrepresented_planned_roles(self):
        controller = PopulationController.__new__(PopulationController)
        controller.state = new_state('a' * 64, 'b' * 64, 1)
        controller._identity_by_key = {
            'active': SimpleNamespace(role_intent='damage', faction='alliance', starter_region='a'),
            'damage': SimpleNamespace(role_intent='damage', faction='alliance', starter_region='a'),
            'healer': SimpleNamespace(role_intent='healer', faction='horde', starter_region='b'),
        }
        self.assertEqual(controller._arrival_order({'damage', 'healer'}, {'active'}, 1), ['healer'])

    def test_manifest_binds_schedule_and_target_to_gate_and_keeps_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('manifest', 'decisions', 'schedule', 'templates', 'appearance'):
                (root / name).write_text('{}')
            row = dict(ordinal=1, identity_key='old', guid_allocation_key='g', account_key='a',
                       account_name='a', account_slot=0, character_name='c', faction='alliance',
                       race_id=1, class_id=1, role_intent='damage', spec_template_id='s',
                       starter_region='r', wave_owner_level=1, wave_ordinal=1, initial_level=1, appearance={})
            result = dict(schema_version=SCHEMA, status='ACCEPTED', realm_id=1, identity_count=1,
                          schedule_version='new-wave-policy', schedule_sha256=digest_file(root / 'schedule'),
                          rows=[row], legacy_identity_proofs=[{'identity': {'identity_key': 'old'}}],
                          manifest_digest='m', decision_digest='d')
            gate = SimpleNamespace(SCHEDULE_VERSION='new-wave-policy', SCHEDULE_SHA256=result['schedule_sha256'],
                                   ROSTER_TARGET=1, load_json=lambda _: {}, validate_manifest=lambda *args: result)
            config = SimpleNamespace(manifest_path=root / 'manifest', manifest_file_sha256=digest_file(root / 'manifest'),
                decisions_path=root / 'decisions', schedule_path=root / 'schedule', template_catalogue_path=root / 'templates',
                appearance_catalogue_path=root / 'appearance', identity_gate_path=root / 'gate', identity_gate_sha256='x',
                durable_target=1, eligibility_path=None, eligibility_file_sha256=None)
            with patch('population_runtime.manifest._load_exact_gate', return_value=gate):
                self.assertEqual(load_manifest(config).legacy_identity_keys, frozenset({'old'}))
                gate.SCHEDULE_SHA256 = '0' * 64
                with self.assertRaises(ArtifactError):
                    load_manifest(config)
                gate.SCHEDULE_SHA256 = result['schedule_sha256']
                gate.ROSTER_TARGET = 2
                with self.assertRaises(ArtifactError):
                    load_manifest(config)
                gate.ROSTER_TARGET = 1
                (root / 'schedule').write_text('{"changed":true}')
                with self.assertRaises(ArtifactError):
                    load_manifest(config)


if __name__ == '__main__':
    unittest.main()
