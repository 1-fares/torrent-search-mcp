import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from re import DOTALL, MULTILINE, Pattern, sub
from re import compile as re_compile
from time import time
from typing import Any
from urllib.parse import quote

import httpx
from crawl4ai import AsyncWebCrawler, CacheMode
from crawl4ai.async_configs import BrowserConfig, CrawlerRunConfig
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from pydantic import ValidationError

from .models import Torrent
from .tpb import SOURCE as TPB_SOURCE
from .tpb import fetch_tpb

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Crawler Configuration
BROWSER_CONFIG = BrowserConfig(
    browser_type="chromium",
    headless=True,
    text_mode=True,
    light_mode=True,
)
DEFAULT_MD_GENERATOR = DefaultMarkdownGenerator(
    options=dict(
        ignore_images=True,
        ignore_links=False,
        skip_internal_links=True,
        escape_html=True,
    )
)
DEFAULT_CRAWLER_RUN_CONFIG = CrawlerRunConfig(
    markdown_generator=DEFAULT_MD_GENERATOR,
    remove_overlay_elements=True,
    exclude_social_media_links=True,
    excluded_tags=["header", "footer", "nav"],
    remove_forms=True,
    cache_mode=CacheMode.DISABLED,
)

# Websites Configuration
FILTERS: dict[str, Pattern[str]] = {
    "full_links": re_compile(r"(http|https|ftp):[/]{1,2}[a-zA-Z0-9.]+[a-zA-Z0-9./?=+~_\-@:%#&]*"),
    "backslashes": re_compile(r"\\"),
    "local_links": re_compile(r"(a href=)*(<|\")\/[a-zA-Z0-9./?=+~()_\-@:%#&]*(>|\")* *"),
    "some_texts": re_compile(r' *"[a-zA-Z ]+" *'),
    "empty_angle_brackets": re_compile(r" *< *> *"),
    "empty_curly_brackets": re_compile(r" *\{ *\} *"),
    "empty_parenthesis": re_compile(r" *\( *\) *"),
    "empty_brackets": re_compile(r" *\[ *\] *"),
    "tags": re_compile(
        r"<img[^>]*>|<a[^>]*>(?:alt|src)=|(?<=<a )(?:alt|src)=|(?<=<img )(?:alt|src)"
    ),
    "input_elements": re_compile(r"<input[^>]*>"),
    "date": re_compile(r'<label title=("[a-zA-Z0-9()+: ]+"|>)'),
    # ThePirateBay specific - remove HTML tags but preserve content
    "html_tags": re_compile(r"<[^>]+>"),
    # ThePirateBay - remove ol tag attributes and gt entity
    "ol_attributes": re_compile(r' class="view-single"'),
}
REPLACERS: dict[str, tuple[Pattern[str], str | Callable[[Any], str]]] = {
    # ThePirateBay specific fixes - must run BEFORE single_angle_bracket
    # Step 1: Extract magnet links from anchor tags (these are special - we keep the URL)
    "thepiratebay_extract_magnet": (
        # Pattern matches: <a href="magnet:?xt=urn:btih:...">...</a>
        # Replace with just the magnet URL wrapped in >...> so it survives tag removal
        re_compile(r'<a[^>]*href="(magnet:\?[^"]*)"[^>]*>[^<]*(?:<img[^>]*>)?(?:&nbsp;)*</a>'),
        r">\1>",
    ),
    # Step 2: For non-magnet anchor tags, keep the text content and remove just the tags
    # Pattern: <a href="...">Text</a> -> Text
    "thepiratebay_extract_anchor_text": (
        re_compile(r"<a[^>]*>([^<]*)</a>"),
        r"\1",
    ),
    # Step 3: Add newlines between list items BEFORE removing closing tags
    # This ensures each torrent entry is on its own line
    "thepiratebay_add_newlines": (
        re_compile(r"</li>\s*<li"),
        "</li>\n<li",
    ),
    # Step 4: Replace the header row
    "thepiratebay_header": (
        # Replace the list-header li element with our header line
        re_compile(r'<li class="list-header">.*?</li>', DOTALL),
        '<li class="list-header">category>filename>date>magnet_link>size>seeders>leechers>uploader</li>',
    ),
    # Step 5: Remove img tags completely (they're just icons)
    "thepiratebay_remove_img_tags": (
        re_compile(r"<img[^>]*>"),
        "",
    ),
    # Step 5: Convert closing tags to separators (but NOT </a> since we already removed them)
    "thepiratebay_remove_html": (
        re_compile(r"</(span|li|div|section|ol|label)[^>]*>"),
        ";",
    ),
    # Step 6: Remove all remaining opening HTML tags
    "thepiratebay_remove_open_tags": (
        re_compile(r"<[^/][^>]*>"),
        "",
    ),
    # Step 7: Convert remaining > to ; for CSV
    "thepiratebay_to_csv": (
        re_compile(r">"),
        ";",
    ),
    # Step 8: Clean up multiple semicolons
    "thepiratebay_normalize_separators": (
        re_compile(r";{2,}"),
        ";",
    ),
    # Step 9: Remove leading/trailing semicolons from lines
    "thepiratebay_trim_separators": (
        # Remove leading and trailing semicolons from each line (but NOT newlines)
        re_compile(r"^;+|;+$", MULTILINE),
        "",
    ),
    # Step 10: Fix category formatting (convert "Category; - ;Subcategory" to "Category - Subcategory")
    "thepiratebay_fix_category": (
        re_compile(r";\s*-\s*;"),
        " - ",
    ),
    # Step 11: Clean whitespace around semicolons
    "thepiratebay_clean_whitespace": (
        re_compile(r"\s*;\s*"),
        ";",
    ),
    # Step 12: Remove empty lines
    "thepiratebay_remove_empty_lines": (
        # Remove empty lines
        re_compile(r"\n\s*\n+"),
        "\n",
    ),
    "thepiratebay_fix_gt_entity": (
        # Convert &gt; to - for category separator (after HTML is stripped)
        re_compile(r"&gt;"),
        "-",
    ),
    "thepiratebay_fix_amp_entity": (
        # Convert &amp; to & in magnet links
        re_compile(r"&amp;"),
        "&",
    ),
    "thepiratebay_fix_category_spacing": (
        # Fix category spacing at start of line: "Video-HD" or "Video -HD" -> "Video - HD"
        # Only matches the first occurrence (in the category field)
        # Group 1 captures everything before the dash (without trailing space), Group 2 is the capital letter
        re_compile(r"^([^;]*?)\s*-\s*([A-Z])", MULTILINE),
        r"\1 - \2",
    ),
    "thepiratebay_fix_double_semicolons": (
        # Fix remaining double semicolons (especially after long magnet links)
        re_compile(r";;"),
        ";",
    ),
    # Nyaa specific fixes - must run BEFORE to_csv and other general replacers
    "nyaa_remove_click_here_line": (
        re_compile(r"^\[Click he.*?\]\n"),
        "",
    ),
    # Replace header block - the header has markdown links between column names
    # Pattern: Category | Name | (optional junk) | Link | (optional)Size | (optional)Date |...
    "nyaa_header_block": (
        re_compile(
            r"Category\s*\|\s*Name\s*\|[^\|]*\|\s*Link\s*\|[^\|]*Size\s*\|[^\|]*Date[^\n]*\n[\|\s\-]+\n"
        ),
        "category | filename | magnet_link | size | date | seeders | leechers | downloads\n",
    ),
    # Remove the comments column entirely (it's the column between category and filename)
    "nyaa_remove_comments": (
        re_compile(r"\|\s*\[\s*\]\s*\([^)]*comments[^)]*\)"),
        "",
    ),
    # Extract category text from category column (pattern adjusted since URLs are filtered out)
    "nyaa_extract_category": (
        re_compile(r'\[\s*\]\s*\(\s*"([^"]+)"\s*\)'),
        r"\1",
    ),
    # Separate category from filename (add separator between them)
    "nyaa_separate_fields": (
        # Match: |  Category - Subcategory  [[Filename]](url)
        # The category ends at the "  [[" (two spaces then double bracket opening)
        re_compile(r"(\|\s*[^|]+\s-\s[^|]+?)(\[\[)"),
        r"\1;\2",
    ),
    # Extract magnet links from the Link column BEFORE stripping other links
    # Pattern matches: |  [ ]()  [ ](magnet:...)  | -> captures the magnet URL
    # Note: there may be empty []() before the actual magnet link
    "nyaa_extract_magnet_link": (
        re_compile(r"\|\s*(?:\[\s*\]\s*\(\s*\)\s*)*\[\s*\]\s*\(\s*(magnet:\?[^\s)]+)\s*\)\s*\|"),
        r";\1;",
    ),
    # Remove the title attribute from markdown links: ( "...") before the closing )
    # Must run BEFORE nyaa_strip_filename_brackets
    "nyaa_remove_link_title": (
        re_compile(r'\s*\("[^"]+"\)\)'),
        ")",
    ),
    # Clean filename - extract just the title from markdown link
    "nyaa_strip_filename_brackets": (
        # Step 1: Replace [[ with [
        re_compile(r"\[\["),
        "[",
    ),
    "nyaa_strip_filename_links": (
        # Step 2: Remove ]( "...") - the title attribute after URL removal
        # Matches ]( followed by a quote (start of title attribute)
        re_compile(r'\]\(\s*"[^"]*"\s*\)'),
        "",
    ),
    # Clean up remaining []() artifacts
    "nyaa_clean_artifacts": (
        re_compile(r"\[\s*\]\s*\(\s*\)"),
        "",
    ),
    # Fix leading semicolon in header
    "nyaa_fix_header": (
        re_compile(r"^;category"),
        "category",
    ),
    # Clean magnet link - extract just the magnet URL
    "nyaa_extract_magnet": (
        # Match: ;[;magnet:link; -> ;magnet:link;
        re_compile(r";\[;?(magnet:\?[^;]+)"),
        r";\1",
    ),
    # Basic text cleaning - runs after nyaa-specific fixes
    "weird_spaces": (re_compile(r"\u00A0"), " "),
    "spans": (re_compile(r"</?span>"), " | "),
    "weird spaced bars": (re_compile(r" *\|[ \|]+"), " | "),
    "double_quotes": (re_compile(r'"[" ]+'), ""),
    "single_angle_bracket": (re_compile(r"<|>"), ""),
    "gt": (re_compile("&gt;"), " -"),
    "amp": (re_compile("&amp;"), "&"),
    # Line formatting
    "bad_starting_spaced_bars": (re_compile(r"\n[\| ]+"), "\n"),
    "bad_ending_spaces": (re_compile(r" +\n"), "\n"),
    "duplicated_spaces": (re_compile(r" {2,4}"), " "),
    # Size formatting
    "size": (re_compile(r"([\d.]+[\s ]?[KMGT])i?B"), r"\1B"),
    "nyaa_fix_leading_spaces": (
        re_compile(r"\n\s+"),
        "\n",
    ),
    # Final formatting
    "to_csv": (re_compile(r" \| *"), ";"),
    # Fix leading semicolon in header and rows (must run AFTER to_csv)
    "nyaa_fix_leading_semicolon": (
        re_compile(r"^[;]+", MULTILINE),
        "",
    ),
    "nyaa_restore_link_titles": (
        # Restore | in link titles after CSV conversion
        re_compile(r"\x00PIPE\x00"),
        "|",
    ),
}
SCRAPER_SOURCES: tuple[str, ...] = (TPB_SOURCE, "nyaa.si")

