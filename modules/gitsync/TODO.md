# Git Sync backlog

- [x] Remote repository cloning and pull/sync are covered by Drive > Git Repo (`modules/gitdrives`); keep that behavior there rather than duplicating it here.
- [x] Clarify account and authentication identity for remote write operations. Remote access uses the HTTPS username + token saved per drive (encrypted, write-only); a read-only access check cannot verify authenticated identity.
- [x] Review selected file changes and create local commits from the app, including per-drive commit identity and commit-message handling.
- [x] Push local commits using the saved credentials, with explicit clean-tree/upstream/ahead checks and generic credential-safe remote errors.
- [x] Fetch and compare local and remote history; explicitly review a no-commit merge, resolve conflicts per path, and abort or complete locally without an implicit push.
- [x] Replace the git executable with dulwich (+ merge3) so the module has no subprocess dependency.