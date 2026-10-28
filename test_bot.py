import io
import json
import os
import sqlite3
import sys
import types
import urllib.error
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bot import (
    ACCOUNT,
    DEFAULT_LIMIT,
    SHORT_LIMIT,
    ApiError,
    Store,
    XClient,
    describe_http_error,
    http_json,
    is_nightly,
    is_pending,
    load_credentials,
    long_post_rejected,
    nightly_text,
    oauth_signature,
    post_limit,
    redact,
    release_text,
    render_dry_run,
    settle_legacy,
    split_thread,
    summarize,
    weight,
    post_release,
    execute,
)
import cloud_auth
from xurls import has_detectable_url

# Keep tests off the real Claude API.
os.environ.pop('ANTHROPIC_API_KEY', None)


class FakeGitHub:
    def __init__(self, releases):
        self._releases = releases

    def nightlies(self):
        return list(self._releases)


class FakeX:
    def __init__(self):
        self.created = []
        self.found = {}
        self.fail_find = None
        self.verified = False

    def verify(self):
        self.verified = True
        return '99'

    def find_post(self, user_id, text, parent):
        if self.fail_find:
            raise self.fail_find
        return self.found.get((text, parent))

    def create(self, text, parent):
        if weight(text) > DEFAULT_LIMIT:
            raise RuntimeError('over limit')
        self.created.append((text, parent))
        return str(len(self.created))


def release(release_id, tag, published, body='## What\'s Changed\n* fix by @a in https://github.com/pingdotgg/t3code/pull/1\n'):
    return {
        'id': release_id,
        'tag_name': tag,
        'published_at': published,
        'draft': False,
        'body': body,
    }


