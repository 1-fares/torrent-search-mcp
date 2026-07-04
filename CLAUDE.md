# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`torrent-search-mcp` is a Python package that exposes a torrent search API across multiple sources (ThePirateBay, Nyaa, YggTorrent via `ygg.gratis`, La Cale) through three surfaces from the same code:

1. **Python library** (`from torrent_search import torrent_search_api`)
2. **MCP server** (FastMCP, transports: `stdio` / `sse` / `streamable-http`)
3. **FastAPI HTTP server** (`/torrent/search`, `/torrent/{id}`, `/sources`, `/docs`)

Entry point is `torrent_search.__main__:cli`, dispatched on `--mode`. Default mode is `stdio` (MCP).

## Common commands

Local dev uses `uv` (see `dev.sh` for the full pre-commit chain):

```bash
uv sync --locked                              # install deps from uv.lock
uvx playwright install --with-deps chromium   # required by crawl4ai (skipped at runtime if cached)
uv run -m torrent_search                      # run MCP server (stdio)
uv run -m torrent_search --mode fastapi       # run FastAPI server (port 8000)
uv run -m torrent_search --mode sse           # run MCP over SSE
uv run -m torrent_search --force-install-browsers  # force a Playwright reinstall
uv run ruff format && uv run ruff check --fix # format + lint
uv run ty check                               # type check (uses `ty`, not mypy)
uv run pytest                                 # run all tests
uv run pytest torrent_search/test_mcp_server.py::test_search_torrents  # single test
./dev.sh                                      # lock + sync + format + lint + typecheck + test
```

Tool config lives entirely in `pyproject.toml`: `[tool.ruff]`, `[tool.ruff.lint]`, `[tool.pytest.ini_options]`. There is no separate `mypy.ini` / `pytest.ini`.

Tests are integration tests that hit live torrent sites through real Chromium (via crawl4ai/playwright), so they require network and an installed browser. There are no unit tests with mocks. Tests no longer hardcode magnet IDs: each `get_torrent` test does a fresh search and resolves the first ID it sees from each source, skipping the case if that source returned nothing for the query.

Docker: `docker compose up --build -d` brings up both `torrent-search-mcp` (port 8000, SSE mode) and `ygg-api` (port 8715, the YggTorrent backend the `fr_torrent_search` dependency talks to). The container always mounts the torrents volume at `/app/torrents` and overrides `FOLDER_TORRENT_FILES` accordingly, regardless of what `.env` says — that env var only takes effect outside Docker. Compose pins Quad9 DNS (`9.9.9.9`) to dodge ISP-level DNS blocks of torrent sites.

## Architecture

The dependency direction is roughly: surfaces (`mcp_server.py`, `fastapi_server.py`, `__main__.py`) → `wrapper.api_client.TorrentSearchApi` → either `wrapper.scraper` (English sources) or the external `fr_torrent_search` package (French sources). All three surfaces import the same `TorrentSearchApi` instance pattern, so behavior changes belong in the wrapper, not in surface files.

### Three scraping paths under one API

`TorrentSearchApi.search_torrents()` fans out concurrent tasks via `asyncio.gather`. The scraper-side dispatcher in `wrapper/scraper.py::search_torrents` then routes each English source to one of two backends:

- **`thepiratebay.org`** goes through `wrapper/tpb.py` directly to the `apibay.org/q.php` JSON API — no Chromium, no regex pipeline. The HTML at thepiratebay.org is just a JS shell that calls apibay; we cut out the middle. `fetch_tpb` builds magnet links locally from each entry's `info_hash` plus the same tracker set TPB's static main.js embeds, so the resulting links are byte-identical to what the official site would hand out. Categories come from a static int→label map (`_TPB_TOP_CATEGORIES` × `_TPB_SUBCATEGORIES`) lifted from main.js. Typical wall-time is ~250-800ms (network-bound on apibay).
- **`nyaa.si`** still goes through `wrapper/scraper.py`'s `crawl4ai` + headless Chromium path, then through the long ordered pipeline of regex `FILTERS` and `REPLACERS` to coerce nyaa's markdown table into a `;`-separated CSV that `extract_torrents` parses into `Torrent` models. The replacer order matters; entries note "must run BEFORE X" / "must run AFTER X". When fixing parsing for nyaa, use `WEBSITES["nyaa.si"]["exclude_patterns"]` to opt out of replacers, rather than weakening the regex. Rows that fail validation are logged at DEBUG (not silently dropped). The TPB-specific `thepiratebay_*` replacers are still in `REPLACERS` for now — they're harmless because nyaa's `exclude_patterns` opts them out, but they could be deleted if no other source ever needs HTML-table parsing.
- **French sources** (YggTorrent, La Cale) come from the external `fr-torrent-search-mcp` package (`fr_torrent_api`), called via `to_thread` because it is sync. The set of FR sources is discovered at `TorrentSearchApi.__init__` time from `fr_torrent.api_names`. They are gated by `EXCLUDE_FR_SOURCES` and the per-source `*_ENABLE` env vars in `.env.example`.

`wrapper/scraper.py::SCRAPER_SOURCES` is the authoritative list of English sources (currently `("thepiratebay.org", "nyaa.si")`) and is what `TorrentSearchApi.__init__` reads to seed `self.sources`. `WEBSITES` only lists Chromium-handled sources (just nyaa today). Adding a JSON-API-backed source means: write `wrapper/<source>.py` with a `fetch_*` function, add it to `SCRAPER_SOURCES`, and wire a branch into `wrapper/scraper.py::search_torrents`.

