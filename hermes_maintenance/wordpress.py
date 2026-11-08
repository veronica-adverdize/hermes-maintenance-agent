"""Local stdio MCP bridge for a single WordPress installation with WP-CLI."""

import argparse
import json
import re
import subprocess
from pathlib import Path

from mcp.server.mcpserver import MCPServer


def bridge(site, root):
    server = MCPServer("Hermes WordPress Maintenance")

    def wp(*args):
        try:
            return subprocess.run(
                ["wp", f"--path={root}", "--no-color", *args], cwd=root,
                capture_output=True, text=True, check=True, timeout=35,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("WP-CLI command failed or timed out") from None

    def check_scope(site_id):
        if site_id != site:
            raise ValueError("Site is outside the bridge scope")

    def inventory():
        plugins = json.loads(wp(
            "plugin", "list", "--skip-update-check",
            "--fields=name,status,version,update_version", "--format=json",
        ))
        return [
            {"slug": row["name"], "version": row["version"],
             "active": row["status"] == "active", "target": row["update_version"] or None}
            for row in plugins if row["status"] in {"active", "inactive"}
        ]

    @server.tool()
    def maintenance_inventory(site_id: str) -> dict:
        check_scope(site_id)
        return {"site_id": site, "plugins": inventory()}

    @server.tool()
    def maintenance_refresh(site_id: str) -> dict:
        check_scope(site_id)
        wp("eval", "wp_clean_plugins_cache(true); wp_update_plugins();")
        return {"site_id": site, "status": "confirmed"}

    @server.tool()
    def maintenance_update(site_id: str, slug: str, from_version: str,
                           target_version: str, active: bool) -> dict:
        check_scope(site_id)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,149}", slug) or not re.fullmatch(
            r"[0-9][A-Za-z0-9._+-]{0,79}", target_version
        ):
            raise ValueError("Invalid plugin slug or release version")
        before = [row for row in inventory() if row["slug"] == slug]
        if len(before) != 1 or (before[0]["version"], before[0]["target"], before[0]["active"]) != (
            from_version, target_version, active
        ):
            raise ValueError("Installed version, offered release, or activation state changed")
        wp("plugin", "update", slug, f"--version={target_version}")
        return {"site_id": site, "slug": slug, "status": "completed"}

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    root = args.path.resolve()
    if not root.is_dir() or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", args.site_id):
        parser.error("Provide an existing WordPress directory and a valid site label.")
    bridge(args.site_id, root).run()


if __name__ == "__main__":
    main()
