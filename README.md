# WoW population controller

Source-only Python population controller based on the v30 release. The runtime
configuration and identity gate bindings also support variable roster targets.
`scripts/login.py`, bytecode, snapshots, credentials, database data, server
configuration, and release artifacts are intentionally excluded.

The native integration boundary is the maintained
[`mod-python-engine`](https://github.com/Akotz89/mod-python-engine) fork at
engine revision `700a4f75a2e550f17022665d7b5b009c24e46a40`. Runtime and gameplay
acceptance remain separate from this source snapshot.

`lib/identity_gate.py` is the existing standalone CPI-196 identity and
ordinary-batch gate, retained byte-for-byte for the runtime's single-file
SHA-256 binding. It is source only: adding the file does not configure or
enable admission. This historical gate fixes a 10,000-identity target and a
specific schedule digest and version; it is not a tunable manifest policy.
Its ordinary-batch validation explicitly does not accept
talent templates; gameplay, restart, and persistence acceptance remain open.
The standalone CLI does not provide an eligibility-validation command; the
controller imports the gate for that purpose.

Its race/class allowlist was checked against
[`playercreateinfo.sql`](https://github.com/azerothcore/azerothcore-wotlk/blob/9fb906bb7296212ff42fc95ff73a92aaf8554f0d/data/sql/base/db_world/playercreateinfo.sql)
at core commit `9fb906bb7296212ff42fc95ff73a92aaf8554f0d` (SQL SHA-256
`0d74fddfeaac43fc07da188d1b6244a30088ca01b4d598d4ca7d317f619fdeac`).
That core revision contains a GPL version 2 license. This repository has no
declared license; provenance and licensing remain under review.

Prepare an offline ordinary-level-one candidate with explicit authority inputs:

```sh
python3 lib/prepare_ordinary_batch.py --corpus /path/source-corpus.json \
  --manifest /path/held-manifest.json --policy /path/policy.json \
  --decisions /path/decisions.json --schedule /path/wave-schedule.txt \
  --templates /path/templates.tsv --appearances /path/appearances.json \
  --corpus-sha256 TRUSTED_CORPUS_SHA256 \
  --output-dir /path/new-candidate-directory
```

The gate validates the manifest against the supplied decisions, schedule and
catalogues before generating output. Obtain the corpus digest and authority
inputs from independently trusted records: matching a caller-supplied digest
binds the bytes but does not establish their provenance or truth.
The output directory must not already exist. The report is published last;
if it is missing, the run is incomplete. An interrupted run may leave its
directory for cleanup. These files are candidates and do not authorize admission.

`lib/build_ordinary_release.py --help` describes the offline release builder.
It revalidates the authority, corpus and complete generator output, and requires
the absolute corpus file that will be named by the new eligibility digest.
It writes a new directory and publishes `release-complete.json` last; consumers
must verify its file hashes and absence means an incomplete run. These writes
are not an atomic multi-file transaction. `lib/release_transition.py` computes
an offline transition only; it performs no database writes or deployment.

Public CI checks Python syntax and an offline ledger integrity regression. It
requires no realm or credentials and does not prove native integration, runtime
behavior, or gameplay acceptance.
