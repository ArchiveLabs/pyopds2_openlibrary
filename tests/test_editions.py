"""The editions of a work: the model fields they ride in on, the hint each
alternate link carries, the ranking, and the work publication that lists them."""
from __future__ import annotations

from unittest.mock import patch

import pytest

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import OpenLibraryDataRecord


def _edition(**kwargs) -> OpenLibraryDataRecord.EditionDoc:
    return OpenLibraryDataRecord.EditionDoc.model_validate(kwargs)


class TestEditionModel:
    def test_parses_publish_year_and_opds_acquisitions(self):
        edition = _edition(
            key="/books/OL1M",
            publish_year=[2010],
            opds_acquisitions=[{
                "rel": "http://opds-spec.org/acquisition/open-access",
                "href": "https://archive.org/download/x/x.epub",
                "type": "application/epub+zip",
                "provider_name": "ia",
                "properties": {"openlibrary_source": "synthesized"},
            }],
        )
        assert edition.publish_year == [2010]
        assert edition.opds_acquisitions is not None
        assert edition.opds_acquisitions[0].rel.endswith("/open-access")
        assert edition.opds_acquisitions[0].provider_name == "ia"

    def test_provider_format_outside_the_old_four_is_kept(self):
        """Open Library also says ``daisy``; a Literal of four rejected the
        whole edition for it."""
        edition = _edition(key="/books/OL1M", providers=[{"format": "daisy", "url": "https://x"}])
        assert edition.providers[0].format == "daisy"

    def test_search_asks_for_the_edition_fields(self):
        with patch.object(openlibrary, "_get") as mock_get:
            mock_get.return_value.json.return_value = {"docs": [], "numFound": 0}
            openlibrary.OpenLibraryDataProvider.search(query="x")
        fields = mock_get.call_args.kwargs["params"]["fields"].split(",")
        assert "publish_year" in fields
        assert "editions.opds_acquisitions" in fields


OA = "http://opds-spec.org/acquisition/open-access"
BORROW = "http://opds-spec.org/acquisition/borrow"
BUY = "http://opds-spec.org/acquisition/buy"
IA_WEBPUB = "https://archive.org/services/loans/loan/?action=webpub&identifier={}&opds=1"


def _acq(rel, href="https://x", **extra):
    return {"rel": rel, "href": href, **extra}


@pytest.fixture(autouse=True)
def _languages():
    with patch.object(openlibrary, "fetch_languages_map", return_value={"eng": "en", "fre": "fr", "chi": "zh"}):
        yield


@pytest.fixture(autouse=True)
def _base_urls():
    OpenLibraryDataProvider = openlibrary.OpenLibraryDataProvider
    before = OpenLibraryDataProvider.OPDS_BASE_URL
    OpenLibraryDataProvider.OPDS_BASE_URL = "https://openlibrary.org/opds"
    yield
    OpenLibraryDataProvider.OPDS_BASE_URL = before


class TestOffer:
    def test_free_beats_a_loan_beats_a_sale(self):
        ranks = [openlibrary.offer_rank(rel, None, None) for rel in (OA, BORROW, BUY)]
        assert ranks == sorted(ranks)

    def test_a_loan_that_can_be_taken_beats_one_whose_state_is_unknown_beats_one_checked_out(self):
        ranks = [openlibrary.offer_rank(BORROW, state, None) for state in ("available", None, "unavailable")]
        assert ranks == sorted(ranks)

    def test_the_cheaper_sale_ranks_first_and_an_unpriced_one_last(self):
        cheap = openlibrary.offer_rank(BUY, None, {"value": 5, "currency": "USD"})
        dear = openlibrary.offer_rank(BUY, None, {"value": 20, "currency": "USD"})
        unpriced = openlibrary.offer_rank(BUY, None, None)
        assert cheap < dear < unpriced

    def test_best_of_open_librarys_own_rows(self):
        edition = _edition(key="/books/OL1M", opds_acquisitions=[
            _acq(BUY, provider_name="bwb", properties={"price": {"value": 20, "currency": "USD"}}),
            _acq(BORROW, provider_name="ia"),
            _acq("http://opds-spec.org/acquisition/sample", provider_name="ia"),
        ])
        offer = openlibrary.edition_offer(edition)
        assert offer == openlibrary.EditionOffer(BORROW, "ia", None, None)

    def test_a_loan_carries_the_editions_availability(self):
        edition = _edition(key="/books/OL1M", availability={"status": "borrow_unavailable"},
                           opds_acquisitions=[_acq(BORROW, provider_name="ia")])
        assert openlibrary.edition_offer(edition).availability == "unavailable"

    def test_a_sale_carries_its_price_and_no_availability(self):
        edition = _edition(key="/books/OL1M", availability={"status": "borrow_available"}, opds_acquisitions=[
            _acq(BUY, provider_name="bwb", properties={"price": {"value": 9.99, "currency": "USD"}})])
        assert openlibrary.edition_offer(edition) == openlibrary.EditionOffer(
            BUY, "bwb", {"value": 9.99, "currency": "USD"}, None)

    def test_an_archive_printing_without_rows_is_still_an_archive_offer(self):
        edition = _edition(key="/books/OL1M", ia=["x"], ebook_access="public")
        assert openlibrary.edition_offer(edition) == openlibrary.EditionOffer(OA, "ia", None, None)
        edition = _edition(key="/books/OL1M", ia=["x"], ebook_access="borrowable")
        assert openlibrary.edition_offer(edition).rel == BORROW

    def test_an_older_record_with_only_providers_is_read_through_the_converter(self):
        edition = _edition(key="/books/OL1M", ebook_access="public", providers=[
            {"url": "https://standardebooks.org/x.epub", "format": "epub", "access": "open-access", "provider_name": "standard_ebooks"}])
        assert openlibrary.edition_offer(edition) == openlibrary.EditionOffer(OA, "standard_ebooks", None, None)

    def test_nothing_to_offer_is_none(self):
        assert openlibrary.edition_offer(_edition(key="/books/OL1M", ebook_access="no_ebook")) is None


