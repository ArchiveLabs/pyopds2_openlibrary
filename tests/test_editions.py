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
        edition = _edition(key="/books/OL5M", ia=["x"], ebook_access="public",
                           opds_acquisitions=[_acq(OA, provider_name="ia", type="audio/mpeg")])
        assert openlibrary.edition_hint(edition, None, openlibrary.edition_offer(edition))["@type"] == "http://schema.org/Audiobook"

    def test_better_world_books_is_named_as_open_library_spells_it(self):
        assert openlibrary.distributor_name("betterworldbooks") == "Better World Books"

    def test_an_unknown_provider_is_named_from_its_id(self):
        assert openlibrary.distributor_name("feed_books") == "Feed Books"
        assert openlibrary.distributor_name(None) is None

    def test_no_offer_no_link(self):
        assert openlibrary.edition_alternate_link(_edition(key="/books/OL6M"), None) is None


BWB_PUBLICATION = "https://www.betterworldbooks.com/opds/publication/9781737408802"


def _brick_house(**overrides):
    """The Brick House Apparent Quarterly, Vol. 1, as Open Library serves it
    on 2026-10-09: one printing the Archive lends and Better World Books sells."""
    doc = dict(
        key="/books/OL32926810M", title="The Brick House Apparent Quarterly, Vol. 1",
        ia=["isbn_9781737408802"], ebook_access="borrowable", language=["eng"],
        availability={"status": "borrow_available"},
        opds_acquisitions=[
            _acq(BUY, "https://www.betterworldbooks.com/purchase/9781737408802", type="text/html",
                 provider_name="betterworldbooks",
                 properties={"price": {"value": 1.25, "currency": "USD"},
                             "indirectAcquisition": [{"type": "application/epub+zip"}],
                             "openlibrary_source": "harvested"}),
            _acq(BORROW, "https://archive.org/details/isbn_9781737408802?view=theater&wrapper=false",
                 type="text/html", provider_name="ia", properties={"openlibrary_source": "synthesized"}),
        ],
    )
    doc.update(overrides)
    return _edition(**doc)


class TestOffersPerProvider:
    def test_a_printing_lent_and_sold_is_two_offers_best_first(self):
        assert openlibrary.edition_offers(_brick_house()) == [
            openlibrary.EditionOffer(BORROW, "ia", None, "available"),
            openlibrary.EditionOffer(BUY, "betterworldbooks", {"value": 1.25, "currency": "USD"}, None),
        ]

    def test_one_provider_keeps_its_best_offer_only(self):
        edition = _edition(key="/books/OL1M", opds_acquisitions=[
            _acq(BUY, provider_name="bwb", properties={"price": {"value": 20, "currency": "USD"}}),
            _acq(BUY, provider_name="bwb", properties={"price": {"value": 5, "currency": "USD"}}),
        ])
        assert openlibrary.edition_offers(edition) == [
            openlibrary.EditionOffer(BUY, "bwb", {"value": 5, "currency": "USD"}, None)]

    def test_brick_house_is_two_alternates_the_loan_first(self):
        links = openlibrary.edition_alternate_links([_brick_house()], ["Paul Ford"])
        assert [l.href for l in links] == [IA_WEBPUB.format("isbn_9781737408802"), BWB_PUBLICATION]
        loan, sale = links
        assert "authenticate" in loan.properties
        assert loan.properties["edition"]["distributor"] == "Internet Archive"
        assert loan.properties["edition"]["offer"] == {"rel": BORROW, "availability": {"state": "available"}}
        assert "authenticate" not in sale.properties
        hint = sale.properties["edition"]
        assert hint["distributor"] == "Better World Books"
        assert hint["offer"] == {"rel": BUY, "price": {"value": 1.25, "currency": "USD"}}
        assert {k: v for k, v in hint.items() if k not in ("distributor", "offer")} == \
            {k: v for k, v in loan.properties["edition"].items() if k not in ("distributor", "offer")}

    def test_an_edition_with_one_provider_is_the_link_it_always_was(self):
        edition = _edition(key="/books/OL3M", ia=["howls0000jone"], ebook_access="borrowable",
                           opds_acquisitions=[_acq(BORROW, provider_name="ia", type="text/html")])
        assert openlibrary.edition_alternate_links([edition], None) == [
            openlibrary.edition_alternate_link(edition, None)]

    def test_a_better_world_books_link_of_another_shape_points_at_the_edition_document(self):
        edition = _brick_house()
        edition.opds_acquisitions[0].href = "https://www.betterworldbooks.com/product/detail/x"
        links = openlibrary.edition_alternate_links([edition], None)
        assert links[1].href == "https://openlibrary.org/opds/books/OL32926810M"

    def test_two_offers_that_would_share_the_edition_document_get_distinct_hrefs(self):
        edition = _edition(key="/books/OL7M", ebook_access="public", opds_acquisitions=[
            _acq(OA, provider_name="standard_ebooks", type="application/epub+zip"),
            _acq(BUY, provider_name="kobo", type="text/html", properties={"price": {"value": 3, "currency": "USD"}}),
        ])
        assert [l.href for l in openlibrary.edition_alternate_links([edition], None)] == [
            "https://openlibrary.org/opds/books/OL7M",
            "https://openlibrary.org/opds/books/OL7M?provider=kobo",
        ]

    def test_offers_rank_across_editions(self):
        free = _edition(key="/books/OL9M", opds_acquisitions=[_acq(OA, provider_name="standard_ebooks")])
        links = openlibrary.edition_alternate_links([_brick_house(), free], None)
        assert [l.properties["edition"]["distributor"] for l in links] == \
            ["Standard Ebooks", "Internet Archive", "Better World Books"]


