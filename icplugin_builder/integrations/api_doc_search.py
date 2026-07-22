"""Web search fallback for API documentation when no spec is attached.

When a user doesn't attach an OpenAPI spec file, this module searches the web
for API documentation relevant to the plugin and action being implemented. It
fetches the most relevant page content and extracts endpoint details that can
be included in the LLM's code-generation prompt.

Uses a lightweight approach:
1. Search for the API documentation using DuckDuckGo's HTML interface (no API key required)
2. Fetch the top result's page content
3. Extract the relevant section for the specific action/endpoint

Results are cached on the session to avoid repeated searches for the same plugin.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["ApiDocSearchResult", "search_api_docs", "ApiDocCache"]

#: Maximum content length to include in the LLM prompt (characters).
MAX_DOC_CONTENT = 4000

#: User-Agent for web requests (identifies as the plugin builder tool).
_USER_AGENT = "InsightConnect-Plugin-Builder/1.0 (API doc lookup)"

#: Request timeout in seconds.
_TIMEOUT = 15


@dataclass
class ApiDocSearchResult:
    """The result of searching for API documentation for an action."""

    found: bool = False
    source_url: str = ""
    relevant_content: str = ""
    error: Optional[str] = None


@dataclass
class ApiDocCache:
    """Caches web-fetched API documentation per plugin to avoid repeated searches.

    Keyed by plugin_name, stores the raw page content so multiple actions in
    the same plugin reuse a single fetch.
    """

    _cache: Dict[str, str] = field(default_factory=dict)
    _urls: Dict[str, str] = field(default_factory=dict)

    def get(self, plugin_name: str) -> Optional[str]:
        """Return cached doc content for a plugin, or None."""
        return self._cache.get(plugin_name)

    def get_url(self, plugin_name: str) -> str:
        """Return the source URL for cached docs."""
        return self._urls.get(plugin_name, "")

    def store(self, plugin_name: str, content: str, url: str) -> None:
        """Cache fetched doc content for a plugin."""
        self._cache[plugin_name] = content
        self._urls[plugin_name] = url


def search_api_docs(
    plugin_name: str,
    plugin_description: str,
    action_name: str,
    action_description: str,
    *,
    cache: Optional[ApiDocCache] = None,
) -> ApiDocSearchResult:
    """Search the web for API documentation relevant to an action.

    Searches DuckDuckGo for the API docs, fetches the top result, and extracts
    content relevant to the specific action. Results are cached per plugin.

    Args:
        plugin_name: the plugin name (e.g. "rapid7_velociraptor").
        plugin_description: the plugin's description for search context.
        action_name: the action to find docs for (e.g. "get_client").
        action_description: the action's description.
        cache: optional cache to avoid repeated fetches for the same plugin.

    Returns:
        An ApiDocSearchResult with the found content or an error.
    """
    # Check cache first
    if cache is not None:
        cached_content = cache.get(plugin_name)
        if cached_content is not None:
            relevant = _extract_relevant_section(cached_content, action_name, action_description)
            return ApiDocSearchResult(
                found=bool(relevant),
                source_url=cache.get_url(plugin_name),
                relevant_content=relevant,
            )

    # Build a search query from the plugin/action context
    # Strip common prefixes like "rapid7_" for better search results
    clean_name = re.sub(r"^(rapid7|komand|icon)_", "", plugin_name)
    query = f"{clean_name} API documentation REST endpoints"

    try:
        url = _search_duckduckgo(query)
    except Exception as exc:
        logger.warning("API doc search failed for %s: %s", plugin_name, exc)
        return ApiDocSearchResult(found=False, error=f"Search failed: {exc}")

    if not url:
        return ApiDocSearchResult(found=False, error="No relevant API documentation found.")

    # Fetch the page content
    try:
        content = _fetch_page(url)
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return ApiDocSearchResult(found=False, source_url=url, error=f"Fetch failed: {exc}")

    # Cache the full page content for this plugin
    if cache is not None:
        cache.store(plugin_name, content, url)

    # Extract the relevant section for this action
    relevant = _extract_relevant_section(content, action_name, action_description)

    return ApiDocSearchResult(
        found=bool(relevant),
        source_url=url,
        relevant_content=relevant,
    )


def _search_duckduckgo(query: str) -> Optional[str]:
    """Search DuckDuckGo and return the URL of the first organic result.

    Uses the DuckDuckGo HTML page (no API key required) and parses the first
    result link. Returns None if no results are found.
    """
    encoded_query = urllib.parse.quote_plus(query)
    search_url = f"https://html.duckduckgo.com/html/?q={encoded_query}"

    req = urllib.request.Request(search_url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
        html = response.read().decode("utf-8", errors="replace")

    # Parse the first result URL from DuckDuckGo HTML results
    # Results are in <a class="result__a" href="...">
    matches = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)
    if not matches:
        # Try alternate pattern
        matches = re.findall(r'<a[^>]*class="[^"]*result[^"]*"[^>]*href="([^"]+)"', html)

    for match in matches:
        # DuckDuckGo wraps URLs in a redirect; extract the actual URL
        if "uddg=" in match:
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(match).query)
            actual = parsed.get("uddg", [None])[0]
            if actual:
                return actual
        elif match.startswith("http"):
            return match

    return None


def _fetch_page(url: str) -> str:
    """Fetch a web page and return its text content (HTML tags stripped)."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
        raw = response.read().decode("utf-8", errors="replace")

    # Strip HTML tags to get plain text
    text = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    # Decode HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    return text.strip()


def _extract_relevant_section(content: str, action_name: str, action_description: str) -> str:
    """Extract the portion of page content most relevant to the action.

    Looks for sections mentioning the action's keywords (e.g. endpoint paths,
    HTTP methods, the action's verbs) and returns a focused excerpt.
    """
    if not content:
        return ""

    # Build search terms from the action name and description
    action_words = action_name.replace("_", " ").split()
    search_terms = action_words + [w.lower() for w in action_description.split()[:5] if len(w) > 3]

    # Find the best matching window in the content
    content_lower = content.lower()
    best_start = 0
    best_score = 0

    # Slide a window across the content and score each position
    window_size = MAX_DOC_CONTENT
    step = 500

    for i in range(0, max(1, len(content) - window_size), step):
        window = content_lower[i : i + window_size]
        score = sum(window.count(term.lower()) for term in search_terms)
        # Bonus for HTTP method keywords near the action terms
        for method in ("get", "post", "put", "patch", "delete"):
            if method in window:
                score += 2
        # Bonus for URL path indicators
        if "/" in window and "api" in window:
            score += 5
        if score > best_score:
            best_score = score
            best_start = i

    if best_score == 0:
        # No good match found; return the beginning of the content as context
        return content[:MAX_DOC_CONTENT]

    return content[best_start : best_start + window_size].strip()
