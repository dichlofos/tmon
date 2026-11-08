from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api_cache import ApiCache
from app import api_call, cache_ttl


class CacheTest(unittest.TestCase):
    def test_persistence_expiry_and_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.sqlite3'
            key = ApiCache.key('one', 'market', 'candles', {'a': 1})
            other = ApiCache.key('two', 'market', 'candles', {'a': 1})
            with patch('api_cache.time.time', return_value=100):
                ApiCache(path).put(key, {'candles': [1]}, 20)
                self.assertEqual(ApiCache(path).get(key), {'candles': [1]})
                self.assertIsNone(ApiCache(path).get(other))
            with patch('api_cache.time.time', return_value=121):
                self.assertIsNone(ApiCache(path).get(key))

    def test_repeat_historical_request_does_not_call_api(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TMON_CACHE_FILE': str(Path(directory) / 'api.sqlite3')}), patch('app.read_token', return_value='test-secret'), patch('app.uncached_api_call', return_value={'candles': []}) as fetch:
            payload = {'to': '2020-01-01T00:00:00Z'}
            self.assertEqual(api_call('MarketDataService', 'GetCandles', payload), {'candles': []})
            self.assertEqual(api_call('MarketDataService', 'GetCandles', payload), {'candles': []})
            self.assertEqual(fetch.call_count, 1)
            self.assertNotIn(b'test-secret', (Path(directory) / 'api.sqlite3').read_bytes())

    def test_current_portfolio_is_never_cached(self):
        self.assertEqual(cache_ttl('OperationsService', 'GetPortfolio', {}), 0)

    def test_failures_are_not_cached(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TMON_CACHE_FILE': str(Path(directory) / 'api.sqlite3')}), patch('app.read_token', return_value='test-secret'), patch('app.uncached_api_call', side_effect=RuntimeError('offline')) as fetch:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    api_call('MarketDataService', 'GetCandles', {'to': '2020-01-01T00:00:00Z'})
            self.assertEqual(fetch.call_count, 2)


class FormSettingsTest(unittest.TestCase):
    def test_saved_form_is_restored_on_get(self):
        from app import app, form_settings
        form = {'account_id': 'test-account', 'date_from': '01.01.2026', 'date_till': '31.01.2026', 'ticker': 'LKOH'}
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TMON_CACHE_FILE': str(Path(directory) / 'api.sqlite3')}), patch('app.read_token', return_value='test-secret'), patch('app.get_accounts', return_value=[{'id': 'test-account', 'name': 'Test'}]):
            form_settings(form)
            self.assertEqual(form_settings(), form)
            html = app.test_client().get('/').get_data(as_text=True)
            self.assertIn('value="LKOH"', html)
            self.assertIn('value="31.01.2026"', html)
            self.assertIn('value="test-account" selected', html)
            with patch('app.read_token', return_value='different-secret'):
                self.assertEqual(form_settings(), {})


class QuoteCacheTest(unittest.TestCase):
    def test_monthly_quotes_reused_without_looking_ahead(self):
        from app import get_closing_price, REPORT_CACHE
        candles = {'candles': [
            {'time': '2025-03-10T10:00:00Z', 'close': {'units': '100'}},
            {'time': '2025-03-20T10:00:00Z', 'close': {'units': '110'}},
        ]}
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'TMON_CACHE_FILE': str(Path(directory) / 'api.sqlite3')}), patch('app.read_token', return_value='test'), patch('app.uncached_api_call', return_value=candles) as fetch:
            marker = REPORT_CACHE.set({})
            try:
                self.assertEqual(get_closing_price('uid', '15.03.2025')[0], Decimal(100))
                self.assertEqual(get_closing_price('uid', '21.03.2025')[0], Decimal(110))
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(fetch.call_args.args[2]['from'], '2025-03-01T00:00:00Z')
            finally:
                REPORT_CACHE.reset(marker)

    def test_fresh_quotes_have_short_ttl_but_positions_do_not(self):
        from datetime import datetime, timedelta, timezone
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.assertEqual(cache_ttl('MarketDataService', 'GetCandles', {'to': tomorrow}), 60)
        self.assertEqual(cache_ttl('OperationsService', 'GetOperationsByCursor', {'to': tomorrow}), 0)
        self.assertEqual(cache_ttl('OperationsService', 'GetPortfolio', {}), 0)
