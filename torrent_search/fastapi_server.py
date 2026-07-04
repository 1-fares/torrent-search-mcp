from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import FileResponse

from .wrapper import Torrent, TorrentSearchApi, close_crawler, ensure_crawler_started

api_client = TorrentSearchApi()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await ensure_crawler_started()
    try:
        yield
    finally:
        await close_crawler()


app = FastAPI(
    title="Torrent Search FastAPI",
    description="FastAPI server for Torrent Search API.",
    lifespan=lifespan,
)


@app.get("/", summary="Health Check", tags=["General"], response_model=dict[str, str])
async def health_check() -> dict[str, str]:
    """
    Endpoint to check the health of the service.
    """
    return {"status": "ok"}


@app.get(
    "/sources",
    summary="List Available Torrent Sources",
    tags=["General"],
    response_model=list[str],
)
async def list_sources() -> list[str]:
    """List the torrent sources currently enabled on this server."""
    return api_client.available_sources()


@app.post(
    "/torrent/search",
    summary="Search Torrents",
    tags=["Torrents"],
    response_model=list[Torrent],
)
async def search_torrents(
    query: str,
    max_items: int = 10,
) -> list[Torrent]:
    """
    Search for torrents on the configured sources.
    Corresponds to `TorrentSearchApi.search_torrents()`.
    """
    torrents: list[Torrent] = await api_client.search_torrents(query, max_items)
    return torrents


@app.get(
    "/torrent/{torrent_id}",
    summary="Get Magnet Link or Torrent File",
    tags=["Torrents"],
    response_model=str,
)
async def get_torrent(
    torrent_id: str = Path(..., description="The ID of the torrent."),
) -> str | FileResponse:
    """
    Get the magnet link or torrent file for a specific torrent by id.
    Corresponds to `TorrentSearchApi.get_torrent()`.
    """
    result: str | None = await api_client.get_torrent(torrent_id)
    if not result:
        raise HTTPException(
            status_code=404,
            detail="Magnet link or torrent file not found or could not be generated.",
        )
    elif result.endswith(".torrent"):
        return FileResponse(
            path=result,
            media_type="application/x-bittorrent",
            filename=result.split("/")[-1],
        )
    return result
