"""``mode`` and ``media_type`` as lists: ``mode=ebooks,open_access`` keeps a
work that belongs under either.

The reader app renders the Availability and Media Type facet groups as
checkboxes, so a reader can tick "Borrow" and "Free" together. The feed ORs
the ticked values, spells the list one way wherever it emits it, and marks
every ticked option ``rel: "self"``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from pyopds2 import Catalog

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import (
    OpenLibraryDataProvider,
    _apply_media_type_filter,
    _build_availability_links,
    _build_media_type_links,
    _mode_ebook_access_clause,
    canonical_media_type,
    canonical_mode,
    parse_media_types,
    parse_modes,
)

LANG_MAP = {"eng": "en", "fre": "fr"}


@pytest.fixture(autouse=True)
def _seeded_language_map():
    openlibrary._iso_to_marc_cache.clear()
    with patch("pyopds2_openlibrary.fetch_languages_map", return_value=LANG_MAP):
        yield
    openlibrary._iso_to_marc_cache.clear()


def _doc(work: str, access: str, providers=None) -> dict:
    return {
        "key": work,
        "title": work,
        "cover_i": 123,
        "ebook_access": access,
        "editions": {
            "docs": [
                {
                    "key": work.replace("/works", "/books").replace("W", "M"),
                    "title": work,
                    "cover_i": 123,
                    "ebook_access": access,
                    "availability": {"status": "borrow_available"},
                    "providers": providers
                    or [{"url": "https://example.org/read", "access": "borrow", "format": "epub"}],
                }
            ]
        },
    }


def _mock_get(*docs: dict, num_found: int | None = None):
    mock_get = MagicMock()
    mock_get.return_value.json.return_value = {
        "numFound": len(docs) if num_found is None else num_found,
        "docs": list(docs),
    }
    return mock_get


def _query_params(href: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(href).query).items()}


class TestParsing:
    def test_nothing_is_everything(self):
        assert parse_modes(None) == []
        assert parse_modes("everything") == []
        assert canonical_mode(None) == "everything"
        assert canonical_mode("") == "everything"

    def test_a_list_is_read_in_canonical_order(self):
        assert parse_modes("buyable, EBOOKS,ebooks") == ["ebooks", "buyable"]
        assert canonical_mode("buyable,ebooks") == canonical_mode("ebooks, buyable")

    def test_unknown_values_are_dropped(self):
        assert parse_modes("ebooks,nonsense") == ["ebooks"]
        assert canonical_mode("nonsense") == "everything"

    def test_everything_in_a_list_is_nothing(self):
        assert canonical_mode("everything,open_access") == "open_access"

    def test_canonical_is_idempotent(self):
        once = canonical_mode("open_access,ebooks")
        assert canonical_mode(once) == once

    def test_media_types(self):
        assert parse_media_types("audiobook,ebook") == ["ebook", "audiobook"]
        assert canonical_media_type(None) is None
        assert canonical_media_type("video") is None
        assert canonical_media_type(" Audiobook ") == "audiobook"


class TestSolrClause:
    def test_a_single_mode_is_spelled_as_it_always_was(self):
        assert _mode_ebook_access_clause("ebooks") == "ebook_access:(borrowable OR printdisabled)"
        assert _mode_ebook_access_clause("open_access") == "ebook_access:public"
        assert _mode_ebook_access_clause("print_disabled") == "ebook_access:printdisabled"
        assert (
            _mode_ebook_access_clause("everything")
            == "ebook_access:(borrowable OR printdisabled OR public)"
        )

    def test_a_list_is_the_or_of_its_modes(self):
        assert (
            _mode_ebook_access_clause("ebooks,open_access")
            == "ebook_access:(borrowable OR printdisabled OR public)"
        )
        assert (
            _mode_ebook_access_clause("open_access,print_disabled")
            == "ebook_access:(printdisabled OR public)"
        )

    def test_both_media_types_filter_nothing(self):
        assert _apply_media_type_filter("cats", "ebook,audiobook") == "cats"
        assert _apply_media_type_filter("cats", "audiobook") == "cats id_librivox:*"


class TestSearch:
    def test_the_query_carries_the_union(self):
        mock_get = _mock_get(_doc("/works/OL1W", "borrowable"))
        with patch("pyopds2_openlibrary._get", mock_get):
            OpenLibraryDataProvider.search("cats", facets={"mode": "open_access,ebooks"})
        q = mock_get.call_args.kwargs["params"]["q"]
        assert "ebook_access:(borrowable OR printdisabled OR public)" in q

    def test_a_record_under_any_listed_mode_is_kept(self):
        mock_get = _mock_get(
            _doc("/works/OL1W", "borrowable"),
            _doc("/works/OL2W", "public"),
        )
        with patch("pyopds2_openlibrary._get", mock_get):
            both = OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks,open_access"})
            borrow = OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks"})
        assert {r.key for r in both.records} == {"/works/OL1W", "/works/OL2W"}
        # One mode alone still keeps its boundary.
        assert {r.key for r in borrow.records} == {"/works/OL1W"}

    def test_buyable_in_a_list_is_tested_per_record(self):
        priced = [{"url": "https://shop.example/1", "access": "buy", "format": "epub",
                   "price": "4.99 USD", "provider_name": "shop"}]
        mock_get = _mock_get(
            _doc("/works/OL1W", "public"),
            _doc("/works/OL2W", "borrowable", providers=priced),
            num_found=500,
        )
        with patch("pyopds2_openlibrary._get", mock_get), \
             patch("pyopds2_openlibrary._has_buyable_provider",
                   side_effect=lambda r: r.key == "/works/OL2W"):
            result = OpenLibraryDataProvider.search("cats", facets={"mode": "open_access,buyable"})
        assert {r.key for r in result.records} == {"/works/OL1W", "/works/OL2W"}
        # A client-side filter makes Solr's count meaningless.
        assert result.total == 2

    def test_pagination_carries_the_lists_canonically(self):
        mock_get = _mock_get(_doc("/works/OL1W", "borrowable"), num_found=100)
        with patch("pyopds2_openlibrary._get", mock_get), \
             patch.object(OpenLibraryDataProvider, "SEARCH_URL", "https://x/search"):
            result = OpenLibraryDataProvider.search(
                "cats", limit=10, offset=20,
                facets={"mode": "open_access , ebooks"}, media_type="audiobook,ebook",
            )
            assert result.params["mode"] == "open_access,ebooks"
            assert result.params["media_type"] == "ebook,audiobook"
            catalog = Catalog.create(response=result)
        for rel in ("first", "previous", "next", "last"):
            link = next(l for l in catalog.links if l.rel == rel)
            params = _query_params(link.href)
            assert params["mode"] == "open_access,ebooks", rel
            assert params["media_type"] == "ebook,audiobook", rel

    def test_both_media_types_keep_audiobooks(self):
        audiobook = _doc("/works/OL1W", "public")
        audiobook["id_librivox"] = ["123"]
        mock_get = _mock_get(audiobook)
        with patch("pyopds2_openlibrary._get", mock_get):
            ebook_only = OpenLibraryDataProvider.search("cats", media_type="ebook")
            both = OpenLibraryDataProvider.search("cats", media_type="ebook,audiobook")
        assert ebook_only.records == []
        assert [r.key for r in both.records] == ["/works/OL1W"]


class TestFacets:
    def test_nothing_ticked_marks_everything(self):
        links = _build_availability_links(mode="everything", href_fn=lambda m: m)
        assert [l["title"] for l in links if l.get("rel") == "self"] == ["Everything"]

    def test_every_listed_mode_is_marked(self):
        links = _build_availability_links(mode="ebooks,open_access", href_fn=lambda m: m)
        assert sorted(l["title"] for l in links if l.get("rel") == "self") == ["Borrow", "Free"]

    def test_each_option_still_narrows_to_one_mode(self):
        links = _build_availability_links(mode="ebooks,open_access", href_fn=lambda m: f"?mode={m}")
        assert [l["href"] for l in links] == [
            "?mode=everything", "?mode=ebooks", "?mode=open_access", "?mode=buyable",
        ]

    def test_the_labels_are_short(self):
        availability = _build_availability_links(mode="everything", href_fn=lambda m: m)
        media = _build_media_type_links(media_type=None, href_fn=lambda m: str(m))
        assert [l["title"] for l in availability] == ["Everything", "Borrow", "Free", "Buy"]
        assert [l["title"] for l in media] == ["All", "Books", "Audiobooks"]

    def test_every_listed_media_type_is_marked(self):
        links = _build_media_type_links(media_type="ebook,audiobook", href_fn=lambda m: str(m))
        assert [l["title"] for l in links if l.get("rel") == "self"] == ["Books", "Audiobooks"]

    def test_search_facets_spell_the_lists_canonically(self):
        facets = OpenLibraryDataProvider.build_facets(
            base_url="https://x/opds", query="cats",
            mode="open_access,ebooks", media_type="audiobook,ebook",
        )
        language_group = next(g for g in facets if g["metadata"]["title"] == "Language")
        for link in language_group["links"]:
            params = _query_params(link["href"])
            assert params["mode"] == "open_access,ebooks"
            assert params["media_type"] == "ebook,audiobook"

    def test_home_facets_carry_the_lists(self):
        facets = OpenLibraryDataProvider.build_home_facets(
            base_url="https://x/opds", mode="ebooks,open_access", media_type="audiobook",
        )
        language_group = next(g for g in facets if g["metadata"]["title"] == "Language")
        params = _query_params(language_group["links"][1]["href"])
        assert params["mode"] == "open_access,ebooks"
        assert params["media_type"] == "audiobook"
