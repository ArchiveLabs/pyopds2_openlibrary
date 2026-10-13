"""A work's related shelves: More by its author and Popular in its genre, as
``rel="related"`` feeds titled with the shelf's heading."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import OpenLibraryDataProvider, OpenLibraryDataRecord


def _related(links):
    return [l for l in links if l.rel == "related"]


def _work(**overrides) -> OpenLibraryDataRecord:
    doc = {
        "key": "/works/OL102749W", "title": "Moby Dick",
        "author_name": ["Herman Melville"], "author_key": ["OL29497A"],
        "subject": ["Whaling", "Science fiction", "History"],
    }
    doc.update(overrides)
    return OpenLibraryDataRecord.model_validate(doc)


def _genre(subjects):
    genre = openlibrary.work_genre(subjects)
    return genre["presentable_name"] if genre else None


class TestWorkGenre:
    def test_the_genre_the_most_subjects_name_wins_wherever_they_sit(self):
        # Pride and Prejudice's own: an exact "History" comes first.
        assert _genre([
            "History", "Fiction, romance, general", "Love stories", "Romance fiction",
            "Fiction, Romance, Historical, Regency",
        ]) == "Romance"

    def test_a_subject_names_a_genre_when_the_genres_words_run_through_it(self):
        assert _genre(["Fiction, fantasy, epic"]) == "Fantasy"
        assert _genre(["Detective and mystery stories"]) is None
        assert _genre(["Mystery and detective stories, English"]) == "Mystery and Detective Stories"

    def test_a_subject_names_only_the_longest_genre_it_does(self):
        # Nineteen Eighty-Four is not a science book.
        assert _genre(["Science fiction", "Fiction, science fiction, general", "Political science"]) == "Science Fiction"

    def test_a_word_is_not_a_part_of_one(self):
        assert _genre(["Artists", "Prehistory"]) is None

    def test_a_tie_goes_to_the_catalogues_order(self):
        assert _genre(["History", "Science fiction"]) == "Science Fiction"

    def test_a_work_in_no_genre_has_none(self):
        assert _genre(["Whaling", "Sea stories"]) is None

    def test_standard_ebooks_is_a_publisher_and_never_a_genre(self):
        assert openlibrary.work_genre(["Standard Ebooks"]) is None

    def test_no_subjects_no_genre(self):
        assert openlibrary.work_genre(None) is None


class TestRelatedLinks:
    def test_more_by_the_first_author_is_their_popular_books_that_can_be_had(self):
        more_by, _ = _related(_work().to_work_publication([], "en").links)
        assert more_by.title == "More by Herman Melville"
        assert more_by.type == "application/opds+json"
        assert more_by.href == "https://openlibrary.org/opds/authors/OL29497A/books?mode=ebooks&language=en&sort=rating"

    def test_popular_in_is_the_genres_own_front_page_feed(self):
        _, popular = _related(_work().to_work_publication([], "en").links)
        assert popular.title == "Popular in Science Fiction"
        genre = next(s for s in OpenLibraryDataProvider.FEATURED_SUBJECTS if s.get("key") == "/subjects/science_fiction")
        assert popular.href == openlibrary.genre_href("https://openlibrary.org/opds/search", genre, language="en")
        query = parse_qs(urlsplit(popular.href).query)
        assert query["sort"] == ["trending"]
        assert query["language"] == ["en"]
        assert query["query"] == ['subject_key:science_fiction -subject:"content_warning:cover" ebook_access:[borrowable TO *]']

    def test_no_language_asked_none_carried(self):
        more_by, popular = _related(_work().to_work_publication([]).links)
        assert "language" not in parse_qs(urlsplit(more_by.href).query)
        assert "language" not in parse_qs(urlsplit(popular.href).query)

    def test_a_work_with_no_author_key_has_no_more_by(self):
        links = _related(_work(author_key=None).to_work_publication([]).links)
        assert [l.title for l in links] == ["Popular in Science Fiction"]

    def test_a_work_in_no_genre_has_no_popular_in(self):
        links = _related(_work(subject=["Whaling"]).to_work_publication([]).links)
        assert [l.title for l in links] == ["More by Herman Melville"]

    def test_a_search_results_card_carries_them_too(self):
        record = _work()
        record.request_language = "fr"
        links = _related(record.to_publication().links)
        assert [l.title for l in links] == ["More by Herman Melville", "Popular in Science Fiction"]
        assert all("language=fr" in l.href for l in links)

    def test_the_service_base_names_the_feeds(self):
        saved = OpenLibraryDataProvider.OPDS_BASE_URL
        OpenLibraryDataProvider.OPDS_BASE_URL = "http://localhost:8080"
        try:
            more_by, popular = _related(_work().to_work_publication([]).links)
        finally:
            OpenLibraryDataProvider.OPDS_BASE_URL = saved
        assert more_by.href.startswith("http://localhost:8080/authors/OL29497A/books?")
        assert popular.href.startswith("http://localhost:8080/search?")


class TestGenreSubject:
    """The genre is said as a subject too, marked by its scheme, so a client
    can show it without reading it out of a shelf's title."""

    def _genre_subject(self, publication):
        subjects = publication.metadata.subject or []
        return next((s for s in subjects if getattr(s, "scheme", None) == openlibrary.SCHEME_GENRE), None)

    def test_the_genre_leads_the_subjects_with_its_scheme_and_code(self):
        publication = _work().to_work_publication([], "en")
        genre = self._genre_subject(publication)
        assert publication.metadata.subject[0] is genre
        assert genre.name == "Science Fiction"
        assert genre.code == "science_fiction"

    def test_it_links_the_same_feed_as_the_popular_in_shelf(self):
        publication = _work().to_work_publication([], "en")
        _, popular = _related(publication.links)
        assert self._genre_subject(publication).links[0].href == popular.href

    def test_a_search_results_card_says_it_too(self):
        record = _work()
        record.request_language = "fr"
        publication = record.to_publication()
        _, popular = _related(publication.links)
        assert self._genre_subject(publication).links[0].href == popular.href

    def test_a_work_in_no_genre_has_no_genre_subject(self):
        publication = _work(subject=["Whaling"]).to_work_publication([])
        assert self._genre_subject(publication) is None
        assert [s.name for s in publication.metadata.subject] == ["Whaling"]

    def test_the_other_subjects_carry_no_scheme(self):
        publication = _work().to_work_publication([])
        assert [s.scheme for s in publication.metadata.subject[1:]] == [None, None, None]

    def test_it_is_written_out_with_its_scheme_and_code(self):
        dumped = _work().to_work_publication([], "en").model_dump(mode="json", exclude_none=True, by_alias=True)
        first = dumped["metadata"]["subject"][0]
        assert first["scheme"] == openlibrary.SCHEME_GENRE
        assert first["code"] == "science_fiction"
        assert first["name"] == "Science Fiction"
