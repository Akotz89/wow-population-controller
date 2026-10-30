"""Offline integrity checks; these do not authorize population admission."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from population_runtime.errors import ArtifactError  # noqa: E402
from population_runtime.ledger import load_ledger, new_ledger, save_ledger  # noqa: E402


class LedgerIntegrityTest(unittest.TestCase):
    def test_round_trip_rejects_wrong_manifest_and_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.json'
            manifest = 'a' * 64
            save_ledger(path, new_ledger(manifest))
            loaded = load_ledger(path, manifest)
            self.assertEqual(loaded.manifest_digest, manifest)
            self.assertEqual(loaded.rows, {})
            with self.assertRaises(ArtifactError):
                load_ledger(path, 'b' * 64)
            content = json.loads(path.read_text())
            content['revision'] = 1
            path.write_text(json.dumps(content))
            with self.assertRaises(ArtifactError):
                load_ledger(path, manifest)


if __name__ == '__main__':
    unittest.main()
