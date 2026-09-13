# WoW population controller

Source-only ownership snapshot for the Python population controller used by the
v30 release. The 17 tracked Python files are copied from the live, source-only
working directory; `scripts/login.py`, bytecode, snapshots, credentials,
database data, server configuration, and release artifacts are intentionally
excluded.

The native integration boundary is the maintained
[`mod-python-engine`](https://github.com/Akotz89/mod-python-engine) fork at
engine revision `700a4f75a2e550f17022665d7b5b009c24e46a40`. Runtime and gameplay
acceptance remain separate from this source snapshot.
