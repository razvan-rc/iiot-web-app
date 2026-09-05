import copy
import gzip
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import app


class DashboardPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.start = datetime(2026, 9, 4, 12)
        self.params = {
            'from': self.start.isoformat() + 'Z',
            'to': (self.start + timedelta(hours=1)).isoformat() + 'Z',
        }
        self.rows = []
        for i in range(12):
            payload = {
                'timestamp': (self.start + timedelta(minutes=5*i)).isoformat()+'Z',
                'health': {'degradation_score': .04+i*.001, 'demo_mode': i == 5,
                           'wear_cycle_hours': 168, 'state': 'RUN'},
                'measurements': {'flow_ml_s': 200+i, 'tank_level_pct': 50},
                'inputs': {'entry_sensor': True},
                'operational': {'cycle_count': i*10, 'cycle_rate_per_min': 2,
                                'state_seconds': {'RUN': i*300}},
                'line': {'production': {'good': i*9, 'rejects': i}},
            }
            self.rows.append({'timestamp': self.start + timedelta(minutes=5*i),
                              'station_name': 'Bottling', 'sample_count': 10,
                              'payload_json': json.dumps(payload)})
        self.connection = MagicMock()
        self.connection.cursor.return_value.__enter__.return_value.fetchall.return_value = []

    def request(self, path, headers=None, rows=None):
        with patch.object(app, 'read_sampled_telemetry',
                          return_value=copy.deepcopy(self.rows if rows is None else rows)) as read:
            with patch.object(app, 'get_db_connection', return_value=self.connection):
                response = self.client.get(path, query_string=self.params, headers=headers)
        return response, read

    def test_shared_samples_preserve_summary_and_chart(self):
        summary, _ = self.request('/api/summary')
        response, read = self.request('/api/dashboard')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        history = result.pop('history')
        self.assertEqual(result, summary.get_json())
        read.assert_called_once_with(self.start, self.start+timedelta(hours=1),
                                     None, resolution=720)
        self.assertEqual(history['count'], 120)
        self.assertEqual(history['sampled_count'], 12)
        for actual, source in zip(history['rows'], self.rows):
            payload = json.loads(source['payload_json'])
            self.assertEqual(actual['timestamp'], source['timestamp'].isoformat()+'Z')
            self.assertEqual(actual['payload']['measurements'], payload['measurements'])
            self.assertEqual(actual['payload']['health']['demo_mode'], payload['health']['demo_mode'])
            self.assertNotIn('inputs', actual['payload'])

    def test_gzip_roundtrip_and_negotiation(self):
        plain, _ = self.request('/api/dashboard')
        compressed, _ = self.request('/api/dashboard', {'Accept-Encoding': 'gzip'})
        disabled, _ = self.request('/api/dashboard', {'Accept-Encoding': 'gzip;q=0'})
        self.assertEqual(compressed.headers['Content-Encoding'], 'gzip')
        self.assertEqual(json.loads(gzip.decompress(compressed.data)), plain.get_json())
        self.assertLess(len(compressed.data), len(plain.data))
        self.assertNotIn('Content-Encoding', disabled.headers)
        self.assertIn('Accept-Encoding', plain.headers['Vary'])

    def test_empty_range(self):
        response, _ = self.request('/api/dashboard', rows=[])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['history']['rows'], [])
        self.assertEqual(response.get_json()['history']['count'], 0)

    def test_invalid_ranges_do_not_query_database(self):
        with patch.object(app, 'read_sampled_telemetry') as read:
            for params in ({'from': 'invalid'}, {'station': 'unknown'},
                           {'from': self.params['to'], 'to': self.params['from']}):
                self.assertEqual(self.client.get('/api/dashboard', query_string=params).status_code, 400)
            read.assert_not_called()

    def test_station_filter_is_forwarded(self):
        self.params['station'] = 'Bottling'
        response, read = self.request('/api/dashboard')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(read.call_args.args[2], 'Bottling')
        event_query = self.connection.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
        self.assertIn('FORCE INDEX (idx_station_time)', event_query)
        self.assertIn('LIMIT 10000', event_query)

    def test_unfiltered_events_use_timestamp_index(self):
        response, _ = self.request('/api/dashboard')
        self.assertEqual(response.status_code, 200)
        event_query = self.connection.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
        self.assertIn('FORCE INDEX (idx_timestamp)', event_query)


if __name__ == '__main__':
    unittest.main()