# Sources handled by the crawl4ai + regex-pipeline path. thepiratebay.org used
# to be here but now goes through the apibay.org JSON API directly (see tpb.py),
# which is ~10x faster and skips Chromium entirely.
WEBSITES: dict[str, dict[str, str | list[str]]] = {
    "nyaa.si": dict(
        search="https://nyaa.si/?f=0&c=0_0&q={query}&s=seeders&o=desc",
        parsing="markdown",
        exclude_patterns=[
            "local_links",
            "thepiratebay_remove_img_tags",
            "thepiratebay_remove_html",
            "thepiratebay_remove_open_tags",
            "thepiratebay_to_csv",
            "empty_brackets",  # Don't remove [] - needed for Nyaa patterns
            "empty_parenthesis",  # Don't remove () - needed for Nyaa patterns
        ],
    ),
}

crawler = AsyncWebCrawler(config=BROWSER_CONFIG, always_bypass_cache=True)

_crawler_lock: asyncio.Lock | None = None
_crawler_started: bool = False
_http_client: httpx.AsyncClient | None = None
_http_lock: asyncio.Lock | None = None


async def ensure_crawler_started() -> None:
    """Lazily start the headless browser. Idempotent and safe to call concurrently."""
    global _crawler_lock, _crawler_started
    if _crawler_started:
        return
    if _crawler_lock is None:
        _crawler_lock = asyncio.Lock()
    async with _crawler_lock:
        if _crawler_started:
            return
        await crawler.start()
        _crawler_started = True


