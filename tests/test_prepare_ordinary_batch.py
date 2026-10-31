"""Offline tests for the ordinary-batch candidate generator."""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import prepare_ordinary_batch as generator


class PrepareOrdinaryBatchTest(unittest.TestCase):
    def setUp(self):
        self.row = {'identity_key': 'one', 'account_key': 'account', 'account_name': 'ACCOUNT',
                    'character_name': 'Example', 'race_id': 1, 'class_id': 1,
                    'appearance': {'gender': 0, 'skin': 7, 'face': 7, 'hair_style': 7,
                                   'hair_color': 7, 'facial_hair': 7}}
        self.old = {'rows': [self.row]}
        self.corpus = {'population_identity': [], 'population_account': [], 'characters': [],
                       'state': {'pending_admission_key': None}}
        self.policy = {'gender_cycle': [0], 'appearance_variants_per_gender': 1}
        self.index = {'sections': {(1, 0, 1, 0, 0), (1, 0, 3, 0, 0)}, 'facial': {(1, 0, 0)}}
        self.patches = [patch.object(generator.source, 'index_source', return_value=self.index),
                        patch.object(generator.source, 'valid_appearance', return_value=True),
                        patch.object(generator.source, 'validate_ordinary_row', return_value=True)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def generate_synthetic(self, inputs, output):
        decisions = output.parent / 'decisions.json'
        decisions.write_text('{}')
        authority = dict(decisions_path=decisions, schedule_path=output.parent / 'schedule',
                         template_path=output.parent / 'templates', appearance_path=output.parent / 'appearances',
                         corpus_sha256=hashlib.sha256(inputs[0].read_bytes()).hexdigest())
        with patch.object(generator.source, 'validate_manifest') as validate:
            result = generator.generate(*inputs, output, **authority)
        validate.assert_called_once_with(self.old, {}, authority['schedule_path'],
                                         authority['template_path'], authority['appearance_path'])
        return result

    def test_reserved_and_pending_rows_preserved(self):
        for field in ('population_identity', 'pending'):
            corpus = copy.deepcopy(self.corpus)
            if field == 'pending':
                corpus['state']['pending_admission_key'] = 'one'
            else:
                corpus['population_identity'] = [{'identity_key': 'one'}]
            rows, holds, _ = generator.prepare(corpus, self.old, self.policy)
            self.assertEqual(rows[0], self.row)
            self.assertEqual(holds, {})
        rows, _, _ = generator.prepare(self.corpus, self.old, self.policy)
        self.assertNotEqual(rows[0]['appearance'], self.row['appearance'])
        self.assertEqual(self.old['rows'][0], self.row)

    def test_invalid_reservations_allocations_and_names_fail(self):
        corpus = copy.deepcopy(self.corpus)
        corpus['population_identity'] = [{'identity_key': 'foreign'}]
        with self.assertRaisesRegex(ValueError, 'foreign native reservation'):
            generator.prepare(corpus, self.old, self.policy)
        corpus = copy.deepcopy(self.corpus)
        corpus['population_account'] = [{'account_key': 'account', 'account_name': 'WRONG', 'expected_slots': 1}]
        with self.assertRaisesRegex(ValueError, 'account allocation mismatch'):
            generator.prepare(corpus, self.old, self.policy)
        corpus = copy.deepcopy(self.corpus)
        corpus['characters'] = [{'name': 'example'}]
        with self.assertRaisesRegex(ValueError, 'already has native character'):
            generator.prepare(corpus, self.old, self.policy)
        with self.assertRaisesRegex(ValueError, 'duplicate manifest identity'):
            generator.prepare(self.corpus, {'rows': [self.row, self.row]}, self.policy)
        with self.assertRaisesRegex(ValueError, 'malformed manifest row'):
            generator.prepare(self.corpus, {'rows': [{'identity_key': 'one'}]}, self.policy)

    def test_missing_faces_or_hair_fails_closed(self):
        for section in (1, 3):
            with self.subTest(section=section):
                self.index['sections'] = {(1, 0, section, 0, 0)}
                with self.assertRaisesRegex(ValueError, 'no verified ordinary appearance'):
                    generator.prepare(self.corpus, self.old, self.policy)

    def test_fresh_outputs_and_no_clobber(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = []
            for name, data in (('corpus', self.corpus), ('manifest', self.old), ('policy', self.policy)):
                path = root / name
                path.write_text(json.dumps(data))
                inputs.append(path)
            output = root / 'output'
            self.generate_synthetic(inputs, output)
            report = json.loads((output / 'preparation-report.json').read_text())
            self.assertFalse(report['template_gameplay_acceptance'])
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            with self.assertRaises(FileExistsError):
                self.generate_synthetic(inputs, output)
            self.assertEqual(before, {path.name: path.read_bytes() for path in output.iterdir()})

    def test_failure_does_not_publish_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = []
            for name, data in (('corpus', self.corpus), ('manifest', self.old), ('policy', self.policy)):
                path = root / name
                path.write_text(json.dumps(data))
                inputs.append(path)
            output = root / 'output'
            with patch.object(generator.os, 'link', side_effect=OSError('injected failure')):
                with self.assertRaisesRegex(OSError, 'injected failure'):
                    self.generate_synthetic(inputs, output)
            self.assertFalse((output / 'preparation-report.json').exists())
            self.assertFalse(output.exists())

    def test_failed_cleanup_preserves_unowned_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = []
            for name, data in (('corpus', self.corpus), ('manifest', self.old), ('policy', self.policy)):
                path = root / name
                path.write_text(json.dumps(data))
                inputs.append(path)
            output = root / 'output'

            def fail_with_unowned_file(*_args):
                (output / 'unowned').write_text('preserve')
                raise OSError('injected failure')

            with patch.object(generator.os, 'link', side_effect=fail_with_unowned_file):
                with self.assertRaisesRegex(OSError, 'injected failure'):
                    self.generate_synthetic(inputs, output)
            self.assertEqual((output / 'unowned').read_text(), 'preserve')
            self.assertEqual([item.name for item in output.iterdir()], ['unowned'])

    def test_invalid_inputs_fail_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = []
            for name, data in (('corpus', self.corpus), ('manifest', self.old), ('policy', self.policy),
                               ('decisions', {})):
                path = root / name
                path.write_text(json.dumps(data))
                inputs.append(path)
            output = root / 'output'
            authority = dict(decisions_path=inputs[3], schedule_path=root / 'schedule',
                             template_path=root / 'templates', appearance_path=root / 'appearances',
                             corpus_sha256=hashlib.sha256(inputs[0].read_bytes()).hexdigest())
            with self.assertRaises(TypeError):
                generator.generate(*inputs[:3], output)
            for wrong in ('0' * 64, 'NOT_A_DIGEST'):
                with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                    generator.generate(*inputs[:3], output, **dict(authority, corpus_sha256=wrong))
                self.assertFalse(output.exists())
            with self.assertRaises(generator.source.GateError):
                generator.generate(*inputs[:3], output, **authority)
            self.assertFalse(output.exists())
            with patch.object(generator.source, 'validate_manifest', side_effect=generator.source.GateError('wrong authority')):
                with self.assertRaisesRegex(generator.source.GateError, 'wrong authority'):
                    generator.generate(*inputs[:3], output, **authority)
            self.assertFalse(output.exists())

    def test_cli_requires_authority_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            result = subprocess.run([sys.executable, '-B', str(Path(generator.__file__)),
                                     '--corpus', 'unused', '--manifest', 'unused',
                                     '--policy', 'unused', '--output-dir', str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('--decisions', result.stderr)
            self.assertIn('--corpus-sha256', result.stderr)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
