# Git Sync backlog

- [x] Remote repository cloning and pull/sync are covered by Drive > Git Repo (`modules/gitdrives`); keep that behavior there rather than duplicating it here.
- [x] Clarify account and authentication identity for remote write operations. Git reuses the server account's configured credentials; the saved account is only a preferred username hint, and a read-only access check cannot verify authenticated identity.
- [x] Review selected file changes and create local commits from the app, including repository-local identity and commit-message handling.
- [x] Push local commits using the preferred username hint, with explicit clean-tree/upstream/ahead checks and generic credential-safe remote errors.
- [x] Fetch and compare local and remote history; explicitly review a no-commit merge, resolve conflicts per path, and abort or complete locally without an implicit push.