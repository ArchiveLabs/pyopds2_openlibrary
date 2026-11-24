"""The browsing categories, their subcategories and the reading interests
(``browsing.py``): what the front page lists, what a category's page offers,
and what a new reader is asked to pick."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import OpenLibraryDataProvider
from pyopds2_openlibrary.browsing import (
    BROWSE_CATEGORIES,
    READING_INTERESTS,
    category,
    plain_keys,
    reading_interest,
    subcategory,
)


def _query(href: str) -> dict:
    return parse_qs(urlsplit(href).query)


class TestTheDesignsLists:
    def test_sixteen_categories_96_subcategories_and_15_hidden_rows(self):
        assert len(BROWSE_CATEGORIES) == 16
        assert sum(len(c["subcategories"]) for c in BROWSE_CATEGORIES) == 96
        assert sum(len(c["sources"]) - len(c["subcategories"]) for c in BROWSE_CATEGORIES) == 15

    def test_25_interests_in_the_designs_order(self):
        assert [i["presentable_name"] for i in READING_INTERESTS] == [
            "Crime", "True crime", "Thrillers", "Horror", "Romance", "Fantasy romance", "Sci-fi",
            "Fantasy", "Fiction", "Historical fiction", "Action and adventure", "Classics",
            "Children’s books", "Biography", "History", "Philosophy", "Religion", "Arts and culture",
            "Poetry", "Cooking", "Self-help", "Travel", "Business", "Politics", "Science",
        ]

    def test_codes_are_unique_in_their_list(self):
        codes = [c["code"] for c in BROWSE_CATEGORIES]
        assert len(set(codes)) == len(codes)
        codes = [i["code"] for i in READING_INTERESTS]
        assert len(set(codes)) == len(codes)
        for c in BROWSE_CATEGORIES:
            subs = [s["code"] for s in c["subcategories"]]
            assert len(set(subs)) == len(subs), c["code"]

    def test_each_names_the_icon_of_its_own_set(self):
        assert category("fantasy")["icon"] == "browse-fantasy"
        assert reading_interest("fantasy")["icon"] == "interest-fantasy"

    def test_a_category_with_no_subcategories_has_a_row_still(self):
        classics = category("classics")
        assert classics["subcategories"] == []
        assert classics["sources"] == ['subject_key:"classic_literature"']


class TestInterests:
    def test_an_interest_is_one_subcategory(self):
        interest = reading_interest("historical-fiction")
        assert interest["address"] == {"category": "fiction", "subcategory": "historical"}
        assert interest["sources"] == subcategory("fiction", "historical")["sources"]

    def test_an_interest_is_one_whole_category(self):
        interest = reading_interest("fantasy")
        assert interest["address"] == {"category": "fantasy"}
        # The hidden row and Fantasy romance's are in it too.
        assert interest["sources"] == category("fantasy")["sources"]
        assert 'subject_key:"fiction_romance_fantasy"' in interest["sources"]

    def test_politics_is_all_of_politics_and_law(self):
        assert reading_interest("politics")["address"] == {"category": "politics-and-law"}

    def test_science_is_the_science_chip_not_education_or_languages(self):
        assert reading_interest("science")["address"] == {"category": "science-and-learning", "subcategory": "science"}


class TestQueries:
    def test_a_category_is_any_of_its_rows_hidden_ones_included(self):
        query = openlibrary.genre_query(category("romance"))
        assert query.startswith('(subject_key:("fiction_romance_general" OR "love_stories" OR "romance" OR "romance_fiction") OR ')
        assert 'subject_key:"fiction_romance_paranormal_general")' in query
        assert query.endswith(' -subject:"content_warning:cover" ebook_access:[borrowable TO *]')

    def test_a_row_keeps_its_conditions(self):
        query = openlibrary.genre_query(subcategory("biography", "women"))
        assert query.startswith('subject_key:("biography" AND "women") ')

    def test_the_plain_keys_leave_out_a_key_that_counts_only_with_another(self):
        assert plain_keys('subject_key:("biography" AND "history")') == []
        assert plain_keys('(subject_key:"mathematics" OR subject_key:("algebra" AND "textbooks"))') == ["mathematics"]
        assert plain_keys('subject_key:("a" OR "b")') == ["a", "b"]


class TestBrowseTarget:
    def test_a_category_or_one_of_its_subcategories(self):
        assert openlibrary.browse_target("fantasy")["presentable_name"] == "Fantasy"
        assert openlibrary.browse_target("fantasy", "epic")["presentable_name"] == "Epic"

    def test_a_code_it_does_not_have_is_none(self):
        assert openlibrary.browse_target("westerns") is None
        assert openlibrary.browse_target("fantasy", "westerns") is None


class TestCategoryNavigation:
    def test_all_then_the_subcategories_the_one_shown_marked_self(self):
        nav = openlibrary.category_navigation("https://x/search", "sci-fi", "space-opera", sort="trending")
        assert [n.title for n in nav] == ["All", "Space opera", "Hard sci-fi"]
        assert [n.rel for n in nav] == [None, "self", None]
        assert _query(nav[0].href) == {"sort": ["trending"], "category": ["sci-fi"]}
        assert _query(nav[2].href) == {"sort": ["trending"], "category": ["sci-fi"], "subcategory": ["hard-sci-fi"]}

    def test_all_is_self_on_the_category(self):
        nav = openlibrary.category_navigation("https://x/search", "sci-fi")
        assert nav[0].rel == "self"

    def test_each_keeps_the_pages_order_filter_and_languages(self):
        nav = openlibrary.category_navigation(
            "https://x/search", "sci-fi", sort="new", mode="ebooks", language="en,fr",
            media_type="audiobook", access="print_disabled",
        )
        assert _query(nav[1].href) == {
            "sort": ["new"], "category": ["sci-fi"], "subcategory": ["space-opera"], "mode": ["ebooks"],
            "language": ["en,fr"], "media_type": ["audiobook"], "access": ["print_disabled"],
        }

    def test_none_for_a_category_with_no_subcategories_or_none_at_all(self):
        assert openlibrary.category_navigation("https://x/search", "classics") == []
        assert openlibrary.category_navigation("https://x/search", "westerns") == []


class TestFacetsOfACategory:
    def test_every_link_keeps_the_category_and_not_the_query(self):
        facets = OpenLibraryDataProvider.build_facets(
            base_url="https://x", query="ignored", sort="trending", title="Fantasy",
            address={"category": "fantasy", "subcategory": "epic"},
        )
        for group in facets:
            for link in group["links"]:
                query = _query(link["href"])
                assert query["category"] == ["fantasy"]
                assert query["subcategory"] == ["epic"]
                assert "query" not in query and "title" not in query
        # A category is opened in an order, so it offers Sort.
        assert facets[-1]["metadata"]["title"] == "Sort"


class TestInterestNavigation:
    def test_an_interest_on_the_front_page(self):
        nav = openlibrary.interest_navigation("https://x/search", reading_interest("true-crime"), language="en")
        assert nav.rel == openlibrary.REL_READING_INTEREST
        assert nav.title == "True crime"
        assert nav.properties == {"code": "true-crime", "icon": "interest-true-crime"}
        assert _query(nav.href) == {
            "sort": ["trending"], "category": ["crime-and-thrillers"], "subcategory": ["true-crime"], "language": ["en"],
        }
