import logging
from os import getenv
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP

from .wrapper import Torrent, TorrentSearchApi, crawler_lifespan

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Torrent Search")

mcp: FastMCP[Any] = FastMCP("Torrent Search Tool", lifespan=crawler_lifespan)

torrent_search_api = TorrentSearchApi()

INCLUDE_LINKS = str(getenv("INCLUDE_LINKS")).lower() == "true"


@mcp.resource("data://torrent_sources")
def available_sources() -> list[str]:
    """Get the list of available torrent sources."""
    return torrent_search_api.available_sources()


def _render(torrent: Torrent, include_links: bool) -> str:
    if include_links:
        return str(torrent)
    payload = torrent.model_dump(
        exclude_unset=True,
        exclude_none=True,
        exclude={"magnet_link", "torrent_file"},
    )
    return str(payload)


@mcp.tool()
async def search_torrents(
    user_intent: str,
    query: str,
) -> str:
    """Perform an advanced torrent search across multiple providers.

    # Arguments:
    - `user_intent`: Must reflect user's overall intention (e.g. "latest episode of Breaking Bad").
    - `query`: Optimized keywords for search. MUST be lowercase and space-separated.

    # Query Construction Rules:
    - **NO** generic terms: remove "movie", "series", "torrent", "download".
    - **NO** filler words: remove "the", "a", "an", "and", "of", "with".
    - **NO** technical tags: do NOT add "1080p", "h265", "bluray", or episode titles, except if explicitly requested by the user.
    - **TV Shows**:
        - Specific episode: `[show name] sXXeYY` (e.g., "shogun s01e05")
        - Full season: `[show name] sXX` (e.g., "shogun s01")
        - Full series: `[show name]` (e.g., "shogun")
    - **Language**: Add `multi` ONLY if the user specifically requests a non-French or multi-language version.

    # Result Analysis & Ranking:
    1. **Quality**: Prefer 1080p or 4k, over 720p.
    2. **Efficiency**: Prefer h265/HEVC for better quality/size ratio.
    3. **Health**: Maximize seeders + leechers.
    4. **Size**: Prefer smaller files within the same quality bracket.

    # Response Requirements:
    - Recommend the **top 3** results maximum.
    - For each recommendation, include: Filename, Size, Seeds/Leechs, Date, Source, and a 1-sentence "Why this?" reason.
    - If results are poor or irrelevant, suggest specific keywords to improve the search.
    """
    _ = user_intent
    logger.info("Searching for torrents: %s", query)
    found_torrents: list[Torrent] = await torrent_search_api.search_torrents(query)
    if not found_torrents:
        return "No torrents found"
    return "\n".join(_render(t, INCLUDE_LINKS) for t in found_torrents)


@mcp.tool()
async def get_torrent(torrent_id: str) -> str:
    """Get the magnet link or torrent filepath for a specific torrent by id."""
    logger.info("Getting magnet link or torrent filepath for torrent: %s", torrent_id)
    result: str | None = await torrent_search_api.get_torrent(torrent_id)
    return result or "Torrent not found"
