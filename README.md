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

## Read-only reconciliation diagnostic

`diagnose-reconciliation` checks only uncertain (`sending`) posts from an explicit,
closed state snapshot. It does not discover/queue releases, publish, update posting
state, create bot logs/locks, refresh OAuth tokens, save the vault, or commit/push.
It verifies the expected account and performs only the same timeline GETs used by
normal recovery, preserving their query parameters and exact text/reply matching.
A match is reported without tweet IDs or text and is **not** saved. No match remains
uncertain, never permission to resend.

Run local tests first (no real credentials or network required):

```sh
python -m unittest -v
```

Only after approval for a live diagnostic, use a trusted environment already
holding the cloud vault and `X_VAULT_KEY`. Do not paste credentials into commands
or logs. The command deliberately cannot use the retired Windows token store.
Use a fresh committed state snapshot from the publisher repository, not a live
SQLite database. For example, in a separate trusted checkout:

```sh
git show HEAD:.bot-state/state.sqlite3 > /tmp/t3codes-state-snapshot.sqlite3
BOT_DATA_DIR="$PWD/.bot-state" python bot.py diagnose-reconciliation \
  --state /tmp/t3codes-state-snapshot.sqlite3
```

Use an isolated temporary filename/directory if one is already present. `HEAD`
must contain the relevant latest state; a stale snapshot cannot establish current
delivery. Snapshot sidecars (`-wal`, `-shm`, `-journal`) are rejected. Do not modify
the snapshot while diagnosis runs. The source state is opened immutable/read-only.
The vault is read only to obtain its current token. If it is expired/near expiry,
or X rejects it with 401, the command stops without refreshing it. An authorized
normal authentication process would be needed separately; do not run publishing
just to obtain a diagnostic token.

Success exits 0; an API failure, unavailable token or unresolved delivery exits 1.
Diagnostics emit method, templated endpoint path, status, recognized error title,
recognized parameter names, bounded numeric error codes and recognized OAuth
error names. They omit query values, account IDs, headers, credentials, post text,
and all free-text error messages/details. Error bodies are read up to 8 KiB plus
one overflow byte; malformed or larger responses are omitted. An unknown error
may therefore still require a separate carefully reviewed investigation.

The existing `nightly.yml` manual dispatch **publishes**. It is not a safe way to
launch this diagnostic. This change does not modify the workflow or live state.

### Separate manual diagnostic workflow

`.github/workflows/diagnose-reconciliation.yml` is a separate manual-only workflow
named **Read-only reconciliation diagnostic**. Its JSON syntax is valid YAML and
lets standard-library tests validate its complete safety-critical structure.
It has no scheduled/push trigger, uses read-only repository permissions, disables
checkout credential persistence and shares the publisher's concurrency group.
It runs mocked tests before exposing `X_VAULT_KEY` exclusively to the final
GET-only diagnostic step. It reads the checked-out commit's database into a
temporary snapshot and removes that snapshot on exit; no artifacts, database,
vault or tokens are uploaded or committed. Confirm the chosen commit includes
current state before dispatching.

Publishing this workflow and dispatching it require separate approval. Select
only **Read-only reconciliation diagnostic**, never **Nightly release posts**.
If credentials are expired or near expiry, the diagnostic fails safely with
`token expired or near expiry; refresh is disabled`; it does not rotate tokens
or retry publishing. Stop there and arrange a separately authorized credential
refresh if needed. A rejected token returns redacted 401 metadata, also without
refresh. Success does not mark any post delivered or resume posting.
