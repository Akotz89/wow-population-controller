What this change does: The foreign-writer scan reads descriptor metadata before resolving a potentially inaccessible foreign descriptor. Proven read-only descriptors or different inodes are skipped; possible writers to the pinned inode still require matching-device/inode resolution.

Looks good. Source-clear for this narrow correction.

All22 tests pass independently, including the four denied-stat/fdinfo regressions and existing real-FD/inode/foreign-writer tests. An extra fallback retry remains byte-idempotent, preserves original failed history, and creates no fabricated journal rows. Read-only metadata skips before denied stat; writable different-inode metadata skips before denied stat; matching writable inode plus denied stat refuses; unreadable fdinfo refuses. The existing actual matching writable log FD and foreign-writer rejection remain covered. Tests enumerate local owned processes; permission failures are simulated at the foreign descriptor boundary.

Reviewed source SHA256: `6e64d9bb085a12693c6bf301e3e402d7280444725b1ddec0da98dd59cec8c4e2`. Pins remain `9f1ab57d5e87782cc02cdf978d74b16c0bc619afb7b0677d4c0788e5a8b0a521`; delta SHA256 is `1c48f4ccb96a47b49bae57f11f6e545da6ddde9903070134284432bdf7ce6572`. Prior b82e source is preserved exactly. All VERIFIED source hashes match. AST comparison confirms only Linux.owned_log_prefix changed; no added/removed functions or custody/lifecycle changes. Active460c is exact and unchanged.

The predicate uses the access-mode bits from actual fdinfo flags and inode metadata. It skips only descriptors proved unable to be the current writable log inode; a possible matching writable inode still resolves device/inode and refuses a true match. Missing/malformed/denied metadata is not caught as benign absence; only disappearing processes/descriptors retain the prior FileNotFound handling. Existing PID/birth/invocation, prefix, staged baseline, original failure and current0e rollback guards are unaffected. No actionable defect established in the narrow delta.

Only fdinfo-review was written. Existing authored logs/review outputs were preserved. Fixture children and temporary trees were cleaned. Parent owns retained review evidence and cleanup after acceptance. Requested settings remain gpt-6.1-sol/medium; host reports gpt-6.1-sol; effective effort remains unobservable.

Not checked: actual production full-/proc scan, live adoption, rollback, dataset acceptance or gameplay. The prior owned-log provenance limits still apply; no network/runtime/service/SQL/actor action ran.
