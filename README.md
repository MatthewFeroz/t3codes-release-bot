# T3Codes nightly release posts

GitHub Actions checks `pingdotgg/t3code` for published nightlies once per hour (17 minutes past the hour). It posts only a previously unseen nightly release. Stable releases, preview releases and runs with no new nightly produce no posts. The old Windows scheduled task is disabled; local publishing is blocked in code.

## Exact format

```text
T3Code nightly v0.0.43-nightly.20260919.1962

- fix(mobile): adapt workspace navigation and expand controls
- chore(mobile): add dev client script with preview environment
- fix: detect installed editors outside PATH
- fix(web): show plain text in collapsed thought previews
```

One release, one post when the list fits. Each PR title is copied verbatim, in release-note order. No descriptions, summaries, authors, extra announcements or links are added. The formatter never abbreviates or truncates titles, and never splits a title across posts.

X's standard post limit is 280 weighted characters. If the complete list is longer, it is posted as a thread: the first post carries the header and as many whole titles as fit, and each reply continues with the next whole titles. If a single title is too long to fit in a post on its own, that release is skipped (`done=2`) and the Action fails once to report it; later runs carry on normally.

Releases held under the old single-post rule (Sep 20 to 27, 2026) were marked skipped when threads were introduced and will not be posted.

## Hosting and state

Private repository: https://github.com/MatthewFeroz/t3codes-release-bot

The workflow is `.github/workflows/nightly.yml`; it supports an hourly schedule and manual dispatch. A workflow in this repository cannot directly receive release events from the upstream repository without upstream access, so GitHub checks the release feed. Scheduled Actions can be delayed.

`.bot-state/state.sqlite3` preserves the starting release, queued posts and published tweet IDs. Existing published-release history was migrated, so the earlier thread will not be reposted.

OAuth credentials are stored in `.bot-state/x-vault.enc`, encrypted with Fernet. The encryption key is the GitHub Actions secret `X_VAULT_KEY`. Replacement refresh tokens are encrypted and committed back automatically. Credentials never appear in source, logs or command arguments. Local DPAPI credentials are retained as a migration backup; GitHub's rotating vault is now authoritative.

The Actions job serializes runs. State is committed and pushed before each X POST and immediately after its response. A runner failure after a send leaves a durable uncertain-delivery record. Subsequent runs reconcile exact text and reply parent against X; if delivery cannot be established, they stop instead of duplicating it. Failed remote checkpoints halt publishing. Explicit API rejection leaves the post pending.

## Operations

```powershell
python bot.py preview   # latest-preview.txt; does not publish
python bot.py status
python -m unittest -v
gh workflow run nightly.yml --repo MatthewFeroz/t3codes-release-bot
gh workflow disable nightly.yml --repo MatthewFeroz/t3codes-release-bot
```

The cloud workflow installs `requirements.txt`. Preview/test on Windows use the Python standard library. `Migrate-Credentials.py` was the one-time credential migration and refuses to overwrite the existing cloud vault.

Do not re-enable the retired Windows task or use its old token store as a concurrent publisher. Keep state history when redeploying. The ten posts from the original format remain on X; this change does not delete them.