class TestHint:
    def test_type_year_language_cover_distributor_and_offer(self):
        edition = _edition(
            key="/books/OL2M", title="Howl's Moving Castle", publish_year=[2010, 2012], language=["eng"],
            cover_i=14625262, cover_width=360, cover_height=552,
            opds_acquisitions=[_acq(OA, provider_name="standard_ebooks", type="application/epub+zip")],
        )
        link = openlibrary.edition_alternate_link(edition, ["Diana Wynne Jones"])
        assert link.rel == "alternate"
        assert link.type == "application/opds-publication+json"
        assert link.href == "https://openlibrary.org/opds/books/OL2M"
        assert link.title == "Howl's Moving Castle"
        assert "authenticate" not in link.properties
        assert link.properties["edition"] == {
            "key": "OL2M",
            "@type": "http://schema.org/Book",
            "author": ["Diana Wynne Jones"],
            "published": "2010-01-01",
            "language": ["en"],
            "cover": {"href": "https://covers.openlibrary.org/b/id/14625262-M.jpg", "type": "image/jpeg", "width": 180, "height": 276},
            "distributor": "Standard Ebooks",
            "offer": {"rel": OA},
        }

    def test_an_archive_edition_points_at_the_archives_document_and_says_how_to_sign_in(self):
        edition = _edition(key="/books/OL3M", ia=["howls0000jone", "other"], ebook_access="borrowable",
                           language=["fre"], opds_acquisitions=[_acq(BORROW, provider_name="ia", type="text/html")])
        link = openlibrary.edition_alternate_link(edition, None)
        assert link.href == IA_WEBPUB.format("howls0000jone")
        assert link.properties["authenticate"]["type"] == "application/opds-authentication+json"
        hint = link.properties["edition"]
        assert hint["distributor"] == "Internet Archive"
        assert hint["offer"] == {"rel": BORROW}
        assert hint["language"] == ["fr"]
        assert "author" not in hint and "cover" not in hint and "published" not in hint

    def test_a_priced_edition_carries_the_price(self):
        edition = _edition(key="/books/OL4M", opds_acquisitions=[
            _acq(BUY, provider_name="bwb", type="text/html", properties={"price": {"value": 20, "currency": "USD"}})])
        hint = openlibrary.edition_alternate_link(edition, None).properties["edition"]
        assert hint["offer"] == {"rel": BUY, "price": {"value": 20, "currency": "USD"}}
        assert hint["distributor"] == "Better World Books"

    def test_an_audio_offer_makes_an_audiobook(self):
        edition = _edition(key="/books/OL5M", opds_acquisitions=[_acq(OA, provider_name="librivox", type="audio/mpeg")])
        assert openlibrary.edition_hint(edition, None, openlibrary.edition_offer(edition))["@type"] == "http://schema.org/Audiobook"

    def test_an_unknown_provider_is_named_from_its_id(self):
        assert openlibrary.distributor_name("feed_books") == "Feed Books"
        assert openlibrary.distributor_name(None) is None

    def test_no_offer_no_link(self):
        assert openlibrary.edition_alternate_link(_edition(key="/books/OL6M"), None) is None


