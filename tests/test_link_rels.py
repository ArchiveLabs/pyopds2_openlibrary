"""Link relations that say what kind of link a thing is.

Every option link of a facet group carries that group's relation, the
applied one ``["self", <relation>]``, so a client finds the Availability
picker by relation whatever its title says. The trending carousel's self link
carries OPDS's ``sort/popular`` besides ``self``. The relations are fixed
strings, not built from the base URL.
"""
from __future__ import annotations

from unittest.mock import patch

from pyopds2 import DataProvider, has_rel

from pyopds2_openlibrary import (
    OpenLibraryDataProvider,
    OpenLibraryDataRecord,
    REL_FACET_ACCESS,
    REL_FACET_AVAILABILITY,
    REL_FACET_LANGUAGE,
    REL_FACET_MEDIA_TYPE,
    REL_PORTRAIT,
    REL_SORT_POPULAR,
    _build_access_links,
    _build_availability_links,
    _build_language_links,
    _build_media_type_links,
)

OPTIONS = [(None, "All"), ("en", "English"), ("fr", "French")]

GROUP_RELS = {
    "Availability": REL_FACET_AVAILABILITY,
    "Language": REL_FACET_LANGUAGE,
    "Media Type": REL_FACET_MEDIA_TYPE,
    "Access": REL_FACET_ACCESS,
}


def _rels(link) -> list[str]:
    rel = link.get("rel")
    return rel if isinstance(rel, list) else [rel]


class TestFacetLinkRels:
    def test_the_relations_are_urls_under_the_rel_path(self):
        for rel in GROUP_RELS.values():
            assert rel.startswith("https://openlibrary.org/opds/rel/facet/")

    def test_every_availability_link_carries_the_group_rel(self):
        links = _build_availability_links(mode="ebooks", href_fn=lambda m: f"?mode={m}")
        assert all(has_rel(l, REL_FACET_AVAILABILITY) for l in links)
        by_title = {l["title"]: l for l in links}
        assert by_title["Borrow"]["rel"] == ["self", REL_FACET_AVAILABILITY]
        assert by_title["Everything"]["rel"] == REL_FACET_AVAILABILITY

    def test_every_language_link_carries_the_group_rel(self):
        with patch("pyopds2_openlibrary.fetch_language_options", return_value=OPTIONS):
            links = _build_language_links(language="fr", href_fn=lambda c: f"?language={c}")
        assert all(has_rel(l, REL_FACET_LANGUAGE) for l in links)
        by_title = {l["title"]: l for l in links}
        assert by_title["French"]["rel"] == ["self", REL_FACET_LANGUAGE]
        assert by_title["All"]["rel"] == REL_FACET_LANGUAGE

    def test_a_list_of_languages_marks_none_self_but_keeps_the_group_rel(self):
        with patch("pyopds2_openlibrary.fetch_language_options", return_value=OPTIONS):
            links = _build_language_links(language="en,fr", href_fn=lambda c: f"?language={c}")
        assert [l["rel"] for l in links] == [REL_FACET_LANGUAGE] * 3

    def test_every_media_type_link_carries_the_group_rel(self):
        links = _build_media_type_links(media_type="audiobook", href_fn=lambda m: f"?media_type={m}")
        assert all(has_rel(l, REL_FACET_MEDIA_TYPE) for l in links)
        by_title = {l["title"]: l for l in links}
        assert by_title["Audiobooks"]["rel"] == ["self", REL_FACET_MEDIA_TYPE]
        assert by_title["All"]["rel"] == REL_FACET_MEDIA_TYPE

    def test_every_access_link_carries_the_group_rel(self):
        links = _build_access_links(access=None, href_fn=lambda a: f"?access={a}")
        by_title = {l["title"]: l for l in links}
        assert by_title["General"]["rel"] == ["self", REL_FACET_ACCESS]
        assert by_title["Print Disabled"]["rel"] == REL_FACET_ACCESS

    def test_exactly_one_group_rel_per_link_and_it_is_the_groups(self):
        with patch("pyopds2_openlibrary.fetch_language_options", return_value=OPTIONS):
            search = OpenLibraryDataProvider.build_facets(base_url="https://x/opds", query="cats", mode="ebooks")
            home = OpenLibraryDataProvider.build_home_facets(base_url="https://x/opds", media_type="ebook")
            author = OpenLibraryDataProvider.build_author_facets(base_url="https://x/opds", olid="OL1A", access="print_disabled")
        for facets in (search, home, author):
            assert [g["metadata"]["title"] for g in facets] == list(GROUP_RELS)
            for group in facets:
                expected = GROUP_RELS[group["metadata"]["title"]]
                for link in group["links"]:
                    rels = _rels(link)
                    assert [r for r in rels if r != "self"] == [expected], link
                applied = [l for l in group["links"] if has_rel(l, "self")]
                assert len(applied) == 1, group["metadata"]["title"]
                assert applied[0]["rel"] == ["self", expected]

    def test_the_rels_do_not_follow_the_base_url(self):
        a = OpenLibraryDataProvider.build_facets(base_url="https://a.example/opds", query="q")
        b = OpenLibraryDataProvider.build_facets(base_url="http://localhost:8090", query="q")
        assert [_rels(l) for g in a for l in g["links"]] == [_rels(l) for g in b for l in g["links"]]


