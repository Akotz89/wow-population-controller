"""Offline regression checks for signed bands and durable admission intent."""
import sys
from collections import deque
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from population_runtime.bands import classify_earned_progress
from population_runtime.config import _load_bands
from population_runtime.controller import PopulationController
from population_runtime.errors import ArtifactError, BridgeError
from population_runtime.models import EarnedProgress, IdentityBinding
from population_runtime.state import load_state, new_state


def band_rows(offsets):
    return [dict(name=name, share=share, owner_level_offset=offset)
            for name, share, offset in zip(
                ('pioneer', 'scout', 'forward', 'main', 'trailing'),
                ('0.05', '0.15', '0.25', '0.35', '0.20'), offsets, strict=True)]


class SignedBandsAndAdmissionTest(unittest.TestCase):
    def test_signed_offsets_are_bounded_ordered_and_legacy_offsets_remain_valid(self):
        for offsets in ((-79, -1, 0, 1, 79), (0, 1, 2, 3, 4)):
            self.assertEqual(tuple(b.owner_level_offset for b in _load_bands(band_rows(offsets), 20)), offsets)
        for offsets in ((-80, -1, 0, 1, 79), (-79, -1, 0, 1, 80),
                        (-5, -3, -1, 0, True), (-5, -3, -1, 0, 0),
                        (-5, -3, 0, -1, 2)):
            with self.subTest(offsets=offsets), self.assertRaises(ArtifactError):
                _load_bands(band_rows(offsets), 20)

    def test_signed_ceilings_clamp_without_changing_earned_progress(self):
        bands = _load_bands(band_rows((-5, -3, -1, 0, 2)), 20)
        records = [EarnedProgress(f'bot-{i}', i + 1, 20, 100 + i, 200 + i, 0, 1, False, False)
                   for i in range(20)]
        before = list(records)
        assignments = classify_earned_progress(records, bands, owner_level=2, global_phase_ceiling=7)
        self.assertEqual({a.band: a.effective_ceiling for a in assignments},
                         {'pioneer': 7, 'scout': 5, 'forward': 3, 'main': 2, 'trailing': 1})
        self.assertEqual([a.identity_key for a in assignments], [r.identity_key for r in reversed(records)])
        self.assertEqual(records, before)
        extremes = _load_bands(band_rows((-79, -1, 0, 1, 79)), 20)
        assignments = classify_earned_progress(records, extremes, owner_level=80, global_phase_ceiling=80)
        ceilings = {a.band: a.effective_ceiling for a in assignments}
        self.assertEqual((ceilings['pioneer'], ceilings['trailing']), (80, 1))

    def test_pending_intent_is_readable_before_reserve_and_survives_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            controller = PopulationController.__new__(PopulationController)
            controller.config = SimpleNamespace(state_path=path, admission_batch_size=1, action_batch_size=1,
                admission_interval_seconds=30, pending_admission_limit=1, admission_capacity=1,
                newcomer_wave_counts=None)
            identity = SimpleNamespace(identity_key='bot', account_key='account', guid_allocation_key='guid',
                                       wave_key='wave', wave_owner_level=1)
            controller.manifest = SimpleNamespace(identities=(identity,), admission_holds=set(), legacy_identity_keys=set())
            controller.state = new_state('manifest', 'capabilities', 1)
            controller._identity_by_key = {'bot': identity}
            controller._account_slots = {'account': 1}
            controller.adapter = Mock()

            def fail_reservation(*args):
                saved = load_state(path, 'manifest', 'capabilities', 1)
                self.assertEqual(saved.pending_admission_keys, ['bot'])
                self.assertEqual(saved.admitted_identity_keys, set())
                raise BridgeError('native reservation failed')

            controller.adapter.population_reserve_identity.side_effect = fail_reservation
            with self.assertRaisesRegex(BridgeError, 'native reservation failed'):
                controller._admit_due_batch(owner_level=1, now=100, admission_slots=1)
            controller.adapter.population_reserve_identity.assert_called_once()
            controller.adapter.population_ensure_account.assert_not_called()
            self.assertEqual(load_state(path, 'manifest', 'capabilities', 1).pending_admission_keys, ['bot'])

    def test_tick_publishes_signed_ceiling_and_hold_from_earned_progress(self):
        controller = PopulationController.__new__(PopulationController)
        controller.config = SimpleNamespace(
            bands=_load_bands(band_rows((-5, -3, -1, 0, 2)), 20),
            admission_capacity=20, rank_denominator='current', online_cap=2,
            action_batch_size=2, admission_batch_size=1, admission_interval_seconds=30,
            snapshot_max_age_seconds=30, state_path='unused', audit_path='unused')
        controller.state = new_state('manifest', 'capabilities', 1)
        records = [EarnedProgress(f'bot-{i:02}', i + 1, 7 if i == 0 else 4 if i == 1 else 1,
                                  100 + i, 200 + i, 0, 1, i < 2, i < 2) for i in range(20)]
        controller.state.progress_cache = {r.identity_key: r for r in records}
        controller.state.admitted_identity_keys = set(controller.state.progress_cache)
        controller._artifact_digests = {}
        controller._recovery_pending = False
        controller._recovery_keys = deque()
        controller._last_release_key = ''
        controller._identity_by_key = {}
        controller.capabilities = SimpleNamespace(generation=1)
        controller.manifest = SimpleNamespace(manifest_digest='manifest', eligibility_digest='e', eligibility_generation=1)
        controller.adapter = Mock()
        controller.adapter.population_read_phase_snapshot.return_value = SimpleNamespace(
            enabled=True, policy_valid=True, level_ceiling=7, phase_id='p', policy_digest='d')
        controller.adapter.population_read_owner_high_water.return_value = SimpleNamespace(
            observed_epoch=100, authenticated=True, level=2)
        controller.adapter.population_read_capacity_snapshot.return_value = SimpleNamespace(
            observed_epoch=100, valid=True, generation=1, online_limit=2, available_sessions=0)
        controller.adapter.population_apply_progression_policy.return_value = True
        with patch.object(controller, '_refresh_progress'), \
             patch.object(controller, '_admit_due_batch', return_value=[]), \
             patch.object(controller, '_rotation_candidates', return_value=set()), \
             patch.object(controller, '_desired_sessions', return_value={'bot-00', 'bot-01'}), \
             patch('population_runtime.controller.save_state'), \
             patch('population_runtime.controller.append_audit'):
            self.assertTrue(controller._tick(100))
        controller.adapter.population_apply_progression_policy.assert_called_once_with([
            {'identity_key': 'bot-00', 'effective_ceiling': 7, 'held': True},
            {'identity_key': 'bot-01', 'effective_ceiling': 5, 'held': False}])
        self.assertEqual(controller.state.progress_cache, {r.identity_key: r for r in records})

    def test_successful_tick_persists_completed_admission_after_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            controller = PopulationController.__new__(PopulationController)
            controller.config = SimpleNamespace(state_path=path, audit_path=Path(directory) / 'audit.jsonl',
                admission_batch_size=1, action_batch_size=1, admission_interval_seconds=30,
                pending_admission_limit=1, admission_capacity=1, newcomer_wave_counts=None,
                snapshot_max_age_seconds=30, online_cap=1)
            identity = SimpleNamespace(identity_key='bot', account_key='account', account_name='account',
                guid_allocation_key='guid', wave_key='wave', wave_owner_level=1, immutable_creation_fields={})
            controller.manifest = SimpleNamespace(identities=(identity,), admission_holds=set(), legacy_identity_keys=set(),
                manifest_digest='manifest', eligibility_digest='e', eligibility_generation=1)
            controller.state = new_state('manifest', 'capabilities', 1)
            controller._identity_by_key = {'bot': identity}
            controller._account_slots = {'account': 1}
            controller._artifact_digests = {}
            controller._recovery_pending = False
            controller._recovery_keys = deque()
            controller._last_release_key = ''
            controller.capabilities = SimpleNamespace(generation=1)
            controller.adapter = Mock()
            controller.adapter.population_read_phase_snapshot.return_value = SimpleNamespace(
                enabled=True, policy_valid=True, level_ceiling=7, phase_id='p', policy_digest='d')
            controller.adapter.population_read_owner_high_water.return_value = SimpleNamespace(
                observed_epoch=100, authenticated=True, level=2)
            controller.adapter.population_read_capacity_snapshot.return_value = SimpleNamespace(
                observed_epoch=100, valid=True, generation=1, online_limit=1, available_sessions=0)

            def confirm_intent(*args):
                self.assertEqual(load_state(path, 'manifest', 'capabilities', 1).pending_admission_keys, ['bot'])
                return True

            controller.adapter.population_reserve_identity.side_effect = confirm_intent
            controller.adapter.population_ensure_account.return_value = 12
            controller.adapter.population_ensure_character.return_value = 34
            controller.adapter.population_bind_identity_once.return_value = IdentityBinding('bot', 12, 34)
            controller.adapter.population_set_admission.return_value = True
            with patch.object(controller, '_refresh_progress'), \
                 patch.object(controller, '_assignments', return_value=()), \
                 patch.object(controller, '_rotation_candidates', return_value=set()), \
                 patch.object(controller, '_desired_sessions', return_value=set()):
                self.assertTrue(controller._tick(100))
            controller.adapter.population_ensure_account.assert_called_once_with('account', 'account', 1)
            saved = load_state(path, 'manifest', 'capabilities', 1)
            self.assertEqual(saved.pending_admission_keys, [])
            self.assertEqual(saved.admitted_identity_keys, {'bot'})
            self.assertEqual(saved.bindings['bot'], IdentityBinding('bot', 12, 34))


if __name__ == '__main__':
    unittest.main()
