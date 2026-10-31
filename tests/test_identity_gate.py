"""Offline contract checks for the hash-bound standalone CPI-196 gate."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from population_runtime.errors import ArtifactError
from population_runtime.manifest import _load_exact_gate

GATE = ROOT / 'lib' / 'identity_gate.py'
GATE_SHA256 = 'ec52e08df775054ac13f7306f9482e87f1d5d8900ce2c78f99a6d4286940882a'


class IdentityGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gate = _load_exact_gate(GATE, GATE_SHA256)

    def signed(self, schema, manifest, releases):
        result = {
            'schema_version': f'cpi-196-admission-eligibility-{schema}',
            'status': 'REVIEWED_CONTROLLED_TRIAL' if schema == 'v1' else 'SOURCE_VERIFIED_ORDINARY_LEVEL1_BATCH',
            'generation': 1, 'manifest_digest': manifest['manifest_digest'], 'releases': releases,
        }
        result['eligibility_digest'] = self.gate.digest(result)
        return result

    def test_loader_enforces_exact_file_and_hash(self):
        self.assertEqual(hashlib.sha256(GATE.read_bytes()).hexdigest(), GATE_SHA256)
        self.assertTrue(callable(self.gate.validate_manifest))
        self.assertTrue(callable(self.gate.validate_eligibility))
        with self.assertRaises(ArtifactError):
            _load_exact_gate(GATE, '0' * 64)
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / 'gate.py'
            link.symlink_to(GATE)
            with self.assertRaises(ArtifactError):
                _load_exact_gate(link, GATE_SHA256)

    def test_empty_v1_and_v2_release_and_invalid_binding(self):
        manifest = {'manifest_digest': 'manifest', 'rows': [], 'admission_holds': {}}
        for schema in ('v1', 'v2'):
            with self.subTest(schema=schema):
                valid = self.signed(schema, manifest, [])
                self.assertEqual(self.gate.validate_eligibility(valid, manifest), valid)
                invalid = dict(valid, manifest_digest='other')
                invalid['eligibility_digest'] = self.gate.digest({k: v for k, v in invalid.items() if k != 'eligibility_digest'})
                with self.assertRaises(self.gate.GateError):
                    self.gate.validate_eligibility(invalid, manifest)
                invalid = self.signed(schema, manifest, [])
                invalid['generation'] = 0
                invalid['eligibility_digest'] = self.gate.digest({k: v for k, v in invalid.items() if k != 'eligibility_digest'})
                with self.assertRaises(self.gate.GateError):
                    self.gate.validate_eligibility(invalid, manifest)

    def test_v2_release_binds_held_row_and_rejects_template_acceptance(self):
        row = {'identity_key': 'bot', 'race_id': 1, 'class_id': 1, 'initial_level': 1,
               'appearance': {'gender': 0, 'skin': 0, 'face': 0, 'hair_style': 0, 'hair_color': 0, 'facial_hair': 0}}
        manifest = {'manifest_digest': 'manifest', 'rows': [row], 'admission_holds': {'bot': 'held'}}
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / 'corpus.json'
            evidence.write_text(json.dumps({}))
            release = {'identity_key': 'bot', 'immutable_row_sha256': self.gate.digest(row),
                       'trial_scope': 'source-verified-ordinary-level1', 'evidence_path': str(evidence),
                       'evidence_sha256': hashlib.sha256(evidence.read_bytes()).hexdigest(), 'template_acceptance': False}
            with patch.object(self.gate, 'index_source', return_value={}), \
                 patch.object(self.gate, 'validate_ordinary_row', return_value=True) as validate:
                valid = self.signed('v2', manifest, [release])
                self.assertEqual(self.gate.validate_eligibility(valid, manifest), valid)
                validate.assert_called_once_with({}, row)
                for change in ({'immutable_row_sha256': '0' * 64}, {'template_acceptance': True},
                               {'evidence_sha256': '0' * 64}, {'identity_key': 'unknown'}):
                    with self.subTest(change=change), self.assertRaises(self.gate.GateError):
                        bad = copy.deepcopy(release)
                        bad.update(change)
                        self.gate.validate_eligibility(self.signed('v2', manifest, [bad]), manifest)

    def test_v1_release_requires_evidence_and_no_template_acceptance(self):
        row = {'identity_key': 'bot', 'race_id': 1, 'class_id': 1, 'initial_level': 1,
               'appearance': {'gender': 0, 'skin': 0, 'face': 0, 'hair_style': 0, 'hair_color': 0, 'facial_hair': 0}}
        manifest = {'manifest_digest': 'manifest', 'rows': [row], 'admission_holds': {'bot': 'held'}}
        scope = 'ordinary-level1-human-warrior-lifecycle'
        data = {'identity': row, 'scope': scope, 'template_acceptance': False,
                'status': 'SOURCE_AND_DBC_SUPPORTED_FOR_ORDINARY_TRIAL',
                'playercreateinfo': {'race': 1, 'class': 1, 'map': 0, 'zone': 12},
                'dbc': [{'name': n, 'matches': [1], 'sha256': 'a' * 64} for n in
                        ('CharSections', 'CharHairGeosets', 'CharacterFacialHairStyles', 'ChrRaces', 'CharStartOutfit')]}
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / 'evidence.json'
            evidence.write_text(json.dumps(data))
            release = {'identity_key': 'bot', 'immutable_row_sha256': self.gate.digest(row), 'trial_scope': scope,
                       'evidence_path': str(evidence), 'evidence_sha256': hashlib.sha256(evidence.read_bytes()).hexdigest(),
                       'template_acceptance': False}
            valid = self.signed('v1', manifest, [release])
            self.assertEqual(self.gate.validate_eligibility(valid, manifest), valid)
            for change in ({'template_acceptance': True}, {'immutable_row_sha256': '0' * 64},
                           {'evidence_sha256': '0' * 64}):
                with self.subTest(change=change), self.assertRaises(self.gate.GateError):
                    bad = dict(release, **change)
                    self.gate.validate_eligibility(self.signed('v1', manifest, [bad]), manifest)


if __name__ == '__main__':
    unittest.main()
