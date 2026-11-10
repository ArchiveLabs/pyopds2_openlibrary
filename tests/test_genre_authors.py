"""A genre's authors: the writers its readers read, for a client to offer
to follow."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import GenreAuthor


def _doc(keys, names, readers):
    return {"author_key": keys, "author_name": names, "readinglog_count": readers}


class TestRankGenreAuthors:
    def test_an_author_is_ranked_by_the_readers_of_all_their_works(self):
        ranked = openlibrary.rank_genre_authors([
            _doc(["OL1A"], ["Big Book"], 900),
            _doc(["OL2A"], ["Steady"], 500),
            _doc(["OL2A"], ["Steady"], 500),
            _doc(["OL1A"], ["Big Book"], 50),
        ])
        assert [(a.name, a.readers, a.works) for a in ranked] == [("Steady", 1000, 2), ("Big Book", 950, 2)]

    def test_a_work_counts_for_its_first_author_only(self):
        ranked = openlibrary.rank_genre_authors([
            _doc(["OL1A", "OL9A"], ["Writer", "Illustrator"], 10),
            _doc(["OL1A", "OL9A"], ["Writer", "Illustrator"], 10),
        ])
        assert [a.key for a in ranked] == ["OL1A"]

    def test_one_book_in_the_genre_comes_after_a_writer_of_it(self):
        ranked = openlibrary.rank_genre_authors([
            _doc(["OL1A"], ["Orwell"], 10_000),
            _doc(["OL2A"], ["Tolkien"], 100),
            _doc(["OL2A"], ["Tolkien"], 100),
        ])
        assert [a.name for a in ranked] == ["Tolkien", "Orwell"]

    def test_a_tie_goes_to_the_author_seen_first(self):
        ranked = openlibrary.rank_genre_authors([_doc(["OL2A"], ["B"], 5), _doc(["OL1A"], ["A"], 5)])
        assert [a.name for a in ranked] == ["B", "A"]

    def test_a_work_with_no_author_is_left_out(self):
        assert openlibrary.rank_genre_authors([{"readinglog_count": 9}, _doc([], [], 3)]) == []

    def test_a_missing_count_is_none_read(self):
        ranked = openlibrary.rank_genre_authors([{"author_key": ["OL1A"], "author_name": ["A"]}])
        assert ranked == [GenreAuthor("OL1A", "A", 0, 1)]


class TestInterleave:
    def test_each_genre_gives_its_next_in_turn(self):
        a = [GenreAuthor(f"OLa{i}A", f"a{i}", 0, 2) for i in range(3)]
        b = [GenreAuthor(f"OLb{i}A", f"b{i}", 0, 2) for i in range(1)]
        assert [x.name for x in openlibrary.interleave_genre_authors([a, b], 12)] == ["a0", "b0", "a1", "a2"]

    def test_an_author_in_two_genres_is_listed_once(self):
        shared = GenreAuthor("OL1A", "Shared", 0, 2)
        out = openlibrary.interleave_genre_authors([[shared], [shared, GenreAuthor("OL2A", "Other", 0, 2)]], 12)
        assert [x.name for x in out] == ["Shared", "Other"]

    def test_the_list_stops_at_the_limit(self):
        a = [GenreAuthor(f"OL{i}A", str(i), 0, 2) for i in range(20)]
        assert len(openlibrary.interleave_genre_authors([a], 12)) == 12

    def test_no_limit_is_every_author(self):
        a = [GenreAuthor(f"OL{i}A", str(i), 0, 2) for i in range(20)]
        b = [GenreAuthor(f"OLb{i}A", str(i), 0, 2) for i in range(5)]
        assert len(openlibrary.interleave_genre_authors([a, b])) == 25

    def test_no_genres_no_authors(self):
        assert openlibrary.interleave_genre_authors([], 12) == []


class TestGenreAuthorQuery:
    def test_it_asks_for_the_genres_books_as_its_feed_does(self):
        fantasy = openlibrary.featured_genre("fantasy")
        assert openlibrary.genre_author_query(fantasy) == (
            'subject_key:fantasy -subject:"content_warning:cover" ebook_access:[borrowable TO *]'
        )

    def test_the_readers_languages_narrow_it(self):
        crime = openlibrary.featured_genre("crime")
        q = openlibrary.genre_author_query(crime, "en,fr")
        assert q.startswith("subject_key:(mystery_and_detective_stories OR thriller OR thrillers) ")
        assert q.endswith(" language:(eng OR fre)")

    def test_an_unknown_code_is_no_genre(self):
        assert openlibrary.featured_genre("westerns") is None


class TestFetchGenreAuthors:
    def test_one_request_for_the_genres_most_read_works(self):
        response = MagicMock()
        response.json.return_value = {"docs": [_doc(["OL1A"], ["A"], 3), _doc(["OL1A"], ["A"], 4)]}
        with patch("pyopds2_openlibrary._get", return_value=response) as get:
            authors = openlibrary.fetch_genre_authors(openlibrary.featured_genre("romance"), "en")
        assert authors == [GenreAuthor("OL1A", "A", 7, 2)]
        params = get.call_args.kwargs["params"]
        assert get.call_args.args[0].endswith("/search.json")
        assert params["sort"] == "readinglog"
        assert params["limit"] == openlibrary.GENRE_AUTHOR_WINDOW
        assert params["fields"] == "author_key,author_name,readinglog_count"
        assert params["q"].endswith("language:eng")
