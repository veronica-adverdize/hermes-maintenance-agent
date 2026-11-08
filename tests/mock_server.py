import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

server = MCPServer("Test Maintenance")
events = Path(sys.argv[1])


def record(event):
    with events.open("a", encoding="utf-8") as file:
        print(event, file=file)


@server.tool()
def maintenance_inventory(site_id: str) -> dict:
    assert site_id == "demo"
    updated = events.exists() and "update" in events.read_text().splitlines()
    return {"site_id": site_id, "plugins": [{
        "slug": "plugin", "version": "1.1" if updated else "1.0",
        "active": True, "target": None if updated else "1.1",
    }]}


@server.tool()
def maintenance_refresh(site_id: str) -> dict:
    assert site_id == "demo"
    record("refresh")
    return {"site_id": site_id, "status": "confirmed"}


@server.tool()
def maintenance_update(site_id: str, slug: str, from_version: str,
                       target_version: str, active: bool) -> dict:
    assert (site_id, slug, from_version, target_version, active) == (
        "demo", "plugin", "1.0", "1.1", True
    )
    record("update")
    return {"site_id": site_id, "slug": slug, "status": "completed"}


if __name__ == "__main__":
    server.run()
