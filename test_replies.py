import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.parse

from bot import ApiError, weight
from xurls import has_detectable_url
import replies
from replies import REPLIES, ReplyStore, evaluate, execute, problem_phrase, summoned

NOW = dt.datetime(2026, 10, 27, 22, 0, tzinfo=dt.timezone.utc)
ME = '99'


def stamp(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat().replace('+00:00', 'Z')


def mention(tweet_id, text, author='5', hours_ago=1, conversation=None, lang='en', mentions=('t3codes',), refs=()):
    return {
        'id': str(tweet_id),
        'text': text,
        'author_id': author,
        'conversation_id': conversation or str(tweet_id),
        'created_at': stamp(hours_ago),
        'lang': lang,
        'entities': {'mentions': [{'username': name} for name in mentions]},
        'referenced_tweets': list(refs),
    }


class FakeX:
    def __init__(self, pages=(), users=None, quoted=None):
        self.pages = list(pages)
        self.users = users or {'5': 'alice', '6': 'bob', '7': 'carol', '8': 'theo'}
        self.quoted = quoted or {}
        self.created = []
        self.found = {}
        self.fail = []
        self.urls = []

    def verify(self):
        return ME

    def call(self, method, url, payload=None):
        self.urls.append(url)
        data = self.pages.pop(0) if self.pages else []
        return {
            'data': data,
            'includes': {
                'users': [{'id': key, 'username': value} for key, value in self.users.items()],
                'tweets': [{'id': key, 'author_id': value} for key, value in self.quoted.items()],
            },
            'meta': {},
        }

    def find_post(self, user_id, text, parent):
        return self.found.get((text, parent))

    def create(self, text, parent):
        if self.fail:
            raise self.fail.pop(0)
        self.created.append((text, parent))
        return str(1000 + len(self.created))


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'replies.json'

    def tearDown(self):
        self.dir.cleanup()

    def store(self):
        return ReplyStore(self.path)

    def run_mode(self, mode, pages, store=None, **kwargs):
        client = FakeX(pages, **{key: kwargs.pop(key) for key in ('users', 'quoted') if key in kwargs})
        store = store or self.store()
        execute(store, client, mode, at=NOW, sleep=lambda _: None, **kwargs)
        return store, client

    def test_reply_texts_are_plain_and_name_the_command(self):
        for text in REPLIES:
            self.assertIn('npx t3 triage', text)
            self.assertLessEqual(weight(text), 280)
            self.assertFalse(has_detectable_url(text), text)
            self.assertNotIn('@', text)
        self.assertEqual(len(set(REPLIES)), len(REPLIES))

    def test_problem_phrases_match_breakage_reports(self):
        for text in (
            '@t3codes why is this broken',
            '@t3codes the nightly broke my sessions',
            '@t3codes t3 code won’t start after the update',
            '@theo @t3codes it keeps crashing on launch',
            '@t3codes is not working for me today',
            '@t3codes stuck on a blank screen',
        ):
            self.assertIsNotNone(problem_phrase(text), text)

    def test_problem_phrases_skip_praise_handles_links_and_fixed(self):
        for text in (
            '@t3codes love the new nightly',
            '@brokenbot @t3codes thoughts?',
            '@t3codes https://t.co/broken123',
            '@t3codes it was broken but works now',
            '@t3codes fixed after restarting, thanks',
            '@t3codes ran npx t3 triage and it found the bug',
        ):
            self.assertIsNone(problem_phrase(text), text)

    def test_summoned_requires_a_mention_or_a_quote_of_t3codes(self):
        self.assertTrue(summoned(mention(1, 'x'), ME, {}))
        self.assertFalse(summoned(mention(1, 'x', mentions=('theo',)), ME, {}))
        quote = mention(1, 'x', mentions=(), refs=[{'type': 'quoted', 'id': '50'}])
        self.assertTrue(summoned(quote, ME, {'50': ME}))
        self.assertFalse(summoned(quote, ME, {'50': '123'}))

    def test_evaluate_skips_unsafe_targets(self):
        state = {'mentions': {}}
        cases = {
            'ignored-author': (mention(1, '@t3codes broken', author='8'), {'username': 'theo'}),
            'too-old': (mention(2, '@t3codes broken', hours_ago=30), {'username': 'alice'}),
            'language': (mention(3, '@t3codes broken', lang='ja'), {'username': 'alice'}),
            'not-summoned': (mention(4, '@theo broken', mentions=('theo',)), {'username': 'alice'}),
            'retweet': (mention(5, 'RT @x: @t3codes broken', refs=[{'type': 'retweeted', 'id': '9'}]),
                        {'username': 'alice'}),
            'no-problem-phrase': (mention(6, '@t3codes nice release'), {'username': 'alice'}),
        }
        for expected, (tweet, author) in cases.items():
            phrase, reason = evaluate(tweet, author, ME, {}, state, NOW, set())
            self.assertIsNone(phrase)
            self.assertEqual(reason, expected)

    def test_dry_run_reads_mentions_and_writes_and_sends_nothing(self):
        pages = [[mention(11, '@t3codes why is this broken'), mention(12, '@t3codes nice')]]
        store, client = self.run_mode('dry-run', pages)
        self.assertEqual(client.created, [])
        self.assertFalse(self.path.exists())
        self.assertIn('/users/99/mentions?', client.urls[0])

    def test_queue_records_matches_without_tweet_text_and_does_not_send(self):
        store, client = self.run_mode('queue', [[mention(11, '@t3codes why is this broken')]])
        self.assertEqual(client.created, [])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['since_id'], '11')
        self.assertEqual(saved['mentions']['11']['status'], 'queued')
        self.assertNotIn('why is this broken', self.path.read_text())

    def test_since_id_is_sent_on_the_next_run(self):
        store, _ = self.run_mode('queue', [[mention(11, '@t3codes broken')]])
        _, client = self.run_mode('queue', [[]], store=self.store())
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(client.urls[0]).query))
        self.assertEqual(query['since_id'], '11')

    def test_approve_sends_one_reply_to_the_queued_mention(self):
        self.run_mode('queue', [[mention(11, '@t3codes broken')]])
        store, client = self.run_mode('queue', [[]], store=self.store(), approve=['11'])
        self.assertEqual(client.created, [(REPLIES[0], '11')])
        row = store.data['mentions']['11']
        self.assertEqual((row['status'], row['reply_id']), ('replied', '1001'))
        _, again = self.run_mode('queue', [[]], store=self.store(), approve=[])
        self.assertEqual(again.created, [])

    def test_approve_refuses_unknown_ids(self):
        with self.assertRaises(RuntimeError):
            self.run_mode('queue', [[]], approve=['404'])

    def test_reject_marks_the_mention_and_it_is_never_sent(self):
        self.run_mode('queue', [[mention(11, '@t3codes broken')]])
        store, client = self.run_mode('auto', [[]], store=self.store(), reject=['11'])
        self.assertEqual(client.created, [])
        self.assertEqual(store.data['mentions']['11']['status'], 'rejected')

    def test_auto_respects_per_run_cap_and_one_reply_per_author(self):
        pages = [[
            mention(11, '@t3codes broken', author='5'),
            mention(12, '@t3codes still broken', author='5'),
            mention(13, '@t3codes crashes', author='6'),
            mention(14, '@t3codes not working', author='7'),
            mention(15, '@t3codes stuck', author='9'),
        ]]
        store, client = self.run_mode('auto', pages, users={'5': 'a', '6': 'b', '7': 'c', '9': 'd'})
        self.assertEqual([parent for _, parent in client.created], ['11', '13', '14'])
        self.assertEqual(store.data['mentions']['15']['status'], 'queued')
        self.assertNotIn('12', store.data['mentions'])
        self.assertEqual(len({text for text, _ in client.created}), 3)

    def test_auto_respects_daily_cap(self):
        store = self.store()
        for index in range(replies.MAX_PER_DAY):
            store.data['mentions'][str(index + 1)] = {
                'tweet_id': str(index + 1), 'author_id': str(100 + index), 'status': 'replied',
                'created_at': stamp(2), 'replied_at': stamp(2),
            }
        _, client = self.run_mode('auto', [[mention(500, '@t3codes broken')]], store=store)
        self.assertEqual(client.created, [])
        self.assertEqual(store.data['mentions']['500']['status'], 'queued')

    def test_author_cooldown_spans_runs(self):
        self.run_mode('auto', [[mention(11, '@t3codes broken', author='5')]])
        store, client = self.run_mode('auto', [[mention(20, '@t3codes broken again', author='5')]],
                                      store=self.store())
        self.assertEqual(client.created, [])
        self.assertNotIn('20', store.data['mentions'])

    def test_second_mention_in_an_answered_conversation_is_skipped(self):
        self.run_mode('auto', [[mention(11, '@t3codes broken', author='5', conversation='11')]])
        store, client = self.run_mode('auto', [[mention(12, '@t3codes same', author='6', conversation='11')]],
                                      store=self.store())
        self.assertNotIn('12', store.data['mentions'])

    def test_duplicate_rejection_tries_the_next_wording(self):
        self.run_mode('queue', [[mention(11, '@t3codes broken')]])
        client = FakeX([[]])
        client.fail = [ApiError(403, 'X HTTP 403: You are not allowed to create a Tweet with duplicate content.')]
        store = self.store()
        execute(store, client, 'queue', approve=['11'], at=NOW, sleep=lambda _: None)
        self.assertEqual(client.created, [(REPLIES[1], '11')])
        self.assertEqual(store.data['mentions']['11']['status'], 'replied')

    def test_x_refusal_is_recorded_and_fails_the_run_loudly(self):
        self.run_mode('queue', [[mention(11, '@t3codes broken')]])
        client = FakeX([[]])
        client.fail = [ApiError(403, 'X HTTP 403: Reply to this conversation is not allowed because you have not been mentioned or otherwise engaged by the author')]
        store = self.store()
        with self.assertRaises(RuntimeError):
            execute(store, client, 'queue', approve=['11'], at=NOW, sleep=lambda _: None)
        row = json.loads(self.path.read_text())['mentions']['11']
        self.assertEqual(row['status'], 'failed')
        self.assertIn('not been mentioned', row['reason'])

    def test_rate_limit_leaves_the_mention_queued(self):
        self.run_mode('queue', [[mention(11, '@t3codes broken')]])
        client = FakeX([[]])
        client.fail = [ApiError(429, 'X HTTP 429: Too Many Requests')]
        with self.assertRaises(ApiError):
            execute(self.store(), client, 'queue', approve=['11'], at=NOW, sleep=lambda _: None)
        self.assertEqual(json.loads(self.path.read_text())['mentions']['11']['status'], 'queued')

    def test_uncertain_send_is_found_instead_of_sent_again(self):
        self.run_mode('queue', [[mention(11, '@t3codes broken')]])
        saved = json.loads(self.path.read_text())
        saved['mentions']['11'].update(status='sending', reply_text=REPLIES[0])
        self.path.write_text(json.dumps(saved))
        client = FakeX([[]])
        client.found[(REPLIES[0], '11')] = '777'
        store = self.store()
        execute(store, client, 'queue', approve=['11'], at=NOW, sleep=lambda _: None)
        self.assertEqual(client.created, [])
        self.assertEqual(store.data['mentions']['11']['reply_id'], '777')

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(RuntimeError):
            execute(self.store(), FakeX(), 'yolo', at=NOW)

    def test_run_outside_actions_refuses_to_reply(self):
        with mock.patch.dict('os.environ', {'REPLY_MODE': 'auto', 'GITHUB_ACTIONS': ''}):
            with self.assertRaises(SystemExit):
                replies.main(['run'])


if __name__ == '__main__':
    unittest.main()
