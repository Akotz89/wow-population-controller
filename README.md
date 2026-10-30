# WoW population controller

Source-only Python population controller based on the v30 release. The runtime
configuration and identity gate bindings also support variable roster targets.
`scripts/login.py`, bytecode, snapshots, credentials, database data, server
configuration, and release artifacts are intentionally excluded.

The native integration boundary is the maintained
[`mod-python-engine`](https://github.com/Akotz89/mod-python-engine) fork at
engine revision `700a4f75a2e550f17022665d7b5b009c24e46a40`. Runtime and gameplay
acceptance remain separate from this source snapshot.

Public CI checks Python syntax and an offline ledger integrity regression. It
requires no realm or credentials and does not prove native integration, runtime
behavior, or gameplay acceptance.
