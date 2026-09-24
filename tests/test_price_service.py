import json
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import wog_price_service as service


class PriceServiceTests(unittest.TestCase):
    def test_rate_limited_refresh_preserves_last_valid_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = service.PriceStore(Path(tmp) / "prices.sqlite3")
            key = "elite cloak (tier 4)"
            store.put(key, {"name": "Elite Cloak (Tier 4)", "price": 2.5,
                            "median": 2.4, "volume": "8", "status": "ok"})
            store.put(key, {"name": "Elite Cloak (Tier 4)", "price": None,
                            "median": None, "volume": None, "status": "rate_limited"})
            row = store.get(key)
            self.assertEqual(row["price"], 2.5)
            self.assertEqual(row["status"], "rate_limited")
            store.close()

    def test_fetch_detects_steam_http_429(self):
        completed = SimpleNamespace(stdout=b"compressed body\n429")
        with patch.object(service.subprocess, "run", return_value=completed):
            row = service._fetch({"name": "Elite Cloak", "tier": 4})
        self.assertEqual(row["status"], "rate_limited")

    def test_fetch_uses_optional_server_cookie_without_putting_it_in_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_file = Path(tmp) / "steam-cookie"
            cookie_file.write_text(
                "steamLoginSecure=private-value; sessionid=session-value", encoding="utf-8")
            try:
                cookie_file.chmod(0o600)
            except OSError:
                pass
            completed = SimpleNamespace(stdout=b'{"lowest_price":"$1.25"}\n200')
            with patch.object(service, "COOKIE_FILE", cookie_file), \
                    patch.object(service.subprocess, "run", return_value=completed) as run:
                row = service._fetch({"name": "Elite Cloak", "tier": 4})
            args, kwargs = run.call_args
            self.assertEqual(row["status"], "ok")
            self.assertNotIn("private-value", " ".join(args[0]))
            self.assertIn("Cookie: steamLoginSecure=private-value", kwargs["input"].decode("utf-8"))

    def test_cookie_file_with_broad_permissions_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_file = Path(tmp) / "steam-cookie"
            cookie_file.write_text("steamLoginSecure=x; sessionid=y", encoding="utf-8")
            try:
                cookie_file.chmod(0o644)
            except OSError:
                pass
            with patch.object(service, "COOKIE_FILE", cookie_file), \
                    patch.object(service.os, "name", "posix"):
                self.assertEqual(service._steam_cookie_header(), "")

    def test_rate_limit_backoff_survives_worker_restart_and_increases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = service.PriceStore(Path(tmp) / "prices.sqlite3")
            first_until = store.mark_rate_limited()
            worker = service.PriceWorker(store, Path(tmp) / "catalog.json")
            self.assertGreater(worker.rate_limited_until, time.monotonic())
            second_until = store.mark_rate_limited()
            self.assertGreaterEqual(second_until - first_until, service.RATE_LIMIT_BACKOFF - 1)
            self.assertEqual(store.rate_limit_state()["count"], 2)
            store.close()

    def test_requested_items_are_promoted_ahead_of_catalog_prewarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = service.PriceStore(Path(tmp) / "prices.sqlite3")
            worker = service.PriceWorker(store, Path(tmp) / "catalog.json")
            worker.enqueue([
                {"name": "Catalog Item", "tier": 4},
                {"name": "Requested Item", "tier": 5},
            ])
            worker.enqueue([{"name": "Requested Item", "tier": 5}], priority=True)
            self.assertEqual(worker.queue[0]["name"], "Requested Item")
            self.assertEqual(len(worker.queue), 2)
            store.close()

    def test_worker_start_does_not_prewarm_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = service.PriceStore(Path(tmp) / "prices.sqlite3")
            catalog = Path(tmp) / "catalog.json"
            catalog.write_text(json.dumps([{
                "name": "Catalog Item", "market_name": "Catalog Item", "tier": 4,
            }]), encoding="utf-8")
            worker = service.PriceWorker(store, catalog)
            worker._load_catalog_file()
            self.assertEqual(len(store.catalog_items()), 1)
            self.assertEqual(len(worker.queue), 0)
            store.close()

    def test_store_catalog_migrates_cached_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.sqlite3"
            store = service.PriceStore(path)
            store.put("elite cloak (tier 4)", {
                "name": "Elite Cloak (Tier 4)", "price": 1.2,
                "median": 1.3, "volume": "4", "status": "ok",
            })
            # A second instance exercises the migration path used on the server.
            store2 = service.PriceStore(path)
            rows = store2.catalog_items()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["key"], "elite cloak (tier 4)")
            store.close()
            store2.close()

    def test_worker_returns_cached_rows_without_http_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = service.PriceStore(Path(tmp) / "prices.sqlite3")
            item = {"name": "Elite Cloak", "market_name": "Elite Cloak", "tier": 4}
            key = store.remember(item)
            store.put(key, {"name": "Elite Cloak (Tier 4)", "price": 1.2,
                            "median": 1.3, "volume": "4", "status": "ok"})
            worker = service.PriceWorker(store, Path(tmp) / "catalog.json")
            with patch.object(service, "_fetch", side_effect=AssertionError("fresh cache fetched")):
                worker._refresh({**item, "key": key})
            store.close()

    def test_worker_refreshes_due_items_in_background_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = service.PriceStore(Path(tmp) / "prices.sqlite3")
            item = {"name": "Elite Cloak", "market_name": "Elite Cloak", "tier": 4}
            key = store.remember(item)
            store.put(key, {"name": "Elite Cloak (Tier 4)", "price": None,
                            "median": None, "volume": None, "status": "error"})
            store.db.execute("UPDATE prices SET updated=? WHERE key=?", (time.time() - service.ERROR_RETRY_INTERVAL - 1, key))
            store.db.commit()
            worker = service.PriceWorker(store, Path(tmp) / "catalog.json")
            with patch.object(service, "_fetch", return_value={
                "name": "Elite Cloak (Tier 4)", "price": 1.5,
                "median": 1.4, "volume": "9", "status": "ok"}):
                worker._refresh({**item, "key": key})
            self.assertEqual(store.get(key)["price"], 1.5)
            store.close()

    def test_catalog_file_is_loaded_and_does_not_fetch_in_http_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "catalog.json"
            catalog.write_text(json.dumps([{
                "name": "Elite Cloak", "market_name": "Elite Cloak", "tier": 4,
            }]), encoding="utf-8")
            store = service.PriceStore(Path(tmp) / "prices.sqlite3")
            worker = service.PriceWorker(store, catalog)
            worker._load_catalog_file()
            self.assertEqual(len(store.catalog_items()), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
