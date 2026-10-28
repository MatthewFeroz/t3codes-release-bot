import unittest
from unittest import mock

from bot import split_thread, weight
from xurls import ensure_plain, has_detectable_url, strip_links, url_spans


# twitter-text 3.1.0 extractUrls results. Version strings and ordinary file
# names are absent. Bare domains and names X would link are present.
EXTRACTION = {
    'v0.1.3': [],
    'v0.1.3-nightly.20261026.3726': [],
    'package.json': [],
    'package.js': [],
    'file.txt': [],
    'archive.tar.gz': [],
    'main.go': [],
    'styles.css': [],
    'index.html': [],
    'image.png': [],
    'tsconfig.json': [],
    'file.config.js': [],
    'config.yaml': [],
    'foo.baz': [],
    '1.2.3': [],
    '0.1.3': [],
    'user@example.com': [],
    'john.doe.jp@mail.com': [],
    'src/bot.py': [],
    'http://no-tld': [],
    'long.test.tar.bz2': [],
    'end of sentence. Next sentence': [],
    'See v0.1.3 in package.json': [],
    'example.com': ['example.com'],
    'github.com/foo': ['github.com/foo'],
    'github.com/pingdotgg/t3code/pull/123': ['github.com/pingdotgg/t3code/pull/123'],
    'www.example.com/path': ['www.example.com/path'],
    'http://example.com/a': ['http://example.com/a'],
    'https://example.com/a': ['https://example.com/a'],
    'README.md': ['README.md'],
    'bot.py': ['bot.py'],
    'script.py': ['script.py'],
    'app.rs': ['app.rs'],
    'notes.md': ['notes.md'],
    'run.sh': ['run.sh'],
    'lib.rs': ['lib.rs'],
    'something.io': ['something.io'],
    'foo.ai': ['foo.ai'],
    'foo.dev': ['foo.dev'],
    'foo.app': ['foo.app'],
    'go.dev': ['go.dev'],
    'x.com': ['x.com'],
    'foo.bar': ['foo.bar'],
    'foo.bar/baz': ['foo.bar/baz'],
    't.co/abc': ['t.co/abc'],
    'bit.ly/abcde': ['bit.ly/abcde'],
    'foo.com.': ['foo.com'],
    'Visit example.com.': ['example.com'],
}


NOTES = """## What's Changed
* fix parser v0.1.3 by @a in https://github.com/pingdotgg/t3code/pull/123
* keep package.json and src/bot.py
* notes in README.md
* bare example.com and github.com/foo
* Read the [docs](https://example.com/docs) please
* Go to <https://example.com/auto>
* weird http://no-tld end

**Full Changelog**: https://github.com/pingdotgg/t3code/compare/v0.1.2...v0.1.3-nightly.20261026.3726
"""

EXPECTED = """## What's Changed
* fix parser v0.1.3 by @a
* keep package.json and src/bot.py
* notes
* bare and
* Read the docs please
* Go to
* weird end"""


class UrlStripTests(unittest.TestCase):
    def test_extraction_matches_twitter_text(self):
        for text, urls in EXTRACTION.items():
            with self.subTest(text=text):
                self.assertEqual([url for _start, _end, url in url_spans(text)], urls)

    def test_markdown_links_keep_their_label(self):
        self.assertEqual(strip_links('See [the docs](https://example.com/docs) please.'), 'See the docs please.')
        self.assertEqual(strip_links('[site](www.example.com)'), 'site')
        self.assertEqual(strip_links('[name](package.json)'), '[name](package.json)')
        self.assertEqual(strip_links('![shot](https://example.com/a.png)'), 'shot')

    def test_bare_urls_and_autolinks_are_removed(self):
        self.assertEqual(strip_links('Go to https://example.com/a and www.example.com/b.'), 'Go to and.')
        self.assertEqual(strip_links('<https://example.com/a>'), '')
        self.assertEqual(strip_links('by @user in <https://example.com/a>'), 'by @user')
        self.assertEqual(strip_links('weird http://no-tld end'), 'weird end')
        self.assertEqual(strip_links('* https://example.com\n* real item\n'), '* real item')

    def test_pr_and_commit_attribution_drops_the_dangling_in(self):
        self.assertEqual(
            strip_links('by @user in https://github.com/pingdotgg/t3code/pull/123'),
            'by @user',
        )
        self.assertEqual(
            strip_links('by @user in https://github.com/pingdotgg/t3code/commit/abcdef0'),
            'by @user',
        )
        self.assertEqual(
            strip_links('fixed in prod by @user in https://github.com/acme/app/pull/9'),
            'fixed in prod by @user',
        )

    def test_full_changelog_line_is_dropped(self):
        body = (
            'Intro\n\n'
            '**Full Changelog**: https://github.com/pingdotgg/t3code/compare/'
            'v0.1.3-nightly.20261026.3717...v0.1.3-nightly.20261026.3726\n'
        )
        self.assertEqual(strip_links(body), 'Intro')
        self.assertEqual(
            strip_links('* document the full changelog format by @a'),
            '* document the full changelog format by @a',
        )

    def test_bare_domains_are_removed_and_versions_and_filenames_stay(self):
        self.assertEqual(strip_links('example.com and github.com/foo'), 'and')
        self.assertEqual(strip_links('README.md and bot.py'), 'and')
        self.assertEqual(strip_links('notes in README.md'), 'notes')
        self.assertEqual(
            strip_links('Ship v0.1.3 in package.json and src/bot.py.'),
            'Ship v0.1.3 in package.json and src/bot.py.',
        )
        self.assertEqual(
            strip_links('See v0.1.3-nightly.20261026.3726 and package.js and file.txt.'),
            'See v0.1.3-nightly.20261026.3726 and package.js and file.txt.',
        )
        self.assertEqual(strip_links('user@example.com stays'), 'user@example.com stays')
        self.assertEqual(strip_links('* mirror example.com and github.com/foo for v0.1.3\n'), '* mirror and for v0.1.3')

    def test_release_notes_fixture(self):
        cleaned = strip_links(NOTES)
        self.assertEqual(cleaned, EXPECTED)
        self.assertNotIn('http', cleaned)
        self.assertNotIn('github.com', cleaned)
        self.assertNotIn('Full Changelog', cleaned)
        self.assertIn('v0.1.3', cleaned)
        self.assertIn('package.json', cleaned)
        self.assertIn('src/bot.py', cleaned)
        self.assertIn('@a', cleaned)
        self.assertFalse(has_detectable_url(cleaned))
        self.assertEqual(strip_links(cleaned), cleaned)

    def test_thread_pieces_have_no_detectable_url(self):
        text = strip_links(NOTES + ('\n* another change by @b' * 40))
        chunks = split_thread(text, limit=280)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(weight(chunk), 280)
            self.assertFalse(has_detectable_url(chunk))
            self.assertEqual(ensure_plain(chunk), chunk)

    def test_ensure_plain_refuses_a_url_that_still_detects(self):
        with mock.patch('xurls.has_detectable_url', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'survived link removal'):
                ensure_plain('hello nightly')


if __name__ == '__main__':
    unittest.main()
