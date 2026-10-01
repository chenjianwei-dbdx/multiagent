"""Gateway-guarded web search/fetch backend (ADR 0002 capability extension).

All egress funnels through :class:`~omas.security.model_gateway.ModelGateway`
(``web_search`` / ``web_fetch`` categories): ``local_only`` allows loopback
instances only; everything else needs an explicit ``llm_allowed`` policy.
HTTP is httpx (already in the locked dependency set) — no new dependencies.
"""

from omas.websearch.client import (
    MAX_PAGE_BYTES,
    QUERY_MAX_CHARS,
    SEARCH_PROVIDERS,
    SearchClient,
    SearchOutcome,
    WebResult,
    WebSearchError,
    fetch_page,
)

__all__ = [
    "MAX_PAGE_BYTES",
    "QUERY_MAX_CHARS",
    "SEARCH_PROVIDERS",
    "SearchClient",
    "SearchOutcome",
    "WebResult",
    "WebSearchError",
    "fetch_page",
]
