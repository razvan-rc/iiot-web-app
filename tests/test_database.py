import unittest
from unittest.mock import patch

import database


class DatabaseConfigTests(unittest.TestCase):
    def test_environment_configuration(self):
        config = database.DatabaseConfig.from_environment({
            'DB_READER_HOST': 'replica.internal',
            'DB_WRITER_HOST': 'primary.internal',
            'DB_USER': 'dashboard',
            'DB_PASSWORD': 'secret',
            'DB_NAME': 'factory',
        })

        self.assertEqual(config.reader_host, 'replica.internal')
        self.assertEqual(config.writer_host, 'primary.internal')
        self.assertEqual(config.user, 'dashboard')
        self.assertEqual(config.password, 'secret')
        self.assertEqual(config.name, 'factory')

    @patch('database.pymysql.connect')
    def test_reads_use_replica(self, connect):
        config = database.DatabaseConfig(
            reader_host='replica', writer_host='primary', user='app',
            password='secret', name='industrial',
        )

        database.get_db_connection(config=config)

        self.assertEqual(connect.call_args.kwargs['host'], 'replica')
        self.assertFalse(connect.call_args.kwargs['autocommit'])
        self.assertEqual(connect.call_args.kwargs['connect_timeout'], 5)
        self.assertEqual(connect.call_args.kwargs['read_timeout'], 20)

    @patch('database.pymysql.connect')
    def test_writes_use_primary(self, connect):
        config = database.DatabaseConfig(
            reader_host='replica', writer_host='primary', user='app',
            password='secret', name='industrial',
        )

        database.get_db_connection(write=True, config=config)

        self.assertEqual(connect.call_args.kwargs['host'], 'primary')
        self.assertTrue(connect.call_args.kwargs['autocommit'])


if __name__ == '__main__':
    unittest.main()
