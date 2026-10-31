"""Offline release boundary tests; no retained realm inputs required."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import build_ordinary_release as release


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.row = {'identity_key': 'bot', 'race_id': 1, 'class_id': 1,
                    'appearance': {'gender': 0, 'skin': 0, 'face': 0, 'hair_style': 0, 'hair_color': 0, 'facial_hair': 0}}
        self.corpus = self.write('corpus.json', {'population_identity': []})
        self.sha = hashlib.sha256(self.corpus.read_bytes()).hexdigest()
        self.old = {'rows': [self.row], 'manifest_digest': 'old', 'admission_holds': {'bot': 'held'}}
        self.manifest = self.write('manifest.json', self.old)
        self.decisions = self.write('decisions.json', {})
        self.policy = self.write('policy.json', {'gender_cycle': [0], 'appearance_variants_per_gender': 1})
        self.schedule = self.write('schedule.json', {})
        self.templates = self.write('templates.json', {})
        self.appearances = self.write('appearances.json', {})
        self.candidate = self.root / 'candidate'
        self.candidate.mkdir()
        fields = ['race_id', 'gender', 'skin', 'face', 'hair_style', 'hair_color', 'facial_hair']
        catalogue = [dict(zip(fields, [1, 0, 0, 0, 0, 0, 0]))]
        report = {'status': 'SOURCE_CANDIDATE_NOT_DEPLOYED', 'rows': 1, 'reserved_preserved': 0,
                  'gender_counts': {'0': 1}, 'race_class_gender_profiles': 1, 'held': {},
                  'source_corpus_sha256': self.sha, 'template_gameplay_acceptance': False,
                  'candidate_defaults_not_user_approved': {'gender_cycle': [0], 'appearance_variants_per_gender': 1}}
        for name, obj in [('rows.candidate.json', [self.row]), ('appearance-catalogue.candidate.json', catalogue), ('preparation-report.json', report)]:
            (self.candidate / name).write_bytes(release.canonical(obj))
        self.args = dict(corpus_path=self.corpus, corpus_sha256=self.sha, manifest_path=self.manifest,
            decisions_path=self.decisions, schedule_path=self.schedule, templates_path=self.templates,
            appearances_path=self.appearances, policy_path=self.policy, candidate_dir=self.candidate,
            generation=3, output_dir=self.root / 'release')
        self.patches = [patch.object(release.gate, 'validate_manifest'), patch.object(release.gate, 'validate_eligibility'),
                        patch.object(release, 'prepare', return_value=([self.row], {}, {}))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def write(self, name, obj):
        path = self.root / name
        path.write_text(json.dumps(obj))
        return path

    def test_complete_candidate_and_no_clobber(self):
        self.assertEqual(release.build(**self.args)['released'], 1)
        out = self.args['output_dir']
        marker = json.loads((out / 'release-complete.json').read_bytes())
        for name, digest in marker['files'].items():
            self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest)
        eligibility = json.loads((out / 'eligibility.candidate.json').read_bytes())
        self.assertIs(eligibility['releases'][0]['template_acceptance'], False)
        original = (out / 'release-complete.json').read_bytes()
        with self.assertRaises(FileExistsError):
            release.build(**self.args)
        self.assertEqual((out / 'release-complete.json').read_bytes(), original)

    def test_rejects_mismatch_and_incomplete_without_output(self):
        with self.assertRaises(ValueError):
            release.build(**dict(self.args, corpus_sha256='0' * 64))
        self.assertFalse(self.args['output_dir'].exists())
        (self.candidate / 'preparation-report.json').unlink()
        with self.assertRaises(FileNotFoundError):
            release.build(**self.args)
        self.assertFalse(self.args['output_dir'].exists())
        with self.assertRaises(ValueError):
            release.build(**dict(self.args, generation=True))
        self.assertFalse(self.args['output_dir'].exists())

    def test_cleanup_failed_write(self):
        with patch.object(release.os, 'fsync', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                release.build(**self.args)
        self.assertFalse(self.args['output_dir'].exists())

    def test_untrusted_authority_or_evidence_fails_before_output(self):
        with patch.object(release.gate, 'validate_manifest', side_effect=ValueError('authority')):
            with self.assertRaisesRegex(ValueError, 'authority'):
                release.build(**self.args)
        self.assertFalse(self.args['output_dir'].exists())
        with patch.object(release.gate, 'validate_eligibility', side_effect=ValueError('evidence')):
            with self.assertRaisesRegex(ValueError, 'evidence'):
                release.build(**self.args)
        self.assertFalse(self.args['output_dir'].exists())
        link = self.root / 'corpus-link.json'
        link.symlink_to(self.corpus)
        with self.assertRaises(ValueError):
            release.build(**dict(self.args, corpus_path=link))
        self.assertFalse(self.args['output_dir'].exists())


if __name__ == '__main__':
    unittest.main()
