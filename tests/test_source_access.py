import json
import unittest
from pathlib import Path
from unittest.mock import patch
import collectors
import monitor


class PublicSourceAccess(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / 'fixtures'
        self.sources = json.loads(Path(monitor.SOURCES_FILE).read_text())['sources']

    def source(self, name):
        return next(s for s in self.sources if s['name'] == name)

    def test_challenge_is_blocked_for_html_and_json_inventory(self):
        html = (self.root / 'siteground_challenge.html').read_text()
        for name in ('Home 4 Everyone', 'Smiley Real Estate'):
            source = self.source(name)
            with self.subTest(source=name), self.assertRaises(collectors.AccessBlocked):
                collectors.parse_page(html, source['url'], source, monitor)

    def test_challenge_does_not_follow_captcha_or_report_empty_inventory(self):
        html = (self.root / 'siteground_challenge.html').read_text()
        for name in ('Home 4 Everyone', 'Smiley Real Estate'):
            source = self.source(name)
            with self.subTest(source=name), patch.object(monitor, 'get_page', return_value=(html, source['url'])) as get:
                items, observations, health = collectors.scrape(source, 'deep', monitor)
            self.assertEqual(get.call_count, 1)
            self.assertEqual((items, observations), ([], []))
            self.assertEqual(health['status'], 'blocked')
            self.assertEqual(health['pages_fetched'], 0)

    def test_successful_inventories_still_parse(self):
        for name, filename, count in [('Home 4 Everyone', 'home_sale_coverage.html', 5), ('Smiley Real Estate', 'smiley_api.json', 4)]:
            source = self.source(name)
            with self.subTest(source=name):
                items, observations, cards, _ = collectors.parse_page((self.root / filename).read_text(), source['url'], source, monitor)
                self.assertEqual(cards, count)
                self.assertEqual(len(observations), count)
                self.assertEqual(len(items), 1)

    def test_intermittent_blocks_recheck_without_exponential_day_delay(self):
        for name in ('Home 4 Everyone', 'Smiley Real Estate'):
            for failures in (1, 6, 20):
                self.assertEqual(collectors.retry_delay_minutes(self.source(name), 'blocked', failures), 15)

    def test_persistent_blocks_and_certificate_errors_keep_backoff(self):
        self.assertEqual(collectors.retry_delay_minutes(self.source('RE/MAX Aruba'), 'blocked', 6), 1440)
        self.assertEqual(collectors.retry_delay_minutes(self.source('Aruba Happy Realty'), 'ssl_error', 6), 1440)
        self.assertEqual(collectors.retry_delay_minutes(self.source('Smiley Real Estate'), 'parser_failed', 2), 120)


if __name__ == '__main__':
    unittest.main()
