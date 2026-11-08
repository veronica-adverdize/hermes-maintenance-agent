"""Translate MCP tools to the maintenance runner's fixed operations."""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


class MaintenanceError(Exception):
    """A safe, operator-facing error without provider response contents."""


@asynccontextmanager
async def connect(config, token):
    if config["transport"] == "stdio":
        transport = stdio_client(StdioServerParameters(
            command=config["command"], args=config.get("args", [])
        ))
        async with transport as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=45) as session:
                await session.initialize()
                yield session
    else:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx2.AsyncClient(headers=headers, timeout=30, follow_redirects=False) as client:
            async with streamable_http_client(config["url"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1], read_timeout_seconds=45) as session:
                    await session.initialize()
                    yield session


class MCPProvider:
    tools = ("maintenance_inventory", "maintenance_refresh", "maintenance_update")

    def __init__(self, session):
        self.session = session

    async def call(self, tool, arguments):
        if tool not in self.tools:
            raise MaintenanceError("Tool is outside the adapter allowlist")
        async with asyncio.timeout(45):
            result = await self.session.call_tool(tool, arguments)
        if result.is_error:
            raise MaintenanceError(f"MCP tool failed: {tool}")
        payload = result.structured_content
        if payload is None and len(result.content) == 1:
            block = result.content[0]
            if block.type == "text":
                payload = json.loads(block.text)
        if not isinstance(payload, dict):
            raise MaintenanceError(f"Invalid response from {tool}")
        return payload

    async def refresh(self, site):
        result = await self.call("maintenance_refresh", {"site_id": site})
        if result.get("site_id") != site or result.get("status") != "confirmed":
            raise MaintenanceError("Inventory refresh was not confirmed")

    async def inventory(self, site, approved):
        result = await self.call("maintenance_inventory", {"site_id": site})
        if result.get("site_id") != site or not isinstance(result.get("plugins"), list):
            raise MaintenanceError("Invalid inventory scope")
        rows = {}
        for row in result["plugins"]:
            if row["slug"] in rows:
                raise MaintenanceError("Duplicate plugin identity")
            rows[row["slug"]] = row
        return rows

    async def update(self, site, row):
        result = await self.call("maintenance_update", {
            "site_id": site, "slug": row["slug"], "from_version": row["version"],
            "target_version": row["target"], "active": row["active"],
        })
        if (result.get("site_id"), result.get("slug"), result.get("status")) != (
            site, row["slug"], "completed"
        ):
            raise MaintenanceError("Update completion was not confirmed")


class ModularProvider(MCPProvider):
    tools = ("site-items-sync", "site-items-show", "sites-actions-show", "components-upgrade")
    intent = "Verifying approved plugin versions and preserving activation state while performing scoped website maintenance through the configured provider."

    async def call(self, tool, arguments):
        return await super().call(tool, {**arguments, "intent": self.intent})

    async def action(self, response, site, kind, row=None):
        actions = response.get("actions")
        if response.get("skipped") != [] or not isinstance(actions, list) or len(actions) != 1:
            raise MaintenanceError("Provider did not accept exactly one site action")
        submitted = actions[0]
        action_id = submitted.get("site_action_id")
        if submitted.get("site", {}).get("id") != site or type(action_id) is not int or action_id <= 0:
            raise MaintenanceError("Provider returned an invalid action identity")
        async with asyncio.timeout(180):
            while True:
                result = await self.call("sites-actions-show", {"site_action_id": action_id})
                action = result["action"]
                if (action.get("id"), action.get("site", {}).get("id"), action.get("type")) != (
                    action_id, site, kind
                ):
                    raise MaintenanceError("Provider action identity changed")
                if action.get("status") == "done":
                    break
                if action.get("status") not in {"pending", "in_progress"}:
                    raise MaintenanceError("Provider action requires review")
                await asyncio.sleep(4)
        if row is not None:
            expected = {
                "site_item_id": row["item_id"], "slug": row["slug"], "type": "plugin",
                "from_version": row["version"], "to_version": row["target"],
            }
            items = action.get("managed_items")
            if not isinstance(items, list) or len(items) != 1 or any(
                items[0].get(key) != value for key, value in expected.items()
            ):
                raise MaintenanceError("Completed action did not match the approved update")

    async def refresh(self, site):
        await self.action(await self.call("site-items-sync", {"sites": [site]}), site, "manager.update")

    async def inventory(self, site, approved):
        rows = {}
        for rule in approved:
            result = await self.call("site-items-show", {"site_item_id": rule["item_id"]})
            item = result["item"]
            if (item.get("site", {}).get("id"), item.get("id"), item.get("component_id"),
                item.get("slug"), item.get("type")) != (
                site, rule["item_id"], rule["component_id"], rule["slug"], "plugin"
            ):
                raise MaintenanceError("Installed plugin identity did not match approval")
            if item.get("status") not in {"active", "inactive"}:
                raise MaintenanceError("Unknown plugin activation state")
            rows[rule["slug"]] = {
                "slug": rule["slug"], "version": item["version"], "active": item["status"] == "active",
                "target": item.get("new_version") if item.get("can_upgrade") is True
                and item.get("in_progress_action_id") is None else None,
                "item_id": rule["item_id"], "component_id": rule["component_id"],
            }
        return rows

    async def update(self, site, row):
        result = await self.call("components-upgrade", {
            "sites": [site], "component_id": row["component_id"],
            "action": "upgrade", "clean_cache": False,
        })
        await self.action(result, site, "manager.upgrade", row)
