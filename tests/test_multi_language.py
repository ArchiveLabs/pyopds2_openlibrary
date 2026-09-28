"""``language`` as a list: ``language=en,fr`` keeps works in either language.

The reader app sends the languages its reader reads in, primary first, and
the feed has to carry the whole list through every link it emits so that the
app only ever injects it at two entry points (the home URL and the search
template).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from pyopds2 import Catalog

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import (
    OpenLibraryDataProvider,
    _build_language_links,
    _is_english_or_all,
    _language_solr_clause,
    canonical_language,
    parse_languages,
)

LANG_MAP = {"eng": "en", "fre": "fr", "ger": "de"}


@pytest.fixture(autouse=True)
def _seeded_language_map():
    openlibrary._iso_to_marc_cache.clear()
    with patch("pyopds2_openlibrary.fetch_languages_map", return_value=LANG_MAP):
        yield
    openlibrary._iso_to_marc_cache.clear()


def _doc(work: str, title: str) -> dict:
    return {
        "key": work,
        "title": title,
        "cover_i": 123,
        "ebook_access": "borrowable",
        "editions": {
            "docs": [
                {
                    "key": work.replace("/works", "/books").replace("W", "M"),
                    "title": title,
                    "cover_i": 123,
                    "ebook_access": "borrowable",
                    "availability": {"status": "borrow_available"},
                    "providers": [{"url": "https://example.org/read", "access": "borrow", "format": "epub"}],
                }
            ]
        },
    }


def _mock_get(num_found: int = 1):
    """A patched ``_get`` answering one borrowable work; returns the mock."""
    mock_get = MagicMock()
    mock_get.return_value.json.return_value = {
        "numFound": num_found,
        "docs": [_doc("/works/OL1W", "Book")],
    }
    return mock_get


def _query_params(href: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(href).query).items()}


class TestParsing:
    def test_nothing_is_no_languages(self):
        assert parse_languages(None) == []
        assert parse_languages("") == []
        assert parse_languages(" , ") == []

    def test_one_code(self):
        assert parse_languages("en") == ["en"]

    def test_a_list_is_stripped_lowercased_and_deduped_in_order(self):
        assert parse_languages(" EN,fr ,,de,en") == ["en", "fr", "de"]

    def test_canonical_spelling(self):
        assert canonical_language(" EN, fr") == "en,fr"
        assert canonical_language("en,fr") == "en,fr"
        assert canonical_language("") is None
        assert canonical_language(None) is None


class TestSolrClause:
    def test_one_language(self):
        assert _language_solr_clause("en") == "language:eng"

    def test_several_languages_are_an_or(self):
        assert _language_solr_clause("en,fr") == "language:(eng OR fre)"

    def test_an_unknown_code_is_dropped(self):
        assert _language_solr_clause("en,xx") == "language:eng"

    def test_all_unknown_is_no_clause(self):
        assert _language_solr_clause("xx,yy") is None

    def test_english_rules(self):
        assert _is_english_or_all(None)
        assert _is_english_or_all("en")
        assert _is_english_or_all("fr,en")
        assert not _is_english_or_all("fr")
        assert not _is_english_or_all("fr,de")


class TestSearch:
    def test_several_languages_filter_with_an_or_and_prefer_the_first(self):
        mock_get = _mock_get()
        with patch("pyopds2_openlibrary._get", mock_get):
            OpenLibraryDataProvider.search("cats", language="en,fr")
        params = mock_get.call_args.kwargs["params"]
        assert "language:(eng OR fre)" in params["q"]
        assert params["lang"] == "en"

    def test_the_first_language_is_the_preferred_edition_language(self):
        mock_get = _mock_get()
        with patch("pyopds2_openlibrary._get", mock_get):
            OpenLibraryDataProvider.search("cats", language="fr,en")
        assert mock_get.call_args.kwargs["params"]["lang"] == "fr"

    def test_an_unknown_language_filters_nothing(self):
        mock_get = _mock_get()
        with patch("pyopds2_openlibrary._get", mock_get):
            OpenLibraryDataProvider.search("cats", language="xx")
        params = mock_get.call_args.kwargs["params"]
        assert "language:" not in params["q"]
        # ``lang`` is still asked for: OL ignores one it does not know.
        assert params["lang"] == "xx"

    def test_the_spelling_is_canonical_however_it_arrived(self):
        mock_get = _mock_get()
        with patch("pyopds2_openlibrary._get", mock_get):
            result = OpenLibraryDataProvider.search("cats", language=" EN, fr")
        assert result.params["language"] == "en,fr"

    def test_pagination_carries_every_parameter(self):
        # pyopds2's own params know only query, limit, page and sort;
        # following ``next`` used to drop the rest.
        mock_get = _mock_get(num_found=100)
        with patch("pyopds2_openlibrary._get", mock_get), \
             patch.object(OpenLibraryDataProvider, "SEARCH_URL", "https://x/search"):
            result = OpenLibraryDataProvider.search(
                "cats", limit=10, offset=20, language="en,fr",
                facets={"mode": "ebooks"}, media_type="ebook",
                access="print_disabled", title="T",
            )
            assert result.params == {
                "query": "cats", "limit": "10", "page": "3", "mode": "ebooks",
                "language": "en,fr", "media_type": "ebook",
                "access": "print_disabled", "title": "T",
            }
            catalog = Catalog.create(response=result)
        for rel in ("first", "previous", "next", "last"):
            link = next(l for l in catalog.links if l.rel == rel)
            params = _query_params(link.href)
            assert params["language"] == "en,fr", rel
            assert params["mode"] == "ebooks", rel
            assert params["media_type"] == "ebook", rel
            assert params["access"] == "print_disabled", rel
            assert params["title"] == "T", rel

    def test_pagination_omits_defaults(self):
        mock_get = _mock_get()
        with patch("pyopds2_openlibrary._get", mock_get):
            result = OpenLibraryDataProvider.search(
                "cats", limit=10, facets={"mode": "everything"}, access=None,
            )
        assert result.params == {"query": "cats", "limit": "10"}


class TestCounts:
    def test_counts_use_the_same_clause_as_results(self):
        mock_get = MagicMock()
        mock_get.return_value.json.return_value = {"numFound": 7}
        with patch("pyopds2_openlibrary._get", mock_get):
            assert OpenLibraryDataProvider._count_for_mode("cats", "ebooks", language="en,fr") == 7
        assert "language:(eng OR fre)" in mock_get.call_args.kwargs["params"]["q"]


class TestHomeRules:
    @staticmethod
    def _titles(groups):
        return [g[0] for g in groups]

    @staticmethod
    def _queries(groups):
        return " ".join(g[1] for g in groups)

    def test_a_list_with_english_keeps_the_english_rules(self):
        groups = OpenLibraryDataProvider._home_groups_config("everything", language="en,fr")
        assert "Standard Ebooks" in self._titles(groups)
        assert "first_publish_year:[1930 TO *]" in self._queries(groups)

    def test_a_list_without_english_uses_the_relaxed_rules(self):
        groups = OpenLibraryDataProvider._home_groups_config("everything", language="fr,de")
        assert "Standard Ebooks" not in self._titles(groups)
        assert "first_publish_year:[1930 TO *]" not in self._queries(groups)


class TestHomeFeed:
    class _FakeCatalog:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.publications = kwargs.get("publications", [])

        @staticmethod
        def create(*args, **kwargs):
            return TestHomeFeed._FakeCatalog(
                metadata={"title": "Group"}, publications=[{"metadata": {"title": "Book"}}],
            )

        def model_dump(self):
            return self.kwargs

    @patch("pyopds2_openlibrary.OpenLibraryDataProvider.search")
    @patch("pyopds2_openlibrary.Catalog")
    def test_the_list_reaches_every_group_and_link_canonically(self, mock_catalog_cls, mock_search):
        mock_catalog_cls.side_effect = lambda **kwargs: self._FakeCatalog(**kwargs)
        mock_catalog_cls.create.side_effect = self._FakeCatalog.create
        mock_search.return_value = object()

        feed = OpenLibraryDataProvider.build_home_feed(
            base="https://example.org/opds", language=" EN,fr ", page=1,
            featured_subjects=[{"presentable_name": "Art", "key": "/subjects/art"}],
        )

        for call in mock_search.call_args_list:
            assert call.kwargs["language"] == "en,fr"
            assert call.kwargs["require_cover"] is True
            assert call.kwargs["limit"] == 25

        links_by_rel = {link.rel: link for link in feed["links"]}
        assert _query_params(links_by_rel["self"].href)["language"] == "en,fr"
        assert _query_params(feed["navigation"][0].href)["language"] == "en,fr"
        assert links_by_rel["search"].href == "https://example.org/opds/search{?query,language}"
        assert links_by_rel["search"].templated is True
        for group in feed["facets"]:
            for link in group["links"]:
                if group["metadata"]["title"] != "Language":
                    assert _query_params(link["href"])["language"] == "en,fr"

    @patch("pyopds2_openlibrary.OpenLibraryDataProvider.search")
    @patch("pyopds2_openlibrary.Catalog")
    def test_a_list_without_english_relaxes_the_groups(self, mock_catalog_cls, mock_search):
        mock_catalog_cls.side_effect = lambda **kwargs: self._FakeCatalog(**kwargs)
        mock_catalog_cls.create.side_effect = self._FakeCatalog.create
        mock_search.return_value = object()

        OpenLibraryDataProvider.build_home_feed(base="https://example.org/opds", language="fr,de")

        for call in mock_search.call_args_list:
            assert call.kwargs["require_cover"] is False
            assert call.kwargs["limit"] == 50


OPTIONS = [(None, "All"), ("en", "English"), ("fr", "French"), ("de", "German")]


class TestLanguageFacet:
    @staticmethod
    def _links(language, counts=None):
        with patch("pyopds2_openlibrary.fetch_language_options", return_value=OPTIONS):
            return _build_language_links(
                language=language,
                href_fn=lambda c: f"/search?language={c}" if c else "/search",
                counts=counts,
            )

    def test_nothing_selected_marks_all_active(self):
        links = self._links(None)
        assert [l["title"] for l in links if l.get("rel") == "self"] == ["All"]

    def test_one_language_marks_itself_active(self):
        links = self._links("fr")
        assert [l["title"] for l in links if l.get("rel") == "self"] == ["French"]

    def test_several_languages_mark_nothing_active(self):
        # The links stay single-select, as OPDS facets are: a selection of
        # several is not any one of them, and "All" is not it either.
        links = self._links("en,fr", counts={"en": 1, "fr": 1, "de": 1})
        assert [l for l in links if l.get("rel") == "self"] == []
        assert all("active" not in l.get("properties", {}) for l in links)

    def test_each_link_narrows_to_one_language(self):
        links = self._links("en,fr")
        assert [l["href"] for l in links] == [
            "/search", "/search?language=en", "/search?language=fr", "/search?language=de",
        ]

    def test_a_selected_language_stays_listed_whatever_its_count(self):
        links = self._links("en,fr", counts={"en": 5})
        assert [l["title"] for l in links] == ["All", "English", "French"]


class TestFacetHrefs:
    def test_search_facets_carry_the_list(self):
        with patch("pyopds2_openlibrary.fetch_language_options", return_value=OPTIONS):
            facets = OpenLibraryDataProvider.build_facets(
                base_url="https://example.org/opds", query="cats", language=" EN,fr",
            )
        for group in facets:
            if group["metadata"]["title"] == "Language":
                continue
            for link in group["links"]:
                assert _query_params(link["href"])["language"] == "en,fr"

    def test_author_facets_carry_the_list(self):
        with patch("pyopds2_openlibrary.fetch_language_options", return_value=OPTIONS):
            facets = OpenLibraryDataProvider.build_author_facets(
                base_url="https://example.org/opds", olid="OL1A", language="en,fr",
            )
        for group in facets:
            if group["metadata"]["title"] == "Language":
                continue
            for link in group["links"]:
                assert _query_params(link["href"])["language"] == "en,fr"
