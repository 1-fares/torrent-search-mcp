import logging
from asyncio import gather, run, to_thread
from os import getenv
from pathlib import Path
from sys import argv
from typing import Any

from aiocache import cached
from fr_torrent_search import fr_torrent_api

from .models import Cache, Torrent
from .scraper import SCRAPER_SOURCES, search_torrents

logger = logging.getLogger(__name__)

FOLDER_TORRENT_FILES: Path = Path(getenv("FOLDER_TORRENT_FILES") or "./torrents")


def key_builder(_namespace: str, _fn: Any, *args: tuple[Any], **kwargs: dict[str, Any]) -> str:
    key = {
        "query": args[0] if len(args) > 0 else "",
        "max_items": args[1] if len(args) > 1 else 10,
    } | kwargs
    return str(key)


class TorrentSearchApi:
    """A client for searching torrents."""

    def __init__(self) -> None:
        self.cache: Cache = Cache()
        self.folder_torrent_files: Path = FOLDER_TORRENT_FILES

        self.include_fr_sources: bool = getenv("EXCLUDE_FR_SOURCES", "false").lower() in (
            "0",
            "false",
        )
        self.exclude_sources: list[str] = sorted(
            {
                source.strip()
                for source in (getenv("EXCLUDE_SOURCES") or "").split(",")
                if source.strip()
            }
        )

        self._fr_torrent = fr_torrent_api()
        self.fr_sources: set[str] = set()
        if self.include_fr_sources:
            self._fr_torrent.ensure_initialized()
            self.fr_sources = set(self._fr_torrent.api_names)

        scraper_sources = set(SCRAPER_SOURCES)
        self.sources: list[str] = sorted(
            (scraper_sources | self.fr_sources) - set(self.exclude_sources)
        )

    # Backward-compat alias for code that referenced the old class attribute.
    @property
    def CACHE(self) -> Cache:  # noqa: N802
        return self.cache

    def available_sources(self) -> list[str]:
        """Get the list of available torrent sources."""
        return list(self.sources)

    def _ensure_torrent_dir(self) -> None:
        self.folder_torrent_files.mkdir(parents=True, exist_ok=True)

    @cached(ttl=300, key_builder=key_builder)  # 5min
    async def search_torrents(
        self,
        query: str,
        max_items: int = 10,
    ) -> list[Torrent]:
        """
        Search for torrents on available sources.

        Args:
            query: Search query.
            max_items: Maximum number of items to return.

        Returns:
            A list of torrent results.
        """
        query = query.lower()
        en_sources = [s for s in self.sources if s not in self.fr_sources]
        fr_active = self.include_fr_sources and any(s in self.fr_sources for s in self.sources)

        tasks: list[tuple[str, Any]] = []
        if en_sources:
            tasks.append(("en", search_torrents(query, en_sources)))
        if fr_active:
            tasks.append(
                (
                    "fr",
                    to_thread(
                        self._fr_torrent.search_torrents,
                        query,
                        max_items=max_items,
                        exclude=self.exclude_sources,
                    ),
                )
            )

        found_torrents: list[Torrent] = []
        if tasks:
            results = await gather(*(coro for _, coro in tasks))
            for (label, _), result in zip(tasks, results, strict=True):
                if label == "en":
                    found_torrents.extend(result)
                else:  # "fr"
                    found_torrents.extend(Torrent.format(**t.model_dump()) for t in result)

        # Health-rank: live seeders dominate; leechers only break ties.
        found_torrents.sort(
            key=lambda torrent: (torrent.seeders, torrent.leechers),
            reverse=True,
        )
        found_torrents = found_torrents[:max_items]

        for torrent in found_torrents:
            torrent.prepend_info(query, max_items)

        self.cache.clean()
        self.cache.update(found_torrents)
        return found_torrents

    async def get_torrent(self, torrent_id: str) -> str | None:
        """
        Get the magnet link or torrent filepath for a previously found torrent.

        Args:
            torrent_id: The ID of the torrent.

        Returns:
            The magnet link or torrent filepath as a string, else None.
        """
        try:
            query, max_items, source, ref_id = Torrent.extract_info(
                torrent_id, known_sources=self.sources
            )
        except Exception as e:
            logger.warning("Invalid torrent ID %r: %s", torrent_id, e)
            return None

        found_torrent: Torrent | None = self.cache.get(torrent_id)

        if not found_torrent:  # Missing or uncached
            torrents: list[Torrent] = await self.search_torrents(query, max_items)
            found_torrent = next(
                (torrent for torrent in torrents if torrent.id == torrent_id), None
            )
            if found_torrent and found_torrent.source:
                source = found_torrent.source

        if found_torrent and self.include_fr_sources and source in self.fr_sources:
            self._ensure_torrent_dir()
            result = self._fr_torrent.get_torrent(ref_id, output_dir=self.folder_torrent_files)
            if result and isinstance(result, str):
                if result.endswith(".torrent"):
                    found_torrent.torrent_file = str(self.folder_torrent_files / result)
                else:
                    found_torrent.magnet_link = result

        self.cache.clean()

        if found_torrent:
            if found_torrent.torrent_file:
                return found_torrent.torrent_file
            elif found_torrent.magnet_link:
                return found_torrent.magnet_link
        return None

    async def cli(self) -> None:
        """
        Command line interface for the API.
        """
        query = argv[1] if len(argv) > 1 else None
        if query:
            found_torrents: list[Torrent] = await self.search_torrents(query, max_items=100)
            if found_torrents:
                found_sources = set()
                for t in found_torrents:
                    found_sources.add(t.source)
                    print(f"{t.id} ({t.seeders}|{t.leechers}|{t.downloads}) - {t.filename}")
                print(f"Fetching: {found_torrents[0].id}")
                print(f"Result: {await self.get_torrent(found_torrents[0].id)}")
                print(
                    f"Found Sources (Excluded: {self.exclude_sources}): "
                    f"{found_sources} | Found Torrents: {len(found_torrents)}"
                )
            else:
                print("No torrents found")
        else:
            print("Please provide a search query.")


if __name__ == "__main__":
    run(TorrentSearchApi().cli())
