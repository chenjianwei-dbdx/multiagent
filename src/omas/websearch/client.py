"""Web search & page fetch behind the ModelGateway (ADR 0002 extension).

Contract highlights (AGENTS.md hard borders apply):

- **Gateway first**: every call runs ``guard_web_endpoint(category=...)``
  *before* any transport is constructed — under ``local_only`` a non-loopback
  URL raises ``ModelGatewayError`` while the process is still fully offline;
  no retry, no fallback afterwards.
- **No system proxy**: ``httpx.Client(trust_env=False)`` so no environment
  proxy silently relays traffic elsewhere.
- **Fail-closed on shape**: URLs must be http(s) with a host and without
  embedded credentials (rejected before any request).
- **Bounded reads**: page fetches stream with a 2 MiB hard byte cap and are
  truncated to ``max_chars``; search responses are parsed defensively.
- **Errors stay metadata-only**: exception messages carry status codes and
  exception class names — never response bodies (I3 spirit; bodies never
  reach logs, the ledger or the user).

Search results are judgement input only (planner context); they are never a
source for final body text (I2 — Renderer has no free-text entry).
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final
from urllib.parse import urljoin, urlparse

import httpx

from omas.config.settings import SearchSettings
from omas.domain.errors import OmasError
from omas.security.model_gateway import ModelGateway

#: Search providers understood by :class:`SearchClient` (bing Needs no key).
SEARCH_PROVIDERS: Final[frozenset[str]] = frozenset({"tavily", "searxng", "generic", "bing"})

#: Queries longer than this are truncated (never rejected) before transport.
QUERY_MAX_CHARS: Final[int] = 400

#: Provider request timeout (search and fetch default).
DEFAULT_TIMEOUT_S: Final[float] = 20.0

#: Hard byte cap for one streamed page fetch (excess bytes are dropped).
MAX_PAGE_BYTES: Final[int] = 2 * 1024 * 1024  # 2 MiB

#: Maximum redirects followed per page fetch (cross-origin included).
MAX_REDIRECTS: Final[int] = 3

#: Browser User-Agent for HTML scraping providers (Bing 302s non-browser UAs).
BROWSER_UA: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})


class WebSearchError(OmasError):
    """A web search/fetch attempt failed (policy-allowed but transport/status/shape).

    Messages carry provider/host/status/class names only — never a response
    body, never page content.
    """

    code = "WEB_SEARCH_FAILED"


@dataclass(frozen=True, slots=True)
class WebResult:
    """One search hit. ``snippet`` is provider-supplied summary text."""

    title: str
    url: str
    snippet: str


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Parsed provider response; ``query`` is the (truncated) query actually sent."""

    results: tuple[WebResult, ...]
    provider: str
    query: str


# --------------------------------------------------------------------- HTML


