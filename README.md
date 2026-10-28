# T3Codes nightly posts

When a nightly release is published on [pingdotgg/t3code](https://github.com/pingdotgg/t3code), this bot posts that release's notes to [@t3codes](https://x.com/t3codes). There is no approval step.

A nightly is a published GitHub release whose tag contains `-nightly.` (for example `v0.1.4-nightly.20261027.3796`). Preview tags (`-preview.`) and stable tags are ignored. The notes are the release body with every link removed before posting. X bills $0.20 for a post that contains a URL and $0.015 for a plain post, so the text that is sent has no link.

Markdown links keep their label (`[docs](https://example.com)` becomes `docs`). Angle-bracket autolinks are dropped. A pull-request line such as `by @user in https://github.com/.../pull/123` becomes `by @user`. A `Full Changelog` line that is only a compare link is dropped. Author @mentions stay. Version strings such as `v0.1.3` and file names such as `package.json` stay. Text that X would auto-link without a scheme is removed too, including a bare `example.com`, `github.com/foo`, or `README.md`. A path such as `src/bot.py` stays. Leftover blank lines, empty bullets, and extra spaces are removed. The remaining words are not rewritten.

@t3codes has X Premium (since 27 Oct 2026), so each nightly is sent as one post on `POST /2/tweets`. Premium allows 25,000 weighted characters. The per-post cap is the repository variable `X_MAX_POST_CHARS` (Settings → Secrets and variables → Actions → Variables). The workflow passes it through as an environment variable of the same name. When that variable is unset, the cap is 25000. Set it to `280` if Premium lapses.

Notes longer than the cap are split on line boundaries into as few replies as possible. The pieces join back to the stripped notes. A single line longer than the cap is split on a space, then a hard break. If a piece would still be over the cap, the job fails before sending it. If a piece would still contain a URL after stripping, the job fails before the request is sent.

If X rejects a long post with HTTP 400 or 403 because the account cannot post long text, the full error body is written to the log and that nightly is posted as a 280-character reply thread. A thread that already has posts is left in progress and continued on the next run, not started over.

This repository is [MatthewFeroz/t3codes-release-bot](https://github.com/MatthewFeroz/t3codes-release-bot). A workflow here cannot subscribe to release events on `pingdotgg/t3code`, so `.github/workflows/nightly.yml` polls that repo's release list at minute 17 of every hour. Scheduled runs can be delayed by GitHub. The same workflow can be started by hand.

The job checks out `main` and commits `.bot-state/posted.json` before and after each post, so a release that was already sent is not sent again. Nightlies that were already posted, previously skipped, or queued but never sent while the old publisher was stuck are recorded as handled. The newest nightly that has never been sent is posted, and every nightly published after it is posted once. That avoids dumping the outage backlog onto @t3codes.

## Secrets

Add these four repository secrets (not variables) while logged in to GitHub as the owner of this repo:

**Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value in the X developer portal |
| --- | --- |
| `X_API_KEY` | API Key (Consumer Key) |
| `X_API_KEY_SECRET` | API Key Secret (Consumer Secret) |
| `X_ACCESS_TOKEN` | Access Token |
| `X_ACCESS_TOKEN_SECRET` | Access Token Secret |

The old `X_VAULT_KEY` secret is not used.

Create the values while logged in to the [X developer portal](https://developer.x.com/en/portal/dashboard) as **@t3codes**. The access token is for the account that owns the app. If the portal is logged in as someone else, the bot refuses to post.

1. Open the app (create a project and an app first if needed).
2. In the app's **Settings**, under **User authentication settings**, choose **Set up** or **Edit**.
3. Set **App permissions** to **Read and write**. Read-only tokens cannot post.
4. Turn on **OAuth 1.0a**. The bot does not use OAuth 2.0 or a bearer token.
5. App type can be **Web App, Automated App or Bot**. The portal requires a callback URL and a website URL even though this bot never opens a browser. `https://localhost` is enough for the callback, and `https://github.com/MatthewFeroz/t3codes-release-bot` is enough for the website.
6. Save.
7. Open **Keys and tokens**.
8. Under **Consumer Keys**, copy the API Key and API Key Secret.
9. Under **Authentication Tokens → Access Token and Secret**, click **Generate** or **Regenerate**. You must regenerate after changing the permission. A token minted while the app was read-only stays read-only.
10. Confirm the new access token says it was created with **Read and Write** permissions for **@t3codes**.
11. Paste the four values into the GitHub secrets above. Do not commit them, print them, or put them in Actions variables.

On each real run the bot calls `GET /2/users/me` with those credentials and posts only if the username is `t3codes`. Posts go to `POST /2/tweets`. A failed auth or a rejected post fails the job, and the log includes the HTTP status and the error text returned by X. Successful posts print lines like:

```text
POST_URL=https://x.com/t3codes/status/1234567890
```

The same URLs are written to the job summary.

## Run it

Dry run (no X call, no state change):

```bash
gh workflow run nightly.yml --repo MatthewFeroz/t3codes-release-bot -f dry_run=true
```

Post for real:

```bash
gh workflow run nightly.yml --repo MatthewFeroz/t3codes-release-bot -f dry_run=false
```

The workflow file is `.github/workflows/nightly.yml`. Its only input is `dry_run` (boolean, default `false`). The hourly schedule does not set it, so scheduled runs post.

Local checks, which do not post:

```bash
python -m unittest -v
python bot.py dry-run
python bot.py status
```

`python bot.py run` posts only inside GitHub Actions.

## Retired publisher

The Windows scheduled task, `XAuth.psm1`, and `Migrate-Credentials.py` are not the publisher. Do not turn the old task back on. The encrypted file `.bot-state/x-vault.enc` is a historical backup and is not read.
