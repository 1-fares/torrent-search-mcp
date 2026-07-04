from typing import Any

import pytest
from fastmcp import Client

from .mcp_server import mcp
from .wrapper import close_crawler, close_http_client, get_http_client


@pytest.fixture(scope="session")
def mcp_client() -> Client[Any]:
    """Create a FastMCP client for testing."""
    return Client(mcp)


@pytest.fixture(scope="session", autouse=True)
async def _scraper_resources() -> Any:
    """Own the HTTP client for the TPB path. Crawler starts lazily on first nyaa hit."""
    await get_http_client()
    yield
    await close_http_client()
    await close_crawler()


def _torrent_id_for(text: str, source_marker: str) -> str | None:
    """Pull the first ID belonging to a given source out of a search result blob."""
    for line in text.splitlines():
        if source_marker in line and "'id':" in line:
            start = line.find("'id': '") + len("'id': '")
            end = line.find("'", start)
            if start > -1 and end > start:
                candidate = line[start:end]
                if source_marker in candidate:
                    return candidate
    return None


async def test_search_torrents(mcp_client: Client[Any]) -> None:
    """Search returns at least one structured torrent."""
    async with mcp_client as client:
        result = await client.call_tool(
            "search_torrents",
            {"user_intent": "complete Breaking Bad series", "query": "breaking bad"},
        )
        assert result is not None
        assert len(result.content[0].text) > 32  # At least 1 torrent found


@pytest.mark.parametrize("source_marker", ["thepiratebay.org", "nyaa.si"])
async def test_get_torrent_resolves(mcp_client: Client[Any], source_marker: str) -> None:
    """For each source, search live, then fetch the first ID. Skips if the source returned nothing."""
    async with mcp_client as client:
        search = await client.call_tool(
            "search_torrents",
            {"user_intent": "popular torrent", "query": "ubuntu"},
        )
        torrent_id = _torrent_id_for(search.content[0].text, source_marker)
        if not torrent_id:
            pytest.skip(f"No live results from {source_marker}")
        result = await client.call_tool("get_torrent", {"torrent_id": torrent_id})
        body = result.content[0].text
        assert body and body != "Torrent not found"
        assert body.startswith("magnet:") or body.endswith(".torrent")
