"""Unit tests for containerization readiness, health checks, and proxy integration."""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app as appmod


class TestContainerDeployment(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        os.environ["SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD"] = "testbootstrap123"

    def tearDown(self):
        os.environ.pop("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD", None)

    def test_healthz_healthy(self):
        appmod.init_db(seed_demo=False)
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data.get("status"), "ok")

    def test_mock_roster_seeding(self):
        appmod.init_db(mock_roster=True)
        conn = appmod.db()
        employee_count = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE role='employee'"
        ).fetchone()["c"]
        manager = conn.execute("SELECT * FROM users WHERE username='manager'").fetchone()
        shift_count = conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"]
        conn.close()

        self.assertEqual(employee_count, 14)
        self.assertIsNotNone(manager)
        self.assertEqual(shift_count, 42)

    def test_cookie_security_defaults(self):
        self.assertTrue(appmod.app.config.get("SESSION_COOKIE_HTTPONLY"))
        self.assertEqual(appmod.app.config.get("SESSION_COOKIE_SAMESITE"), "Lax")


if __name__ == "__main__":
    unittest.main()
