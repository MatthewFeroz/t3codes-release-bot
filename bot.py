"""Post new T3 Code nightlies from pingdotgg/t3code to @t3codes.

Nightlies are GitHub releases whose tags contain ``-nightly.``. This
repository cannot receive release events from that upstream repo, so the
workflow polls the release list. Links are removed from the release notes
before posting, so a post stays plain text. Notes longer than X's limit are
split into a reply thread. The remaining words are not rewritten.

OAuth 1.0a user credentials come from GitHub Actions secrets. Nothing here
reads the retired encrypted vault.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from xurls import ensure_plain

ROOT = Path(__file__).resolve().parent
SOURCE_REPO = 'pingdotgg/t3code'
ACCOUNT = 't3codes'
LIMIT = 280
API = 'https://api.x.com/2'
SECRET_NAMES = ('X_API_KEY', 'X_API_KEY_SECRET', 'X_ACCESS_TOKEN', 'X_ACCESS_TOKEN_SECRET')
LOG = logging.getLogger('release-bot')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def data_dir():
    return Path(os.environ.get('BOT_DATA_DIR', str(ROOT / '.bot-state')))


def env_flag(name):
    return os.environ.get(name, '').strip().lower() in {'1', 'true', 'yes'}


def pct(value):
    # RFC 3986 unreserved characters, including "~", stay literal.
    return urllib.parse.quote(str(value), safe='~')


def redact(text, secret_values=()):
    if not text:
        return ''
    text = re.sub(r'x-access-token:[^@\s]+', 'x-access-token:***', text)
    text = re.sub(r'://[^/\s:@]+:[^@\s]+@', '://***:***@', text)
    for secret in secret_values:
        if secret and len(secret) >= 8:
            text = text.replace(secret, '***')
    return text


class ApiError(RuntimeError):
    def __init__(self, status, detail):
        self.status = status
        super().__init__(detail)


def describe_http_error(status, body, secret_values=()):
    raw = redact(body or '', secret_values).strip()
    parts = []
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    if isinstance(parsed, dict):
        for key in ('title', 'detail'):
            if parsed.get(key):
                parts.append(str(parsed[key]))
        errors = parsed.get('errors')
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, dict) and error.get('message'):
                    parts.append(str(error['message']))
                elif isinstance(error, str):
                    parts.append(error)
    message = '; '.join(parts) if parts else (raw[:500] or 'no error body')
    return f'HTTP {status}: {redact(message, secret_values)[:800]}'


def http_json(method, url, headers, data=None, secret_values=(), timeout=45):
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')
        raise ApiError(exc.code, 'X ' + describe_http_error(exc.code, raw, secret_values)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = redact(str(getattr(exc, 'reason', exc)), secret_values)
        raise RuntimeError(
            'Network request to X failed (' + reason + '). '
            'The post may already exist; the next run checks @t3codes before sending it again.'
        ) from None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError('X returned a response that was not JSON.') from None


def oauth_signature(method, base_url, params, consumer_secret, token_secret):
    encoded = [pct(key) + '=' + pct(params[key]) for key in sorted(params, key=pct)]
    base = '&'.join([method.upper(), pct(base_url), pct('&'.join(encoded))])
    key = pct(consumer_secret) + '&' + pct(token_secret)
    digest = hmac.new(key.encode('utf-8'), base.encode('utf-8'), hashlib.sha1).digest()
    return base64.b64encode(digest).decode('ascii')


def authorization_header(params):
    parts = []
    for key in sorted(params):
        if key.startswith('oauth_'):
            parts.append(f'{pct(key)}="{pct(params[key])}"')
    return 'OAuth ' + ', '.join(parts)


def weight(text):
    # X counts U+0000..U+10FF and a few punctuation ranges as 1, else 2.
    # Emoji can be overcounted. URLs are counted in full, which is stricter
    # than X wrapping them to a 23-character t.co link.
    total = 0
    for char in unicodedata.normalize('NFC', text):
        code = ord(char)
        if (code <= 0x10FF or 0x2000 <= code <= 0x200D or 0x2010 <= code <= 0x201F
                or 0x2032 <= code <= 0x2037):
            total += 1
        else:
            total += 2
    return total


def _max_prefix(text, limit):
    lo, hi = 1, len(text)
    if weight(text[:1]) > limit:
        return 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if weight(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _prefer_boundary(text, cut):
    window = text[:cut]
    for separator in ('\n', ' '):
        index = window.rfind(separator)
        if index > 0 and window[:index].strip():
            return index + 1
    return cut


def split_thread(text, limit=LIMIT):
    """Split text into posts of at most ``limit`` weighted characters.

    The pieces join back to ``text``. The split prefers a newline, then a
    space, and only then breaks a long line. It does not rewrite or drop
    characters.
    """
    if text is None or not str(text).strip():
        raise RuntimeError('Refusing to post empty text. The nightly notes were not truncated.')
    text = str(text)
    if weight(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if weight(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = _prefer_boundary(remaining, _max_prefix(remaining, limit))
        if cut < 1 or not remaining[:cut].strip():
            raise RuntimeError(
                f'Refusing to post: the nightly text cannot be split inside {limit} weighted '
                'characters without a blank post. It was not truncated.'
            )
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if ''.join(chunks) != text or any(weight(chunk) > limit or not chunk.strip() for chunk in chunks):
        raise RuntimeError('Internal error: thread split changed the nightly text or exceeded the limit. Nothing was posted.')
    return chunks


def is_nightly(release):
    return not release.get('draft') and '-nightly.' in (release.get('tag_name') or '')


def release_text(release):
    """Return nightly notes with links removed.

    X bills a post that contains a URL at the link rate. Bare URLs, markdown
    links, autolinks, and anything twitter-text would auto-link are removed.
    The remaining notes are not rewritten. Trailing whitespace is removed so a
    final newline cannot become its own blank post.
    """
    body = release.get('body')
    tag = release.get('tag_name') or release.get('id')
    if body is None:
        raise RuntimeError(f'{tag}: GitHub release payload had no notes. Nothing was posted.')
    text = ensure_plain(str(body)).strip()
    if not text.strip():
        raise RuntimeError(f'{tag}: nightly release notes are empty. Nothing was posted.')
    return text


def load_credentials(env=None):
    env = os.environ if env is None else env
    missing = [name for name in SECRET_NAMES if not (env.get(name) or '').strip()]
    if missing:
        raise RuntimeError(
            'Missing GitHub Actions secrets: ' + ', '.join(missing)
            + '. Add them under Settings > Secrets and variables > Actions. See README.md.'
        )
    return {name: env[name].strip() for name in SECRET_NAMES}


class XClient:
    def __init__(self, creds, http=http_json):
        self.api_key = creds['X_API_KEY']
        self.api_secret = creds['X_API_KEY_SECRET']
        self.access_token = creds['X_ACCESS_TOKEN']
        self.access_secret = creds['X_ACCESS_TOKEN_SECRET']
        self.http = http
        self._secrets = (self.api_key, self.api_secret, self.access_token, self.access_secret)

    def auth_header(self, method, base_url, query, nonce=None, timestamp=None):
        oauth = {
            'oauth_consumer_key': self.api_key,
            'oauth_nonce': nonce or secrets.token_hex(16),
            'oauth_signature_method': 'HMAC-SHA1',
            'oauth_timestamp': str(timestamp if timestamp is not None else int(time.time())),
            'oauth_token': self.access_token,
            'oauth_version': '1.0',
        }
        params = {**query, **oauth}
        oauth['oauth_signature'] = oauth_signature(method, base_url, params, self.api_secret, self.access_secret)
        return authorization_header(oauth)

    def call(self, method, url, payload=None):
        parts = urllib.parse.urlsplit(url)
        base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        headers = {
            'Authorization': self.auth_header(method, base, query),
            'User-Agent': 't3codes-release-bot',
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        return self.http(method, url, headers, data, secret_values=self._secrets)

    def verify(self):
        result = self.call('GET', API + '/users/me')
        user = result.get('data') or {}
        username = user.get('username') or ''
        if username.lower() != ACCOUNT:
            raise RuntimeError(f'Refusing to post: authenticated as @{username or "unknown"}, expected @{ACCOUNT}.')
        if not user.get('id'):
            raise RuntimeError('X users/me response did not include an account id.')
        return str(user['id'])

    def create(self, text, parent):
        text = ensure_plain(text)
        size = weight(text)
        if size > LIMIT:
            raise RuntimeError(
                f'Refusing to post: text is {size} weighted characters, over the {LIMIT} limit. '
                'It was not truncated.'
            )
        if not str(text).strip():
            raise RuntimeError('Refusing to post empty text. It was not truncated.')
        payload = {'text': text}
        if parent:
            payload['reply'] = {'in_reply_to_tweet_id': str(parent)}
        result = self.call('POST', API + '/tweets', payload)
        tweet_id = (result.get('data') or {}).get('id')
        if not tweet_id:
            raise RuntimeError('X response did not include a post id. Nothing was recorded as posted.')
        return str(tweet_id)

    def find_post(self, user_id, text, parent):
        # No start_time: OAuth 1.0a user context rejects that parameter, and a
        # start_time older than X's recent window was the HTTP 400 that stalled
        # the previous publisher. Scan the newest posts instead.
        params = {'max_results': '100', 'tweet.fields': 'referenced_tweets'}
        for _ in range(5):
            url = f'{API}/users/{user_id}/tweets?' + urllib.parse.urlencode(params)
            result = self.call('GET', url)
            for post in result.get('data') or []:
                reply_to = next((item['id'] for item in post.get('referenced_tweets') or []
                                 if item.get('type') == 'replied_to'), None)
                same_parent = reply_to is None if parent is None else str(reply_to) == str(parent)
                if post.get('text') == text and same_parent:
                    return str(post['id'])
            token = (result.get('meta') or {}).get('next_token')
            if not token:
                return None
            params['pagination_token'] = token
        return None


class GitHub:
    def __init__(self, repo=SOURCE_REPO):
        self.repo = repo

    def get(self, path):
        result = subprocess.run(
            ['gh', 'api', f'repos/{self.repo}/{path}'],
            capture_output=True, encoding='utf-8', timeout=90,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode:
            detail = redact((result.stderr or result.stdout or '').strip())
            raise RuntimeError(f'GitHub request failed for {path}: {detail[:500]}')
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f'GitHub response for {path} was not JSON.') from None

    def nightlies(self):
        releases = []
        for page in range(1, 101):
            batch = self.get(f'releases?per_page=100&page={page}')
            if not isinstance(batch, list):
                raise RuntimeError('GitHub releases response was not a list.')
            releases.extend(release for release in batch if is_nightly(release))
            if len(batch) < 100:
                releases.sort(key=lambda release: (release['published_at'], release['id']))
                return releases
        raise RuntimeError('Release pagination exceeded safety limit; refusing incomplete discovery.')


def load_legacy(path):
    if not path.exists():
        return None
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        baseline_row = connection.execute("SELECT value FROM meta WHERE key='baseline'").fetchone()
        releases = [dict(row) for row in connection.execute('SELECT id, tag, published, done FROM releases')]
        grouped = {}
        for post in connection.execute(
            'SELECT release_id, position, status, tweet_id FROM posts ORDER BY release_id, position'
        ):
            grouped.setdefault(post['release_id'], []).append(dict(post))
    finally:
        connection.close()
    items = []
    for release in releases:
        posts = grouped.get(release['id'], [])
        tweet_ids = [str(post['tweet_id']) for post in posts if post['tweet_id']]
        started = any(post['status'] in ('posted', 'sending') or post['tweet_id'] for post in posts)
        items.append({**release, 'tweet_ids': tweet_ids, 'started': started})
    return {'baseline': baseline_row['value'] if baseline_row else None, 'items': items}


def settle_legacy(legacy):
    """Mark the pre-fix queue so a repaired poster cannot flood @t3codes.

    Releases already on X stay posted. Releases the old single-title rule
    skipped stay skipped. Unpublished nightlies that piled up while posting was
    stuck are skipped as well, except the newest one, which is left open so the
    next run posts it. Anything published after that newest release is posted
    normally.
    """
    items = legacy['items']
    if not items:
        return {
            'version': 1,
            'outage_settled': True,
            'baseline': legacy.get('baseline'),
            'post_after': None,
            'releases': {},
        }
    newest = max(items, key=lambda item: (item['published'], item['id']))
    leave_open = newest['done'] == 0 and not newest['started']
    releases = {}
    for item in items:
        if leave_open and item['id'] == newest['id']:
            continue
        if item['done'] == 2 or (item['done'] == 0 and not item['started']):
            status = 'skipped'
            reason = 'previously-skipped' if item['done'] == 2 else 'outage-backlog'
        else:
            status = 'posted'
            reason = 'already-posted'
        releases[str(item['id'])] = {
            'tag': item['tag'],
            'published': item['published'],
            'status': status,
            'reason': reason,
            'tweet_ids': item['tweet_ids'],
        }
    return {
        'version': 1,
        'outage_settled': True,
        'baseline': legacy.get('baseline'),
        'post_after': {'id': newest['id'], 'published': newest['published'], 'tag': newest['tag']},
        'releases': releases,
    }


def settle_fresh(releases):
    newest = releases[-1]
    return {
        'version': 1,
        'outage_settled': True,
        'baseline': newest['published_at'],
        'post_after': {
            'id': newest['id'],
            'published': newest['published_at'],
            'tag': newest['tag_name'],
        },
        'releases': {},
    }


def is_pending(release, state):
    row = (state.get('releases') or {}).get(str(release['id']))
    if row and row.get('status') in ('posted', 'skipped'):
        return False
    if row and row.get('status') == 'in_progress':
        return True
    baseline = state.get('baseline')
    if baseline and release['published_at'] < baseline:
        return False
    cursor = state.get('post_after')
    if cursor and (release['published_at'], int(release['id'])) < (cursor['published'], int(cursor['id'])):
        return False
    return True


def checkpoint(path):
    if os.environ.get('BOT_REMOTE_STATE') != '1':
        return
    relative = Path(os.path.relpath(path, Path.cwd()))
    if relative.is_absolute() or '..' in relative.parts:
        raise RuntimeError('Posting state is outside the repository; refusing to checkpoint it.')

    def git(*args):
        result = subprocess.run(['git', *args], capture_output=True, text=True)
        if result.returncode:
            detail = redact((result.stderr or result.stdout or '').strip())
            raise RuntimeError('Remote state checkpoint failed; publishing halted. ' + detail[:500])
        return result.stdout

    git('add', '--', str(relative))
    if git('diff', '--cached', '--name-only').strip():
        git('commit', '-m', 'Persist release bot progress')
    # A previous push may have failed after the commit existed locally.
    git('push', 'origin', 'HEAD:main')


class Store:
    def __init__(self, path, persist=True):
        self.path = Path(path)
        self.persist = persist
        self.data = self._load()

    def _load(self):
        if not self.path.exists():
            return {'version': 1, 'outage_settled': False, 'releases': {}}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        data.setdefault('version', 1)
        data.setdefault('outage_settled', False)
        data.setdefault('releases', {})
        return data

    def save(self):
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(temporary, self.path)
        checkpoint(self.path)


def ensure_git_identity():
    if os.environ.get('BOT_REMOTE_STATE') != '1':
        return
    for key in ('user.name', 'user.email'):
        result = subprocess.run(['git', 'config', '--get', key], capture_output=True, text=True)
        if result.returncode or not result.stdout.strip():
            raise RuntimeError(f'git {key} is not set; refusing to post without a durable state commit.')


def write_summary(text):
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(text)


def prepare_state(store, releases, dry_run):
    if store.data.get('outage_settled'):
        return store
    legacy_path = store.path.parent / 'state.sqlite3'
    legacy = load_legacy(legacy_path)
    planned = settle_legacy(legacy) if legacy and legacy['items'] else settle_fresh(releases)
    if dry_run:
        preview = Store(store.path, persist=False)
        preview.data = planned
        return preview
    ensure_git_identity()
    store.data = planned
    store.save()
    skipped = sum(1 for row in planned['releases'].values() if row['status'] == 'skipped')
    cursor = (planned.get('post_after') or {}).get('tag')
    LOG.info('Settled existing state. Skipped %s historical releases. Next cursor is %s.', skipped, cursor)
    return store


def post_release(store, client, user_id, release, sleep):
    release_id = str(release['id'])
    row = store.data['releases'].get(release_id)
    if not row or row.get('status') != 'in_progress':
        text = release_text(release)
        chunks = split_thread(text)
        if ''.join(chunks) != text:
            raise RuntimeError(f'{release["tag_name"]}: thread split changed the notes. Nothing was posted.')
        row = {
            'tag': release['tag_name'],
            'published': release['published_at'],
            'status': 'in_progress',
            'chunks': chunks,
            'tweet_ids': [],
            'attempted_index': None,
            'attempted_at': None,
        }
        store.data['releases'][release_id] = row
        store.save()
    if not row.get('chunks'):
        raise RuntimeError(f'{row.get("tag")}: in-progress post is missing its text. Refusing to send it again blindly.')
    ids = [str(tweet_id) for tweet_id in row.get('tweet_ids') or []]
    chunks = list(row['chunks'])
    row['chunks'] = chunks
    for index, chunk in enumerate(chunks):
        if index < len(ids):
            continue
        plain = ensure_plain(chunk)
        if plain != chunk:
            chunks[index] = plain
            store.save()
        chunk = plain
        if not chunk.strip():
            continue
        size = weight(chunk)
        if size > LIMIT:
            raise RuntimeError(
                f'Refusing to post {row["tag"]} part {index + 1}: it is {size} weighted characters '
                f'(limit {LIMIT}). The notes were not truncated.'
            )
        parent = ids[-1] if ids else None
        tweet_id = None
        if row.get('attempted_index') == index:
            tweet_id = client.find_post(user_id, chunk, parent)
        if tweet_id is None:
            row['attempted_index'] = index
            row['attempted_at'] = now()
            store.save()
            try:
                tweet_id = client.create(chunk, parent)
            except ApiError as exc:
                # A duplicate means this text is already on the account. Record that
                # post instead of failing closed on every later nightly.
                if 'duplicate' not in str(exc).lower():
                    raise
                tweet_id = client.find_post(user_id, chunk, parent)
                if not tweet_id:
                    raise RuntimeError(
                        f'{row["tag"]} part {index + 1} was rejected as a duplicate, but the existing '
                        f'post was not found on @{ACCOUNT}. Not sending another copy. X said: {exc}'
                    ) from None
        ids.append(str(tweet_id))
        row['tweet_ids'] = ids
        row['attempted_index'] = None
        row['attempted_at'] = None
        store.save()
        url = f'https://x.com/{ACCOUNT}/status/{tweet_id}'
        print(f'POST_URL={url}', flush=True)
        LOG.info('Posted %s %s/%s %s', row['tag'], index + 1, len(chunks), url)
        write_summary(f'- {row["tag"]} {index + 1}/{len(chunks)}: {url}\n')
        if index + 1 < len(chunks):
            sleep(2)
    if not ids:
        raise RuntimeError(f'{row.get("tag")}: link removal left no post text. Nothing was posted.')
    row['status'] = 'posted'
    row['reason'] = 'posted'
    row.pop('chunks', None)
    row.pop('attempted_index', None)
    row.pop('attempted_at', None)
    store.save()


def render_dry_run(pending):
    lines = ['DRY RUN. Nothing will be posted to X.', '']
    if not pending:
        lines.append('No new nightly releases.')
        return '\n'.join(lines) + '\n'
    for release in pending:
        text = release_text(release)
        chunks = split_thread(text)
        lines.append(
            f'{release["tag_name"]}: {len(chunks)} post(s), {weight(text)} weighted characters.'
        )
        for index, chunk in enumerate(chunks, start=1):
            lines.append(f'--- post {index}/{len(chunks)} ({weight(chunk)} weighted characters) ---')
            lines.append(chunk)
            lines.append('')
    lines.append('Dry run: not posted. No post URL.')
    return '\n'.join(lines) + '\n'


def format_status(state):
    releases = state.get('releases') or {}
    counts = {}
    for row in releases.values():
        counts[row.get('status') or 'unknown'] = counts.get(row.get('status') or 'unknown', 0) + 1
    cursor = (state.get('post_after') or {}).get('tag')
    lines = [
        f'source={SOURCE_REPO}',
        f'account=@{ACCOUNT}',
        f'settled={bool(state.get("outage_settled"))}',
        f'post_after={cursor}',
        'counts=' + json.dumps(counts, sort_keys=True),
    ]
    active = [row for row in releases.values() if row.get('status') == 'in_progress']
    for row in active:
        lines.append(f'in_progress={row.get("tag")} tweets={len(row.get("tweet_ids") or [])}')
    posted = [row for row in releases.values() if row.get('status') == 'posted' and row.get('tweet_ids')]
    posted.sort(key=lambda row: row.get('published') or '')
    for row in posted[-5:]:
        lines.append(f'{row.get("tag")}: ' + ' '.join(
            f'https://x.com/{ACCOUNT}/status/{tweet_id}' for tweet_id in row['tweet_ids']
        ))
    return '\n'.join(lines) + '\n'


def execute(store, github, dry_run, client=None, sleep=time.sleep):
    releases = github.nightlies()
    if not releases:
        raise RuntimeError(f'No published nightly releases found on {SOURCE_REPO}.')
    store = prepare_state(store, releases, dry_run)
    pending = [release for release in releases if is_pending(release, store.data)]
    handled = store.data.get('releases') or {}
    skipped = sum(1 for row in handled.values() if row.get('status') == 'skipped')
    posted = sum(1 for row in handled.values() if row.get('status') == 'posted')
    print(
        f'Nightly source: {SOURCE_REPO} tags containing -nightly. '
        f'Handled {posted} posted, {skipped} skipped. Pending {len(pending)}.',
        flush=True,
    )
    if dry_run:
        report = render_dry_run(pending)
        print(report, flush=True)
        write_summary(report)
        return 0
    if not pending:
        print('No new nightly releases.', flush=True)
        write_summary('No new nightly releases.\n')
        return 0
    ensure_git_identity()
    if client is None:
        client = XClient(load_credentials())
    user_id = client.verify()
    LOG.info('Authenticated as @%s (%s).', ACCOUNT, user_id)
    write_summary(f'## Posted to @{ACCOUNT}\n\n')
    for release in pending:
        post_release(store, client, user_id, release, sleep)
    print('Run complete.', flush=True)
    return 0


@contextlib.contextmanager
def run_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
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


def configure_logging(directory):
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / 'bot.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler, logging.StreamHandler()],
        format='%(asctime)s %(levelname)s %(message)s',
        force=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description='Post new T3 Code nightlies to @t3codes.')
    parser.add_argument('command', choices=['run', 'dry-run', 'status'])
    args = parser.parse_args(argv)
    dry_run = args.command == 'dry-run' or env_flag('DRY_RUN')
    if args.command == 'run' and not dry_run and os.environ.get('GITHUB_ACTIONS') != 'true':
        parser.error('Publishing runs only in GitHub Actions. Use: python bot.py dry-run')
    directory = data_dir()
    configure_logging(directory)
    secret_values = [os.environ.get(name, '') for name in SECRET_NAMES]
    try:
        with run_lock(directory / 'bot.lock'):
            store = Store(directory / 'posted.json')
            if args.command == 'status':
                print(format_status(store.data), flush=True)
                return 0
            return execute(store, GitHub(), dry_run)
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError, KeyError, ApiError) as exc:
        message = redact(str(exc), secret_values)
        LOG.error('%s', message)
        write_summary(f'\nFailed: {message}\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
