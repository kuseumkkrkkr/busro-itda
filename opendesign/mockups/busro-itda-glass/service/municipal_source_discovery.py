"""Bounded discovery of official municipal bus datasets on data.go.kr.

Discovery is deliberately separate from ingestion: search results are
untrusted metadata and can only produce a reviewable candidate list. A
candidate must still be registered, downloaded into quarantine, schema-checked,
and reconciled before it can affect the route graph.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ORIGIN = "https://www.data.go.kr"
SEARCH_PATH = "/tcs/dss/selectDataSetList.do"
MAX_QUERY_LENGTH = 80
MAX_PAGES = 10
MAX_PER_PAGE = 100
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 1_000
_DATASET_HREF = re.compile(r"^/data/([1-9][0-9]{0,19})/(openapi|fileData|standard)\.do$")


class DiscoveryError(RuntimeError):
    """Raised when the official search page cannot be safely consumed."""


@dataclass(frozen=True, slots=True)
class MunicipalCandidate:
    public_data_pk: str
    title: str
    detail_url: str
    dataset_kind: str
    query: str
    page: int


def _safe_query(value: str) -> str:
    query = str(value or "").strip()
    if not 1 <= len(query) <= MAX_QUERY_LENGTH or any(ord(c) < 32 for c in query):
        raise DiscoveryError("query must contain 1-80 printable characters")
    return query


def _bounded_int(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DiscoveryError(f"{name} must be between {minimum} and {maximum}")
    return value


def _clean_text(value: str) -> str:
    return " ".join(value.split())


class _SearchParser(HTMLParser):
    """Read only dataset links and their visible title text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._text: list[str] = []
        self.candidates: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = dict(attrs).get("href")
        if not isinstance(href, str):
            return
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            if parsed.scheme != "https" or parsed.hostname != "www.data.go.kr":
                return
        path = parsed.path if parsed.scheme else href
        match = _DATASET_HREF.fullmatch(path)
        if match is None or parsed.query or parsed.fragment:
            return
        self._href = path
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._href is None:
            return
        match = _DATASET_HREF.fullmatch(self._href)
        if match is not None:
            title = _clean_text("".join(self._text))
            if title:
                self.candidates.append((match.group(1), title, match.group(2)))
        self._href = None
        self._text = []


def parse_search_html(html: str, *, query: str, page: int) -> tuple[MunicipalCandidate, ...]:
    query = _safe_query(query)
    page = _bounded_int(page, "page", 1, MAX_PAGES)
    parser = _SearchParser()
    parser.feed(html)
    parser.close()
    result: list[MunicipalCandidate] = []
    seen: set[str] = set()
    for public_data_pk, title, dataset_kind in parser.candidates:
        if public_data_pk in seen:
            continue
        seen.add(public_data_pk)
        result.append(
            MunicipalCandidate(
                public_data_pk=public_data_pk,
                title=title[:240],
                detail_url=f"{ORIGIN}/data/{public_data_pk}/{dataset_kind}.do",
                dataset_kind=dataset_kind,
                query=query,
                page=page,
            )
        )
        if len(result) >= MAX_CANDIDATES:
            break
    return tuple(result)


class DataGoKrMunicipalDiscovery:
    """HTTPS-only, redirect-free client for the public search page."""

    def __init__(self, *, timeout_seconds: float = 15.0, opener=None) -> None:
        if not 1.0 <= float(timeout_seconds) <= 30.0:
            raise DiscoveryError("timeout_seconds must be between 1 and 30")
        self.timeout_seconds = float(timeout_seconds)
        self.opener = opener or build_opener(_RejectRedirects(), ProxyHandler({}))

    def search(self, query: str, *, page: int = 1, per_page: int = 20) -> tuple[MunicipalCandidate, ...]:
        query = _safe_query(query)
        page = _bounded_int(page, "page", 1, MAX_PAGES)
        per_page = _bounded_int(per_page, "per_page", 1, MAX_PER_PAGE)
        # An empty type filter is intentional: municipal feeds are published
        # both as Open APIs and as static file datasets.  Restricting this to
        # ``API`` silently misses the static timetables and stop lists needed
        # for the planner's verified snapshot catalog.
        params = urlencode(
            {"dType": "", "keyword": query, "currentPage": page, "perPage": per_page}
        )
        request = Request(
            f"{ORIGIN}{SEARCH_PATH}?{params}",
            method="GET",
            headers={
                "Accept": "text/html",
                "User-Agent": "busro-itda-municipal-discovery/1.0",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                if int(getattr(response, "status", 200)) != 200:
                    raise DiscoveryError("official search returned a non-200 status")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise DiscoveryError(f"official search returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DiscoveryError("official search could not be reached") from exc
        if len(payload) > MAX_RESPONSE_BYTES:
            raise DiscoveryError("official search response exceeds 2 MiB")
        try:
            html = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise DiscoveryError("official search response is not UTF-8") from exc
        return parse_search_html(html, query=query, page=page)

    def discover(self, queries: Iterable[str], *, pages: int = 1, per_page: int = 20) -> tuple[MunicipalCandidate, ...]:
        pages = _bounded_int(pages, "pages", 1, MAX_PAGES)
        per_page = _bounded_int(per_page, "per_page", 1, MAX_PER_PAGE)
        selected: list[MunicipalCandidate] = []
        seen: set[str] = set()
        for raw_query in queries:
            query = _safe_query(raw_query)
            for page in range(1, pages + 1):
                for candidate in self.search(query, page=page, per_page=per_page):
                    if candidate.public_data_pk in seen:
                        continue
                    seen.add(candidate.public_data_pk)
                    selected.append(candidate)
                    if len(selected) >= MAX_CANDIDATES:
                        return tuple(selected)
        return tuple(selected)


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover official municipal bus datasets")
    parser.add_argument("--query", action="append", required=True, help="portal search term; repeatable")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--per-page", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        candidates = DataGoKrMunicipalDiscovery().discover(
            args.query, pages=args.pages, per_page=args.per_page
        )
    except DiscoveryError as exc:
        parser.error(str(exc))
    document = {
        "schema_version": 1,
        "source": ORIGIN + SEARCH_PATH,
        "candidate_count": len(candidates),
        "candidates": [asdict(item) for item in candidates],
    }
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(json.dumps({"status": "WRITTEN", "path": str(output), "candidate_count": len(candidates)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DataGoKrMunicipalDiscovery",
    "DiscoveryError",
    "MunicipalCandidate",
    "MAX_CANDIDATES",
    "MAX_PAGES",
    "MAX_PER_PAGE",
    "parse_search_html",
]
