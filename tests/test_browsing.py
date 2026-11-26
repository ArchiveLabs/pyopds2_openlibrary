"""The browsing categories, their subcategories and the reading interests
(``browsing.py``): what the front page lists, what a category's page offers,
and what a new reader is asked to pick."""
from __future__ import annotations

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import OpenLibraryDataProvider
from pyopds2_openlibrary.browsing import (
    BROWSE_CATEGORIES,
    NOT_FICTION,
    READING_INTERESTS,
    category,
    plain_keys,
    reading_interest,
    subcategory,
)


def _query(href: str) -> dict:
    return parse_qs(urlsplit(href).query)


class TestTheDesignsLists:
    def test_seventeen_categories_95_subcategories_and_15_hidden_rows(self):
        assert len(BROWSE_CATEGORIES) == 17
        assert sum(len(c["subcategories"]) for c in BROWSE_CATEGORIES) == 95
        assert sum(len(c["sources"]) - len(c["subcategories"]) for c in BROWSE_CATEGORIES) == 15

    def test_26_interests_in_the_designs_order(self):
        assert [i["presentable_name"] for i in READING_INTERESTS] == [
            "Crime", "True crime", "Thrillers", "Horror", "Romance", "Fantasy", "Fantasy romance",
            "Sci-fi", "Fiction", "Historical fiction", "Adventure", "Classics", "Children’s books",
            "Humor", "Biography", "History", "Philosophy", "Religion", "Arts", "Poetry", "Cooking",
            "Self-help", "Travel", "Business", "Politics", "Science",
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

    def test_humor_is_a_category_of_its_own_with_no_subcategories(self):
        humor = category("humor")
        assert humor["subcategories"] == []
        assert humor["sources"] == ['subject_key:"humor"']
        assert subcategory("arts", "humor") is None

    def test_funny_is_childrens_fiction_that_is_humorous(self):
        assert subcategory("childrens-books", "funny")["sources"] == [
            'subject_key:("children\'s_fiction" AND "humorous_stories")',
        ]

    def test_the_politics_chip_is_political_science_alone(self):
        assert subcategory("politics-and-law", "politics")["sources"] == [
            f'(subject_key:"political_science" AND {NOT_FICTION})',
        ]


class TestNonFiction:
    def test_a_non_fiction_row_leaves_out_fiction(self):
        assert subcategory("history", "ancient")["sources"] == [
            f'(subject_key:("ancient_civilization" OR "ancient_history") AND {NOT_FICTION})',
        ]
        assert NOT_FICTION == 'NOT subject_key:("children\'s_fiction" OR "fiction" OR "juvenile_fiction")'

    def test_every_row_of_a_non_fiction_category_hidden_ones_included(self):
        for code in (
            "biography", "history", "philosophy-and-religion", "health-and-self-help",
            "food-and-lifestyle", "business", "politics-and-law", "science-and-learning",
        ):
            assert all(s.endswith(f" AND {NOT_FICTION})") for s in category(code)["sources"]), code

    def test_poetry_and_theater_are_literary_forms_and_keep_fiction(self):
        assert subcategory("arts", "poetry")["sources"] == ['subject_key:"poetry"']
        assert subcategory("arts", "theater")["sources"] == ['subject_key:("drama" OR "plays" OR "theater")']
        assert subcategory("arts", "music")["sources"][0].endswith(f" AND {NOT_FICTION})")
        assert category("arts")["sources"][-1] == f'(subject_key:"sculpture" AND {NOT_FICTION})'

    def test_fiction_categories_and_humor_keep_fiction(self):
        for code in ("crime-and-thrillers", "romance", "fantasy", "sci-fi", "fiction", "classics", "childrens-books", "humor"):
            assert not any(NOT_FICTION in s for s in category(code)["sources"]), code

    def test_an_interest_of_a_non_fiction_category_leaves_out_fiction_too(self):
        assert all(NOT_FICTION in s for s in reading_interest("science")["sources"])
        arts = reading_interest("arts")["sources"]
        # All but Poetry and Theater.
        assert sum(NOT_FICTION in s for s in arts) == len(arts) - 2

    def test_the_fiction_keys_a_row_leaves_out_do_not_name_the_category(self):
        assert "fiction" not in category("history")["keys"]
        assert "juvenile_fiction" not in category("science-and-learning")["keys"]

    def test_the_whole_query_still_takes_the_readers_languages(self):
        with patch.object(openlibrary, "fetch_languages_map", return_value={"eng": "en", "fre": "fr"}):
            query = openlibrary.genre_author_query(category("history"), "en,fr")
        assert query.startswith(f'((subject_key:"history" AND {NOT_FICTION}) OR ')
        assert query.endswith(" language:(eng OR fre)")


class TestInterests:
    def test_an_interest_is_addressed_by_its_own_code(self):
        # Not by its category's codes: its page has no subcategories to
        # choose from, as a category's has.
        assert reading_interest("crime")["address"] == {"interest": "crime"}
        assert reading_interest("romance")["address"] == {"interest": "romance"}

    def test_an_interest_is_one_subcategory(self):
        interest = reading_interest("historical-fiction")
        assert interest["target"] == {"category": "fiction", "subcategory": "historical"}
        assert interest["sources"] == subcategory("fiction", "historical")["sources"]

    def test_an_interest_is_one_whole_category(self):
        interest = reading_interest("fantasy")
        assert interest["target"] == {"category": "fantasy"}
        # The hidden row and Fantasy romance's are in it too.
        assert interest["sources"] == category("fantasy")["sources"]
        assert 'subject_key:"fiction_romance_fantasy"' in interest["sources"]

    def test_new_and_renamed_interests(self):
        assert reading_interest("humor")["target"] == {"category": "humor"}
        assert reading_interest("humor")["icon"] == "interest-humor"
        assert reading_interest("arts")["target"] == {"category": "arts"}
        assert reading_interest("adventure")["target"] == {"category": "fiction", "subcategory": "adventure"}
        assert reading_interest("arts-and-culture") is None
        assert reading_interest("action-and-adventure") is None

    def test_politics_is_all_of_politics_and_law(self):
        assert reading_interest("politics")["target"] == {"category": "politics-and-law"}

    def test_science_is_the_science_chip_not_education_or_languages(self):
        assert reading_interest("science")["target"] == {"category": "science-and-learning", "subcategory": "science"}


class TestQueries:
    def test_a_category_is_any_of_its_rows_hidden_ones_included(self):
        query = openlibrary.genre_query(category("romance"))
        assert query.startswith('(subject_key:("fiction_romance_general" OR "love_stories" OR "romance" OR "romance_fiction") OR ')
        assert 'subject_key:"fiction_romance_paranormal_general")' in query
        assert query.endswith(' -subject:"content_warning:cover" ebook_access:[borrowable TO *]')

    def test_a_row_keeps_its_conditions(self):
        query = openlibrary.genre_query(subcategory("biography", "women"))
        assert query.startswith(f'(subject_key:("biography" AND "women") AND {NOT_FICTION}) ')

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
        assert [n.rel for n in nav] == [openlibrary.REL_ALL, "self", None]
        assert _query(nav[0].href) == {"sort": ["trending"], "category": ["sci-fi"]}
        assert _query(nav[2].href) == {"sort": ["trending"], "category": ["sci-fi"], "subcategory": ["hard-sci-fi"]}

    def test_all_is_self_on_the_category(self):
        nav = openlibrary.category_navigation("https://x/search", "sci-fi")
        assert nav[0].rel == ["self", openlibrary.REL_ALL]
        assert all(n.rel is None for n in nav[1:])

    def test_all_says_what_it_is(self):
        assert openlibrary.REL_ALL == "https://openlibrary.org/opds/rel/all"

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
            "sort": ["trending"], "interest": ["true-crime"], "language": ["en"],
        }
