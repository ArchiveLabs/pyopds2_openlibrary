"""A general-access search asks Open Library for an edition the reader can have.

Search returns one edition per work. When that edition was print-disabled,
the general-access filter dropped the whole work, even when the work had a
borrowable edition: Robert Jordan's author page listed one book of ten.
The ``edition.``-scoped clauses make Open Library pick another edition.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import OpenLibraryDataProvider, _edition_scope_clause

LANG_MAP = {"eng": "en", "fre": "fr", "swe": "sv"}


@pytest.fixture(autouse=True)
def _seeded_language_map():
    openlibrary._iso_to_marc_cache.clear()
    with patch("pyopds2_openlibrary.fetch_languages_map", return_value=LANG_MAP):
        yield
    openlibrary._iso_to_marc_cache.clear()


def _doc(edition_access: str) -> dict:
    return {
        "key": "/works/OL1W",
        "title": "The Eye of the World",
        "cover_i": 123,
        "ebook_access": "borrowable",
        "editions": {
            "docs": [
                {
                    "key": "/books/OL1M",
                    "title": "The Eye of the World",
                    "cover_i": 123,
                    "ebook_access": edition_access,
                    "ia": ["eyeofworld00jord"],
                    "providers": [{"provider_name": "ia", "url": "https://archive.org/details/eyeofworld00jord", "access": "borrow"}],
                }
            ]
        },
    }


def _search_q(**kwargs) -> str:
    """The ``q`` of the search call that ``search`` makes, with *kwargs*."""
    mock_get = MagicMock()
    mock_get.return_value.json.return_value = {"numFound": 1, "docs": [_doc("borrowable")]}
    with patch("pyopds2_openlibrary._get", mock_get), \
         patch("pyopds2_openlibrary.list_editions"):
        OpenLibraryDataProvider.search(kwargs.pop("query", "author_key:OL233594A"), **kwargs)
    return mock_get.call_args_list[0].kwargs["params"]["q"]


class TestClause:
    def test_everything_asks_for_a_borrowable_or_free_edition(self):
        assert _edition_scope_clause("everything", None, None) == "edition.ebook_access:(borrowable OR public)"

    def test_each_mode_asks_for_its_own_values(self):
        assert _edition_scope_clause("ebooks", None, None) == "edition.ebook_access:borrowable"
        assert _edition_scope_clause("open_access", None, None) == "edition.ebook_access:public"
        assert _edition_scope_clause("buyable", None, None) == "edition.ebook_access:(borrowable OR public)"
        assert _edition_scope_clause("ebooks,open_access", None, None) == "edition.ebook_access:(borrowable OR public)"

    def test_languages_scope_the_edition(self):
        assert _edition_scope_clause("everything", "sv,en", "general") == (
            "edition.ebook_access:(borrowable OR public) edition.language:(swe OR eng)"
        )

    def test_print_disabled_access_has_no_clause(self):
        assert _edition_scope_clause("everything", "en", "print_disabled") is None

    def test_a_mode_with_nothing_to_have_has_no_clause(self):
        assert _edition_scope_clause("print_disabled", "en", None) is None


class TestSearch:
    def test_general_access_asks_for_an_edition_the_reader_can_have(self):
        q = _search_q()
        assert "edition.ebook_access:(borrowable OR public)" in q
        assert "edition.language" not in q

    def test_languages_keep_the_work_clause_and_scope_the_edition(self):
        q = _search_q(language="sv,en")
        assert "language:(swe OR eng)" in q.replace("edition.language:(swe OR eng)", "")
        assert "edition.language:(swe OR eng)" in q

    def test_print_disabled_access_asks_for_any_edition(self):
        assert "edition." not in _search_q(access="print_disabled", language="en")

    def test_an_edition_lookup_has_no_clause(self):
        assert "edition." not in _search_q(query="edition_key:OL1M", language="en")

    def test_the_work_is_kept_with_the_edition_open_library_gives(self):
        mock_get = MagicMock()
        mock_get.return_value.json.return_value = {"numFound": 1, "docs": [_doc("borrowable")]}
        with patch("pyopds2_openlibrary._get", mock_get), \
             patch("pyopds2_openlibrary.list_editions"):
            result = OpenLibraryDataProvider.search("author_key:OL233594A", require_cover=False)
        assert [r.title for r in result.records] == ["The Eye of the World"]


class TestCounts:
    def test_counts_ask_for_the_same_editions_as_results(self):
        mock_get = MagicMock()
        mock_get.return_value.json.return_value = {"numFound": 4}
        with patch("pyopds2_openlibrary._get", mock_get):
            assert OpenLibraryDataProvider._count_for_mode("cats", "everything", language="sv,en") == 4
        assert mock_get.call_args.kwargs["params"]["q"].endswith(
            "edition.ebook_access:(borrowable OR public) edition.language:(swe OR eng)"
        )
