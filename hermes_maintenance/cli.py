"""Preview and execute locally approved maintenance without model-selected writes."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from .providers import MCPProvider, MaintenanceError, ModularProvider, connect


def load(path):
    raw = path.read_bytes()
    config = json.loads(raw)
    provider = config["provider"]
    if provider["adapter"] not in {"standard", "modular"}:
        raise MaintenanceError("Unknown adapter")
    if provider["transport"] == "http":
        url = urlsplit(provider["url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise MaintenanceError("HTTP requires an HTTPS MCP URL without embedded credentials")
    elif provider["transport"] != "stdio" or not provider.get("command"):
        raise MaintenanceError("Unknown or incomplete MCP transport")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", config["instance"]):
        raise MaintenanceError("Invalid provider instance label")
    limit = config.get("max_updates", 5)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise MaintenanceError("max_updates must be between 1 and 20")
    seen = set()
    if not config["sites"]:
        raise MaintenanceError("No sites configured")
    for site in config["sites"]:
        sid = site["id"]
        valid_id = type(sid) is int and sid > 0 if provider["adapter"] == "modular" else (
            isinstance(sid, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", sid))
        )
        if not valid_id or sid in seen:
            raise MaintenanceError("Invalid or duplicate site identity")
        seen.add(sid)
        slugs = set()
        for row in site["plugins"]:
            slug = row["slug"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", slug) or slug in slugs:
                raise MaintenanceError("Invalid or duplicate approved plugin")
            slugs.add(slug)
            for field in ("from_version", "target_version"):
                if not re.fullmatch(r"[A-Za-z0-9._+!-]{1,80}", row[field]):
                    raise MaintenanceError("Invalid approved version")
            if type(row["active"]) is not bool or row["from_version"] == row["target_version"]:
                raise MaintenanceError("Invalid activation state or version pair")
            if provider["adapter"] == "modular" and any(
                type(row.get(key)) is not int or row[key] <= 0 for key in ("item_id", "component_id")
            ):
                raise MaintenanceError("Modular approvals require item_id and component_id")
    return config, hashlib.sha256(raw).hexdigest()


def decide(rule, row):
    if not row or type(row.get("active")) is not bool or row["active"] != rule["active"]:
        return "HELD_IDENTITY_OR_STATE"
    if row.get("version") == rule["target_version"]:
        return "ALREADY_CURRENT"
    if (row.get("version"), row.get("target")) != (rule["from_version"], rule["target_version"]):
        return "HELD_VERSION_MISMATCH"
    return "READY"


def reserve(db, instance, site, rule):
    key = (instance, str(site), rule["slug"], rule["from_version"], rule["target_version"])
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM attempts WHERE state != 'VERIFIED' LIMIT 1").fetchone():
                raise MaintenanceError("Audit database has an unresolved or in-flight update")
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, 'SUBMITTED_OR_UNKNOWN', CURRENT_TIMESTAMP)", key)
    except sqlite3.IntegrityError:
        raise MaintenanceError("This version pair was already attempted") from None
    return key


async def run(provider, config, sites, apply, db):
    results = []
    count = 0
    for site in sites:
        sid, rules = site["id"], site["plugins"]
        if apply:
            try:
                await provider.refresh(sid)
            except Exception:
                results.append({"site_id": sid, "status": "HELD_REFRESH_UNCONFIRMED"})
                continue
        rows = await provider.inventory(sid, rules)
        for rule in rules:
            slug = rule["slug"]
            row = rows.get(slug)
            status = decide(rule, row)
            result = {"site_id": sid, "slug": slug, "status": status}
            results.append(result)
            if status != "READY" or not apply:
                continue
            if count >= config.get("max_updates", 5):
                result["status"] = "HELD_BATCH_LIMIT"
                continue
            row = (await provider.inventory(sid, [rule])).get(slug)
            result["status"] = decide(rule, row)
            if result["status"] != "READY":
                continue
            key = reserve(db, config["instance"], sid, rule)
            count += 1
            try:
                await provider.update(sid, row)
                after = (await provider.inventory(sid, [rule])).get(slug)
                if decide(rule, after) != "ALREADY_CURRENT":
                    raise MaintenanceError("Installed version or activation state did not verify")
                with db:
                    db.execute(
                        """UPDATE attempts SET state='VERIFIED'
                           WHERE instance=? AND site=? AND slug=? AND old_version=? AND target_version=?""", key,
                    )
            except Exception:
                result["status"] = "UPDATE_OUTCOME_UNVERIFIED"
                return results, False
            result["status"] = "UPDATED_VERIFIED"
    return results, not any(row["status"].startswith("HELD") for row in results)


async def execute(args):
    config, digest = load(args.config)
    sites = [site for site in config["sites"] if args.site is None or str(site["id"]) == args.site]
    if not sites:
        raise MaintenanceError("Site is outside the configured scope")
    apply = args.mode == "apply"
    if apply and (args.confirm != digest or args.audit is None):
        raise MaintenanceError("Apply requires the reviewed configuration SHA-256 and an audit database")
    provider_config = config["provider"]
    if apply and provider_config["adapter"] == "modular" and (
        provider_config.get("accept_provider_selected_target") is not True
    ):
        raise MaintenanceError("Modular apply requires explicit acceptance of provider-selected targets")
    token_name = provider_config.get("token_env")
    token = os.environ.get(token_name) if token_name else None
    if token_name and not token:
        raise MaintenanceError("Configured MCP token environment variable is missing")
    with closing(sqlite3.connect(args.audit if apply else ":memory:")) as db:
        if apply:
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                instance TEXT, site TEXT, slug TEXT, old_version TEXT, target_version TEXT,
                state TEXT, created_at TEXT, PRIMARY KEY (instance, site, slug, old_version, target_version)
            )""")
            if db.execute("SELECT 1 FROM attempts WHERE state != 'VERIFIED' LIMIT 1").fetchone():
                raise MaintenanceError("Audit database has an unresolved update attempt")
        async with connect(provider_config, token) as session:
            adapter = ModularProvider if provider_config["adapter"] == "modular" else MCPProvider
            results, passed = await run(adapter(session), config, sites, apply, db)
    print(json.dumps({"mode": args.mode, "config_sha256": digest, "results": results}, indent=2))
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "apply"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--site", help="Limit to one configured site")
    parser.add_argument("--confirm", help="Reviewed config SHA-256 printed by preview")
    parser.add_argument("--audit", type=Path, help="Persistent SQLite audit database for live updates")
    try:
        return asyncio.run(execute(parser.parse_args()))
    except MaintenanceError as error:
        print(str(error), file=sys.stderr)
    except Exception as error:
        print(f"Maintenance stopped ({type(error).__name__}). Review the audit before retrying.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