class BotTests(unittest.TestCase):
    def test_oauth1_signature_matches_twitter_sample(self):
        params = {
            'status': 'Hello Ladies + Gentlemen, a signed OAuth request!',
            'include_entities': 'true',
            'oauth_consumer_key': 'xvz1evFS4wEEPTGEFPHBog',
            'oauth_nonce': 'kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg',
            'oauth_signature_method': 'HMAC-SHA1',
            'oauth_timestamp': '1318622958',
            'oauth_token': '370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb',
            'oauth_version': '1.0',
        }
        signature = oauth_signature(
            'POST',
            'https://api.twitter.com/1.1/statuses/update.json',
            params,
            'kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw',
            'LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE',
        )
        self.assertEqual(signature, 'hCtSmYh+iHYCEqBWrE7C7hYmtUk=')

    def test_authorization_header_omits_secrets(self):
        client = XClient({
            'X_API_KEY': 'api-key-value',
            'X_API_KEY_SECRET': 'api-secret-value',
            'X_ACCESS_TOKEN': 'access-token-value',
            'X_ACCESS_TOKEN_SECRET': 'access-secret-value',
        })
        header = client.auth_header('POST', 'https://api.x.com/2/tweets', {}, nonce='abc', timestamp=1700000000)
        self.assertTrue(header.startswith('OAuth '))
        self.assertIn('oauth_consumer_key="api-key-value"', header)
        self.assertIn('oauth_signature="', header)
        self.assertNotIn('api-secret-value', header)
        self.assertNotIn('access-secret-value', header)

    def test_split_preserves_text_and_prefers_breaks(self):
        self.assertEqual(split_thread('hello\nworld', limit=6), ['hello\n', 'world'])
        self.assertEqual(split_thread('hello world', limit=6), ['hello ', 'world'])
        self.assertEqual(split_thread('helloworld', limit=4), ['hell', 'owor', 'ld'])
        notes = '## What\'s Changed\n* fix by @a in https://github.com/pingdotgg/t3code/pull/1\n' + ('word ' * 80)
        chunks = split_thread(notes.rstrip(), limit=SHORT_LIMIT)
        self.assertEqual(''.join(chunks), notes.rstrip())
        self.assertTrue(all(weight(chunk) <= SHORT_LIMIT and chunk.strip() for chunk in chunks))
        self.assertGreater(len(chunks), 1)
        self.assertIn('@a', ''.join(chunks))
        self.assertIn('https://github.com/pingdotgg/t3code/pull/1', ''.join(chunks))

    def test_cjk_weight_and_no_truncation(self):
        self.assertEqual(weight('a' * 280), 280)
        self.assertEqual(weight('\u754c'), 2)
        chunks = split_thread('\u754c' * 141, limit=SHORT_LIMIT)
        self.assertEqual(''.join(chunks), '\u754c' * 141)
        self.assertTrue(all(weight(chunk) <= SHORT_LIMIT for chunk in chunks))
        self.assertGreater(len(chunks), 1)

    def test_premium_limit_posts_notes_as_one_piece_until_25k(self):
        self.assertEqual(post_limit({}), DEFAULT_LIMIT)
        self.assertEqual(post_limit({'X_MAX_POST_CHARS': ''}), DEFAULT_LIMIT)
        notes = 'line of release notes\n' * 40
        self.assertGreater(weight(notes), SHORT_LIMIT)
        self.assertLess(weight(notes), DEFAULT_LIMIT)
        with mock.patch.dict(os.environ, {'X_MAX_POST_CHARS': ''}):
            self.assertEqual(split_thread(notes), [notes])
        with self.assertRaisesRegex(RuntimeError, 'positive integer'):
            post_limit({'X_MAX_POST_CHARS': 'nope'})

    def test_notes_over_25k_split_on_lines_into_as_few_posts_as_possible(self):
        line = 'fix by @a ' + ('w' * 80) + '\n'
        text = line * 300
        self.assertGreater(weight(text.strip()), DEFAULT_LIMIT)
        chunks = split_thread(text.strip(), limit=DEFAULT_LIMIT)
        self.assertEqual(''.join(chunks), text.strip())
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].endswith('\n'))
        self.assertLessEqual(weight(chunks[0]), DEFAULT_LIMIT)
        self.assertLessEqual(weight(chunks[1]), DEFAULT_LIMIT)
        nxt = chunks[1].split('\n', 1)[0]
        self.assertGreater(weight(chunks[0] + nxt), DEFAULT_LIMIT)

    def test_release_text_keeps_notes_and_drops_links(self):
        body = '## What\'s Changed\n* fix by @a in https://github.com/pingdotgg/t3code/pull/1\n\n'
        text = release_text({'tag_name': 'v1-nightly.1', 'body': body})
        self.assertEqual(text, '## What\'s Changed\n* fix')
        self.assertNotIn('@a', text)
        self.assertNotIn('http', text)
        self.assertNotIn('github.com', text)
        with self.assertRaisesRegex(RuntimeError, 'empty'):
            release_text({'tag_name': 'v1-nightly.1', 'body': '\n\n'})
        with self.assertRaisesRegex(RuntimeError, 'empty'):
            release_text({'tag_name': 'v1-nightly.1', 'body': 'https://github.com/pingdotgg/t3code/pull/1\n'})

    def test_release_text_drops_author_credits_and_new_contributors(self):
        body = (
            '## What\'s Changed\n'
            '* fix by @a in https://github.com/pingdotgg/t3code/pull/1\n'
            '* pair work by @b, @c and @dependabot[bot] in https://github.com/pingdotgg/t3code/pull/2\n'
            '* bump by @app/renovate in https://github.com/pingdotgg/t3code/pull/3\n'
            '* @-mention a machine by @d in https://github.com/pingdotgg/t3code/pull/4\n'
            '* thanks to @e for the report\n'
            '\n'
            '## New Contributors\n'
            '* @f made their first contribution in https://github.com/pingdotgg/t3code/pull/5\n'
            '\n'
            '**Full Changelog**: https://github.com/pingdotgg/t3code/compare/a...b\n'
        )
        text = release_text({'tag_name': 'v1-nightly.1', 'body': body})
        self.assertEqual(text, (
            '## What\'s Changed\n'
            '* fix\n'
            '* pair work\n'
            '* bump\n'
            '* @-mention a machine\n'
            '* thanks to @e for the report'
        ))

    def test_stuck_post_drops_credits_before_retrying(self):
        # A nightly saved with `by @a` credits before this change still posts without them.
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'posted.json')
            store.data = {'outage_settled': True, 'releases': {
                '5': {
                    'tag': 'v-nightly.5',
                    'published': '2026-10-05T00:00:00Z',
                    'status': 'in_progress',
                    'chunks': ['## What\'s Changed\n* fix by @a\n* other by @b'],
                    'tweet_ids': [],
                    'attempted_index': 0,
                    'attempted_at': '2026-10-28T03:43:38Z',
                },
            }}
            api = FakeX()
            post_release(store, api, '99', release(5, 'v-nightly.5', '2026-10-05T00:00:00Z'), sleep=lambda _: None)
            self.assertEqual(api.created, [('## What\'s Changed\n* fix\n* other', None)])
            self.assertEqual(store.data['releases']['5']['status'], 'posted')

    def test_http_error_includes_x_message_and_redacts_secrets(self):
        secret = 'super-secret-token'
        detail = describe_http_error(
            400,
            json.dumps({
                'title': 'Invalid Request',
                'detail': 'One or more parameters to your request was invalid.',
                'errors': [{'message': f'The start_time is invalid for {secret}'}],
            }),
            [secret],
        )
        self.assertIn('HTTP 400', detail)
        self.assertIn('Invalid Request', detail)
        self.assertIn('start_time is invalid', detail)
        self.assertNotIn(secret, detail)
        self.assertIn('***', detail)

    def test_redact_strips_git_credentials(self):
        raw = 'failed https://x-access-token:ghs_abcdefghijklmnop@github.com/org/repo'
        cleaned = redact(raw)
        self.assertNotIn('ghs_abcdefghijklmnop', cleaned)
        self.assertIn('***:***@', cleaned)

    def test_missing_secrets_list_names_only(self):
        with self.assertRaises(RuntimeError) as caught:
            load_credentials({'X_API_KEY': 'present-secret-value'})
        message = str(caught.exception)
        self.assertIn('X_API_KEY_SECRET', message)
        self.assertIn('X_ACCESS_TOKEN', message)
        self.assertIn('X_ACCESS_TOKEN_SECRET', message)
        self.assertNotIn('present-secret-value', message)

    def test_nightly_filter_ignores_preview_and_stable(self):
        self.assertTrue(is_nightly({'draft': False, 'tag_name': 'v0.1.3-nightly.20261026.3726'}))
        self.assertFalse(is_nightly({'draft': False, 'tag_name': 'v0.1.3-preview.20261026.3702'}))
        self.assertFalse(is_nightly({'draft': False, 'tag_name': 'v0.1.3'}))
        self.assertFalse(is_nightly({'draft': True, 'tag_name': 'v0.1.3-nightly.20261026.3726'}))

    def test_settlement_posts_only_the_newest_unpublished_nightly(self):
        legacy = {
            'baseline': '2026-09-19T00:00:00Z',
            'items': [
                {'id': 1, 'tag': 'posted-tag', 'published': '2026-10-01T00:00:00Z', 'done': 1,
                 'tweet_ids': ['11'], 'started': True},
                {'id': 2, 'tag': 'old-skip', 'published': '2026-10-02T00:00:00Z', 'done': 2,
                 'tweet_ids': [], 'started': False},
                {'id': 3, 'tag': 'backlog', 'published': '2026-10-03T00:00:00Z', 'done': 0,
                 'tweet_ids': [], 'started': False},
                {'id': 4, 'tag': 'partial', 'published': '2026-10-04T00:00:00Z', 'done': 0,
                 'tweet_ids': ['44'], 'started': True},
                {'id': 5, 'tag': 'newest-open', 'published': '2026-10-05T00:00:00Z', 'done': 0,
                 'tweet_ids': [], 'started': False},
            ],
        }
        state = settle_legacy(legacy)
        self.assertNotIn('5', state['releases'])
        self.assertEqual(state['releases']['1']['status'], 'posted')
        self.assertEqual(state['releases']['1']['tweet_ids'], ['11'])
        self.assertEqual(state['releases']['2']['status'], 'skipped')
        self.assertEqual(state['releases']['3']['reason'], 'outage-backlog')
        self.assertEqual(state['releases']['4']['status'], 'posted')
        self.assertEqual(state['post_after']['tag'], 'newest-open')
        newer = release(6, 'v-nightly.newer', '2026-10-06T00:00:00Z')
        older_gap = release(9, 'v-nightly.gap', '2026-10-03T12:00:00Z')
        self.assertTrue(is_pending(release(5, 'newest-open', '2026-10-05T00:00:00Z'), state))
        self.assertTrue(is_pending(newer, state))
        self.assertFalse(is_pending(older_gap, state))
        self.assertFalse(is_pending(release(1, 'posted-tag', '2026-10-01T00:00:00Z'), state))

    def test_thread_replies_and_second_run_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'posted.json')
            store.data = {
                'outage_settled': True,
                'baseline': '2026-10-01T00:00:00Z',
                'post_after': {'id': 5, 'published': '2026-10-05T00:00:00Z', 'tag': 'v-nightly.5'},
                'releases': {},
            }
            body = '## What\'s Changed\n* fix by @a in https://github.com/pingdotgg/t3code/pull/1\n' + ('change ' * 70)
            item = release(5, 'v-nightly.5', '2026-10-05T00:00:00Z', body)
            api = FakeX()
            with mock.patch.dict(os.environ, {'X_MAX_POST_CHARS': ''}):
                post_release(store, api, '99', item, sleep=lambda _: None)
            self.assertEqual(len(api.created), 1)
            self.assertIsNone(api.created[0][1])
            joined = api.created[0][0]
            self.assertEqual(joined, nightly_text(item))
            self.assertGreater(weight(joined), SHORT_LIMIT)
            self.assertLessEqual(weight(joined), DEFAULT_LIMIT)
            self.assertIn('* fix\n', joined)
            self.assertNotIn('@a', joined)
            self.assertNotIn('http', joined)
            self.assertNotIn('github.com', joined)
            self.assertEqual(store.data['releases']['5']['status'], 'posted')
            post_release_again = len(api.created)
            self.assertFalse(is_pending(item, store.data))
            self.assertEqual(len(api.created), post_release_again)

    def test_resume_sends_only_the_remaining_reply(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'posted.json')
            store.data = {'outage_settled': True, 'releases': {
                '5': {
                    'tag': 'v-nightly.5',
                    'published': '2026-10-05T00:00:00Z',
                    'status': 'in_progress',
                    'chunks': ['first line', 'second line'],
                    'tweet_ids': ['10'],
                    'attempted_index': None,
                },
            }}
            api = FakeX()
            post_release(store, api, '99', release(5, 'v-nightly.5', '2026-10-05T00:00:00Z'), sleep=lambda _: None)
            self.assertEqual(api.created, [('second line', '10')])
            self.assertEqual(store.data['releases']['5']['tweet_ids'], ['10', '1'])
            self.assertEqual(store.data['releases']['5']['status'], 'posted')

    def test_uncertain_attempt_is_reused_when_found_and_not_resent_when_lookup_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'posted.json'
            store = Store(path)
            store.data = {'outage_settled': True, 'releases': {
                '5': {
                    'tag': 'v-nightly.5',
                    'published': '2026-10-05T00:00:00Z',
                    'status': 'in_progress',
                    'chunks': ['exact nightly text'],
                    'tweet_ids': [],
                    'attempted_index': 0,
                    'attempted_at': '2026-10-26T00:00:00Z',
                },
            }}
            api = FakeX()
            api.found[('exact nightly text', None)] = '77'
            post_release(store, api, '99', release(5, 'v-nightly.5', '2026-10-05T00:00:00Z'), sleep=lambda _: None)
            self.assertEqual(api.created, [])
            self.assertEqual(store.data['releases']['5']['tweet_ids'], ['77'])

            store.data['releases']['6'] = {
                'tag': 'v-nightly.6',
                'published': '2026-10-06T00:00:00Z',
                'status': 'in_progress',
                'chunks': ['other text'],
                'tweet_ids': [],
                'attempted_index': 0,
                'attempted_at': '2026-10-26T00:00:00Z',
            }
            api.fail_find = ApiError(400, 'X HTTP 400: Invalid Request; start_time is not valid')
            with self.assertRaises(ApiError):
                post_release(store, api, '99', release(6, 'v-nightly.6', '2026-10-06T00:00:00Z'), sleep=lambda _: None)
            self.assertEqual(api.created, [])
            self.assertEqual(store.data['releases']['6']['tweet_ids'], [])

    def test_dry_run_prints_exact_notes_without_posting_or_writing_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._legacy_db(root / 'state.sqlite3')
            store = Store(root / 'posted.json')
            api = FakeX()
            github = FakeGitHub([
                release(3, 'v-nightly.backlog', '2026-10-03T00:00:00Z', 'old notes that must not post\n'),
                release(5, 'v-nightly.newest', '2026-10-05T00:00:00Z', '## What\'s Changed\n* fix by @a in https://github.com/pingdotgg/t3code/pull/9\n'),
            ])
            with mock.patch('bot.write_summary'):
                code = execute(store, github, dry_run=True, client=api, sleep=lambda _: None)
            self.assertEqual(code, 0)
            self.assertFalse(api.verified)
            self.assertEqual(api.created, [])
            self.assertFalse((root / 'posted.json').exists())

    def test_execute_posts_pending_body_as_is(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._legacy_db(root / 'state.sqlite3')
            store = Store(root / 'posted.json')
            api = FakeX()
            body = '## What\'s Changed\n* fix by @a in https://github.com/pingdotgg/t3code/pull/9\n'
            github = FakeGitHub([
                release(3, 'v-nightly.backlog', '2026-10-03T00:00:00Z', 'old notes\n'),
                release(5, 'v-nightly.newest', '2026-10-05T00:00:00Z', body),
                release(6, 'v-nightly.after', '2026-10-06T00:00:00Z', 'later nightly\n'),
            ])
            with mock.patch('bot.write_summary'):
                code = execute(store, github, dry_run=False, client=api, sleep=lambda _: None)
            self.assertEqual(code, 0)
            texts = [text for text, _ in api.created]
            self.assertEqual(texts, [
                'T3Code nightly v-nightly.newest\n\n## What\'s Changed\n* fix',
                'T3Code nightly v-nightly.after\n\nlater nightly',
            ])
            self.assertNotIn('http', texts[0])
            self.assertNotIn('old notes', texts)
            self.assertEqual(store.data['releases']['3']['status'], 'skipped')
            self.assertEqual(store.data['releases']['5']['status'], 'posted')
            self.assertTrue((root / 'posted.json').exists())

    def test_create_refuses_over_limit_before_http(self):
        calls = []

        def http(*args, **kwargs):
            calls.append(args)
            return {'data': {'id': '1', 'username': ACCOUNT}}

        client = XClient({
            'X_API_KEY': 'api-key-value',
            'X_API_KEY_SECRET': 'api-secret-value',
            'X_ACCESS_TOKEN': 'access-token-value',
            'X_ACCESS_TOKEN_SECRET': 'access-secret-value',
        }, http=http)
        with mock.patch.dict(os.environ, {'X_MAX_POST_CHARS': ''}):
            with self.assertRaisesRegex(RuntimeError, 'not truncated'):
                client.create('a' * (DEFAULT_LIMIT + 1), None)
        self.assertEqual(calls, [])
        with mock.patch.dict(os.environ, {'X_MAX_POST_CHARS': '280'}):
            with self.assertRaisesRegex(RuntimeError, 'not truncated'):
                client.create('a' * 281, None)
            self.assertEqual(calls, [])
            self.assertEqual(client.create('a' * 280, None), '1')
        self.assertEqual(len(calls), 1)

    def test_verify_refuses_unexpected_account(self):
        def http(method, url, headers, data=None, secret_values=(), timeout=45):
            self.assertEqual(method, 'GET')
            self.assertTrue(url.endswith('/users/me'))
            self.assertTrue(headers['Authorization'].startswith('OAuth '))
            self.assertNotIn('api-secret-value', headers['Authorization'])
            return {'data': {'id': '5', 'username': 'someone_else'}}

        client = XClient({
            'X_API_KEY': 'api-key-value',
            'X_API_KEY_SECRET': 'api-secret-value',
            'X_ACCESS_TOKEN': 'access-token-value',
            'X_ACCESS_TOKEN_SECRET': 'access-secret-value',
        }, http=http)
        with self.assertRaisesRegex(RuntimeError, 'someone_else'):
            client.verify()

    def test_create_posts_json_reply_to_previous_tweet(self):
        seen = {}

        def http(method, url, headers, data=None, secret_values=(), timeout=45):
            seen['method'] = method
            seen['url'] = url
            seen['body'] = json.loads(data.decode())
            self.assertTrue(headers['Authorization'].startswith('OAuth '))
            self.assertNotIn('start_time', url)
            return {'data': {'id': '55'}}

        client = XClient({
            'X_API_KEY': 'api-key-value',
            'X_API_KEY_SECRET': 'api-secret-value',
            'X_ACCESS_TOKEN': 'access-token-value',
            'X_ACCESS_TOKEN_SECRET': 'access-secret-value',
        }, http=http)
        self.assertEqual(client.create('hello nightly', '9'), '55')
        self.assertEqual(seen['method'], 'POST')
        self.assertEqual(seen['url'], 'https://api.x.com/2/tweets')
        self.assertEqual(seen['body'], {'text': 'hello nightly', 'reply': {'in_reply_to_tweet_id': '9'}})

    def test_lookup_does_not_send_start_time(self):
        seen = {}

        def http(method, url, headers, data=None, secret_values=(), timeout=45):
            seen['url'] = url
            return {'data': [], 'meta': {}}

        client = XClient({
            'X_API_KEY': 'api-key-value',
            'X_API_KEY_SECRET': 'api-secret-value',
            'X_ACCESS_TOKEN': 'access-token-value',
            'X_ACCESS_TOKEN_SECRET': 'access-secret-value',
        }, http=http)
        self.assertIsNone(client.find_post('99', 'hello', None))
        self.assertNotIn('start_time', seen['url'])
        self.assertIn('/users/99/tweets', seen['url'])

    def test_api_error_from_post_is_loud(self):
        def http(method, url, headers, data=None, secret_values=(), timeout=45):
            raise ApiError(403, 'X HTTP 403: Forbidden; Your credentials do not allow access to this resource')

        client = XClient({
            'X_API_KEY': 'api-key-value',
            'X_API_KEY_SECRET': 'api-secret-value',
            'X_ACCESS_TOKEN': 'access-token-value',
            'X_ACCESS_TOKEN_SECRET': 'access-secret-value',
        }, http=http)
        with self.assertRaisesRegex(ApiError, 'Your credentials do not allow access'):
            client.create('hello', None)

    def test_duplicate_rejection_reuses_the_existing_post(self):
        class DuplicateX(FakeX):
            def create(self, text, parent):
                raise ApiError(403, 'X HTTP 403: Forbidden; You are not allowed to create a Tweet with duplicate content.')

            def find_post(self, user_id, text, parent):
                return '88'

        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'posted.json')
            store.data = {'outage_settled': True, 'releases': {}}
            post_release(
                store, DuplicateX(), '99',
                release(5, 'v-nightly.5', '2026-10-05T00:00:00Z', 'hello nightly\n'),
                sleep=lambda _: None,
            )
            self.assertEqual(store.data['releases']['5']['status'], 'posted')
            self.assertEqual(store.data['releases']['5']['tweet_ids'], ['88'])

    def test_dry_run_renderer_has_no_url(self):
        report = render_dry_run([release(1, 'v-nightly.1', '2026-10-05T00:00:00Z', 'hello nightly\n')])
        self.assertIn('DRY RUN', report)
        self.assertIn('hello nightly', report)
        self.assertIn('No post URL', report)
        self.assertNotIn('https://x.com/t3codes/status/', report)

    def test_dry_run_renderer_strips_links_from_notes(self):
        body = (
            '## What\'s Changed\n'
            '* fix by @a in https://github.com/pingdotgg/t3code/pull/9\n'
            '**Full Changelog**: https://github.com/pingdotgg/t3code/compare/a...b\n'
        )
        report = render_dry_run([release(1, 'v-nightly.1', '2026-10-05T00:00:00Z', body)])
        self.assertIn('* fix', report)
        self.assertNotIn('@a', report)
        self.assertNotIn('http', report)
        self.assertNotIn('github.com', report)
        self.assertNotIn('Full Changelog', report)
        self.assertIn('1 post(s)', report)

    def test_create_strips_urls_before_the_request(self):
        seen = {}

        def http(method, url, headers, data=None, secret_values=(), timeout=45):
            seen['body'] = json.loads(data.decode())
            return {'data': {'id': '55'}}

        client = XClient({
            'X_API_KEY': 'api-key-value',
            'X_API_KEY_SECRET': 'api-secret-value',
            'X_ACCESS_TOKEN': 'access-token-value',
            'X_ACCESS_TOKEN_SECRET': 'access-secret-value',
        }, http=http)
        self.assertEqual(client.create('ship https://example.com/a today', None), '55')
        self.assertEqual(seen['body'], {'text': 'ship today'})

    def test_create_does_not_post_when_a_url_survives(self):
        calls = []

        def http(method, url, headers, data=None, secret_values=(), timeout=45):
            calls.append(url)
            return {'data': {'id': '1'}}

        client = XClient({
            'X_API_KEY': 'api-key-value',
            'X_API_KEY_SECRET': 'api-secret-value',
            'X_ACCESS_TOKEN': 'access-token-value',
            'X_ACCESS_TOKEN_SECRET': 'access-secret-value',
        }, http=http)
        with mock.patch(
            'bot.ensure_plain',
            side_effect=RuntimeError('Refusing to post: a URL survived link removal. Nothing was posted.'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'survived link removal'):
                client.create('https://example.com', None)
        self.assertEqual(calls, [])

    def test_resume_strips_a_stored_url_before_sending(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'posted.json')
            store.data = {'outage_settled': True, 'releases': {
                '5': {
                    'tag': 'v-nightly.5',
                    'published': '2026-10-05T00:00:00Z',
                    'status': 'in_progress',
                    'chunks': ['see https://example.com/x today'],
                    'tweet_ids': [],
                    'attempted_index': None,
                },
            }}
            api = FakeX()
            post_release(store, api, '99', release(5, 'v-nightly.5', '2026-10-05T00:00:00Z'), sleep=lambda _: None)
            self.assertEqual(api.created, [('see today', None)])
            self.assertEqual(store.data['releases']['5']['status'], 'posted')

    def test_retired_vault_cannot_mint_a_token(self):
        with self.assertRaisesRegex(RuntimeError, 'OAuth 1.0a'):
            cloud_auth.access_token()

    def test_configurable_limit_splits_a_nightly_on_lines(self):
        line = 'ship the nightly fix\n'
        body = line * 6
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'posted.json')
            store.data = {'outage_settled': True, 'releases': {}}
            api = FakeX()
            with mock.patch.dict(os.environ, {'X_MAX_POST_CHARS': '40'}):
                post_release(
                    store, api, '99',
                    release(5, 'v-nightly.5', '2026-10-05T00:00:00Z', body),
                    sleep=lambda _: None,
                )
            texts = [text for text, _ in api.created]
            self.assertGreater(len(texts), 1)
            self.assertEqual(''.join(texts), nightly_text({'tag_name': 'v-nightly.5', 'body': body}))
            self.assertTrue(all(weight(text) <= 40 for text in texts))
            self.assertTrue(all(text.endswith('\n') for text in texts[:-1]))
            self.assertIsNone(api.created[0][1])
            self.assertEqual(api.created[1][1], '1')

    def test_long_post_rejection_falls_back_to_280_thread(self):
        full_body = '{"detail":"You are not permitted to create a long Post.","tail":"' + ('Q' * 1200) + '"}'

        class RejectLong(FakeX):
            def create(self, text, parent):
                if weight(text) > SHORT_LIMIT:
                    raise ApiError(
                        403,
                        'X HTTP 403: Forbidden; You are not permitted to create a long Post.',
                        body=full_body,
                    )
                return super().create(text, parent)

        notes = '* change by @a\n' * 40
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'posted.json')
            store.data = {'outage_settled': True, 'releases': {}}
            api = RejectLong()
            item = release(5, 'v-nightly.5', '2026-10-05T00:00:00Z', notes)
            with mock.patch.dict(os.environ, {'X_MAX_POST_CHARS': ''}):
                with self.assertLogs('release-bot', level='ERROR') as logs:
                    post_release(store, api, '99', item, sleep=lambda _: None)
            logged = '\n'.join(logs.output)
            self.assertIn(full_body, logged)
            self.assertIn('HTTP 403', logged)
            texts = [text for text, _ in api.created]
            self.assertGreater(len(texts), 1)
            self.assertTrue(all(weight(text) <= SHORT_LIMIT for text in texts))
            self.assertEqual(''.join(texts), nightly_text(item))
            self.assertIsNone(api.created[0][1])
            self.assertEqual(api.created[1][1], '1')
            self.assertEqual(store.data['releases']['5']['status'], 'posted')
            self.assertTrue(store.data['releases']['5'].get('long_post_fallback'))
            self.assertGreater(len(store.data['releases']['5']['tweet_ids']), 1)

    def test_long_post_rejection_keeps_a_partial_thread(self):
        class RejectLong(FakeX):
            def create(self, text, parent):
                raise ApiError(400, 'X HTTP 400: Invalid Request; Text is too long.', body='{"detail":"Text is too long."}')

        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'posted.json')
            chunks = ['a' * 400, 'b' * 400]
            store.data = {'outage_settled': True, 'releases': {
                '5': {
                    'tag': 'v-nightly.5',
                    'published': '2026-10-05T00:00:00Z',
                    'status': 'in_progress',
                    'chunks': list(chunks),
                    'tweet_ids': ['10'],
                    'attempted_index': None,
                },
            }}
            api = RejectLong()
            with self.assertRaises(ApiError):
                post_release(store, api, '99', release(5, 'v-nightly.5', '2026-10-05T00:00:00Z'), sleep=lambda _: None)
            row = store.data['releases']['5']
            self.assertEqual(row['tweet_ids'], ['10'])
            self.assertEqual(row['chunks'], chunks)
            self.assertEqual(row['status'], 'in_progress')
            self.assertFalse(row.get('long_post_fallback'))
            self.assertEqual(api.created, [])
            self.assertEqual(row['attempted_index'], 1)

    def test_non_length_error_does_not_fall_back(self):
        self.assertFalse(long_post_rejected(ApiError(403, 'X HTTP 403: Forbidden; Your credentials do not allow access')))
        self.assertFalse(long_post_rejected(ApiError(500, 'X HTTP 500: Text is too long.')))
        self.assertTrue(long_post_rejected(ApiError(400, 'X HTTP 400: Tweet needs to be a bit shorter.')))
        self.assertTrue(long_post_rejected(ApiError(403, 'X HTTP 403: Forbidden', body='{"errors":[{"code":186,"message":"nope"}]}')))

        class AuthFail(FakeX):
            def create(self, text, parent):
                raise ApiError(403, 'X HTTP 403: Forbidden; Your credentials do not allow access to this resource')

        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'posted.json')
            store.data = {'outage_settled': True, 'releases': {}}
            notes = '* change by @a\n' * 40
            with mock.patch.dict(os.environ, {'X_MAX_POST_CHARS': ''}):
                with self.assertRaisesRegex(ApiError, 'credentials do not allow'):
                    post_release(
                        store, AuthFail(), '99',
                        release(5, 'v-nightly.5', '2026-10-05T00:00:00Z', notes),
                        sleep=lambda _: None,
                    )
            row = store.data['releases']['5']
            self.assertEqual(row['status'], 'in_progress')
            self.assertEqual(row['tweet_ids'], [])
            self.assertFalse(row.get('long_post_fallback'))
            self.assertEqual(len(row['chunks']), 1)
            self.assertGreater(weight(row['chunks'][0]), SHORT_LIMIT)

    def test_http_error_keeps_the_full_body(self):
        raw = json.dumps({'detail': 'Text is too long.', 'pad': 'Q' * 2000}).encode()
        error = urllib.error.HTTPError(
            'https://api.x.com/2/tweets', 403, 'Forbidden', {}, io.BytesIO(raw),
        )
        with mock.patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(ApiError) as caught:
                http_json('POST', 'https://api.x.com/2/tweets', {})
        self.assertIn('Text is too long', str(caught.exception))
        self.assertIn('Q' * 2000, caught.exception.body)
        self.assertEqual(caught.exception.status, 403)

    def _legacy_db(self, path):
        connection = sqlite3.connect(path)
        connection.executescript('''
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE releases (
                id INTEGER PRIMARY KEY, tag TEXT NOT NULL, published TEXT NOT NULL,
                plan TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE posts (
                release_id INTEGER NOT NULL, position INTEGER NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', tweet_id TEXT, attempted TEXT);
        ''')
        connection.execute("INSERT INTO meta VALUES ('baseline', '2026-09-19T00:00:00Z')")
        connection.execute(
            "INSERT INTO releases VALUES (1, 'old-posted', '2026-10-01T00:00:00Z', '{}', 1)"
        )
        connection.execute(
            "INSERT INTO posts VALUES (1, 0, 'already live', 'posted', '100', NULL)"
        )
        connection.execute(
            "INSERT INTO releases VALUES (3, 'v-nightly.backlog', '2026-10-03T00:00:00Z', '{}', 0)"
        )
        connection.execute(
            "INSERT INTO posts VALUES (3, 0, 'queued only', 'pending', NULL, NULL)"
        )
        connection.execute(
            "INSERT INTO releases VALUES (5, 'v-nightly.newest', '2026-10-05T00:00:00Z', '{}', 0)"
        )
        connection.execute(
            "INSERT INTO posts VALUES (5, 0, 'queued newest', 'pending', NULL, NULL)"
        )
        connection.commit()
        connection.close()


