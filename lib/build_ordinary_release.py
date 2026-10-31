"""Build a distinct offline ordinary-batch release from verified inputs."""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path

import identity_gate as gate
from prepare_ordinary_batch import canonical, prepare


def build(*, corpus_path, corpus_sha256, manifest_path, decisions_path, schedule_path,
          templates_path, appearances_path, policy_path, candidate_dir, generation, output_dir):
    corpus_path = Path(corpus_path)
    if not corpus_path.is_absolute() or corpus_path.is_symlink() or not corpus_path.is_file():
        raise ValueError('corpus must be an absolute regular non-symlink file')
    corpus_bytes = corpus_path.read_bytes()
    if not isinstance(corpus_sha256, str) or not gate.HEX64.fullmatch(corpus_sha256) or hashlib.sha256(corpus_bytes).hexdigest() != corpus_sha256:
        raise ValueError('corpus digest mismatch')
    if type(generation) is not int or generation < 1:
        raise ValueError('generation must be a positive integer')
    corpus = json.loads(corpus_bytes)
    old = json.loads(Path(manifest_path).read_bytes())
    decisions = json.loads(Path(decisions_path).read_bytes())
    policy = json.loads(Path(policy_path).read_bytes())
    gate.validate_manifest(old, decisions, Path(schedule_path), Path(templates_path), Path(appearances_path))
    candidate_dir = Path(candidate_dir)
    rows, holds, _ = prepare(corpus, old, policy)
    fields = ['race_id', 'gender', 'skin', 'face', 'hair_style', 'hair_color', 'facial_hair']
    catalogue = [dict(zip(fields, entry)) for entry in sorted({(r['race_id'], *(r['appearance'][k] for k in fields[1:])) for r in rows})]
    expected_report = {'status': 'SOURCE_CANDIDATE_NOT_DEPLOYED', 'rows': len(rows),
        'reserved_preserved': len(corpus['population_identity']),
        'gender_counts': dict(collections.Counter(r['appearance']['gender'] for r in rows)),
        'race_class_gender_profiles': len({(r['race_id'], r['class_id'], r['appearance']['gender']) for r in rows}),
        'held': holds, 'source_corpus_sha256': corpus_sha256,
        'template_gameplay_acceptance': False, 'candidate_defaults_not_user_approved': policy}
    for name, expected in (('rows.candidate.json', rows), ('appearance-catalogue.candidate.json', catalogue), ('preparation-report.json', expected_report)):
        if (candidate_dir / name).read_bytes() != canonical(expected):
            raise ValueError('incomplete or inconsistent generator output: ' + name)
    new_decisions = dict(decisions, candidate_appearance_catalogue_sha256=hashlib.sha256(canonical(catalogue)).hexdigest())
    manifest = dict(old, rows=rows, decision_digest=gate.digest(new_decisions))
    manifest.pop('manifest_digest')
    manifest['manifest_digest'] = gate.digest(manifest)
    gate.validate_manifest(manifest, new_decisions, Path(schedule_path), Path(templates_path), candidate_dir / 'appearance-catalogue.candidate.json')
    releases = [dict(identity_key=r['identity_key'], immutable_row_sha256=gate.digest(r),
        trial_scope='source-verified-ordinary-level1', template_acceptance=False,
        evidence_path=str(corpus_path), evidence_sha256=corpus_sha256)
        for r in rows if r['identity_key'] in manifest['admission_holds'] and r['identity_key'] not in holds]
    eligibility = dict(schema_version='cpi-196-admission-eligibility-v2', status='SOURCE_VERIFIED_ORDINARY_LEVEL1_BATCH',
        generation=generation, manifest_digest=manifest['manifest_digest'], releases=releases)
    eligibility['eligibility_digest'] = gate.digest(eligibility)
    gate.validate_eligibility(eligibility, manifest)
    outputs = {'decisions.held.json': canonical(new_decisions), 'identity-manifest.held.json': canonical(manifest),
               'eligibility.candidate.json': canonical(eligibility)}
    marker = canonical({'status': 'SOURCE_CANDIDATE_NOT_DEPLOYED', 'files': {n: hashlib.sha256(b).hexdigest() for n,b in outputs.items()}})
    output = Path(output_dir)
    output.mkdir(mode=0o700)
    created = []
    try:
        for name, data in (*outputs.items(), ('.release-complete.tmp', marker)):
            path = output / name
            with path.open('xb') as handle:
                st = os.fstat(handle.fileno())
                created.append((path, st.st_dev, st.st_ino))
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        pending = output / '.release-complete.tmp'
        st = pending.stat()
        created.append((output / 'release-complete.json', st.st_dev, st.st_ino))
        os.link(pending, output / 'release-complete.json')
        pending.unlink()
    except BaseException:
        for path, device, inode in reversed(created):
            try:
                st = path.lstat()
                if (st.st_dev, st.st_ino) == (device, inode):
                    path.unlink()
            except OSError:
                pass
        try:
            output.rmdir()
        except OSError:
            pass
        raise
    return {'manifest_digest': manifest['manifest_digest'], 'released': len(releases), 'source_profile_failures': len(holds)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('corpus', 'manifest', 'decisions', 'schedule', 'templates', 'appearances', 'policy', 'candidate-dir', 'output-dir'):
        parser.add_argument('--' + option, type=Path, required=True)
    parser.add_argument('--corpus-sha256', required=True)
    parser.add_argument('--generation', type=int, required=True)
    a = parser.parse_args()
    print(json.dumps(build(corpus_path=a.corpus, corpus_sha256=a.corpus_sha256, manifest_path=a.manifest,
        decisions_path=a.decisions, schedule_path=a.schedule, templates_path=a.templates,
        appearances_path=a.appearances, policy_path=a.policy, candidate_dir=a.candidate_dir,
        generation=a.generation, output_dir=a.output_dir)))


if __name__ == '__main__':
    main()
