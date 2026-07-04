from hashlib import sha256
from time import time
from typing import Any

from pydantic import BaseModel

from .utils import Compress62


class Torrent(BaseModel):
    id: str
    filename: str
    category: str | None = None
    size: str
    seeders: int
    leechers: int
    downloads: int | str | None = None
    date: str
    magnet_link: str | None = None
    torrent_file: str | None = None
    uploader: str | None = None
    source: str | None = None

    @classmethod
    def format(cls, **data: Any) -> "Torrent":
        data["id"] = (
            data["source"]
            + "-"
            + str(
                data.get("id")
                or (
                    sha256(data["magnet_link"].encode()).hexdigest()[:10]
                    if data.get("magnet_link")
                    else "none"
                )
            )
        )
        data["filename"] = data["filename"].strip()
        data["seeders"] = int(data["seeders"]) if data.get("seeders") else 0
        data["leechers"] = int(data["leechers"]) if data.get("leechers") else 0
        data["downloads"] = data["downloads"] if data.get("downloads") else None
        data["date"] = data["date"][:10]
        return cls(**data)

    def prepend_info(self, query: str, max_items: int) -> None:
        self.id = f"{Compress62.compress(query)}-{max_items}-{self.id}"

    @staticmethod
    def extract_info(
        torrent_id: str, known_sources: list[str] | None = None
    ) -> tuple[str, int, str, str]:
        # Format: {compressed_query}-{max_items}-{source}-{ref_id}
        # compressed_query is base62 (no dashes); max_items is int (no dashes).
        # source/ref_id may legitimately contain dashes, so resolve them by
        # matching the longest known source prefix instead of splitting blind.
        parts = torrent_id.split("-", 2)
        if len(parts) < 3:
            raise ValueError(f"Invalid torrent ID format: {torrent_id!r}")
        compressed_query, max_items_str, rest = parts
        query = Compress62.decompress(compressed_query)
        max_items = int(max_items_str)
        if known_sources:
            # Match the longest known source first so e.g. "foo.bar" wins over "foo".
            sorted_sources: list[str] = sorted(known_sources, key=lambda s: len(s), reverse=True)
            for src in sorted_sources:
                prefix = f"{src}-"
                if rest.startswith(prefix):
                    return query, max_items, src, rest[len(prefix) :]
        source, _, ref_id = rest.rpartition("-")
        if not source or not ref_id:
            raise ValueError(f"Invalid torrent ID format: {torrent_id!r}")
        return query, max_items, source, ref_id

    def __str__(self) -> str:
        return str(self.model_dump(exclude_unset=True, exclude_none=True))


class ScrappedTorrent(BaseModel):
    torrent: Torrent
    timestamp: int


class Cache:
    def __init__(self, ttl: int = 60 * 60):
        self.cache: dict[str, ScrappedTorrent] = {}
        self.ttl: int = ttl

    def clean(self) -> None:
        deadline: int = int(time()) - self.ttl
        self.cache = dict(
            filter(
                lambda torrent: torrent[1].timestamp > deadline,
                self.cache.items(),
            )
        )  # Filter out torrents older than ttl

    def update(self, torrents: list[Torrent]) -> None:
        timestamp: int = int(time())
        self.cache.update(
            {
                torrent.id: ScrappedTorrent(torrent=torrent, timestamp=timestamp)
                for torrent in torrents
            }
        )

    def get(self, torrent_id: str) -> Torrent | None:
        item = self.cache.get(torrent_id)
        if item:
            item.timestamp = int(time())  # Refresh timestamp
            return item.torrent
        return None
