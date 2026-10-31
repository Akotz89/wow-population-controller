"""Prepare an offline ordinary-level-one candidate, preserving native identities."""

import argparse
import collections
import copy
import hashlib
import json
import os
from pathlib import Path

import identity_gate as source


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode() + b'\n'


def prepare(corpus, old, policy):
    """Prepare from previously validated corpus and manifest inputs."""
    if not isinstance(policy, dict) or set(policy) != {'gender_cycle', 'appearance_variants_per_gender'}:
        raise ValueError('composition policy keyset')
    genders = policy['gender_cycle']
    if not isinstance(genders, list) or not genders or any(type(g) is not int or g not in (0, 1) for g in genders):
        raise ValueError('gender cycle must contain zero/one integers')
    variants = policy['appearance_variants_per_gender']
    if type(variants) is not int or not 1 <= variants <= 256:
        raise ValueError('appearance variants must be 1..256')
    if not isinstance(old, dict) or not isinstance(old.get('rows'), list):
        raise ValueError('manifest rows must be a list')
    required = {'identity_key', 'account_key', 'account_name', 'character_name', 'race_id', 'class_id', 'appearance'}
    rows = old['rows']
    if any(not isinstance(row, dict) or not required <= row.keys() or
           not isinstance(row['identity_key'], str) or not row['identity_key'] or
           not isinstance(row['account_key'], str) or not isinstance(row['account_name'], str) or
           not isinstance(row['character_name'], str) or type(row['race_id']) is not int or
           type(row['class_id']) is not int or not isinstance(row['appearance'], dict) for row in rows):
        raise ValueError('malformed manifest row')
    oldrows = {row['identity_key']: row for row in rows}
    if len(oldrows) != len(rows):
        raise ValueError('duplicate manifest identity')

    index = source.index_source(corpus)
    reserved = {row['identity_key'] for row in corpus['population_identity']}
    pending = corpus['state'].get('pending_admission_key')
    if pending is not None:
        reserved.add(pending)
    names = {row['name'].casefold() for row in corpus['characters']}
    if not reserved <= oldrows.keys():
        raise ValueError('foreign native reservation')
    for account in corpus['population_account']:
        account_rows = [row for row in rows if row['account_key'] == account['account_key']]
        if (not account_rows or {row['account_name'] for row in account_rows} != {account['account_name']}
                or len(account_rows) != account['expected_slots']):
            raise ValueError('account allocation mismatch')

    choices = {}
    for race in sorted({row['race_id'] for row in rows}):
        for gender in set(genders):
            faces = sorted((t[3], t[4]) for t in index['sections'] if t[:3] == (race, gender, 1))
            hairs = sorted((t[3], t[4]) for t in index['sections'] if t[:3] == (race, gender, 3))
            facial = sorted(t[2] for t in index['facial'] if t[:2] == (race, gender))
            if not faces or not hairs:
                raise ValueError('no verified ordinary appearance ' + str((race, gender)))
            candidates = []
            for n in range(max(len(faces), len(hairs))):
                face, skin = faces[n % len(faces)]
                hair, color = hairs[n % len(hairs)]
                for style in facial:
                    appearance = dict(gender=gender, skin=skin, face=face, hair_style=hair,
                                      hair_color=color, facial_hair=style)
                    if source.valid_appearance(index, race, appearance):
                        candidates.append(appearance)
                        break
                if len(candidates) >= variants:
                    break
            if not candidates:
                raise ValueError('no verified ordinary appearance ' + str((race, gender)))
            choices[race, gender] = candidates

    result = copy.deepcopy(rows)
    counts = collections.Counter()
    holds = {}
    for row in result:
        key, race = row['identity_key'], row['race_id']
        if key not in reserved:
            if row['character_name'].casefold() in names:
                raise ValueError('unreserved identity already has native character')
            n = counts[race]
            gender = genders[n % len(genders)]
            counts[race] += 1
            row['appearance'] = copy.deepcopy(choices[race, gender][(n // len(genders)) % len(choices[race, gender])])
        try:
            source.validate_ordinary_row(index, row)
        except ValueError as error:
            holds[key] = str(error)
        if key in reserved and row != oldrows[key]:
            raise AssertionError('reserved row changed')
    return result, holds, choices


def generate(corpus_path, manifest_path, policy_path, output_dir, *, decisions_path,
             schedule_path, template_path, appearance_path, corpus_sha256):
    """Write a candidate into a new directory; report presence marks complete output."""
    corpus_bytes = Path(corpus_path).read_bytes()
    if not isinstance(corpus_sha256, str) or source.HEX64.fullmatch(corpus_sha256) is None:
        raise ValueError('expected corpus SHA-256 must be a lowercase hexadecimal digest')
    if hashlib.sha256(corpus_bytes).hexdigest() != corpus_sha256:
        raise ValueError('source corpus SHA-256 mismatch')
    old = json.loads(Path(manifest_path).read_bytes())
    decisions = json.loads(Path(decisions_path).read_bytes())
    source.validate_manifest(old, decisions, Path(schedule_path), Path(template_path), Path(appearance_path))
    corpus = json.loads(corpus_bytes)
    policy = json.loads(Path(policy_path).read_bytes())
    rows, holds, _ = prepare(corpus, old, policy)
    fields = ['race_id', 'gender', 'skin', 'face', 'hair_style', 'hair_color', 'facial_hair']
    catalogue = {(row['race_id'], *(row['appearance'][key] for key in fields[1:])) for row in rows}
    genders = dict(collections.Counter(row['appearance']['gender'] for row in rows))
    report = {'status': 'SOURCE_CANDIDATE_NOT_DEPLOYED', 'rows': len(rows),
              'reserved_preserved': len(corpus['population_identity']), 'gender_counts': genders,
              'race_class_gender_profiles': len({(row['race_id'], row['class_id'], row['appearance']['gender']) for row in rows}),
              'held': holds, 'source_corpus_sha256': hashlib.sha256(corpus_bytes).hexdigest(),
              'template_gameplay_acceptance': False, 'candidate_defaults_not_user_approved': policy}
    output = Path(output_dir)
    output.mkdir(mode=0o700)
    created = []

    def write_new(path, value):
        with path.open('xb') as handle:
            identity = os.fstat(handle.fileno())
            created.append((path, identity.st_dev, identity.st_ino))
            handle.write(canonical(value))
            handle.flush()
            os.fsync(handle.fileno())

    try:
        for name, value in (('rows.candidate.json', rows),
                            ('appearance-catalogue.candidate.json', [dict(zip(fields, t)) for t in sorted(catalogue)])):
            write_new(output / name, value)
        # Publish the complete report only after all candidate data is written.
        pending = output / '.preparation-report.tmp'
        write_new(pending, report)
        identity = pending.stat()
        created.append((output / 'preparation-report.json', identity.st_dev, identity.st_ino))
        os.link(pending, output / 'preparation-report.json')
        pending.unlink()
    except BaseException:
        for path, device, inode in reversed(created):
            try:
                identity = path.lstat()
                if (identity.st_dev, identity.st_ino) == (device, inode):
                    path.unlink()
            except OSError:
                pass
        try:
            output.rmdir()
        except OSError:
            pass
        raise
    return {'rows': len(rows), 'held': len(holds), 'gender_counts': genders}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--decisions', type=Path, required=True)
    parser.add_argument('--schedule', type=Path, required=True)
    parser.add_argument('--templates', type=Path, required=True)
    parser.add_argument('--appearances', type=Path, required=True)
    parser.add_argument('--corpus-sha256', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.corpus, args.manifest, args.policy, args.output_dir,
                              decisions_path=args.decisions, schedule_path=args.schedule,
                              template_path=args.templates, appearance_path=args.appearances,
                              corpus_sha256=args.corpus_sha256)))


if __name__ == '__main__':
    main()