def fake_anthropic(text='', stop_reason='end_turn', error=None):
    """A stand-in for the anthropic package that records the request."""
    module = types.ModuleType('anthropic')
    module.APIError = type('APIError', (Exception,), {})
    module.requests = []

    def create(**kwargs):
        module.requests.append(kwargs)
        if error:
            raise module.APIError(error)
        block = types.SimpleNamespace(type='text', text=text)
        return types.SimpleNamespace(stop_reason=stop_reason, content=[block])

    messages = types.SimpleNamespace(create=create)
    module.Anthropic = lambda api_key: types.SimpleNamespace(beta=types.SimpleNamespace(messages=messages))
    return module


class SummaryTests(unittest.TestCase):
    notes = '## What\'s Changed\n* feat(server): link to another environment by @a in https://github.com/pingdotgg/t3code/pull/1\n'
    env = {'ANTHROPIC_API_KEY': 'test-key'}

    def test_title_then_summary_then_notes(self):
        item = {'tag_name': 'v0.1.4-nightly.20261027.3796', 'body': self.notes}
        with mock.patch('bot.summarize', return_value='Link T3 Code to another machine.'):
            self.assertEqual(
                nightly_text(item),
                'T3Code nightly v0.1.4-nightly.20261027.3796\n\n'
                'Link T3 Code to another machine.\n\n## What\'s Changed\n* feat(server): link to another environment',
            )
        with mock.patch('bot.summarize', return_value=None):
            self.assertEqual(
                nightly_text(item),
                'T3Code nightly v0.1.4-nightly.20261027.3796\n\n' + release_text(item),
            )

    def test_title_keeps_the_version_and_has_no_link(self):
        item = {'tag_name': 'v0.1.4-nightly.20261027.3796', 'body': self.notes}
        with mock.patch('bot.summarize', return_value=None):
            text = nightly_text(item)
        self.assertTrue(text.startswith('T3Code nightly v0.1.4-nightly.20261027.3796\n'))
        self.assertFalse(has_detectable_url(text))

    def test_summarize_sends_the_notes_and_returns_one_line(self):
        module = fake_anthropic('Link T3 Code to another\nmachine.')
        with mock.patch.dict(sys.modules, {'anthropic': module}):
            self.assertEqual(summarize('notes', self.env), 'Link T3 Code to another machine.')
        request = module.requests[0]
        self.assertEqual(request['model'], 'claude-opus-5-5')
        self.assertEqual(request['messages'], [{'role': 'user', 'content': 'notes'}])
        self.assertEqual(request['fallbacks'], 'default')

    def test_summarize_returns_none_instead_of_blocking_the_post(self):
        self.assertIsNone(summarize('notes', {}))
        cases = [
            fake_anthropic(error='overloaded'),
            fake_anthropic('Cut short', stop_reason='max_tokens'),
            fake_anthropic(''),
            fake_anthropic('x' * 201),
            fake_anthropic('Thanks to @juliusmarminge for linked environments.'),
        ]
        for module in cases:
            with mock.patch.dict(sys.modules, {'anthropic': module}):
                self.assertIsNone(summarize('notes', self.env))

    def test_summarize_strips_links_and_keeps_at_dash_mention(self):
        module = fake_anthropic('Adds @-mention for machines, see https://t3.codes now.')
        with mock.patch.dict(sys.modules, {'anthropic': module}):
            self.assertEqual(summarize('notes', self.env), 'Adds @-mention for machines, see now.')


if __name__ == '__main__':
    unittest.main()
