import asyncio
import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from hermes_maintenance.cli import decide
from hermes_maintenance.providers import MaintenanceError, ModularProvider


class MaintenanceTests(unittest.TestCase):
    def test_version_or_activation_drift_holds(self):
        rule = {"from_version": "1.0", "target_version": "1.1", "active": True}
        self.assertEqual(decide(rule, {"version": "1.0", "target": "1.2", "active": True}),
                         "HELD_VERSION_MISMATCH")
        self.assertEqual(decide(rule, {"version": "1.1", "active": False}),
                         "HELD_IDENTITY_OR_STATE")

    def test_modular_rejects_action_on_another_site(self):
        provider = ModularProvider(None)
        provider.call = AsyncMock(return_value={
            "actions": [{"site": {"id": 2}, "site_action_id": 1}], "skipped": [],
        })
        with self.assertRaises(MaintenanceError):
            asyncio.run(provider.refresh(1))

    def test_modular_confirms_scoped_sync(self):
        provider = ModularProvider(None)
        provider.call = AsyncMock(side_effect=[
            {"actions": [{"site": {"id": 1}, "site_action_id": 8}], "skipped": []},
            {"action": {"id": 8, "site": {"id": 1}, "type": "manager.update", "status": "done"}},
        ])
        asyncio.run(provider.refresh(1))
        provider.call.assert_any_await("site-items-sync", {"sites": [1]})
        provider.call.assert_any_await("sites-actions-show", {"site_action_id": 8})

    def test_real_mcp_stdio_preview_apply_and_repeat(self):
        fixture = Path(__file__).with_name("mock_server.py")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.json"
            audit = root / "audit.sqlite3"
            events = root / "events"
            config.write_text(json.dumps({
                "instance": "test",
                "provider": {"adapter": "standard", "transport": "stdio",
                             "command": sys.executable, "args": [str(fixture), str(events)]},
                "sites": [{"id": "demo", "plugins": [{"slug": "plugin", "from_version": "1.0",
                                                      "target_version": "1.1", "active": True}]}],
            }), encoding="utf-8")

            def invoke(mode, *args):
                return subprocess.run(
                    [sys.executable, "-m", "hermes_maintenance.cli", mode,
                     "--config", str(config), *args], capture_output=True, text=True, timeout=25,
                )

            preview = invoke("preview")
            self.assertEqual(preview.returncode, 0, preview.stderr)
            plan = json.loads(preview.stdout)
            self.assertEqual(plan["results"][0]["status"], "READY")
            self.assertFalse(events.exists())
            denied = invoke("apply", "--audit", str(audit), "--confirm", "incorrect")
            self.assertEqual(denied.returncode, 1)
            self.assertFalse(events.exists())
            flags = ("--audit", str(audit), "--confirm", plan["config_sha256"])
            apply = invoke("apply", *flags)
            self.assertEqual(apply.returncode, 0, apply.stderr)
            self.assertEqual(json.loads(apply.stdout)["results"][0]["status"], "UPDATED_VERIFIED")
            again = invoke("apply", *flags)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(json.loads(again.stdout)["results"][0]["status"], "ALREADY_CURRENT")
            self.assertEqual(events.read_text().splitlines(), ["refresh", "update", "refresh"])
            with sqlite3.connect(audit) as db:
                self.assertEqual(db.execute("SELECT state FROM attempts").fetchall(), [("VERIFIED",)])