class TestRanking:
    def test_best_offer_first_duplicates_once_and_the_offerless_left_out(self):
        buy = _edition(key="/books/OL1M", opds_acquisitions=[_acq(BUY, properties={"price": {"value": 1, "currency": "USD"}})])
        borrow = _edition(key="/books/OL2M", opds_acquisitions=[_acq(BORROW)])
        free = _edition(key="/books/OL3M", opds_acquisitions=[_acq(OA)])
        nothing = _edition(key="/books/OL4M")
        ranked = openlibrary.rank_editions([buy, borrow, free, borrow, nothing])
        assert [e.key for e in ranked] == ["/books/OL3M", "/books/OL2M", "/books/OL1M"]

    def test_the_given_order_holds_within_a_rank(self):
        first = _edition(key="/books/OL1M", opds_acquisitions=[_acq(BORROW)])
        second = _edition(key="/books/OL2M", opds_acquisitions=[_acq(BORROW)])
        assert [e.key for e in openlibrary.rank_editions([first, second])] == ["/books/OL1M", "/books/OL2M"]


class TestComposition:
    def test_one_query_per_tier_and_language_tiers_outer(self):
        queries = openlibrary._edition_queries("OL60149W", "en,fr", None)
        assert queries == [
            ("key:/works/OL60149W edition.ebook_access:public edition.language:eng", "en"),
            ("key:/works/OL60149W edition.ebook_access:public edition.language:fre", "fr"),
            ("key:/works/OL60149W edition.ebook_access:borrowable edition.language:eng", "en"),
            ("key:/works/OL60149W edition.ebook_access:borrowable edition.language:fre", "fr"),
        ]

    def test_no_language_is_one_query_per_tier_in_any_language(self):
        assert openlibrary._edition_queries("OL1W", None, None) == [
            ("key:/works/OL1W edition.ebook_access:public", None),
            ("key:/works/OL1W edition.ebook_access:borrowable", None),
        ]

    def test_the_clauses_are_scoped_to_editions_so_the_work_is_not_filtered(self):
        """A public-domain work's ``ebook_access`` is ``public``; an unscoped
        ``ebook_access:borrowable`` would match no work and list no loans."""
        for q, _ in openlibrary._edition_queries("OL66554W", "en", None):
            assert " ebook_access:" not in q and " language:" not in q

    def test_print_disabled_asks_for_that_tier_alone(self):
        assert [q for q, _ in openlibrary._edition_queries("OL1W", "en", "print_disabled")] == [
            "key:/works/OL1W edition.ebook_access:printdisabled edition.language:eng"]

    def test_editions_of_work_composes_and_ranks(self):
        def answer(url, *, params=None, **_):
            q = params["q"]
            edition = None
            if "public" in q and "fre" in q:
                edition = {"key": "/books/OLfreeM", "language": ["fre"], "ebook_access": "public", "ia": ["free"]}
            elif "borrowable" in q and "eng" in q:
                edition = {"key": "/books/OLloanM", "language": ["eng"], "ebook_access": "borrowable", "ia": ["loan"]}
            elif "borrowable" in q and "fre" in q:
                edition = {"key": "/books/OLloanM", "language": ["eng"], "ebook_access": "borrowable", "ia": ["loan"]}
            docs = [{"key": "/works/OL1W", "editions": {"docs": [edition] if edition else []}}]
            assert params["editions"] == "true" and params["limit"] == 1
            assert "editions.opds_acquisitions" in params["fields"]
            response = type("R", (), {})()
            response.json = lambda: {"docs": docs}
            return response

        with patch.object(openlibrary, "_get", side_effect=answer) as mock_get:
            editions = openlibrary.editions_of_work("OL1W", "en,fr")
        assert mock_get.call_count == 4
        assert [e.key for e in editions] == ["/books/OLfreeM", "/books/OLloanM"]
        assert mock_get.call_args_list[0].kwargs["params"]["lang"] == "en"


def _work_doc(**overrides):
    doc = {
        "key": "/works/OL60149W", "title": "Howl's Moving Castle", "subtitle": None,
        "description": "Sophie has the great misfortune…", "author_name": ["Diana Wynne Jones"], "author_key": ["OL34184A"],
        "cover_i": 1, "language": ["eng", "fre", "chi"], "number_of_pages_median": 329,
        "ratings_average": 4.3, "ratings_count": 812, "subject": ["Fantasy"],
        "editions": {"docs": [{"key": "/books/OL1M", "title": "Howl's Moving Castle", "ebook_access": "borrowable",
                                "ia": ["howls"], "language": ["eng"], "publish_year": [1986], "cover_i": 2,
                                "providers": [{"provider_name": "ia", "url": "https://archive.org/details/howls",
                                               "format": "web", "access": "borrow"}]}]},
    }
    doc.update(overrides)
    return doc


