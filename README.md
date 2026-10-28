# T3Codes nightly posts

When a nightly release is published on [pingdotgg/t3code](https://github.com/pingdotgg/t3code), this bot posts that release's notes to [@t3codes](https://x.com/t3codes). There is no approval step.

A nightly is a published GitHub release whose tag contains `-nightly.` (for example `v0.1.4-nightly.20261027.3796`). Preview tags (`-preview.`) and stable tags are ignored. The notes are the release body with every link removed before posting. X bills $0.20 for a post that contains a URL and $0.015 for a plain post, so the text that is sent has no link.

Markdown links keep their label (`[docs](https://example.com)` becomes `docs`). Angle-bracket autolinks are dropped. A `Full Changelog` line that is only a compare link is dropped. Version strings such as `v0.1.3` and file names such as `package.json` stay. Text that X would auto-link without a scheme is removed too, including a bare `example.com`, `github.com/foo`, or `README.md`. A path such as `src/bot.py` stays. Leftover blank lines, empty bullets, and extra spaces are removed. The remaining words are not rewritten.

Author credits are removed too. X rejects a post with too many @mentions, and a nightly credits dozens of authors. A line such as `* fix by @user in https://github.com/.../pull/123` becomes `* fix`, and the New Contributors section is dropped. Other @mentions in the notes stay.

Each post opens with a one-sentence summary of the nightly, then a blank line, then the notes. On X a long post shows only its first few lines before "Show more", so the summary is what most readers see. Claude (`claude-opus-5-5`, low effort) writes the sentence from the stripped notes. It must be under 200 characters with no link or @handle. If the `ANTHROPIC_API_KEY` repository secret is missing, the request fails, or the sentence breaks those rules, the error is logged and the nightly is posted without a summary. The summary is written once, when a nightly is first queued, and a dry run shows it.

@t3codes has X Premium (since 27 Oct 2026), so each nightly is sent as one post on `POST /2/tweets`. Premium allows 25,000 weighted characters. The per-post cap is the repository variable `X_MAX_POST_CHARS` (Settings → Secrets and variables → Actions → Variables). The workflow passes it through as an environment variable of the same name. When that variable is unset, the cap is 25000. Set it to `280` if Premium lapses.

Notes longer than the cap are split on line boundaries into as few replies as possible. The pieces join back to the stripped notes. A single line longer than the cap is split on a space, then a hard break. If a piece would still be over the cap, the job fails before sending it. If a piece would still contain a URL after stripping, the job fails before the request is sent.

If X rejects a long post with HTTP 400 or 403 because the account cannot post long text, the full error body is written to the log and that nightly is posted as a 280-character reply thread. A thread that already has posts is left in progress and continued on the next run, not started over.

This repository is [MatthewFeroz/t3codes-release-bot](https://github.com/MatthewFeroz/t3codes-release-bot). A workflow here cannot subscribe to release events on `pingdotgg/t3code`, so `.github/workflows/nightly.yml` polls that repo's release list at minute 17 of every hour. Scheduled runs can be delayed by GitHub. The same workflow can be started by hand.

The job checks out `main` and commits `.bot-state/posted.json` before and after each post, so a release that was already sent is not sent again. Nightlies that were already posted, previously skipped, or queued but never sent while the old publisher was stuck are recorded as handled. The newest nightly that has never been sent is posted, and every nightly published after it is posted once. That avoids dumping the outage backlog onto @t3codes.

## Secrets

Add these five repository secrets (not variables) while logged in to GitHub as the owner of this repo:

**Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value in the X developer portal |
| --- | --- |
| `X_API_KEY` | API Key (Consumer Key) |
| `X_API_KEY_SECRET` | API Key Secret (Consumer Secret) |
| `X_ACCESS_TOKEN` | Access Token |
| `X_ACCESS_TOKEN_SECRET` | Access Token Secret |
| `ANTHROPIC_API_KEY` | Not from X: an Anthropic API key for the summary line |

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

## Triage replies

`replies.py` answers posts that tell @t3codes that T3 Code is broken. The reply points to `npx t3 triage`, which starts Claude Code or Codex in a terminal with the user's T3 Code logs and the source for their version. It works even when the app does not start.

X only lets an API app reply when the post's author @mentioned the replying account or quoted one of its posts. Other replies are rejected with HTTP 403. This rule covers every self-serve API tier, X Premium included, so the bot reads the @t3codes mention timeline (`GET /2/users/:id/mentions`). It cannot answer a post that only tags @theo.

A mention gets a reply only if all of these hold:

- It mentions @t3codes or quotes a @t3codes post.
- It is in English, under 24 hours old, and not a retweet.
- It is not from @t3codes, @theo, or @pingdotgg.
- After handles and links are removed, it contains a breakage phrase such as `broken`, `crash`, `stuck`, `error`, or `won't start`. It does not say the problem is fixed (`works now`, `fixed`, `nvm`) and does not mention triage.
- The author has not had a reply or a queued reply in the last 7 days, and the conversation has not been answered.

There are three wordings. Each is 180 characters with no link, and they rotate because X rejects duplicate text. A reply costs $0.01, the summoned-reply rate. Reading a mention costs $0.001.

Modes:

| Mode | What it does |
| --- | --- |
| `dry-run` | Reads mentions and prints each one with its decision and the reply it would send. Writes no state and posts nothing. This is the default. |
| `queue` | Records matching mentions in `.bot-state/replies.json` (post IDs and usernames only, no post text). Sends nothing until approved. |
| `auto` | Replies to matches, at most 3 per run and 10 per 24 hours. Anything over the cap stays queued. |

Scheduled runs (minute 47 of every hour) do nothing until the repository variable `REPLY_MODE` is set to `queue` or `auto`. By hand:

```bash
gh workflow run replies.yml --repo MatthewFeroz/t3codes-release-bot -f mode=dry-run
gh workflow run replies.yml --repo MatthewFeroz/t3codes-release-bot -f mode=queue
gh workflow run replies.yml --repo MatthewFeroz/t3codes-release-bot -f mode=queue -f approve=<post id>
gh workflow run replies.yml --repo MatthewFeroz/t3codes-release-bot -f mode=queue -f reject=<post id>
```

Locally, `python replies.py dry-run` reads mentions with the four X secrets from the environment and posts nothing. `python replies.py status` lists queued mentions.

## Retired publisher

The Windows scheduled task, `XAuth.psm1`, and `Migrate-Credentials.py` are not the publisher. Do not turn the old task back on. The encrypted file `.bot-state/x-vault.enc` is a historical backup and is not read.
