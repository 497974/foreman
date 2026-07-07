"""web_search tests — offline (canned HTML / monkeypatched fetch), no network."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import foreman.websearch as ws
from foreman.websearch import web_search

CANNED = """
<div class="result">
<a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Felo&amp;rut=abc">Elo <b>rating</b> system</a>
<a class="result__snippet">The Elo system measures relative skill.</a>
</div>
<div class="result">
<a class="result__a" href="https://plain.example.org/page">Plain link result</a>
<a class="result__snippet">Second snippet here.</a>
</div>
"""


def test_parses_titles_urls_snippets(monkeypatch):
    monkeypatch.setattr(ws, "_fetch", lambda q: CANNED)
    out = web_search("elo rating")
    assert "1. Elo rating system" in out          # tags stripped from title
    assert "https://example.com/elo" in out        # uddg redirect unwrapped
    assert "The Elo system measures relative skill." in out
    assert "2. Plain link result" in out
    assert "https://plain.example.org/page" in out


def test_network_failure_is_a_tool_result_not_an_exception(monkeypatch):
    def boom(q):
        raise OSError("no route to host")
    monkeypatch.setattr(ws, "_fetch", boom)
    out = web_search("anything")
    assert out.startswith("error: search unavailable")
    assert "proceed without web results" in out


def test_no_results_reports_plainly(monkeypatch):
    monkeypatch.setattr(ws, "_fetch", lambda q: "<html>nothing here</html>")
    assert "no web results" in web_search("zebra flux capacitor")


def test_empty_query_rejected():
    assert web_search("   ") == "error: empty search query"


def test_result_cap(monkeypatch):
    many = "".join(
        f'<a class="result__a" href="https://e{i}.com">R{i}</a>'
        f'<a class="result__snippet">s{i}</a>' for i in range(20)
    )
    monkeypatch.setattr(ws, "_fetch", lambda q: many)
    out = web_search("q")
    assert "5. R4" in out and "6." not in out  # capped at MAX_RESULTS=5
