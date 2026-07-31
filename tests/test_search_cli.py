"""Search CLI JSON, configuration, logging, output, and exit-code tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from media_scope.jackett_client import JackettClient
from media_scope.search_cli import main

FIXTURES = Path(__file__).parent / "fixtures"
SCOPE_PATH = FIXTURES / "scope_inputs" / "completed_tv.json"
JACKETT_FIXTURES = FIXTURES / "jackett_responses"


def factory_for(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[str, str], JackettClient]:
    return lambda url, key: JackettClient(
        url,
        key,
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )


def configured(monkeypatch: Any) -> None:
    monkeypatch.setenv("JACKETT_URL", "http://127.0.0.1:9117/")
    monkeypatch.setenv("JACKETT_API_KEY", "test-secret")
    monkeypatch.delenv("JACKETT_INDEXERS", raising=False)
    monkeypatch.delenv("MEDIA_SEARCH_MIN_SEEDERS", raising=False)
    monkeypatch.setattr("media_scope.search_cli.load_dotenv", lambda: False)


def fixture_handler(request: httpx.Request) -> httpx.Response:
    query_type = request.url.params["t"]
    if query_type == "caps":
        return httpx.Response(200, content=(JACKETT_FIXTURES / "caps.xml").read_bytes())
    return httpx.Response(200, content=(JACKETT_FIXTURES / "results.xml").read_bytes())


def test_missing_api_key_is_structured_exit_four(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    monkeypatch.setenv("JACKETT_URL", "http://localhost:9117")
    monkeypatch.delenv("JACKETT_API_KEY", raising=False)
    monkeypatch.setattr("media_scope.search_cli.load_dotenv", lambda: False)
    exit_code = main(["--scope", str(SCOPE_PATH), "--indexer", "alpha"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error_code"] == "JACKETT_CONFIGURATION_ERROR"


def test_invalid_cli_and_missing_scope_are_json_exit_two(capsys: Any) -> None:
    exit_code = main(["--scope", "missing.json", "--max-rejected", "-1"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error_code"] == "INVALID_CLI_INPUT"

    exit_code = main(["--scope", "missing.json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error_code"] == "INVALID_SCOPE_INPUT"


def test_non_tv_valid_scope_is_exit_three(
    tmp_path: Path,
    capsys: Any,
) -> None:
    path = tmp_path / "movie.json"
    payload = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
    payload["media_type"] = "movie"
    path.write_text(json.dumps(payload), encoding="utf-8")
    exit_code = main(["--scope", str(path)])
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert result["error_code"] == "UNSUPPORTED_SCOPE"


def test_success_stdout_output_logging_and_flags_are_separate(
    monkeypatch: Any,
    capsys: Any,
    tmp_path: Path,
) -> None:
    configured(monkeypatch)
    output = tmp_path / "results.json"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return fixture_handler(request)

    exit_code = main(
        [
            "--scope",
            str(SCOPE_PATH),
            "--indexer",
            "alpha",
            "--fresh",
            "--pretty",
            "--verbose",
            "--output",
            str(output),
        ],
        client_factory=factory_for(handler),
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["result"] == "search_completed"
    assert payload["search"]["accepted_candidate_count"] == 1
    assert output.read_text(encoding="utf-8") == captured.out
    assert "Loaded scope" in captured.err
    assert "test-secret" not in captured.out
    assert "test-secret" not in captured.err
    search_requests = [value for value in requests if value.url.params["t"] != "caps"]
    assert search_requests
    assert all(value.url.params["cache"] == "false" for value in search_requests)


def test_environment_indexers_and_min_seeders_are_used(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    configured(monkeypatch)
    monkeypatch.setenv("JACKETT_INDEXERS", " alpha,alpha ")
    monkeypatch.setenv("MEDIA_SEARCH_MIN_SEEDERS", "50")
    exit_code = main(
        ["--scope", str(SCOPE_PATH)],
        client_factory=factory_for(fixture_handler),
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["search"]["indexers_requested"] == ["alpha"]
    assert payload["search"]["min_seeders_for_ranking"] == 50


def test_authentication_and_all_indexer_failure_are_exit_four(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    configured(monkeypatch)

    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    exit_code = main(
        ["--scope", str(SCOPE_PATH), "--indexer", "alpha"],
        client_factory=factory_for(unauthorized),
    )
    assert exit_code == 4
    assert json.loads(capsys.readouterr().out)["error_code"] == ("JACKETT_AUTHENTICATION_ERROR")

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    exit_code = main(
        ["--scope", str(SCOPE_PATH), "--indexer", "alpha"],
        client_factory=factory_for(unavailable),
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error_code"] == "ALL_INDEXERS_FAILED"
    assert payload["indexer_diagnostics"]


def test_malformed_xml_is_total_failure(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    configured(monkeypatch)

    def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<caps>")

    exit_code = main(
        ["--scope", str(SCOPE_PATH), "--indexer", "alpha"],
        client_factory=factory_for(malformed),
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error_code"] == "ALL_INDEXERS_FAILED"


def test_no_candidates_is_valid_completed_search(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    configured(monkeypatch)
    episode_xml = b"""<rss><channel><item>
      <title>The.Good.Place.S02E04.1080p</title>
      <guid>episode</guid><category>5000</category>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["t"] == "caps":
            return httpx.Response(200, content=(JACKETT_FIXTURES / "caps.xml").read_bytes())
        return httpx.Response(200, content=episode_xml)

    exit_code = main(
        ["--scope", str(SCOPE_PATH), "--indexer", "alpha", "--max-rejected", "0"],
        client_factory=factory_for(handler),
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["candidates"] == []
    assert payload["rejected_results"] == []
    assert any(
        warning["code"] == "NO_COMPLETE_SERIES_CANDIDATES" for warning in payload["warnings"]
    )


def test_unicode_release_title_is_valid_utf8_json(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    configured(monkeypatch)
    unicode_xml = """<rss><channel><item>
      <title>The.Good.Place.S01-S04.Complete.中文</title>
      <guid>unicode-result</guid><category>5000</category>
      <attr name="infohash" value="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" />
    </item></channel></rss>""".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["t"] == "caps":
            return httpx.Response(200, content=(JACKETT_FIXTURES / "caps.xml").read_bytes())
        return httpx.Response(200, content=unicode_xml)

    exit_code = main(
        ["--scope", str(SCOPE_PATH), "--indexer", "alpha"],
        client_factory=factory_for(handler),
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["candidates"][0]["original_title"].endswith("中文")


def test_output_write_failure_is_structured_exit_five(
    monkeypatch: Any,
    capsys: Any,
    tmp_path: Path,
) -> None:
    configured(monkeypatch)
    exit_code = main(
        [
            "--scope",
            str(SCOPE_PATH),
            "--indexer",
            "alpha",
            "--output",
            str(tmp_path),
        ],
        client_factory=factory_for(fixture_handler),
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 5
    assert payload["error_code"] == "OUTPUT_WRITE_ERROR"
