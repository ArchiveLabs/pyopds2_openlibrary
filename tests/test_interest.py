"""The reading interest a work counts for (``work_interest``), said as a
subject with its own scheme, so a client can count a reader's library in
the words of the interests it asks them to pick."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import OpenLibraryDataRecord
from pyopds2_openlibrary.browsing import (
    READING_INTERESTS,
    clause_matches,
    subject_key,
    work_interest,
)


def _code(subjects):
    interest = work_interest(subjects)
    return interest["code"] if interest else None


class TestSubjectKey:
    def test_is_open_librarys_key_for_the_name(self):
        assert subject_key("Fiction, romance, fantasy") == "fiction_romance_fantasy"
        assert subject_key("Fiction, mystery & detective, police procedural") == (
            "fiction_mystery__detective_police_procedural"
        )
        assert subject_key("Children's fiction") == "children's_fiction"


class TestClauseMatches:
    def test_one_key_and_a_group_of_keys(self):
        assert clause_matches('subject_key:"a"', {"a"}, set())
        assert clause_matches('subject_key:("a" OR "b")', {"b"}, set())
        assert not clause_matches('subject_key:("a" OR "b")', {"c"}, set())

    def test_keys_that_count_only_together(self):
        assert clause_matches('subject_key:("a" AND "b")', {"a", "b"}, set())
        assert not clause_matches('subject_key:("a" AND "b")', {"a"}, set())

    def test_a_row_that_leaves_out_fiction(self):
        row = '(subject_key:"history" AND NOT subject_key:("fiction" OR "juvenile_fiction"))'
        assert clause_matches(row, {"history"}, set())
        assert not clause_matches(row, {"history", "fiction"}, set())

    def test_a_subject_by_its_name(self):
        row = '(subject_key:"a" OR (subject:"Business" AND subject_key:"textbooks"))'
        assert clause_matches(row, {"textbooks"}, {"business"})
        assert not clause_matches(row, {"textbooks"}, set())

    def test_every_interests_rows_can_be_read(self):
        for interest in READING_INTERESTS:
            for source in interest["sources"]:
                clause_matches(source, set(), set())


class TestWorkInterest:
    def test_the_most_specific_interest_wins(self):
        # A fantasy romance is Fantasy romance, not Fantasy or Romance.
        assert _code(["Fantasy fiction", "Fiction, romance, fantasy", "Love stories"]) == "fantasy-romance"

    def test_a_whole_category(self):
        assert _code(["Fantasy fiction"]) == "fantasy"
        assert _code(["Love stories"]) == "romance"

    def test_a_novel_is_not_counted_for_a_non_fiction_interest(self):
        assert _code(["Biography", "Fiction"]) == "fiction"

    def test_among_as_specific_the_most_named_then_the_onboardings_order(self):
        assert _code(["Biography", "History"]) == "biography"
        assert _code(["History", "Ancient history", "Biography"]) == "history"

    def test_a_work_in_no_interest_has_none(self):
        assert _code(["Whaling"]) is None
        assert _code([]) is None
        assert _code(None) is None


class TestInterestSubject:
    def _work(self, subjects):
        return OpenLibraryDataRecord.model_validate({
            "key": "/works/OL1W", "title": "A book",
            "author_name": ["An Author"], "author_key": ["OL1A"],
            "subject": subjects,
        })

    def _interest(self, publication):
        return next(
            (s for s in publication.metadata.subject or [] if s.scheme == openlibrary.SCHEME_INTEREST),
            None,
        )

    def test_follows_the_genre_with_its_scheme_code_and_feed(self):
        publication = self._work(["Fantasy fiction", "Fiction, romance, fantasy"]).to_work_publication([], "en")
        subjects = publication.metadata.subject
        assert subjects[0].scheme == openlibrary.SCHEME_GENRE
        interest = self._interest(publication)
        assert subjects[1] is interest
        assert (interest.name, interest.code) == ("Fantasy romance", "fantasy-romance")
        query = parse_qs(urlsplit(interest.links[0].href).query)
        assert query["interest"] == ["fantasy-romance"]
        assert query["language"] == ["en"]

    def test_a_work_in_no_interest_says_none(self):
        assert self._interest(self._work(["Whaling"]).to_work_publication([])) is None

    def test_it_is_written_out_with_its_scheme_and_code(self):
        dumped = self._work(["Love stories"]).to_work_publication([]).model_dump(
            mode="json", exclude_none=True, by_alias=True,
        )
        interest = next(s for s in dumped["metadata"]["subject"] if s.get("scheme") == openlibrary.SCHEME_INTEREST)
        assert interest["code"] == "romance"