def _record(title: str) -> OpenLibraryDataRecord:
    return OpenLibraryDataRecord.model_validate({
        "key": "/works/OL1W",
        "title": title,
        "author_name": ["A"],
        "author_key": ["OL1A"],
        "editions": {"numFound": 1, "start": 0, "numFoundExact": True,
                     "docs": [{"key": "/books/OL1M", "title": title}]},
    })


def _response(**kwargs) -> DataProvider.SearchResponse:
    return DataProvider.SearchResponse(
        provider=OpenLibraryDataProvider, records=[_record("Book")], total=1,
        query=kwargs.get("query", "q"), limit=kwargs.get("limit", 25), offset=0,
        sort=kwargs.get("sort"), title=kwargs.get("title"),
    )


class TestCarouselRels:
    @patch("pyopds2_openlibrary.OpenLibraryDataProvider.search")
    def test_only_the_trending_carousel_is_marked_popular(self, mock_search):
        mock_search.side_effect = lambda **kw: _response(**kw)
        with patch("pyopds2_openlibrary.fetch_language_options", return_value=OPTIONS):
            feed = OpenLibraryDataProvider.build_home_feed(base="https://x/opds", page=1)
        groups = feed["groups"]
        assert [g["metadata"]["title"] for g in groups][0] == "Trending Books"
        for group in groups:
            self_links = [l for l in group["links"] if has_rel(l, "self")]
            assert len(self_links) == 1, group["metadata"]["title"]
            if group["metadata"]["title"] == "Trending Books":
                assert self_links[0]["rel"] == ["self", REL_SORT_POPULAR]
            else:
                assert self_links[0]["rel"] == "self"

    def test_sort_popular_is_the_registered_opds_relation(self):
        assert REL_SORT_POPULAR == "http://opds-spec.org/sort/popular"


class TestAuthorPageRels:
    def test_both_contributor_links_refer_to_the_author(self):
        [author] = _record("Book").metadata().author
        assert all(has_rel(link, "author") for link in author.links)
        assert {link.type for link in author.links} == {"text/html", "application/opds+json"}

    def test_the_portrait_relation_is_a_fixed_url_under_the_rel_path(self):
        assert REL_PORTRAIT == "https://openlibrary.org/opds/rel/portrait"

    def test_author_facet_links_keep_the_sort(self):
        with patch("pyopds2_openlibrary.fetch_language_options", return_value=OPTIONS):
            groups = OpenLibraryDataProvider.build_author_facets(base_url="https://x/opds", olid="OL1A", sort="rating")
            plain = OpenLibraryDataProvider.build_author_facets(base_url="https://x/opds", olid="OL1A")
        assert all("sort=rating" in l["href"] for g in groups for l in g["links"])
        assert not any("sort=" in l["href"] for g in plain for l in g["links"])
