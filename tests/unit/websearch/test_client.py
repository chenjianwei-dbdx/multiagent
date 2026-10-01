"""Web search client: config, gateway gating, provider parsing, error paths.

Every test is offline: the ``httpx`` module reference inside
``omas.websearch.client`` is replaced by a recording stub, so a gateway
rejection can be proven to happen *before* any transport is constructed.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

import omas.websearch.client as ws_client
from omas.config.settings import SearchSettings, load_models_config
from omas.domain.errors import ModelGatewayError
from omas.domain.task import DataPolicy
from omas.security.model_gateway import ModelGateway
from omas.websearch import (
    MAX_PAGE_BYTES,
    QUERY_MAX_CHARS,
    SearchClient,
    WebResult,
    WebSearchError,
    fetch_page,
)
from omas.websearch.client import _TRANSPORT_RETRIES

# --------------------------------------------------------------- httpx stub


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        json_data: Any = None,
        chunks: list[bytes] | None = None,
        text: str | None = None,
        url: str = "https://www.bing.com/search",
    ) -> None:
        self.status_code = status_code
        self.headers = headers if headers is not None else {}
        self._json_data = json_data
        self._chunks = chunks if chunks is not None else []
        self.text = text if text is not None else ""
        self.url = url

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("no JSON body")
        return self._json_data

    def iter_bytes(self):
        yield from self._chunks


class _StreamContext:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    def __enter__(self) -> FakeResponse:
        return self._response

    def __exit__(self, *exc: object) -> bool:
        return False


class HttpxStub:
    """Installs a fake ``httpx`` module into the client; records every call."""

    def __init__(self) -> None:
        self.clients_opened = 0
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.client_kwargs: dict[str, Any] = {}
        self._responses: list[FakeResponse] = []
        self._index = 0
        self._exc: Exception | None = None
        self._exc_first_n = 0

    def install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        responses: list[FakeResponse] | None = None,
        exc: Exception | None = None,
        exc_first_n: int = 0,
    ) -> HttpxStub:
        if responses is not None:
            self._responses = list(responses)
        self._exc = exc
        self._exc_first_n = exc_first_n
        stub = self

        class _Client:
            def __init__(self, **kwargs: Any) -> None:
                stub.clients_opened += 1
                stub.client_kwargs = kwargs

            def __enter__(self) -> _Client:
                return self

            def __exit__(self, *exc_info: object) -> bool:
                return False

            def _next(self) -> FakeResponse:
                assert stub._index < len(stub._responses), "unexpected extra HTTP request"
                response = stub._responses[stub._index]
                stub._index += 1
                return response

            def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
                stub.requests.append((method, url, kwargs))
                if stub._exc is not None and (
                    stub._exc_first_n == 0 or len(stub.requests) <= stub._exc_first_n
                ):
                    raise stub._exc
                return self._next()

            def stream(self, method: str, url: str, **kwargs: Any) -> _StreamContext:
                stub.requests.append((method, url, kwargs))
                if stub._exc is not None:
                    raise stub._exc
                return _StreamContext(self._next())

        fake = types.SimpleNamespace(
            Client=_Client,
            TimeoutException=httpx.TimeoutException,
            HTTPError=httpx.HTTPError,
        )
        monkeypatch.setattr(ws_client, "httpx", fake)
        return self


# ----------------------------------------------------------------- fixtures


def _tavily_settings(**overrides: Any) -> SearchSettings:
    fields: dict[str, Any] = {
        "provider": "tavily",
        "base_url": "https://api.tavily.com",
        "api_key_env": "TAVILY_API_KEY",
        "max_results": 5,
    }
    fields.update(overrides)
    return SearchSettings.model_validate(fields)


def _searxng_settings(**overrides: Any) -> SearchSettings:
    fields: dict[str, Any] = {
        "provider": "searxng",
        "base_url": "http://127.0.0.1:8888",
        "api_key_env": None,
        "max_results": 5,
    }
    fields.update(overrides)
    return SearchSettings.model_validate(fields)


def _local_only() -> ModelGateway:
    return ModelGateway()


def _llm_allowed() -> ModelGateway:
    return ModelGateway(policy=DataPolicy.LLM_ALLOWED)


def _results_payload(*items: dict[str, str]) -> dict[str, Any]:
    return {"results": list(items)}


# ---------------------------------------------------------- config parsing


def test_models_toml_search_section_parsed(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        "[model]\n"
        'provider = "anthropic"\n'
        'model_id = "claude-sonnet-4-5"\n'
        "[search]\n"
        'provider = "tavily"\n'
        'base_url = "https://api.tavily.com"\n'
        'api_key_env = "TAVILY_API_KEY"\n'
        "max_results = 5\n",
        encoding="utf-8",
    )
    config = load_models_config(path)
    assert config.search is not None
    assert config.search.provider == "tavily"
    assert config.search.base_url == "https://api.tavily.com"
    assert config.search.api_key_env == "TAVILY_API_KEY"
    assert config.search.max_results == 5


def test_models_toml_without_search_defaults_to_none(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        '[model]\nprovider = "anthropic"\nmodel_id = "m"\n', encoding="utf-8"
    )
    config = load_models_config(path)
    assert config.model is not None
    assert config.search is None


def test_missing_models_file_still_has_no_search(tmp_path: Path) -> None:
    assert load_models_config(tmp_path / "absent.toml").search is None


def test_search_settings_bounds_and_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        SearchSettings(provider="tavily", base_url="https://x", max_results=0)
    with pytest.raises(ValidationError):
        SearchSettings(provider="tavily", base_url="https://x", max_results=11)
    with pytest.raises(ValidationError):
        SearchSettings.model_validate(
            {"provider": "tavily", "base_url": "https://x", "api_key": "literal"}
        )
    # api_key_env is optional; max_results defaults to 5.
    minimal = SearchSettings(provider="searxng", base_url="http://127.0.0.1:8888")
    assert minimal.api_key_env is None
    assert minimal.max_results == 5


# ---------------------------------------------------------- gateway gating


def test_local_only_rejects_remote_search_before_http(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = HttpxStub().install(monkeypatch)
    client = SearchClient(_tavily_settings(), _local_only())
    with pytest.raises(ModelGatewayError) as excinfo:
        client.search("weekly report")
    assert "联网能力需要 llm_allowed" in str(excinfo.value)
    assert stub.clients_opened == 0
    assert stub.requests == []


def test_local_only_rejects_remote_fetch_before_http(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = HttpxStub().install(monkeypatch)
    with pytest.raises(ModelGatewayError) as excinfo:
        fetch_page("https://example.com/page", gateway=_local_only())
    assert "联网能力需要 llm_allowed" in str(excinfo.value)
    assert stub.clients_opened == 0
    assert stub.requests == []


def test_local_only_allows_loopback_search_and_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(
                json_data=_results_payload(
                    {"title": "S", "url": "http://x", "snippet": "sn"}
                )
            ),
            FakeResponse(
                headers={"content-type": "text/plain"}, chunks=[b"loopback page"]
            ),
        ],
    )
    outcome = SearchClient(_searxng_settings(), _local_only()).search("hello")
    assert outcome.results == (WebResult("S", "http://x", "sn"),)
    text = fetch_page("http://127.0.0.1:8888/page", gateway=_local_only())
    assert text == "loopback page"
    assert stub.clients_opened == 2


def test_llm_allowed_allows_remote_and_reason_records_category() -> None:
    decision = _llm_allowed().check_web_endpoint(
        "https://api.tavily.com", category="web_search"
    )
    assert decision.allowed
    assert "web_search" in decision.reason
    fetch_decision = _llm_allowed().check_web_endpoint(
        "https://example.com/a", category="web_fetch"
    )
    assert fetch_decision.allowed
    assert "web_fetch" in fetch_decision.reason


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "http:///missing-host", "https://user:pw@example.com/x", ""],
)
def test_check_web_endpoint_rejects_malformed_under_llm_allowed(url: str) -> None:
    decision = _llm_allowed().check_web_endpoint(url, category="web_fetch")
    assert not decision.allowed


def test_check_web_endpoint_rejects_unknown_category() -> None:
    with pytest.raises(ValueError, match="category"):
        _llm_allowed().check_web_endpoint("https://example.com", category="email")


# ---------------------------------------------------------- provider parse


def test_tavily_search_request_and_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-123")
    stub = HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(
                json_data=_results_payload(
                    {"title": "T1", "url": "https://a.example", "content": "first hit"},
                    {"title": "T2", "url": "https://b.example", "content": "second hit"},
                )
            )
        ],
    )
    outcome = SearchClient(_tavily_settings(), _llm_allowed()).search("weekly report")
    assert outcome.provider == "tavily"
    assert outcome.query == "weekly report"
    assert outcome.results == (
        WebResult("T1", "https://a.example", "first hit"),
        WebResult("T2", "https://b.example", "second hit"),
    )
    method, url, kwargs = stub.requests[0]
    assert (method, url) == ("POST", "https://api.tavily.com/search")
    assert kwargs["headers"] == {"Authorization": "Bearer sk-test-123"}
    assert kwargs["json"] == {"query": "weekly report", "max_results": 5}
    # no system proxy is honoured；connect 超时收窄到 5s 以便故障出口快速降级
    kw = stub.client_kwargs
    assert kw["trust_env"] is False
    assert kw["follow_redirects"] is False
    assert kw["timeout"].connect == 5.0
    assert kw["timeout"].read == 10.0  # 搜索页 read 收窄到 10s，故障出口快速降级


def test_tavily_missing_api_key_env_fails_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    stub = HttpxStub().install(monkeypatch)
    client = SearchClient(_tavily_settings(), _llm_allowed())
    with pytest.raises(WebSearchError, match="TAVILY_API_KEY"):
        client.search("anything")
    assert stub.clients_opened == 0


def test_searxng_search_request_and_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(
                json_data=_results_payload(
                    {"title": "S1", "url": "https://s.example/1", "content": "via content"},
                    {"title": "S2", "url": "https://s.example/2", "snippet": "via snippet"},
                )
            )
        ],
    )
    outcome = SearchClient(_searxng_settings(), _llm_allowed()).search("周报")
    assert outcome.provider == "searxng"
    assert outcome.results == (
        WebResult("S1", "https://s.example/1", "via content"),
        WebResult("S2", "https://s.example/2", "via snippet"),
    )
    method, url, kwargs = stub.requests[0]
    assert (method, url) == ("GET", "http://127.0.0.1:8888/search")
    assert kwargs["params"] == {"q": "周报", "format": "json"}


def test_generic_search_posts_to_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SearchSettings(
        provider="generic", base_url="https://search.internal.example/api"
    )
    stub = HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(
                json_data=_results_payload(
                    {"title": "G1", "url": "https://g.example", "content": "c"},
                    {"title": "G2", "url": "https://g2.example", "snippet": "s"},
                )
            )
        ],
    )
    outcome = SearchClient(settings, _llm_allowed()).search("hello", max_results=2)
    assert outcome.results == (
        WebResult("G1", "https://g.example", "c"),
        WebResult("G2", "https://g2.example", "s"),
    )
    method, url, kwargs = stub.requests[0]
    assert (method, url) == ("POST", "https://search.internal.example/api")
    assert kwargs["json"] == {"query": "hello", "max_results": 2}


def test_search_slices_results_to_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-123")
    payload = _results_payload(
        *(
            {"title": f"T{i}", "url": f"https://x.example/{i}", "content": "c"}
            for i in range(8)
        )
    )
    HttpxStub().install(monkeypatch, responses=[FakeResponse(json_data=payload)])
    outcome = SearchClient(_tavily_settings(max_results=3), _llm_allowed()).search("q")
    assert len(outcome.results) == 3


def test_search_clamps_explicit_max_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-123")
    stub = HttpxStub().install(
        monkeypatch, responses=[FakeResponse(json_data=_results_payload())]
    )
    SearchClient(_tavily_settings(), _llm_allowed()).search("q", max_results=99)
    assert stub.requests[0][2]["json"]["max_results"] == 10


def test_unknown_provider_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = HttpxStub().install(monkeypatch)
    settings = SearchSettings(provider="algolia", base_url="https://example.com")
    with pytest.raises(WebSearchError, match="unsupported search provider"):
        SearchClient(settings, _llm_allowed()).search("q")
    assert stub.clients_opened == 0


def test_disabled_client_raises_and_reports_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = HttpxStub().install(monkeypatch)
    client = SearchClient(None, _llm_allowed())
    assert client.enabled is False
    with pytest.raises(WebSearchError, match="disabled"):
        client.search("q")
    assert stub.clients_opened == 0


# ------------------------------------------------------------ query limits


def test_long_query_truncated_to_400_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-123")
    stub = HttpxStub().install(
        monkeypatch, responses=[FakeResponse(json_data=_results_payload())]
    )
    long_query = "x" * (QUERY_MAX_CHARS + 50)
    outcome = SearchClient(_tavily_settings(), _llm_allowed()).search(long_query)
    assert len(outcome.query) == QUERY_MAX_CHARS
    sent = stub.requests[0][2]["json"]["query"]
    assert sent == "x" * QUERY_MAX_CHARS


@pytest.mark.parametrize("query", ["", "   \t\n"])
def test_empty_query_rejected(query: str, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = HttpxStub().install(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-123")
    with pytest.raises(WebSearchError, match="query"):
        SearchClient(_tavily_settings(), _llm_allowed()).search(query)
    assert stub.clients_opened == 0


# ------------------------------------------------------------ error paths


def test_non_2xx_search_status_in_error_not_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-123")
    HttpxStub().install(
        monkeypatch, responses=[FakeResponse(status_code=500, json_data={"error": "BOOM"})]
    )
    with pytest.raises(WebSearchError) as excinfo:
        SearchClient(_tavily_settings(), _llm_allowed()).search("q")
    assert "500" in str(excinfo.value)
    assert "BOOM" not in str(excinfo.value)


def test_malformed_json_search_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-123")
    HttpxStub().install(monkeypatch, responses=[FakeResponse()])
    with pytest.raises(WebSearchError, match="JSON"):
        SearchClient(_tavily_settings(), _llm_allowed()).search("q")


def test_search_timeout_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-123")
    HttpxStub().install(monkeypatch, exc=httpx.TimeoutException("connect timed out"))
    with pytest.raises(WebSearchError, match="timed out"):
        SearchClient(_tavily_settings(), _llm_allowed()).search("q")


def test_fetch_timeout_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    HttpxStub().install(monkeypatch, exc=httpx.TimeoutException("read timed out"))
    with pytest.raises(WebSearchError, match="timed out"):
        fetch_page("https://example.com/slow", gateway=_llm_allowed())


def test_fetch_non_text_content_type_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(
                headers={"content-type": "application/pdf"}, chunks=[b"%PDF-1.7 fake"]
            )
        ],
    )
    with pytest.raises(WebSearchError, match="不支持的内容类型"):
        fetch_page("https://example.com/doc.pdf", gateway=_llm_allowed())


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/file", "http:///nohost", ""]
)
def test_fetch_rejects_non_http_url_shapes(url: str) -> None:
    with pytest.raises(ValueError):
        fetch_page(url, gateway=_llm_allowed())


def test_fetch_rejects_embedded_credentials() -> None:
    with pytest.raises(ValueError, match="credentials"):
        fetch_page("https://user:secret@example.com/page", gateway=_llm_allowed())


def test_fetch_truncates_at_2mib_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(
                headers={"content-type": "text/plain"},
                chunks=[b"a" * MAX_PAGE_BYTES, b"b" * 4096],
            )
        ],
    )
    text = fetch_page(
        "https://example.com/huge", gateway=_llm_allowed(), max_chars=10_000_000
    )
    assert len(text) == MAX_PAGE_BYTES
    assert text == "a" * MAX_PAGE_BYTES


def test_fetch_truncates_to_max_chars_default(monkeypatch: pytest.MonkeyPatch) -> None:
    HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(headers={"content-type": "text/plain"}, chunks=[b"b" * 30_000])
        ],
    )
    text = fetch_page("https://example.com/page", gateway=_llm_allowed())
    assert len(text) == 20_000


def test_fetch_strips_html_tags_script_and_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        "<html><head><script>var evil = 1;</script>"
        "<style>body { color: red; }</style></head>"
        "<body><h1>Report</h1><p>Fresh &amp; clean</p></body></html>"
    )
    HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(
                headers={"content-type": "text/html; charset=utf-8"},
                chunks=[html.encode("utf-8")],
            )
        ],
    )
    text = fetch_page("https://example.com/report.html", gateway=_llm_allowed())
    assert "Report" in text
    assert "Fresh & clean" in text
    assert "evil" not in text
    assert "color: red" not in text
    assert "<" not in text and "</" not in text


def test_fetch_json_content_type_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(
                headers={"content-type": "application/json; charset=utf-8"},
                chunks=[b'{"results": [{"url": "https://x"}]}'],
            )
        ],
    )
    text = fetch_page("https://example.com/api", gateway=_llm_allowed())
    assert text == '{"results": [{"url": "https://x"}]}'


def test_fetch_non_2xx_status_in_error(monkeypatch: pytest.MonkeyPatch) -> None:
    HttpxStub().install(monkeypatch, responses=[FakeResponse(status_code=404)])
    with pytest.raises(WebSearchError) as excinfo:
        fetch_page("https://example.com/missing", gateway=_llm_allowed())
    assert "404" in str(excinfo.value)


def test_fetch_follows_relative_and_cross_origin_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(status_code=302, headers={"location": "/next"}),
            FakeResponse(status_code=302, headers={"location": "https://cdn.example/final"}),
            FakeResponse(
                headers={"content-type": "text/plain"}, chunks=[b"final content"]
            ),
        ],
    )
    text = fetch_page("https://start.example/begin", gateway=_llm_allowed())
    assert text == "final content"
    assert [url for _, url, _ in stub.requests] == [
        "https://start.example/begin",
        "https://start.example/next",
        "https://cdn.example/final",
    ]


def test_fetch_rejects_redirect_to_file_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(status_code=302, headers={"location": "file:///etc/passwd"})
        ],
    )
    with pytest.raises(WebSearchError, match="redirect target"):
        fetch_page("https://example.com/redir", gateway=_llm_allowed())


def test_fetch_rejects_more_than_three_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(status_code=302, headers={"location": f"/r{i}"}) for i in range(4)
        ]
        + [FakeResponse(chunks=[b"never reached"])],
    )
    with pytest.raises(WebSearchError, match="redirects"):
        fetch_page("https://example.com/start", gateway=_llm_allowed())
    assert len(stub.requests) == 4  # initial + 3 followed redirects


def test_local_only_fetch_redirect_to_remote_blocked_per_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(
                status_code=302, headers={"location": "https://remote.example/exfil"}
            )
        ],
    )
    with pytest.raises(ModelGatewayError):
        fetch_page("http://127.0.0.1:8080/doc", gateway=_local_only())
    assert stub.clients_opened == 1  # first hop happened, second was blocked\n

# ---------------------------------------------------------------------- bing

_BING_HTML = """<html><body><ul>
<li class="b_algo">
  <h2 class=""><a target="_blank" href="https://omasofficial.com/"
    h="ID=SERP,1.2"><strong>OMAS</strong> | Iconic Italian Pens Since 1925</a></h2>
  <p class="b_lineclamp4 b_algoSlug">Discover the timeless elegance of OMAS pens.</p>