class TestWorkPublication:
    def test_a_feed_record_is_a_work_with_its_surfaced_edition_as_the_one_alternate(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        record.request_language = "en,fr"
        publication = record.to_publication().model_dump()
        self_link = next(l for l in publication["links"] if l["rel"] == "self")
        assert self_link["href"] == "https://openlibrary.org/opds/works/OL60149W?language=en,fr"
        editions = [l for l in publication["links"] if l.get("properties", {}).get("edition")]
        assert [l["href"] for l in editions] == [IA_WEBPUB.format("howls")]
        assert publication["metadata"]["title"] == "Howl's Moving Castle"
        assert publication["metadata"]["language"] == ["en"]
        assert publication["metadata"]["@type"] == "http://schema.org/Book"
        assert publication["metadata"]["description"].startswith("Sophie")
        assert publication["metadata"]["aggregateRating"]["ratingValue"] == 4.3
        assert publication["images"][0]["href"] == "https://covers.openlibrary.org/b/id/2-L.jpg"
        assert not any("/acquisition/" in str(l["rel"]) for l in publication["links"])

    def test_the_self_link_has_no_language_when_none_was_asked(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        self_link = next(l for l in record.to_work_publication().links if l.rel == "self")
        assert self_link.href == "https://openlibrary.org/opds/works/OL60149W"

    def test_the_composed_list_is_the_alternates_in_order_and_its_languages_the_metadata(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        free = _edition(key="/books/OL2M", title="Picture Book", language=["fre"], ebook_access="public", ia=["pb"], publish_year=[2010])
        loan = _edition(key="/books/OL1M", language=["eng"], ebook_access="borrowable", ia=["howls"])
        publication = record.to_work_publication([free, loan], "en,fr")
        hrefs = [l.href for l in publication.links if l.properties and "edition" in l.properties]
        assert hrefs == [IA_WEBPUB.format("pb"), IA_WEBPUB.format("howls")]
        assert publication.metadata.language == ["fr", "en"]
        assert publication.images[0].href == "https://covers.openlibrary.org/b/id/1-L.jpg"

    def test_a_work_with_no_english_edition_listed_is_not_described_in_english(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        french = _edition(key="/books/OL2M", language=["fre"], ebook_access="public", ia=["pb"])
        assert record.to_work_publication([french], "fr").metadata.description is None

    def test_the_edition_document_keeps_its_shape_and_gains_published(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        publication = record.to_edition_publication().model_dump()
        self_link = next(l for l in publication["links"] if l["rel"] == "self")
        assert self_link["href"] == "https://openlibrary.org/opds/books/OL1M"
        assert publication["metadata"]["published"] == "1986-01-01"
        alternates = [l for l in publication["links"] if l["type"] == "application/opds-publication+json" and l["rel"] == "alternate"]
        assert [l["href"] for l in alternates] == [IA_WEBPUB.format("howls")]
        assert "edition" not in alternates[0].get("properties", {})


class TestWorkRoute:
    def test_work_fetches_the_record_then_composes_its_editions(self):
        def answer(url, *, params=None, **_):
            q = params["q"]
            response = type("R", (), {})()
            if q == "key:/works/OL60149W":
                response.json = lambda: {"docs": [_work_doc()]}
            elif "public" in q:
                response.json = lambda: {"docs": [{"key": "/works/OL60149W", "editions": {"docs": [
                    {"key": "/books/OL2M", "title": "Free one", "language": ["eng"], "ebook_access": "public", "ia": ["free"]}]}}]}
            else:
                response.json = lambda: {"docs": [{"key": "/works/OL60149W", "editions": {"docs": []}}]}
            return response

        with patch.object(openlibrary, "_get", side_effect=answer):
            publication = openlibrary.OpenLibraryDataProvider.work("OL60149W", "en")
        dumped = publication.model_dump()
        assert next(l for l in dumped["links"] if l["rel"] == "self")["href"].endswith("/works/OL60149W?language=en")
        editions = [l for l in dumped["links"] if l.get("properties", {}).get("edition")]
        assert [l["title"] for l in editions] == ["Free one"]

    def test_no_such_work_is_none(self):
        with patch.object(openlibrary, "_get") as mock_get:
            mock_get.return_value.json.return_value = {"docs": []}
            assert openlibrary.OpenLibraryDataProvider.work("OL0W", None) is None

    def test_a_work_with_nothing_to_list_is_none(self):
        def answer(url, *, params=None, **_):
            response = type("R", (), {})()
            if params["q"] == "key:/works/OL60149W":
                response.json = lambda: {"docs": [_work_doc()]}
            else:
                response.json = lambda: {"docs": [{"key": "/works/OL60149W", "editions": {"docs": []}}]}
            return response

        with patch.object(openlibrary, "_get", side_effect=answer):
            assert openlibrary.OpenLibraryDataProvider.work("OL60149W", "zh") is None
