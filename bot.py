"""T3Code nightly -> X post (or thread) containing exact PR titles."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('BOT_DATA_DIR', str(Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'T3CodesReleaseBot')))
REPO = 'pingdotgg/t3code'
LOG = logging.getLogger('release-bot')
LIMIT = 280


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


class ApiError(RuntimeError):
    def __init__(self, status, service):
        self.status = status
        super().__init__(f'{service} HTTP {status}')


def request_json(url, *, headers=None, payload=None):
    req = urllib.request.Request(url, headers=headers or {},
                                 data=json.dumps(payload).encode() if payload is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Never log request headers, credentials or untrusted response bodies.
        raise ApiError(exc.code, urllib.parse.urlsplit(url).hostname) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError('Network request failed; delivery may be uncertain.') from None


class GitHub:
    def get(self, path):
        result = subprocess.run(['gh', 'api', f'repos/{REPO}/{path}'],
                                capture_output=True, encoding='utf-8', timeout=90,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('GitHub request failed. Check gh authentication and rate limits.')
        return json.loads(result.stdout)

    def releases(self):
        # Do not assume GitHub returns releases in published_at order (preview
        # releases and older drafts published later can interleave).
        releases = []
        for page in range(1, 101):
            batch = self.get(f'releases?per_page=100&page={page}')
            releases.extend(r for r in batch if not r['draft'] and '-nightly.' in r['tag_name'])
            if len(batch) < 100:
                return sorted(releases, key=lambda r: (r['published_at'], r['id']))
        raise RuntimeError('Release pagination exceeded safety limit; refusing incomplete discovery.')


def clean(text):
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = html.unescape(text)
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    # Prevent auto-linking bare domains/emails and mentioning GitHub usernames on X.
    text = re.sub(r'\b[\w-]+(?:\.[\w-]+)*\.[a-zA-Z]{2,}(?:/\S*)?', lambda m: m[0].replace('.', '[dot]'), text)
    text = re.sub(r'@(?=[\w])', '', text)
    text = re.sub(r'`|(?<!\w)[*_]+|[*_]+(?!\w)', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    return ' '.join(text.split()).strip()


def weight(text):
    # X's Unicode ranges; emoji sequences may be overcounted, never undercounted.
    return sum(1 if ord(c) <= 0x10ff or 0x2000 <= ord(c) <= 0x200d or
               0x2010 <= ord(c) <= 0x201f or 0x2032 <= ord(c) <= 0x2037 else 2
               for c in unicodedata.normalize('NFC', text))


def release_items(release, github):
    body = release.get('body') or ''
    # Only the release's change section identifies inclusion; PR body references
    # never introduce extra PRs into the release.
    change_section = body.split('## New Contributors')[0].split('**Full Changelog**')[0]
    numbers = list(dict.fromkeys(int(n) for n in re.findall(
        r'https://github\.com/pingdotgg/t3code/pull/(\d+)\b', change_section)))
    if not numbers:
        numbers = list(dict.fromkeys(int(n) for n in re.findall(r'\bin #(\d+)\b', change_section)))
    extras = []
    if numbers:
        for line in change_section.splitlines():
            if re.match(r'^\s*[-*]\s', line) and not re.search(r'/pull/\d+|\bin #\d+', line):
                extras.append(clean(re.sub(r'^\s*[-*]\s*', '', line)))
    else:
        comparison = re.search(r'https://github\.com/pingdotgg/t3code/compare/([^\s]+)', body)
        if not comparison:
            raise RuntimeError(f"{release['tag_name']}: no PR list or comparison yet; will retry.")
        comparison = comparison[1].rstrip(')')
        commits = []
        for page in range(1, 101):
            data = github.get(f'compare/{comparison}?per_page=100&page={page}')
            commits.extend(data['commits'])
            if len(commits) >= data['total_commits']:
                break
        else:
            raise RuntimeError('Incomplete commit comparison.')
        for commit in commits:
            associated = github.get(f"commits/{commit['sha']}/pulls?per_page=100")
            merged = [p['number'] for p in associated if p.get('merged_at')]
            numbers.extend(merged)
            if not merged:
                extras.append(clean(commit['commit']['message'].splitlines()[0]))
        numbers = list(dict.fromkeys(numbers))
    prs = [github.get(f'pulls/{number}') for number in numbers]
    return prs, extras


def split_posts(header, lines):
    # Pack whole title lines into posts of at most LIMIT characters. Titles are
    # never summarized, truncated or split across posts; a single title too long
    # for one post yields an oversized post, which publish() skips.
    posts, current = [], [header + '\n']
    for line in lines:
        # The first line of each post always goes in, even if it alone is too long.
        if len(current) > 1 and weight('\n'.join(current + [line])) > LIMIT:
            posts.append('\n'.join(current))
            current = []
        current.append(line)
    posts.append('\n'.join(current))
    return posts


def build_plan(release, github):
    prs, extras = release_items(release, github)
    if not prs:
        raise RuntimeError('No PR list available; waiting for release information.')
    header = 'T3Code nightly ' + release['tag_name']
    posts = split_posts(header, ['- ' + pr['title'] for pr in prs])
    return {'release': release, 'prs': prs, 'extras': extras, 'posts': posts}


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS releases (
                id INTEGER PRIMARY KEY, tag TEXT NOT NULL, published TEXT NOT NULL,
                plan TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS posts (
                release_id INTEGER NOT NULL, position INTEGER NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', tweet_id TEXT, attempted TEXT,
                PRIMARY KEY(release_id, position));
        ''')

    def meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, value))
            self.checkpoint()
            return value
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def queue(self, plan):
        r = plan['release']
        with self.db:
            self.db.execute('INSERT INTO releases(id,tag,published,plan) VALUES (?,?,?,?)',
                            (r['id'], r['tag_name'], r['published_at'], json.dumps(plan)))
            self.db.executemany('INSERT INTO posts(release_id,position,text) VALUES (?,?,?)',
                                [(r['id'], i, p) for i, p in enumerate(plan['posts'])])

        self.checkpoint()

    def checkpoint(self):
        if os.environ.get('BOT_REMOTE_STATE') != '1':
            return
        # WAL is folded into the committed database before the remote checkpoint.
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        from cloud_auth import checkpoint
        checkpoint()


class X:
    def __init__(self):
        self.token = None

    def authenticate(self, force=False):
        if os.environ.get('X_VAULT_KEY'):
            from cloud_auth import access_token
            self.token = access_token(force=force)
            return
        module = str(ROOT / 'XAuth.psm1').replace("'", "''")
        command = f"Import-Module '{module}' -Force; Get-T3XAccessToken" + (' -ForceRefresh' if force else '')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
                                capture_output=True, encoding='utf-8', timeout=90,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or not result.stdout.strip():
            raise RuntimeError('Cannot load/refresh encrypted X credentials.')
        self.token = result.stdout.strip()

    def call(self, path, payload=None):
        if not self.token:
            self.authenticate()
        for attempt in range(2):
            try:
                return request_json('https://api.x.com/2/' + path,
                                    headers={'Authorization': 'Bearer ' + self.token,
                                             'Content-Type': 'application/json'}, payload=payload)
            except ApiError as exc:
                if exc.status == 401 and attempt == 0:
                    self.authenticate(force=True)
                    continue
                raise

    def verify(self):
        user = self.call('users/me')['data']
        if user['username'].lower() != 't3codes':
            raise RuntimeError('Refusing to post to an unexpected account.')
        return user['id']

    def create(self, text, parent):
        data = {'text': text}
        if parent:
            data['reply'] = {'in_reply_to_tweet_id': parent}
        result = self.call('tweets', data)
        if not result.get('data', {}).get('id'):
            raise RuntimeError('X response did not confirm a post ID.')
        return result['data']['id']

    def reconcile(self, user, row, parent):
        # Reconciliation is read-only. Absence from a timeline does not prove the
        # original POST failed, so never blindly re-send an ambiguous request.
        start = (dt.datetime.fromisoformat(row['attempted'].replace('Z', '+00:00')) - dt.timedelta(minutes=2)).isoformat().replace('+00:00', 'Z')
        params = {'max_results': 100, 'start_time': start, 'tweet.fields': 'referenced_tweets'}
        for _ in range(32):
            result = self.call(f'users/{user}/tweets?' + urllib.parse.urlencode(params))
            for post in result.get('data', []):
                reply_to = next((p['id'] for p in post.get('referenced_tweets', []) if p['type'] == 'replied_to'), None)
                if post['text'] == row['text'] and reply_to == parent:
                    return post['id']
            token = result.get('meta', {}).get('next_token')
            if not token:
                break
            params['pagination_token'] = token
        raise RuntimeError('Uncertain post delivery: awaiting reconciliation. No duplicate was sent.')


def skip_backlog(store):
    # One-time: releases held under the old single-post rule are marked skipped
    # (done=2) instead of being posted late as threads.
    if store.meta('thread_backlog_skipped') is not None:
        return
    for release in store.db.execute('SELECT * FROM releases WHERE done=0').fetchall():
        rows = store.db.execute('SELECT * FROM posts WHERE release_id=?', (release['id'],)).fetchall()
        if all(r['status'] == 'pending' for r in rows) and any(weight(r['text']) > LIMIT for r in rows):
            with store.db:
                store.db.execute('UPDATE releases SET done=2 WHERE id=?', (release['id'],))
            LOG.info('Skipped held backlog release %s', release['tag'])
    store.meta('thread_backlog_skipped', now())


def publish(store, api, sleep=time.sleep):
    pending = store.db.execute('SELECT * FROM releases WHERE done=0 ORDER BY published,id').fetchall()
    if not pending:
        return
    user = None
    skipped = []
    for release in pending:
        rows = store.db.execute('SELECT * FROM posts WHERE release_id=?', (release['id'],)).fetchall()
        if any(weight(r['text']) > LIMIT for r in rows):
            # Only possible when one title alone exceeds the limit. Skip it so
            # the failure is reported once rather than on every run.
            with store.db:
                store.db.execute('UPDATE releases SET done=2 WHERE id=?', (release['id'],))
            store.checkpoint()
            skipped.append(release['tag'])
            LOG.error('Skipped %s: a single title exceeds %s characters and titles are never truncated.', release['tag'], LIMIT)
            continue
        if user is None:
            user = api.verify()
        parent = None
        for row in store.db.execute('SELECT * FROM posts WHERE release_id=? ORDER BY position', (release['id'],)).fetchall():
            if row['status'] == 'posted':
                parent = row['tweet_id']
                continue
            if row['status'] == 'sending':
                post_id = api.reconcile(user, row, parent)
            else:
                with store.db:
                    store.db.execute("UPDATE posts SET status='sending',attempted=? WHERE release_id=? AND position=?",
                                     (now(), release['id'], row['position']))
                store.checkpoint()
                try:
                    post_id = api.create(row['text'], parent)
                except ApiError as exc:
                    # Explicit rejection is safe to retry on a later run.
                    # 5xx/network failures stay 'sending' for reconciliation.
                    if 400 <= exc.status < 500 and exc.status != 408:
                        with store.db:
                            store.db.execute("UPDATE posts SET status='pending' WHERE release_id=? AND position=?",
                                             (release['id'], row['position']))
                    store.checkpoint()
                    raise
            with store.db:
                store.db.execute("UPDATE posts SET status='posted',tweet_id=? WHERE release_id=? AND position=?",
                                 (post_id, release['id'], row['position']))
            store.checkpoint()
            LOG.info('Published %s post %s as %s', release['tag'], row['position'] + 1, post_id)
            parent = post_id
            sleep(2)
        with store.db:
            store.db.execute('UPDATE releases SET done=1 WHERE id=?', (release['id'],))
        store.checkpoint()
    if skipped:
        raise RuntimeError('Releases skipped because a single title exceeds the post limit: ' + ', '.join(skipped))


@contextlib.contextmanager
def run_lock(path):
    with open(path, 'a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('Another bot run is active.') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['preview', 'run', 'status'])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'run' and os.environ.get('GITHUB_ACTIONS') != 'true':
        parser.error('Publishing now runs only in GitHub Actions. Local scheduling is disabled.')
    DATA.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(DATA / 'bot.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()],
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        with run_lock(DATA / 'bot.lock'):
            store = Store(DATA / 'state.sqlite3')
            if args.command == 'status':
                print(json.dumps([dict(r) for r in store.db.execute(
                    'SELECT r.tag,r.done,p.status,COUNT(*) AS posts FROM releases r JOIN posts p ON r.id=p.release_id GROUP BY r.id,p.status')], indent=2))
                return 0
            github = GitHub()
            releases = github.releases()
            if not releases:
                raise RuntimeError('No published nightly releases found.')
            if args.command == 'preview':
                plan = build_plan(releases[-1], github)
                output = args.output or ROOT / 'latest-preview.txt'
                output.write_text('\n\n---\n\n'.join(plan['posts']) + '\n', encoding='utf-8')
                LOG.info('Preview: %s; %s PRs; %s posts; %s', plan['release']['tag_name'], len(plan['prs']), len(plan['posts']), output)
                return 0
            # Start at the newest nightly, excluding the historic backlog.
            # The baseline is durable BEFORE fetching PRs or attempting X writes.
            baseline = store.meta('baseline')
            if baseline is None:
                baseline = store.meta('baseline', releases[-1]['published_at'])
            for release in releases:
                if release['published_at'] < baseline:
                    continue
                if store.db.execute('SELECT 1 FROM releases WHERE id=?', (release['id'],)).fetchone():
                    continue
                plan = build_plan(release, github)
                store.queue(plan)
                LOG.info('Queued %s with %s PRs and %s posts', release['tag_name'], len(plan['prs']), len(plan['posts']))
            skip_backlog(store)
            publish(store, X())
            LOG.info('Run complete.')
            return 0
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        # Exception text from our code is deliberately free of credentials.
        LOG.error('%s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