def _librivox_086(**overrides):
    """Librivox Short Story Collection 086 as Open Library serves it on
    2026-10-14: its one row is LibriVox's catalogue page, synthesized as an
    open-access ``audio/*`` acquisition, and the recording is the Archive's."""
    doc = dict(
        key="/books/OL33206228M", ia=["ss086_2004_librivox"], ebook_access="public",
        opds_acquisitions=[_acq(OA, "https://librivox.org/14917", type="audio/*", provider_name="librivox",
                                properties={"openlibrary_source": "synthesized"})],
        providers=[{"access": "open-access", "format": "audio", "url": "https://librivox.org/14917",
                    "provider_name": "librivox"}],
    )
    doc.update(overrides)
    return _edition(**doc)


class TestOffersTheirDocumentServes:
    def test_a_row_the_edition_document_leaves_out_is_no_offer(self):
        """The edition document draws no link for LibriVox's page, so an
        alternate pointing there would be a page with nothing to do."""
        assert openlibrary.edition_offers(_librivox_086(ia=None)) == []

    def test_the_archives_recording_is_the_offer_instead(self):
        assert openlibrary.edition_offers(_librivox_086()) == [
            openlibrary.EditionOffer(OA, "ia", None, None)]
        links = openlibrary.edition_alternate_links([_librivox_086()], None)
        assert [l.href for l in links] == [IA_WEBPUB.format("ss086_2004_librivox")]
        assert links[0].properties["edition"]["@type"] == "http://schema.org/Audiobook"

    def test_a_web_page_that_is_not_a_sale_is_no_offer(self):
        edition = _edition(key="/books/OL1M", opds_acquisitions=[
            _acq(OA, "https://standardebooks.org/ebooks/x", type="text/html", provider_name="standard_ebooks")])
        assert openlibrary.edition_offers(edition) == []

    def test_what_the_edition_document_draws_is_kept(self):
        edition = _edition(key="/books/OL1M", opds_acquisitions=[
            _acq(OA, "https://standardebooks.org/x.epub", type="application/epub+zip", provider_name="standard_ebooks"),
            _acq(BUY, "https://shop.example/x", type="text/html", provider_name="kobo"),
        ])
        assert [o.provider_name for o in openlibrary.edition_offers(edition)] == ["standard_ebooks", "kobo"]

    def test_the_archives_and_a_providers_own_rows_are_kept_whatever_their_type(self):
        assert openlibrary.edition_offers(_brick_house()) == [
            openlibrary.EditionOffer(BORROW, "ia", None, "available"),
            openlibrary.EditionOffer(BUY, "betterworldbooks", {"value": 1.25, "currency": "USD"}, None),
        ]


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

    def test_audiobooks_alone_ask_every_call_for_a_recording(self):
        """The search matched the work on its LibriVox recording; listing
        its printings first put an ebook on an audiobook shelf."""
        assert openlibrary._edition_queries("OL1W", "en", None, "audiobook") == [
            ("key:/works/OL1W edition.ebook_access:public edition.language:eng edition.id_librivox:*", "en"),
            ("key:/works/OL1W edition.ebook_access:borrowable edition.language:eng edition.id_librivox:*", "en"),
        ]

    def test_ebooks_or_both_list_every_edition(self):
        for media_type in ("ebook", "ebook,audiobook", None):
            for q, _ in openlibrary._edition_queries("OL1W", None, None, media_type):
                assert "id_librivox" not in q

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