class _TextExtractor(HTMLParser):
    """Stdlib-only tag stripper: drops markup and ``script``/``style`` bodies.

    Entity/character references are resolved by ``HTMLParser`` itself
    (``convert_charrefs=True``); no new dependency is introduced.
    """

    _SKIPPED_BODIES: Final[frozenset[str]] = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED_BODIES:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED_BODIES and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _strip_html(markup: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    return extractor.text()


# ---------------------------------------------------------------- URL checks


def _validate_url_shape(url: str) -> None:
    """Reject non-http(s), host-less or credential-bearing URLs (ValueError).

    This mirrors the gateway's shape rules so callers get the mandated
    ``ValueError`` for malformed input URLs regardless of policy.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"only http/https URLs are supported, got scheme {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("URL has no host component")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not embed user credentials")


# ------------------------------------------------------------ search client


class SearchClient:
    """Provider-dispatching search client; disabled when unconfigured.

    ``settings=None`` (models.toml without a ``[search]`` section) keeps the
    client permanently disabled. Even when configured, every ``search`` call
    re-checks the endpoint through the gateway first: under ``local_only``
    only loopback instances (self-hosted SearXNG) pass.
    """

    def __init__(self, settings: SearchSettings | None, gateway: ModelGateway) -> None:
        self._settings = settings
        self._gateway = gateway

    @property
    def enabled(self) -> bool:
        return self._settings is not None

    def search(self, query: str, *, max_results: int | None = None) -> SearchOutcome:
        """Run one provider search and parse it into :class:`SearchOutcome`.

        Raises :class:`ModelGatewayError` (policy denial, before any network
        byte) or :class:`WebSearchError` (disabled, bad query, transport
        failure, non-2xx status, malformed payload).
        """
        settings = self._settings
        if settings is None:
            raise WebSearchError("web search is disabled: models.toml has no [search] section")
        base_url = settings.base_url.rstrip("/")
        # Policy gate first — a local_only denial must happen before any HTTP.
        self._gateway.guard_web_endpoint(base_url, category="web_search")

        provider = settings.provider.strip().lower()
        if provider not in SEARCH_PROVIDERS:
            raise WebSearchError(
                f"unsupported search provider {settings.provider!r}; "
                f"expected one of {sorted(SEARCH_PROVIDERS)}"
            )

        normalized_query = query.strip()
        if not normalized_query:
            raise WebSearchError("search query must not be empty")
        normalized_query = normalized_query[:QUERY_MAX_CHARS]

        limit = settings.max_results if max_results is None else max(1, min(10, max_results))

        if provider == "tavily":
            payload = self._search_tavily(settings, base_url, normalized_query, limit)
        elif provider == "searxng":
            payload = self._search_searxng(base_url, normalized_query)
        elif provider == "bing":
            payload = self._search_bing(base_url, normalized_query, limit)
        else:
            payload = self._search_generic(base_url, normalized_query, limit)
        return SearchOutcome(
            results=_parse_results(payload, limit=limit),
            provider=provider,
            query=normalized_query,
        )

    # ------------------------------------------------------------ providers

    def _search_tavily(
        self, settings: SearchSettings, base_url: str, query: str, limit: int
    ) -> dict[str, object]:
        headers: dict[str, str] = {}
        if settings.api_key_env:
            key = os.environ.get(settings.api_key_env, "")
            if not key:
                raise WebSearchError(
                    f"API key environment variable {settings.api_key_env} is not set"
                )
            headers["Authorization"] = f"Bearer {key}"
        response = self._request(
            "POST",
            f"{base_url}/search",
            headers=headers,
            json={"query": query, "max_results": limit},
        )
        return _json_payload(response)

    def _search_searxng(self, base_url: str, query: str) -> dict[str, object]:
        response = self._request(
            "GET", f"{base_url}/search", params={"q": query, "format": "json"}
        )
        # searxng's format=json returns search metadata plus results[];
        # the result cap is applied client-side in _parse_results.
        return _json_payload(response)

    def _search_generic(self, base_url: str, query: str, limit: int) -> dict[str, object]:
        response = self._request(
            "POST", base_url, json={"query": query, "max_results": limit}
        )
        return _json_payload(response)

    def _search_bing(self, base_url: str, query: str, limit: int) -> dict[str, object]:
        """Bing 通用搜索页（HTML 免 key 方案）：解析 b_algo 自然结果块。

        仅供 llm_allowed：www.bing.com 非本地地址，local_only 时网关在构造
        传输之前即拒绝。Bing 对非浏览器 UA 返回 302 跳转（如 www→cn.bing.com），
        故带浏览器 UA 并跟随跳转，且对最终落地 URL 再过一次网关。响应体只在这
        里解析成结构化结果，错误信息不含正文。
        """
        response = self._request(
            "GET",
            f"{base_url}/search",
            params={"q": query, "count": str(max(10, limit * 2))},
            headers={
                "User-Agent": BROWSER_UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
            final_url_category="web_search",
        )
        markup = _text_body(response)
        payload = _parse_bing_html(markup, limit=limit)
        if not payload["results"]:
            raise WebSearchError(
                "bing 搜索页未解析到自然结果（可能为验证页或结构变更）"
            )
        return payload

    # ----------------------------------------------------------------- http

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: object = None,
        params: dict[str, str] | None = None,
        follow_redirects: bool = False,
        final_url_category: str | None = None,
    ) -> httpx.Response:
        """One guarded, proxy-ignoring HTTP request; errors stay body-free."""
        try:
            with httpx.Client(
                timeout=DEFAULT_TIMEOUT_S, trust_env=False, follow_redirects=follow_redirects
            ) as client:
                response = client.request(
                    method, url, headers=headers, json=json, params=params
                )
                if final_url_category:
                    # 跳转链的落地页必须重新过网关，防止静默落到别的域
                    self._gateway.guard_web_endpoint(
                        str(response.url), category=final_url_category
                    )
                _require_success(response, what="search provider")
                return response
        except httpx.TimeoutException as exc:
            raise WebSearchError(f"search request timed out ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            raise WebSearchError(f"search request failed ({type(exc).__name__})") from exc


def _json_payload(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise WebSearchError("search provider response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise WebSearchError("search provider response is not a JSON object")
    return payload


def _text_body(response: httpx.Response) -> str:
    """Decode an HTML/JSON-agnostic response body to text (gateway already ran)."""
    return response.text


_BING_BLOCK = re.compile(r'<li\s+class="b_algo".*?</li>', re.DOTALL)
_BING_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_BING_ANY_LINK = re.compile(r'href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_BING_SNIPPET = re.compile(r'<p[^>]*>(.*?)</p>', re.DOTALL)


def _parse_bing_html(markup: str, *, limit: int) -> dict[str, object]:
    """把 Bing SERP HTML 解析成 results 数组（b_algo 自然结果）。

    纯离线函数，可单测；解析失败返回空 results 由调用方决定是否报错。
    """
    results: list[dict[str, str]] = []
    for block in _BING_BLOCK.findall(markup)[: max(limit, 1)]:
        link = _BING_LINK.search(block) or _BING_ANY_LINK.search(block)
        if not link:
            continue
        url = html.unescape(link.group(1))
        title = html.unescape(_strip_tags(link.group(2))).strip()
        snippet_match = _BING_SNIPPET.search(block)
        snippet = (
            html.unescape(_strip_tags(snippet_match.group(1))).strip()
            if snippet_match
            else ""
        )
        if not url or not title:
            continue
        results.append({"title": title, "url": url, "content": snippet})
    return {"results": results}


def _strip_tags(fragment: str) -> str:
    """去标签并压缩空白（片段级，不解析整个文档）。"""
    text = re.sub(r"<[^>]+>", "", fragment)
    return re.sub(r"\s+", " ", text)


def _parse_results(payload: dict[str, object], *, limit: int) -> tuple[WebResult, ...]:
    raw = payload.get("results")
    if not isinstance(raw, list):
        raise WebSearchError("search provider response has no 'results' array")
    results: list[WebResult] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        snippet = item.get("content") or item.get("snippet") or ""
        results.append(
            WebResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(snippet),
            )
        )
    return tuple(results)


# --------------------------------------------------------------- page fetch


def fetch_page(
    url: str,
    *,
    gateway: ModelGateway,
    max_chars: int = 20000,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Fetch one page as bounded plain text (judge input, never body source).

    - ``ValueError`` for non-http(s), host-less or credential-bearing URLs
      (before the gateway, before any transport)
    - ``ModelGatewayError`` when the policy denies this host (per redirect
      hop, so a loopback page cannot bounce traffic to a remote host)
    - ``WebSearchError`` for >3 redirects, non-http(s) redirect targets,
      non-2xx statuses, unsupported content types and transport failures
    - Body streams under a 2 MiB byte cap; text-ish payloads are tag-stripped
      and truncated to ``max_chars``
    """
    _validate_url_shape(url)
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        # Re-check every hop: policy applies to the URL actually requested.
        gateway.guard_web_endpoint(current, category="web_fetch")
        response, body = _stream_get(current, timeout_s=timeout_s)
        if response.status_code in _REDIRECT_STATUSES:
            location = response.headers.get("location")
            if not location:
                raise WebSearchError(
                    f"page fetch got HTTP {response.status_code} without a Location header"
                )
            target = urljoin(current, location)
            try:
                _validate_url_shape(target)
            except ValueError as exc:
                raise WebSearchError(
                    f"redirect target is not an acceptable http(s) URL: {exc}"
                ) from exc
            current = target
            continue
        _require_success(response, what="page fetch")
        content_type = response.headers.get("content-type", "")
        media = content_type.split(";", 1)[0].strip().lower()
        if not _is_text_media(media):
            raise WebSearchError(f"不支持的内容类型: {media[:60] or '(none)'}")
        text = _decode_body(body, content_type)
        if not text:
            return ""
        return _strip_html(text)[:max_chars]
    raise WebSearchError(f"page fetch exceeded {MAX_REDIRECTS} redirects")


def _stream_get(url: str, *, timeout_s: float) -> tuple[httpx.Response, bytes]:
    """Stream one GET, stopping at the 2 MiB byte cap (excess dropped)."""
    collected: list[bytes] = []
    total = 0
    try:
        with httpx.Client(
            timeout=timeout_s, trust_env=False, follow_redirects=True
        ) as client, client.stream(
            "GET", url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"}
        ) as response:
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_PAGE_BYTES:
                    remaining = MAX_PAGE_BYTES - (total - len(chunk))
                    if remaining > 0:
                        collected.append(chunk[:remaining])
                    break
                collected.append(chunk)
            return response, b"".join(collected)
    except httpx.TimeoutException as exc:
        raise WebSearchError(f"page fetch timed out ({type(exc).__name__})") from exc
    except httpx.HTTPError as exc:
        raise WebSearchError(f"page fetch failed ({type(exc).__name__})") from exc


def _require_success(response: httpx.Response, *, what: str) -> None:
    if not 200 <= response.status_code < 300:
        # Status code only — the body must never enter an error message.
        raise WebSearchError(f"{what} returned HTTP {response.status_code}")


def _is_text_media(media: str) -> bool:
    if not media:
        return True  # absent Content-Type: assume text, cap+truncate still apply
    return (
        media.startswith("text/")
        or media == "application/json"
        or media == "application/xhtml+xml"
    )


def _decode_body(body: bytes, content_type: str) -> str:
    """Decode with the declared charset, falling back to UTF-8 (replace)."""
    charset: str | None = None
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset" and value:
            charset = value.strip("'\" ")
            break
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


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
