# Private source review status — 2026-10-21

This note accompanies a historical, source-only candidate for `Akotz89/wow-population-controller` on branch `codex/cpi225-controller-source-20261021`.

- Candidate base: `4db9f9e068b93e011a0725abbcedda12e71fb8a4`.
- The original candidate remains exactly 92 paths; its binary staged-diff SHA-256 before this note is `9c840990a79cf4c6625dace4464076b94179dec90621a265d4bf25f74682a64a`.
- This note is the only additional path. It records unresolved review findings and does not change candidate source.
- The publication is private historical source custody only. It is not a merge to `main` or acceptance of build, deployment, runtime, gameplay, or corrected behavior.
- Remediation ownership remains in a separate CPI-225 successor scope; this publication task does not patch the findings. Root must assign or confirm each corrective owner and acceptance before fixes proceed.
- The existing Gitleaks 8.30.1 receipt records one `generic-api-key` detector match, classified as a stable actor-identity digest rather than an authentication credential, with zero scanner errors. No value is reproduced here and the scan was not rerun for publication.

## Open findings

1. **P1 — Approved build host is not enforced.** `scripts/wow-build-host-guard:4-14` rejects `server-01` and accepts any kernel string containing `microsoft` or `wsl`, which does not prove the required regular-PC WSL2 host. The CMake caller invokes the guard before project configuration and owned targets (`native/CMakeLists.txt:2-8,44-49`).
2. **P1 — Custody recovery accepts incomplete or falsely settled rows.** `lib/native_custody_ack.py:80-88` validates present rows but does not require an exact census of terminal originals or a retained native result for applied/refused rows. `pending()` skips settled statuses (`:90-92`), so an edited or truncated queue can suppress required acknowledgements.
3. **P1 — Per-band quota can release protected sessions outside rotation.** `lib/population_runtime/controller.py:380-394,444-481,572-604` can leave a protected current identity outside `desired`, then release it even when it was not selected for interval-qualified rotation.
4. **P2 — Online-only occupants are omitted from request accounting.** `lib/population_runtime/controller.py:526-538,572-585` builds requests from `desired - current` without excluding identities that are online while `session_requested` is false; native capacity can be miscounted.
5. **P2 — Fail-closed persists an unobserved offline state.** `lib/population_runtime/controller.py:116-125,226-272,486-493` persists `online=false` before every record is replaced by an authoritative snapshot. The reviewed tests do not assert the durable cache state or establish that fail-closed guarantees physical/logical offline status.
6. **P2 — Witness release can remain pending for an offline actor.** `lib/witness_runtime.py:373-378,161-168,435-438` requires an online actor before release; the caught error leaves `release_pending` unchanged and can stall cleanup until reconnect.

These findings are retained from the review triage at the candidate baseline. No corrective change or test was made as part of this source publication.
