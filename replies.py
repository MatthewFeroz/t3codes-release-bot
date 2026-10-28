"""Reply to @t3codes mentions that say T3 Code is broken.

The reply points to ``npx t3 triage``, which starts the user's own coding
agent (Claude Code or Codex) with their T3 Code logs and the source for their
version.

X lets a self-serve app reply only when the post's author summoned the replying
account by @mentioning it or quoting one of its posts. A reply to anyone else is
rejected, so this reads the @t3codes mention timeline instead of searching all
of X.

Modes come from ``REPLY_MODE``:

- ``dry-run`` (default) reads mentions and prints what it would send. It does
  not write state or post.
- ``queue`` records matching mentions in ``.bot-state/replies.json``. Nothing
  is sent until it is approved: by an ID in ``REPLY_APPROVE``, or, when
  Agent Handler is configured, by the ``approve`` label on the mention's
  review issue. Closing that issue skips the mention.
- ``auto`` replies to matching mentions within the caps below.

The OAuth 1.0a client, state checkpoint, and lookup of an uncertain post come
from bot.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse

from bot import (
    ACCOUNT,
    API,
    SECRET_NAMES,
    ApiError,
    Store,
    XClient,
    configure_logging,
    data_dir,
    ensure_git_identity,
    load_credentials,
    now,
    redact,
    run_lock,
    weight,
    write_summary,
)
from agent_handler import AH_SECRET_NAMES, AgentHandler, AgentHandlerError, configured as agent_handler_configured
from xurls import ensure_plain

LOG = logging.getLogger('release-bot')
MODES = ('dry-run', 'queue', 'auto')

# One wording, sent as a reply to each matching mention. No URL: a post with a
# link costs $0.20. X can reject identical text as a duplicate. With one
# wording there is nothing to fall back to, so that mention is marked failed
# and the run fails loudly instead of sending something else.
REPLIES = (
    'Run npx t3 triage in a terminal. Your coding agent reads your T3 Code logs, '
    'checks known issues for your version, and writes up the GitHub issue for you.',
)

MAX_AGE = dt.timedelta(hours=24)
AUTHOR_COOLDOWN = dt.timedelta(days=7)
MAX_PER_RUN = 3
MAX_PER_DAY = 10
MAX_PAGES = 5
# @t3codes itself and the people who would answer by hand.
IGNORED_AUTHORS = frozenset({ACCOUNT, 'theo', 'pingdotgg'})
# X tags very short posts "und" (undetermined) or "qam" (mentions only).
LANGS = frozenset({'en', 'und', 'qam'})
REVIEW_REPO = 'MatthewFeroz/t3codes-release-bot'
REVIEW_LABEL = 'triage-reply'
APPROVE_LABEL = 'approve'

_PROBLEM = re.compile(
    r"\b(?:broken|broke|breaks|crash(?:es|ed|ing)?|bug(?:gy|s)?|errors?|stuck|frozen|"
    r"freez(?:es|ing)|hangs|hanging|fail(?:s|ed|ing)?|unusable)\b"
    r"|\b(?:doesn't|does not|didn't|isn't|is not|won't|will not|can't|cannot|not|stopped)\s+"
    r"(?:work|working|load|loading|start|starting|open|opening|launch|launching|"
    r"connect|connecting|run|running)\b",
    re.IGNORECASE,
)
_RESOLVED = re.compile(
    r"\b(?:fixed|works now|working now|working again|no longer|not broken|isn't broken|"
    r"resolved|nvm|never ?mind)\b",
    re.IGNORECASE,
)
_HANDLE = re.compile(r'@\w+')
_LINK = re.compile(r'https?://\S+', re.IGNORECASE)


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def problem_phrase(text):
    """Return the phrase that reads as a breakage report, or None.

    Handles and links are removed first, so a username such as @brokenbot or a
    t.co slug cannot match. A post that says the problem is already fixed does
    not match.
    """
    text = _LINK.sub(' ', _HANDLE.sub(' ', text.replace('’', "'")))
    if _RESOLVED.search(text) or re.search(r'\btriage\b', text, re.IGNORECASE):
        return None
    match = _PROBLEM.search(text)
    return match.group(0) if match else None


def summoned(tweet, user_id, quoted_authors):
    """True when X allows @t3codes to reply: it is @mentioned or quoted."""
    for mention in (tweet.get('entities') or {}).get('mentions') or []:
        if (mention.get('username') or '').lower() == ACCOUNT:
            return True
    for ref in tweet.get('referenced_tweets') or []:
        if ref.get('type') == 'quoted' and quoted_authors.get(str(ref.get('id'))) == str(user_id):
            return True
    return False


def replied_rows(state):
    return [row for row in state['mentions'].values() if row.get('status') == 'replied']


def recent_authors(state, at):
    """Author ids that have a reply sent, sending, or queued inside the cooldown."""
    authors = set()
    for row in state['mentions'].values():
        if row.get('status') not in ('queued', 'sending', 'replied'):
            continue
        stamp = row.get('replied_at') or row.get('created_at')
        if stamp and at - parse_time(stamp) < AUTHOR_COOLDOWN:
            authors.add(str(row['author_id']))
    return authors


def evaluate(tweet, author, user_id, quoted_authors, state, at, authors):
    """Return ``(phrase, None)`` for a mention to answer or ``(None, reason)``."""
    username = (author.get('username') or '').lower()
    if str(tweet['id']) in state['mentions']:
        return None, 'already-handled'
    if username in IGNORED_AUTHORS or str(tweet.get('author_id')) == str(user_id):
        return None, 'ignored-author'
    if any(ref.get('type') == 'retweeted' for ref in tweet.get('referenced_tweets') or []):
        return None, 'retweet'
    if tweet.get('lang') not in LANGS and tweet.get('lang') is not None:
        return None, 'language'
    if at - parse_time(tweet['created_at']) > MAX_AGE:
        return None, 'too-old'
    if not summoned(tweet, user_id, quoted_authors):
        return None, 'not-summoned'
    phrase = problem_phrase(tweet.get('text') or '')
    if not phrase:
        return None, 'no-problem-phrase'
    if str(tweet.get('author_id')) in authors:
        return None, 'author-cooldown'
    conversation = str(tweet.get('conversation_id') or '')
    if conversation and any(
        row.get('conversation_id') == conversation and row.get('status') in ('queued', 'sending', 'replied')
        for row in state['mentions'].values()
    ):
        return None, 'conversation-already-answered'
    return phrase, None


class ReplyStore(Store):
    def _load(self):
        if not self.path.exists():
            return {'version': 1, 'since_id': None, 'mentions': {}}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        data.setdefault('version', 1)
        data.setdefault('since_id', None)
        data.setdefault('mentions', {})
        return data


def fetch_mentions(client, user_id, since_id):
    """Return mentions newest first, with author and quoted-post author maps."""
    params = {
        'max_results': '100',
        'tweet.fields': 'author_id,conversation_id,created_at,entities,lang,referenced_tweets',
        'expansions': 'author_id,referenced_tweets.id,referenced_tweets.id.author_id',
        'user.fields': 'username',
    }
    if since_id:
        params['since_id'] = str(since_id)
    tweets, users, quoted = [], {}, {}
    for _ in range(MAX_PAGES):
        url = f'{API}/users/{user_id}/mentions?' + urllib.parse.urlencode(params)
        result = client.call('GET', url)
        includes = result.get('includes') or {}
        for user in includes.get('users') or []:
            users[str(user['id'])] = user
        for post in includes.get('tweets') or []:
            quoted[str(post['id'])] = str(post.get('author_id'))
        tweets.extend(result.get('data') or [])
        token = (result.get('meta') or {}).get('next_token')
        if not token or not since_id:
            break
        params['pagination_token'] = token
    return tweets, users, quoted


def next_variants(state):
    start = len(replied_rows(state)) % len(REPLIES)
    return [(start + offset) % len(REPLIES) for offset in range(len(REPLIES))]


def tweet_url(username, tweet_id):
    return f'https://x.com/{username or "i"}/status/{tweet_id}'


def send(store, client, user_id, tweet_id):
    """Reply to one stored mention. Returns the reply id, or None if X refused it."""
    row = store.data['mentions'][tweet_id]
    if row.get('status') == 'replied':
        return row.get('reply_id')
    if row.get('status') == 'sending' and row.get('reply_text'):
        # The last run sent this and may have died before recording the id.
        found = client.find_post(user_id, row['reply_text'], tweet_id)
        if found:
            return _record(store, row, found)
    order = next_variants(store.data)
    if row.get('draft') in REPLIES:
        # The review issue showed this wording, so it is what gets sent.
        first = REPLIES.index(row['draft'])
        order = [first] + [index for index in order if index != first]
    for index in order:
        text = ensure_plain(REPLIES[index])
        row.update(status='sending', reply_text=text, attempted_at=now())
        store.save()
        try:
            reply_id = client.create(text, tweet_id)
        except ApiError as exc:
            if 'duplicate' in str(exc).lower():
                continue
            if exc.status in (400, 403):
                # X answered and created nothing. A 403 here is usually the
                # summoned-only reply rule or a deleted or protected post.
                row.update(status='failed', reason=str(exc), decided_at=now())
                row.pop('attempted_at', None)
                store.save()
                LOG.error('Reply to %s refused: %s', tweet_url(row.get('username'), tweet_id), exc)
                return None
            row['status'] = 'queued'
            row.pop('attempted_at', None)
            store.save()
            raise
        return _record(store, row, reply_id)
    row.update(status='failed', reason='X rejected the reply as duplicate content', decided_at=now())
    store.save()
    return None


def _record(store, row, reply_id):
    row.update(status='replied', reply_id=str(reply_id), replied_at=now())
    row.pop('attempted_at', None)
    store.save()
    url = f'https://x.com/{ACCOUNT}/status/{reply_id}'
    print(f'REPLY_URL={url}', flush=True)
    write_summary(f'- replied to {tweet_url(row.get("username"), row["tweet_id"])}: {url}\n')
    return str(reply_id)


def _fence(text):
    # Post text sits in a code block so its @handles cannot ping GitHub users.
    return '```text\n' + re.sub(r'`{3,}', "'''", text.strip()) + '\n```'


def review_body(row, text=None):
    url = tweet_url(row.get('username'), row['tweet_id'])
    lines = [
        'A mention of `@t3codes` reads like a T3 Code breakage report.',
        '',
        f'Post: {url}',
    ]
    if text:
        lines += ['', _fence(text)]
    lines += [
        '',
        f'Matched phrase: `{row.get("phrase")}`',
        '',
        '`@t3codes` will reply with:',
        '',
        _fence(row.get('draft') or REPLIES[0]),
        '',
        f'Add the `{APPROVE_LABEL}` label to send this reply. Close the issue to skip it.',
    ]
    return '\n'.join(lines) + '\n'


def _issue(result):
    return result.get('issue') if isinstance(result.get('issue'), dict) else result


class ReviewQueue:
    """One GitHub issue per queued reply, opened, read and closed through Agent Handler."""

    def __init__(self, agent_handler, repo=REVIEW_REPO):
        self.ah = agent_handler
        self.owner, self.repo = repo.split('/', 1)

    def _where(self, **extra):
        return {'owner': self.owner, 'repo': self.repo, **extra}

    def open(self, row, text=None):
        result = self.ah.call('github__create_issue', self._where(
            title=f'Triage reply: x.com/{row.get("username")} "{row.get("phrase")}"',
            body=review_body(row, text),
            assignee=None, assignees=None, milestone=None, labels=[REVIEW_LABEL], type=None,
        ))
        number = _issue(result).get('number')
        if not number:
            raise AgentHandlerError('github__create_issue returned no issue number.')
        return int(number)

    def decision(self, number):
        """Return 'approve', 'reject', or None while the issue waits."""
        issue = _issue(self.ah.call('github__get_issue', self._where(issue_number=int(number))))
        labels = {label.get('name') if isinstance(label, dict) else label for label in issue.get('labels') or []}
        if APPROVE_LABEL in labels:
            return 'approve'
        if issue.get('state') == 'closed':
            return 'reject'
        return None

    def comment(self, number, reply_url):
        self.ah.call('github__create_issue_comment', self._where(
            issue_number=int(number), body=f'Replied from `@{ACCOUNT}`: {reply_url}',
        ))

    def close(self, number):
        self.ah.call('github__update_issue', self._where(
            issue_number=int(number), title=None, body=None, state='closed', state_reason='completed',
            assignee=None, assignees=None, milestone=None, labels=None, type=None,
            duplicate_of_issue_id=None, remove_milestone=None, remove_assignees=None,
        ))


def sent_in_last_day(state, at):
    return sum(
        1 for row in replied_rows(state)
        if row.get('replied_at') and at - parse_time(row['replied_at']) < dt.timedelta(days=1)
    )


def split_ids(value):
    return [part for part in re.split(r'[\s,]+', value or '') if part]


def render(mode, decisions, approvals):
    lines = [f'Mode: {mode}. Account: @{ACCOUNT}.', '']
    if mode == 'dry-run':
        lines.insert(0, 'DRY RUN. Nothing will be posted to X and no state is written.')
    matches = [item for item in decisions if item['phrase']]
    lines.append(f'{len(decisions)} new mention(s), {len(matches)} match.')
    lines.append('')
    for item in decisions:
        tweet, author = item['tweet'], item['author']
        url = tweet_url(author.get('username'), tweet['id'])
        text = ' '.join((tweet.get('text') or '').split())
        if item['phrase']:
            lines.append(f'MATCH "{item["phrase"]}" {url}')
            lines.append(f'  post: {text}')
            lines.append(f'  reply ({weight(item["reply"])} chars): {item["reply"]}')
            lines.append(f'  action: {item["action"]}')
        else:
            lines.append(f'skip ({item["reason"]}) {url}')
            lines.append(f'  post: {text}')
        lines.append('')
    for tweet_id, action in approvals:
        lines.append(f'approve {tweet_id}: {action}')
    return '\n'.join(lines).rstrip() + '\n'


def execute(store, client, mode, approve=(), reject=(), at=None, sleep=time.sleep, review=None, scan=True):
    if mode not in MODES:
        raise RuntimeError(f'REPLY_MODE must be one of {", ".join(MODES)}; got {mode!r}.')
    at = at or dt.datetime.now(dt.timezone.utc)
    dry_run = mode == 'dry-run'
    if dry_run:
        store.persist = False
    else:
        ensure_git_identity()
    user_id = client.verify()
    state = store.data

    approvals = []
    for tweet_id in reject:
        row = state['mentions'].get(tweet_id)
        if not row or row.get('status') != 'queued':
            raise RuntimeError(f'{tweet_id} is not a queued mention; nothing was rejected.')
        if not dry_run:
            row.update(status='rejected', decided_at=now())
            store.save()
        approvals.append((tweet_id, 'would reject' if dry_run else 'rejected'))

    approve = list(approve)
    review_errors = []
    if review:
        for tweet_id, row in sorted(state['mentions'].items(), key=lambda item: int(item[0])):
            if row.get('status') not in ('queued', 'sending') or not row.get('issue'):
                continue
            try:
                verdict = review.decision(row['issue'])
            except AgentHandlerError as exc:
                review_errors.append(f'reading issue #{row["issue"]}: {exc}')
                continue
            if verdict == 'approve' and tweet_id not in approve:
                approve.append(tweet_id)
            elif verdict == 'reject' and row.get('status') == 'queued':
                if not dry_run:
                    row.update(status='rejected', decided_at=now())
                    store.save()
                approvals.append((tweet_id, f'issue #{row["issue"]} closed: ' + ('would reject' if dry_run else 'rejected')))

    tweets, users, quoted = fetch_mentions(client, user_id, state.get('since_id')) if scan else ([], {}, {})
    authors = recent_authors(state, at)
    decisions = []
    variant = next_variants(state)[0]
    for tweet in sorted(tweets, key=lambda post: int(post['id'])):
        author = users.get(str(tweet.get('author_id'))) or {}
        phrase, reason = evaluate(tweet, author, user_id, quoted, state, at, authors)
        item = {'tweet': tweet, 'author': author, 'phrase': phrase, 'reason': reason}
        decisions.append(item)
        if not phrase:
            continue
        authors.add(str(tweet.get('author_id')))
        item['reply'] = REPLIES[variant]
        variant = (variant + 1) % len(REPLIES)
        item['action'] = {
            'dry-run': 'would queue or reply' + (' and open a review issue' if review else ''),
            'queue': 'queued for approval', 'auto': 'queued',
        }[mode]
        state['mentions'][str(tweet['id'])] = {
            'tweet_id': str(tweet['id']),
            'author_id': str(tweet.get('author_id')),
            'username': author.get('username'),
            'conversation_id': str(tweet.get('conversation_id') or ''),
            'created_at': tweet['created_at'],
            'phrase': phrase,
            'status': 'queued',
            'draft': item['reply'],
        }
    if tweets:
        state['since_id'] = str(max(int(post['id']) for post in tweets))
    store.save()

    if review and mode == 'queue':
        texts = {str(tweet['id']): tweet.get('text') for tweet in tweets}
        for tweet_id, row in sorted(state['mentions'].items(), key=lambda item: int(item[0])):
            if row.get('status') != 'queued' or row.get('issue') or tweet_id in approve:
                continue
            try:
                row['issue'] = review.open(row, texts.get(tweet_id))
            except AgentHandlerError as exc:
                review_errors.append(f'opening a review issue for {tweet_id}: {exc}')
                continue
            store.save()
            for item in decisions:
                if str(item['tweet']['id']) == tweet_id:
                    item['action'] = f'queued, review issue #{row["issue"]}'

    queued = [tweet_id for tweet_id, row in state['mentions'].items() if row.get('status') in ('queued', 'sending')]
    to_send = [tweet_id for tweet_id in approve]
    for tweet_id in to_send:
        if state['mentions'].get(tweet_id, {}).get('status') not in ('queued', 'sending'):
            raise RuntimeError(f'{tweet_id} is not a queued mention; nothing was approved.')
    if mode == 'auto':
        budget = min(MAX_PER_RUN, MAX_PER_DAY - sent_in_last_day(state, at)) - len(to_send)
        fresh = sorted(
            (tweet_id for tweet_id in queued
             if tweet_id not in to_send and at - parse_time(state['mentions'][tweet_id]['created_at']) <= MAX_AGE),
            key=int,
        )
        to_send += fresh[:max(budget, 0)]
        for item in decisions:
            if item['phrase'] and str(item['tweet']['id']) not in to_send:
                item['action'] = 'held: over the reply cap, left queued'

    failed = []
    if dry_run:
        approvals += [(tweet_id, 'would reply') for tweet_id in to_send]
    else:
        for position, tweet_id in enumerate(to_send):
            reply_id = send(store, client, user_id, tweet_id)
            if reply_id is None:
                failed.append(tweet_id)
            for item in decisions:
                if str(item['tweet']['id']) == tweet_id:
                    item['reply'] = state['mentions'][tweet_id].get('reply_text') or item['reply']
                    item['action'] = f'replied https://x.com/{ACCOUNT}/status/{reply_id}' if reply_id else 'refused by X'
            if tweet_id in approve:
                approvals.append((tweet_id, f'replied {reply_id}' if reply_id else 'refused by X'))
            if position + 1 < len(to_send):
                sleep(10)

    if review and not dry_run:
        for tweet_id, row in sorted(state['mentions'].items(), key=lambda item: int(item[0])):
            if row.get('status') != 'replied' or not row.get('issue') or row.get('issue_closed'):
                continue
            try:
                if not row.get('issue_commented'):
                    review.comment(row['issue'], f'https://x.com/{ACCOUNT}/status/{row["reply_id"]}')
                    row['issue_commented'] = True
                    store.save()
                review.close(row['issue'])
            except AgentHandlerError as exc:
                review_errors.append(f'closing issue #{row["issue"]}: {exc}')
                continue
            row['issue_closed'] = True
            store.save()

    report = render(mode, decisions, approvals)
    print(report, flush=True)
    write_summary(report)
    if review_errors:
        raise RuntimeError('Agent Handler review issue step failed: ' + '; '.join(review_errors))
    if failed:
        raise RuntimeError('X refused replies to ' + ', '.join(failed) + '. See the log for the reason.')
    return 0


def format_status(state):
    counts = {}
    for row in state['mentions'].values():
        counts[row.get('status')] = counts.get(row.get('status'), 0) + 1
    lines = [f'account=@{ACCOUNT}', f'since_id={state.get("since_id")}',
             'counts=' + json.dumps(counts, sort_keys=True)]
    for tweet_id, row in sorted(state['mentions'].items(), key=lambda item: int(item[0])):
        if row.get('status') == 'queued':
            issue = f' issue #{row["issue"]}' if row.get('issue') else ''
            lines.append(f'queued {tweet_id} "{row.get("phrase")}" {tweet_url(row.get("username"), tweet_id)}{issue}')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reply to @t3codes mentions that say T3 Code is broken.')
    parser.add_argument('command', choices=['run', 'dry-run', 'status'])
    args = parser.parse_args(argv)
    mode = 'dry-run' if args.command == 'dry-run' else (os.environ.get('REPLY_MODE') or 'dry-run').strip()
    if args.command == 'run' and mode != 'dry-run' and os.environ.get('GITHUB_ACTIONS') != 'true':
        parser.error('Replying runs only in GitHub Actions. Use: python replies.py dry-run')
    directory = data_dir()
    configure_logging(directory)
    secret_values = [os.environ.get(name, '') for name in SECRET_NAMES + AH_SECRET_NAMES]
    try:
        with run_lock(directory / 'bot.lock'):
            store = ReplyStore(directory / 'replies.json')
            if args.command == 'status':
                print(format_status(store.data), flush=True)
                return 0
            review = None
            if agent_handler_configured():
                review = ReviewQueue(AgentHandler.from_env(), os.environ.get('GITHUB_REPOSITORY') or REVIEW_REPO)
            elif mode == 'queue':
                LOG.warning('Agent Handler secrets are not set. Queued replies get no review issue; approve by ID.')
            return execute(
                store,
                XClient(load_credentials()),
                mode,
                approve=split_ids(os.environ.get('REPLY_APPROVE')),
                reject=split_ids(os.environ.get('REPLY_REJECT')),
                review=review,
                scan=os.environ.get('REPLY_SCAN', '1') != '0',
            )
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError, KeyError, ApiError) as exc:
        message = redact(str(exc), secret_values)
        LOG.error('%s', message)
        write_summary(f'\nFailed: {message}\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