The two task results are gathered with labels (`("en", coro)` / `("fr", coro)`) so the merge step doesn't depend on positional indexing into the `gather` result list. Results are sorted by `(seeders, leechers)` descending — seeders dominate, leechers only break ties — then truncated to `max_items`, then `prepend_info(query, max_items)` rewrites each `id`.

### Crawler + HTTP client lifecycle

Two long-lived resources live in `wrapper/scraper.py`:

- **`crawler`** — module-level `AsyncWebCrawler`, used only by the nyaa path. Started lazily via `ensure_crawler_started()` (idempotent, lock-guarded) on first nyaa request. **Not** started by the lifespan — that way a missing/broken Playwright install can't block server startup if the user only ever queries TPB.
- **`_http_client`** — module-level `httpx.AsyncClient`, used by the TPB path. Started by `crawler_lifespan` on app boot (cheap, ~5MB) so TPB requests have zero per-call setup overhead.

`crawler_lifespan` is wired into both `mcp_server.mcp` (via `FastMCP(lifespan=...)`) and `fastapi_server.app` (via `FastAPI(lifespan=...)`). It owns both close functions in `finally`. Direct library users who never start the lifespan still work — `get_http_client()` and `ensure_crawler_started()` are both idempotent and safe to call on demand.

If you add a new entry point, either wrap it in `crawler_lifespan` or rely on the lazy ensure functions. Do not reintroduce `async with crawler:` inside `scrape_torrents`, which would launch and tear down Chromium on every call.

### Torrent ID encoding

`Torrent.id` is built in two stages:

1. `Torrent.format()` sets `id = f"{source}-{ref_id}"`, where `ref_id` is the source's native ID or the first 10 hex chars of `sha256(magnet_link)`.
2. `prepend_info()` then prepends a base62-zlib-compressed copy of the original query plus the `max_items` value: `id = f"{compressed_query}-{max_items}-{source}-{ref_id}"`.

`Torrent.extract_info(torrent_id, known_sources=...)` is the inverse. Pass the live `TorrentSearchApi.sources` list as `known_sources` so the source name is matched by longest prefix instead of being recovered by a blind `split("-")`. This protects against future source names that contain dashes. Without `known_sources`, it falls back to `rpartition("-")`. The test fixtures no longer hardcode IDs (they search live and reuse what comes back), so changing the ID format only requires updating `prepend_info` + `extract_info`.

### Caching

Two layers, both in-process:

- `aiocache.cached(ttl=300)` on `search_torrents`, keyed by `(query, max_items)` via `key_builder`. 5-minute TTL.
- `TorrentSearchApi.cache` (a `wrapper.models.Cache`, 1-hour TTL) maps `torrent_id → Torrent` so `get_torrent` can resolve magnet/file links without re-scraping. Updated on every search, cleaned on every search and `get_torrent` call. The old uppercase `CACHE` attribute remains as a property alias for external callers.

There is no shared cache across processes; running multiple FastAPI workers means each has its own cache.

### Configuration is read at `TorrentSearchApi.__init__`

Env vars are no longer captured at module import. `TorrentSearchApi()` reads them once and stores them as instance attributes (`include_fr_sources`, `exclude_sources`, `fr_sources`, `sources`, `folder_torrent_files`). This makes the wrapper testable with overridden config and avoids creating the torrent-files directory just by importing the package. The torrent-files directory is created lazily on first FR `get_torrent`, not at import.

Vars (all optional, loaded by `python-dotenv` from `.env` at MCP server import time):

- `INCLUDE_LINKS=true` — include magnet links/torrent file paths in MCP `search_torrents` output. Defaults to false to save tokens; the MCP tool serializes via `model_dump(exclude={"magnet_link","torrent_file"})` rather than `deepcopy`-and-clear.
- `EXCLUDE_SOURCES=nyaa.si,...` — comma-separated source names to drop.
- `EXCLUDE_FR_SOURCES=true` — skip the French scraper entirely; also avoids initializing `fr_torrent`.
- `FOLDER_TORRENT_FILES=/path` — where `.torrent` files are saved (default `./torrents`). Inside Docker, compose forces this to `/app/torrents`.
- `LA_CALE_API_KEY`, `YGG_LOCAL_API`, `LA_CALE_ENABLE`, `YGG_ENABLE`, `LA_CALE_DOMAIN` — consumed by the `fr_torrent_search` dependency, not by this repo's code directly.

### MCP tool prompt is part of the contract

The docstring on `mcp_server.search_torrents` is a structured prompt that tells the LLM client how to construct queries, how to rank results, and how to respond. It is not just developer documentation. Editing it changes runtime behavior of any MCP client calling this tool.

### Playwright install on startup

`__main__.cli()` checks for an existing chromium build under `$PLAYWRIGHT_BROWSERS_PATH` (or `~/.cache/ms-playwright`) and only runs `playwright install` if no `chromium*` directory exists. Use `--force-install-browsers` to override. This avoids a multi-second install subprocess on every cold start of the stdio MCP server.

## Release process

Release is tag-driven. Pushing a tag to GitHub runs `.github/workflows/python-package-ci.yml` which: tests → builds wheel/sdist → publishes to PyPI (trusted publisher / OIDC, no token) → builds and pushes Docker image to Docker Hub using `Dockerfile.publish`. Bump `version` in `pyproject.toml` and update `CHANGELOG.md` (managed by `git-cliff`, config in `cliff.toml`) before tagging.