async def close_crawler() -> None:
    global _crawler_started
    if not _crawler_started:
        return
    try:
        await crawler.close()
    finally:
        _crawler_started = False


async def get_http_client() -> httpx.AsyncClient:
    """Lazily build a shared httpx client for the JSON-API scrapers."""
    global _http_client, _http_lock
    if _http_client is not None:
        return _http_client
    if _http_lock is None:
        _http_lock = asyncio.Lock()
    async with _http_lock:
        if _http_client is None:
            _http_client = httpx.AsyncClient(
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json,*/*"},
                follow_redirects=True,
            )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is None:
        return
    try:
        await _http_client.aclose()
    finally:
        _http_client = None


@asynccontextmanager
async def crawler_lifespan(_app: Any = None) -> AsyncIterator[None]:
    """Lifespan helper for FastAPI / FastMCP that owns scraper resources.

    Only the HTTP client is started eagerly: it's cheap and always needed
    (the TPB path uses it). Chromium is started lazily on first nyaa request
    so a missing or broken Playwright install can't block server startup.
    """
    await get_http_client()
    try:
        yield
    finally:
        await close_http_client()
        await close_crawler()


def parse_result(
    text: str,
    exclude_patterns: list[str] | None = None,
    max_chars: int = 5000,
) -> str:
    """
    Parse the text result.

    Args:
        text: The text to parse.
        exclude_patterns: List of patterns to exclude.
        max_chars: Maximum number of characters to return.

    Returns:
        The parsed text.
    """
    # For ThePirateBay, extract only the torrent list section
    if '<ol id="torrents"' in text:
        text = text.split('<ol id="torrents"', 1)[-1]
        text = text.split("</ol>", 1)[0] if "</ol>" in text else text
    else:
        # Fallback for other sources
        text = text.split("<li>", 1)[-1].replace("<li>", "")

    for name, pattern in FILTERS.items():
        if exclude_patterns and name in exclude_patterns:
            continue
        text = pattern.sub("", text)

    for name, replacer_config in REPLACERS.items():
        if exclude_patterns and name in exclude_patterns:
            continue
        pattern, replacement_str = replacer_config
        text = pattern.sub(replacement_str, text)

    if len(text) > max_chars:
        safe_truncate_pos = text.rfind("\n", 0, max_chars)
        text = text[:max_chars] if safe_truncate_pos == -1 else text[:safe_truncate_pos]
    text = sub(r"\n{2,}", "\n", text)
    return text.strip()


async def scrape_torrents(query: str, sources: list[str] | None = None) -> list[str]:
    """Run the crawl4ai pipeline against each WEBSITES entry the caller asked for."""
    await ensure_crawler_started()
    results_list: list[str] = []
    for source, data in WEBSITES.items():
        if sources is not None and source not in sources:
            continue
        url = str(data["search"]).format(query=quote(query))
        try:
            crawl_result: Any = await crawler.arun(  # type: ignore
                url=url, config=DEFAULT_CRAWLER_RUN_CONFIG
            )
            raw_content = (
                crawl_result.cleaned_html if data["parsing"] == "html" else crawl_result.markdown
            )
            processed_text = parse_result(
                raw_content,
                list(data.get("exclude_patterns", [])),
            )
            results_list.append(f"SOURCE -> {source}\n{processed_text}")
        except Exception as e:
            logger.warning("Error scraping %s for %r at %s: %s", source, query, url, e)
    return results_list


def extract_torrents(texts: list[str]) -> list[Torrent]:
    """
    Extract torrents from the parsed texts.

    Args:
        texts: The texts to extract torrents from.

    Returns:
        A list of torrent results.
    """
    torrents: list[Torrent] = []
    for text in texts:
        source, content = text.split("\n", 1)
        if "No results" in content:
            continue
        source = source[10:]
        data = content.splitlines()
        headers = data[0].split(";")
        for line in data[1:]:
            try:
                values = line.split(";")
                if len(values) > len(headers):
                    extra_count = len(values) - len(headers)
                    # If extra values are at the end (trailing empty), just trim them
                    if all(not v.strip() for v in values[len(headers) :]):
                        values = values[: len(headers)]
                    elif len(values) > 1:
                        # Extra values are in the middle - likely filename overflow
                        # For Nyaa: filename parts should be joined, magnet_link is separate
                        # values: [category, filename_p1, filename_p2, ..., magnet, size, date, ...]
                        # Join filename parts (indices 1 to 1+extra_count), keep rest as-is
                        filename_parts = values[1 : 1 + extra_count]
                        values[1] = " - ".join(filename_parts)
                        # Remove the extra filename parts (indices 2 to 2+extra_count-1)
                        del values[2 : 1 + extra_count]
                torrents.append(
                    Torrent.format(**dict(zip(headers, values, strict=False)), source=source)
                )
            except ValidationError as e:
                logger.debug("Skipped row from %s (validation): %s | row=%r", source, e, line)
                continue
            except Exception as e:
                logger.debug("Skipped row from %s (parse): %s | row=%r", source, e, line)
                continue
    return torrents


async def _search_tpb_path(query: str) -> list[Torrent]:
    client = await get_http_client()
    return await fetch_tpb(query, client)


async def _search_crawl4ai_path(query: str, sources: list[str]) -> list[Torrent]:
    if not sources:
        return []
    scraped_results = await scrape_torrents(query, sources=sources)
    return extract_torrents(scraped_results)


async def search_torrents(
    query: str,
    sources: list[str] | None = None,
) -> list[Torrent]:
    """Run all enabled English scrapers concurrently and merge their results."""
    start_time = time()
    enabled = (
        list(SCRAPER_SOURCES) if sources is None else [s for s in SCRAPER_SOURCES if s in sources]
    )

    tasks: list[Any] = []
    if TPB_SOURCE in enabled:
        tasks.append(_search_tpb_path(query))
    crawl_sources = [s for s in enabled if s != TPB_SOURCE and s in WEBSITES]
    if crawl_sources:
        tasks.append(_search_crawl4ai_path(query, crawl_sources))

    torrents: list[Torrent] = []
    if tasks:
        for batch in await asyncio.gather(*tasks):
            torrents.extend(batch)
    logger.info("Extracted %d torrents in %.2fs", len(torrents), time() - start_time)
    return torrents


if __name__ == "__main__":
    # To check if the scraper is working
    from asyncio import run

    from rich import print as pr

    found_torrents = run(search_torrents("attack"))
    found_sources: dict[str, int] = {}
    for torrent in found_torrents:
        pr(torrent)
        if torrent.source:
            found_sources[torrent.source] = found_sources.get(torrent.source, 0) + 1
    pr(found_sources)