</li>
<li class="b_algo">
  <h2><a href="https://baike.baidu.com/item/OMAS/2797875">OMAS_百度百科</a></h2>
  <p>OMAS的名称，代表 OFFICINA Meccanica Armondo 西蒙尼。</p>
</li>
<li class="b_algo"><div>无标题无链接的损坏块</div></li>
<li class="b_algo"><h2><a href="https://no-title.example/"> </a></h2>
  <p>空标题被跳过</p>
</li>
</ul></body></html>"""


def test_bing_parse_extracts_structured_results() -> None:
    from omas.websearch.client import _parse_bing_html

    payload = _parse_bing_html(_BING_HTML, limit=10)
    assert payload["results"] == [
        {
            "title": "OMAS | Iconic Italian Pens Since 1925",
            "url": "https://omasofficial.com/",
            "content": "Discover the timeless elegance of OMAS pens.",
        },
        {
            "title": "OMAS_百度百科",
            "url": "https://baike.baidu.com/item/OMAS/2797875",
            "content": "OMAS的名称，代表 OFFICINA Meccanica Armondo 西蒙尼。",
        },
    ]


def test_bing_search_request_and_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = HttpxStub().install(
        monkeypatch, responses=[FakeResponse(text=_BING_HTML)]
    )
    settings = SearchSettings(provider="bing", base_url="https://www.bing.com")
    outcome = SearchClient(settings, _llm_allowed()).search("OMAS 钢笔")
    assert outcome.provider == "bing"
    assert outcome.query == "OMAS 钢笔"
    assert outcome.results[0] == WebResult(
        "OMAS | Iconic Italian Pens Since 1925",
        "https://omasofficial.com/",
        "Discover the timeless elegance of OMAS pens.",
    )
    method, url, kwargs = stub.requests[0]
    assert (method, url) == ("GET", "https://www.bing.com/search")
    assert kwargs["params"]["q"] == "OMAS 钢笔"
    assert kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0")
    # 跟随 www.bing.com → cn.bing.com 的 302，且信任环境变量代理被关闭
    kw = stub.client_kwargs
    assert kw["trust_env"] is False
    assert kw["follow_redirects"] is True
    assert kw["timeout"].connect == 5.0
    assert kw["timeout"].read == 10.0  # 搜索页 read 收窄到 10s，故障出口快速降级


def test_bing_captcha_page_raises_without_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = HttpxStub().install(
        monkeypatch,
        responses=[FakeResponse(text="<html><body>验证页面</body></html>")],
    )
    settings = SearchSettings(provider="bing", base_url="https://www.bing.com")
    with pytest.raises(WebSearchError, match="未解析到自然结果"):
        SearchClient(settings, _llm_allowed()).search("anything")
    assert stub.clients_opened == 1  # 请求确实发出，只是页面非结果页


def test_local_only_rejects_bing_before_http(monkeypatch: pytest.MonkeyPatch) -> None:
    from omas.domain.task import DataPolicy

    stub = HttpxStub().install(monkeypatch)
    settings = SearchSettings(provider="bing", base_url="https://www.bing.com")
    with pytest.raises(ModelGatewayError):
        SearchClient(settings, ModelGateway(policy=DataPolicy.LOCAL_ONLY)).search("x")
    assert stub.clients_opened == 0


# ----------------------------------------------------------- transport retry


def test_transport_hiccup_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """传输层瞬断（连接重置）应重试到成功，不改变结果解析。"""
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-123")
    monkeypatch.setattr(ws_client.time, "sleep", lambda _s: None)
    stub = HttpxStub().install(
        monkeypatch,
        responses=[
            FakeResponse(
                json_data=_results_payload(
                    {"title": "T1", "url": "https://a.example", "content": "hit"}
                )
            )
        ],
        exc=httpx.ConnectError("connection reset"),
        exc_first_n=2,
    )
    outcome = SearchClient(_tavily_settings(), _llm_allowed()).search("weekly report")
    assert outcome.results == (WebResult("T1", "https://a.example", "hit"),)
    # 首次 + 2 次重试 = 3 次请求
    assert len(stub.requests) == 3


def test_transport_retry_exhausted_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """持续传输失败时重试耗尽抛 WebSearchError；策略拒绝不在此层重试。"""
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-123")
    monkeypatch.setattr(ws_client.time, "sleep", lambda _s: None)
    stub = HttpxStub().install(
        monkeypatch,
        exc=httpx.ReadTimeout("read timed out"),
        exc_first_n=0,  # 每次都抛
    )
    with pytest.raises(WebSearchError, match="timed out"):
        SearchClient(_tavily_settings(), _llm_allowed()).search("weekly report")
    assert len(stub.requests) == _TRANSPORT_RETRIES + 1


# ----------------------------------------------------- sogou parser + fallback

_SOGOU_MARKUP = (
    '<html><body><div id="header"></div>'
    '<div class="vrwrap"><h3 class="vr-title">'
    '<a target="_blank" href="/link?url=abc123def456">'
    '<em>LangGraph</em>:基于图结构的智能体框架'
    "</a></h3><p>摘要文本一</p></div>"
    '<div class="vrwrap"><h3 class="vr-title">'
    '<a target="_blank" href="https://example.com/post-2">第二篇结果</a>'
    "</h3><p>摘要文本二</p></div>"
    '<div class="vrwrap"><h3 class="vr-title">no-anchor heading</h3></div>'
    "</body></html>"
)


def _bing_with_sogou_fallback(**overrides: Any) -> SearchSettings:
    fields: dict[str, Any] = {
        "provider": "bing",
        "base_url": "https://www.bing.com",
        "fallback_provider": "sogou",
        "fallback_base_url": "https://www.sogou.com",
        "max_results": 5,
    }
    fields.update(overrides)
    return SearchSettings.model_validate(fields)


def test_sogou_parser_full_block() -> None:
    from omas.websearch.client import _parse_sogou_html

    payload = _parse_sogou_html(_SOGOU_MARKUP, limit=5)
    results = payload["results"]
    # 第三块（h3 里没有 a）被跳过
    assert len(results) == 2
    assert results[0]["url"] == "https://www.sogou.com/link?url=abc123def456"
    assert results[0]["title"] == "LangGraph:基于图结构的智能体框架"
    assert results[0]["content"] == "摘要文本一"
    assert results[1]["url"] == "https://example.com/post-2"
    assert results[1]["content"] == "摘要文本二"


def test_fallback_engine_used_when_primary_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 bing 持续传输失败 → 自动降级 sogou 并复用其解析结果。"""
    monkeypatch.setattr(ws_client.time, "sleep", lambda _s: None)
    stub = HttpxStub().install(
        monkeypatch,
        responses=[FakeResponse(text=_SOGOU_MARKUP)],
        exc=httpx.ConnectError("primary unreachable"),
        # 覆盖主引擎的首次 + 2 次重试；第 4 次请求（fallback）返回响应
        exc_first_n=_TRANSPORT_RETRIES + 1,
    )
    outcome = SearchClient(_bing_with_sogou_fallback(), _llm_allowed()).search(
        "LangGraph"
    )
    assert outcome.provider == "sogou"
    assert outcome.results[0].title == "LangGraph:基于图结构的智能体框架"
    assert outcome.results[0].url == "https://www.sogou.com/link?url=abc123def456"
    assert stub.requests[0][1].startswith("https://www.bing.com")
    assert stub.requests[-1][1].startswith("https://www.sogou.com")


def test_fallback_endpoint_rejected_under_local_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """降级路径同样过策略门：local_only 不能借备用引擎出网。"""
    monkeypatch.setattr(ws_client.time, "sleep", lambda _s: None)
    # 主引擎用回环 searxng（local_only 允许），让它传输失败触发降级
    stub = HttpxStub().install(
        monkeypatch,
        responses=[],
        exc=httpx.ConnectError("loopback hiccup"),
        exc_first_n=_TRANSPORT_RETRIES + 1,
    )
    settings = _bing_with_sogou_fallback(
        provider="searxng",
        base_url="http://127.0.0.1:8888",
    )
    with pytest.raises(ModelGatewayError, match="web_search"):
        SearchClient(settings, _local_only()).search("LangGraph")
    # 降级端点的 guard 在任何备用 HTTP 字节之前
    assert all("sogou" not in url for url, _, _ in stub.requests)
