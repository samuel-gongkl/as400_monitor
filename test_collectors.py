import unittest
from unittest.mock import patch

from collectors.base import BackgroundTableCollector
from config import Config
from db import set_current_lpar


class _PerLparCollector(BackgroundTableCollector):
    columns = ["LPAR"]
    ttl_seconds = 60

    def query_live(self):
        from db import current_lpar
        return [[current_lpar()]]

    def mock_rows(self):
        return []


class CollectorCacheTests(unittest.TestCase):
    def test_background_table_cache_is_partition_specific(self):
        collector = _PerLparCollector()
        with patch.object(Config, "MODE", "live"):
            collector._refresh("lpar1")
            collector._refresh("lpar2")
            set_current_lpar("lpar1")
            first = collector.fetch_live()
            set_current_lpar("lpar2")
            second = collector.fetch_live()
        self.assertEqual(first["rows"], [["lpar1"]])
        self.assertEqual(second["rows"], [["lpar2"]])


if __name__ == "__main__":
    unittest.main()