class TestEditionsOfAFeedPage:
    def test_one_query_answers_every_work(self):
        q, _ = openlibrary._edition_queries(["OL1W", "OL2W"], None, None)[0]
        assert q == "key:(/works/OL1W OR /works/OL2W) edition.ebook_access:public"

    def test_each_work_gets_its_best_edition_under_each_pair_ranked(self):
        def answer(url, *, params=None, **_):
            assert params["limit"] == 2
            docs = []
            if "public" in params["q"]:
                docs = [{"key": "/works/OL1W", "editions": {"docs": [{"key": "/books/OL1freeM", "ebook_access": "public", "ia": ["f"]}]}}]
            elif "borrowable" in params["q"]:
                docs = [
                    {"key": "/works/OL1W", "editions": {"docs": [{"key": "/books/OL1loanM", "ebook_access": "borrowable", "ia": ["l"]}]}},
                    {"key": "/works/OL2W", "editions": {"docs": [{"key": "/books/OL2loanM", "ebook_access": "borrowable", "ia": ["m"]}]}},
                ]
            response = type("R", (), {})()
            response.json = lambda: {"docs": docs}
            return response

        with patch.object(openlibrary, "_get", side_effect=answer) as mock_get:
            listed = openlibrary.editions_of_works(["OL1W", "OL2W", "OL1W"], None, None)
        assert mock_get.call_count == 2
        assert {olid: [e.key for e in eds] for olid, eds in listed.items()} == {
            "OL1W": ["/books/OL1freeM", "/books/OL1loanM"],
            "OL2W": ["/books/OL2loanM"],
        }

    def test_no_works_no_calls(self):
        with patch.object(openlibrary, "_get") as mock_get:
            assert openlibrary.editions_of_works([], "en", None) == {}
        mock_get.assert_not_called()

    @pytest.mark.lists_editions
    def test_a_card_lists_what_its_work_page_lists(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        free = _edition(key="/books/OL2M", ebook_access="public", ia=["pb"], language=["eng"])
        loan = _edition(key="/books/OL1M", ebook_access="borrowable", ia=["howls"], language=["eng"])
        with patch.object(openlibrary, "editions_of_works", return_value={"OL60149W": [free, loan]}) as composed:
            openlibrary.list_editions([record], "en", None, "audiobook")
        composed.assert_called_once_with(["OL60149W"], "en", None, "audiobook")
        hrefs = [l.href for l in record.to_publication().links if l.properties and "edition" in l.properties]
        assert hrefs == [IA_WEBPUB.format("pb"), IA_WEBPUB.format("howls")]

    @pytest.mark.lists_editions
    def test_a_failed_composition_leaves_the_surfaced_edition(self, caplog):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        with patch.object(openlibrary, "editions_of_works", side_effect=RuntimeError("500")):
            openlibrary.list_editions([record], "en", None)
        assert record.listed_editions is None
        hrefs = [l.href for l in record.to_publication().links if l.properties and "edition" in l.properties]
        assert hrefs == [IA_WEBPUB.format("howls")]
        assert "composing the editions" in caplog.text

    @pytest.mark.lists_editions
    def test_search_lists_its_cards_editions_but_an_edition_lookup_does_not(self):
        answer = type("R", (), {})()
        answer.json = lambda: {"docs": [_work_doc()], "numFound": 1}
        with patch.object(openlibrary, "_get", return_value=answer), \
             patch.object(openlibrary, "list_editions") as listing:
            openlibrary.OpenLibraryDataProvider.search(query="howl", language="en", require_cover=False)
            assert listing.call_count == 1
            openlibrary.OpenLibraryDataProvider.search(query="edition_key:OL1M", require_cover=False)
            assert listing.call_count == 1


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
        assert self_link["href"] == "https://openlibrary.org/opds/works/OL60149W?language=en,fr&edition=OL1M"
        editions = [l for l in publication["links"] if l.get("properties", {}).get("edition")]
        assert [l["href"] for l in editions] == [IA_WEBPUB.format("howls")]
        assert publication["metadata"]["title"] == "Howl's Moving Castle"
        assert publication["metadata"]["language"] == ["en"]
        assert publication["metadata"]["@type"] == "http://schema.org/Book"
        assert publication["metadata"]["description"].startswith("Sophie")
        assert publication["metadata"]["aggregateRating"]["ratingValue"] == 4.3
        assert publication["images"][0]["href"] == "https://covers.openlibrary.org/b/id/2-L.jpg"
        assert not any("/acquisition/" in str(l["rel"]) for l in publication["links"])

    def test_the_self_link_echoes_audiobooks_alone_as_the_list_depends_on_it(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        record.request_media_type = "audiobook"
        self_link = next(l for l in record.to_work_publication().links if l.rel == "self")
        assert self_link.href == "https://openlibrary.org/opds/works/OL60149W?media_type=audiobook&edition=OL1M"
        for media_type in ("ebook", "ebook,audiobook"):
            record.request_media_type = media_type
            self_link = next(l for l in record.to_work_publication().links if l.rel == "self")
            assert self_link.href == "https://openlibrary.org/opds/works/OL60149W?edition=OL1M"

    def test_the_self_link_has_no_language_when_none_was_asked(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        self_link = next(l for l in record.to_work_publication().links if l.rel == "self")
        assert self_link.href == "https://openlibrary.org/opds/works/OL60149W?edition=OL1M"

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

    def test_the_title_is_the_leading_editions(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc(subtitle="A work subtitle"))
        french = _edition(key="/books/OL2M", title="Le Château de Hurle", language=["fre"], ebook_access="public", ia=["pb"])
        metadata = record.to_work_publication([french], "fr").metadata
        assert metadata.title == "Le Château de Hurle"
        assert metadata.subtitle is None

    def test_an_untitled_edition_keeps_the_works_title(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc(subtitle="A work subtitle"))
        untitled = _edition(key="/books/OL2M", ebook_access="public", ia=["pb"])
        metadata = record.to_work_publication([untitled]).metadata
        assert (metadata.title, metadata.subtitle) == ("Howl's Moving Castle", "A work subtitle")

    def test_a_pinned_edition_leads_although_another_ranks_better(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        loan = _edition(key="/books/OL1M", language=["eng"], ebook_access="borrowable", ia=["howls"])
        free = _edition(key="/books/OL2M", language=["eng"], ebook_access="public", ia=["pb"])
        publication = record.to_work_publication([loan, free], "en", pinned=True)
        hrefs = [l.href for l in publication.links if l.properties and "edition" in l.properties]
        assert hrefs == [IA_WEBPUB.format("howls"), IA_WEBPUB.format("pb")]
        assert next(l for l in publication.links if l.rel == "self").href.endswith("?language=en&edition=OL1M")

    def test_the_edition_document_keeps_its_shape_and_gains_published(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        publication = record.to_edition_publication().model_dump()
        self_link = next(l for l in publication["links"] if l["rel"] == "self")
        assert self_link["href"] == "https://openlibrary.org/opds/books/OL1M"
        assert publication["metadata"]["published"] == "1986-01-01"
        alternates = [l for l in publication["links"] if l["type"] == "application/opds-publication+json" and l["rel"] == "alternate"]
        assert [l["href"] for l in alternates] == [IA_WEBPUB.format("howls")]
        assert "edition" not in alternates[0].get("properties", {})


class TestEditionDocumentScopedToAProvider:
    def _record(self):
        doc = _work_doc()
        doc["editions"]["docs"][0]["providers"] = [
            {"provider_name": "ia", "url": "https://archive.org/details/howls", "format": "web", "access": "borrow"},
            {"provider_name": "kobo", "url": "https://kobo.example/howls", "format": "web", "access": "buy", "price": "3.00 USD"},
        ]
        return OpenLibraryDataRecord.model_validate(doc)

    def test_unscoped_it_has_every_providers_links(self):
        links = self._record().to_edition_publication().links
        assert any(l.href.startswith("https://archive.org/") for l in links)
        assert any(l.href == "https://kobo.example/howls" for l in links)

    def test_scoped_it_has_that_providers_links_alone_and_says_so_in_its_self(self):
        links = self._record().to_edition_publication("kobo").links
        assert next(l for l in links if l.rel == "self").href == "https://openlibrary.org/opds/books/OL1M?provider=kobo"
        assert not any(l.href.startswith("https://archive.org/") for l in links)
        assert any(l.href == "https://kobo.example/howls" for l in links)


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
        assert next(l for l in dumped["links"] if l["rel"] == "self")["href"].endswith("/works/OL60149W?language=en&edition=OL2M")
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


class TestPinnedEdition:
    """A link names the edition it showed; following it again shows that one."""

    def _answer(self, lookup=None):
        def answer(url, *, params=None, **_):
            q = params["q"]
            response = type("R", (), {})()
            if q == "key:/works/OL60149W":
                response.json = lambda: {"docs": [_work_doc()]}
            elif "edition_key:" in q:
                response.json = lambda: {"docs": [{"key": "/works/OL60149W", "editions": {"docs": [lookup]}}] if lookup else []}
            elif "public" in q:
                response.json = lambda: {"docs": [{"key": "/works/OL60149W", "editions": {"docs": [
                    {"key": "/books/OL2M", "title": "Free one", "language": ["eng"], "ebook_access": "public", "ia": ["free"]}]}}]}
            elif "borrowable" in q:
                response.json = lambda: {"docs": [{"key": "/works/OL60149W", "editions": {"docs": [
                    {"key": "/books/OL1M", "title": "Lent one", "language": ["eng"], "ebook_access": "borrowable", "ia": ["howls"]}]}}]}
            else:
                response.json = lambda: {"docs": [{"key": "/works/OL60149W", "editions": {"docs": []}}]}
            return response
        return answer

    def _alternates(self, publication):
        return [l for l in publication.model_dump()["links"] if l.get("properties", {}).get("edition")]

    def test_unpinned_the_best_offer_leads_and_the_self_names_it(self):
        with patch.object(openlibrary, "_get", side_effect=self._answer()):
            publication = openlibrary.OpenLibraryDataProvider.work("OL60149W", "en")
        assert [l["title"] for l in self._alternates(publication)] == ["Free one", "Lent one"]
        assert publication.metadata.title == "Free one"
        assert next(l for l in publication.links if l.rel == "self").href.endswith("&edition=OL2M")

    def test_a_listed_edition_named_leads_and_titles_the_page(self):
        with patch.object(openlibrary, "_get", side_effect=self._answer()) as mock_get:
            publication = openlibrary.OpenLibraryDataProvider.work("OL60149W", "en", edition="OL1M")
        assert [l["title"] for l in self._alternates(publication)] == ["Lent one", "Free one"]
        assert publication.metadata.title == "Lent one"
        assert next(l for l in publication.links if l.rel == "self").href.endswith("?language=en&edition=OL1M")
        assert not any("edition_key:" in c.kwargs["params"]["q"] for c in mock_get.call_args_list)

    def test_an_edition_fallen_out_of_the_list_is_fetched_and_leads(self):
        gone = {"key": "/books/OL9M", "title": "Old printing", "language": ["eng"], "ebook_access": "borrowable", "ia": ["old"]}
        with patch.object(openlibrary, "_get", side_effect=self._answer(gone)):
            publication = openlibrary.OpenLibraryDataProvider.work("OL60149W", "en", edition="OL9M")
        assert [l["title"] for l in self._alternates(publication)] == ["Old printing", "Free one", "Lent one"]
        assert next(l for l in publication.links if l.rel == "self").href.endswith("&edition=OL9M")

    def test_an_edition_the_work_does_not_have_is_ignored(self):
        with patch.object(openlibrary, "_get", side_effect=self._answer()):
            publication = openlibrary.OpenLibraryDataProvider.work("OL60149W", "en", edition="OL404M")
        assert [l["title"] for l in self._alternates(publication)] == ["Free one", "Lent one"]
        assert next(l for l in publication.links if l.rel == "self").href.endswith("&edition=OL2M")

    def test_a_feed_card_names_the_edition_it_shows(self):
        record = OpenLibraryDataRecord.model_validate(_work_doc())
        self_link = next(l for l in record.to_publication().links if l.rel == "self")
        assert self_link.href == "https://openlibrary.org/opds/works/OL60149W?edition=OL1M"
