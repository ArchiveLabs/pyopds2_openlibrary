from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pyopds2 import DataProvider

from pyopds2_openlibrary import (
    OpenLibraryDataProvider,
    OpenLibraryDataRecord,
    _build_availability_links,
    _build_external_acquisition_link,
    _build_ia_alternate_link,
    _ebook_access_rank,
    _has_acquisition_options,
    _has_buyable_provider,
    _is_currently_available,
    _parse_price_amount,
    _resolve_preferred_edition,
    build_facets,
    fetch_facet_counts,
    fetch_languages_map,
    map_ol_format_to_mime,
    marc_language_to_iso_639_1,
    ol_acquisition_to_opds_links,
)


def _provider(**kwargs) -> OpenLibraryDataRecord.EditionProvider:
    return OpenLibraryDataRecord.EditionProvider.model_validate(kwargs)


def _edition(**kwargs) -> OpenLibraryDataRecord.EditionDoc:
    return OpenLibraryDataRecord.EditionDoc.model_validate(kwargs)


def _record(**kwargs) -> OpenLibraryDataRecord:
    return OpenLibraryDataRecord.model_validate(kwargs)


def _record_with_edition_providers(*providers: OpenLibraryDataRecord.EditionProvider) -> OpenLibraryDataRecord:
    return _record(
        key="/works/OL1W",
        title="Work",
        editions={
            "docs": [
                {
                    "key": "/books/OL1M",
                    "title": "Edition",
                    "providers": [p.model_dump(exclude_none=True) for p in providers],
                }
            ]
        },
    )


class TestModels:
    def test_openlibrary_data_record_parses_full_doc(self):
        doc = {
            "key": "/works/OL45804W",
            "title": "Fantastic Mr Fox",
            "subtitle": "A Story",
            "description": "A fox story",
            "cover_i": 8739161,
            "ebook_access": "printdisabled",
            "language": ["eng"],
            "ia": ["fantasticmrfox00dahl"],
            "author_name": ["Roald Dahl"],
            "author_key": ["OL34184A"],
            "number_of_pages_median": 200,
            "editions": {
                "numFound": 1,
                "docs": [
                    {
                        "key": "/books/OL7353617M",
                        "title": "Fantastic Mr Fox",
                        "cover_i": 8739161,
                        "ia": ["fantasticmrfox00dahl"],
                        "providers": [
                            {
                                "provider_name": "ia",
                                "url": "https://archive.org/details/fantasticmrfox00dahl",
                                "access": "borrow",
                                "format": "web",
                                "price": "0.00 USD",
                            }
                        ],
                    }
                ],
            },
        }
        record = OpenLibraryDataRecord.model_validate(doc)
        assert record.key == "/works/OL45804W"
        assert record.editions is not None
        assert record.editions.docs is not None
        assert record.editions.docs[0].providers[0].provider_name == "ia"

    def test_openlibrary_data_record_parses_minimal_doc(self):
        record = OpenLibraryDataRecord.model_validate({"key": "/works/OL1W", "title": "Minimal"})
        assert record.key == "/works/OL1W"
        assert record.title == "Minimal"
        assert record.editions is None

    @pytest.mark.parametrize("status", ["borrow_available", "borrow_unavailable", "open", "private", "error"])
    def test_edition_availability_valid_statuses(self, status: str):
        availability = OpenLibraryDataRecord.EditionAvailability.model_validate({"status": status})
        assert availability.status == status

    def test_edition_provider_parsing_full_and_optional_missing(self):
        full = OpenLibraryDataRecord.EditionProvider.model_validate(
            {
                "access": "borrow",
                "format": "epub",
                "price": "9.99 USD",
                "url": "https://example.org/book.epub",
                "provider_name": "Standard Ebooks",
            }
        )
        assert full.access == "borrow"
        assert full.format == "epub"
        assert full.provider_name == "Standard Ebooks"

        minimal = OpenLibraryDataRecord.EditionProvider.model_validate({"url": "https://example.org"})
        assert minimal.url == "https://example.org"
        assert minimal.access is None
        assert minimal.format is None
        assert minimal.price is None
        assert minimal.provider_name is None

    def test_editions_result_set_parsing_docs_and_null_docs(self):
        with_docs = OpenLibraryDataRecord.EditionsResultSet.model_validate(
            {"numFound": 1, "docs": [{"key": "/books/OL1M", "title": "Edition"}]}
        )
        assert with_docs.docs is not None
        assert len(with_docs.docs) == 1

        null_docs = OpenLibraryDataRecord.EditionsResultSet.model_validate({"numFound": 0, "docs": None})
        assert null_docs.docs is None


class TestLinks:
    def test_base_links_always_present(self):
        links = _record(key="/works/OL1W", title="Book").links()
        assert len(links) == 3
        assert any(l.rel == "self" for l in links)
        assert any(l.rel == "alternate" and l.type == "text/html" for l in links)
        assert any(l.rel == "alternate" and l.type == "application/json" for l in links)

    def test_no_providers_only_base_links(self):
        record = _record(
            key="/works/OL2W",
            title="Book",
            editions={"docs": [{"key": "/books/OL2M", "title": "Edition", "providers": []}]},
        )
        assert len(record.links()) == 3

    def test_ia_provider_with_ia_builds_alternate_webpub_link(self):
        record = _record(
            key="/works/OL3W",
            title="Book",
            editions={
                "docs": [
                    {
                        "key": "/books/OL3M",
                        "title": "Edition",
                        "ia": ["fooid"],
                        "providers": [{"provider_name": "ia", "url": "https://archive.org/details/fooid", "access": "borrow", "format": "web"}],
                    }
                ]
            },
        )
        links = record.links()
        alt = next(l for l in links if l.rel == "alternate" and l.type == "application/opds-publication+json")
        assert "identifier=fooid" in alt.href
        assert alt.properties["authenticate"]["type"] == "application/opds-authentication+json"

    def test_ia_provider_without_edition_ia_falls_back_to_external_acquisition(self):
        record = _record(
            key="/works/OL4W",
            title="Book",
            editions={
                "docs": [
                    {
                        "key": "/books/OL4M",
                        "title": "Edition",
                        "providers": [{"provider_name": "ia", "url": "https://archive.org/details/fooid", "access": "borrow", "format": "web"}],
                    }
                ]
            },
        )
        links = record.links()
        acq = [l for l in links if "/acquisition" in l.rel]
        assert len(acq) == 1

    def test_non_ia_provider_yields_acquisition_link(self):
        record = _record(
            key="/works/OL5W",
            title="Book",
            editions={
                "docs": [
                    {
                        "key": "/books/OL5M",
                        "title": "Edition",
                        "providers": [{"provider_name": "standardebooks", "url": "https://standardebooks.org", "access": "borrow", "format": "epub"}],
                    }
                ]
            },
        )
        acq = [l for l in record.links() if "/acquisition" in l.rel]
        assert len(acq) == 1
        assert acq[0].rel == "http://opds-spec.org/acquisition/borrow"

    def test_multiple_providers_mix_ia_and_non_ia(self):
        record = _record(
            key="/works/OL6W",
            title="Book",
            editions={
                "docs": [
                    {
                        "key": "/books/OL6M",
                        "title": "Edition",
                        "ia": ["ia-id"],
                        "providers": [
                            {"provider_name": "ia", "url": "https://archive.org/details/ia-id", "access": "borrow", "format": "web"},
                            {"provider_name": "standardebooks", "url": "https://standardebooks.org/book.epub", "access": "borrow", "format": "epub"},
                        ],
                    }
                ]
            },
        )
        links = record.links()
        assert any(l.rel == "alternate" and l.type == "application/opds-publication+json" for l in links)
        assert any("/acquisition" in l.rel for l in links)

    def test_provider_without_url_is_skipped(self):
        record = _record(
            key="/works/OL7W",
            title="Book",
            editions={
                "docs": [
                    {
                        "key": "/books/OL7M",
                        "title": "Edition",
                        "providers": [{"provider_name": "ia", "access": "borrow", "format": "web", "url": None}],
                    }
                ]
            },
        )
        assert len(record.links()) == 3

    def test_self_link_uses_opds_base_url_when_set(self):
        original = OpenLibraryDataProvider.OPDS_BASE_URL
        OpenLibraryDataProvider.OPDS_BASE_URL = "https://opds.example.org"
        try:
            links = _record(key="/works/OL8W", title="Book").links()
            self_link = next(l for l in links if l.rel == "self")
            assert self_link.href == "https://opds.example.org/works/OL8W"
        finally:
            OpenLibraryDataProvider.OPDS_BASE_URL = original

    def test_self_link_falls_back_to_base_url_opds(self):
        original = OpenLibraryDataProvider.OPDS_BASE_URL
        OpenLibraryDataProvider.OPDS_BASE_URL = None
        try:
            links = _record(key="/works/OL9W", title="Book").links()
            self_link = next(l for l in links if l.rel == "self")
            assert self_link.href == "https://openlibrary.org/opds/works/OL9W"
        finally:
            OpenLibraryDataProvider.OPDS_BASE_URL = original


class TestIaAlternateBuilder:
    def test_ia_alternate_href_format(self):
        link = _build_ia_alternate_link(_edition(key="/books/OL1M", ia=["abc123"]))
        assert link.href == "https://archive.org/services/loans/loan/?action=webpub&identifier=abc123&opds=1"

    def test_ia_alternate_rel(self):
        link = _build_ia_alternate_link(_edition(key="/books/OL1M", ia=["abc123"]))
        assert link.rel == "alternate"

    def test_ia_alternate_type(self):
        link = _build_ia_alternate_link(_edition(key="/books/OL1M", ia=["abc123"]))
        assert link.type == "application/opds-publication+json"

    def test_ia_alternate_authenticate_property(self):
        link = _build_ia_alternate_link(_edition(key="/books/OL1M", ia=["abc123"]))
        auth = link.properties["authenticate"]
        assert auth["href"] == "https://archive.org/services/loans/loan/?action=authentication_document"
        assert auth["type"] == "application/opds-authentication+json"


class TestExternalAcquisitionBuilder:
    def test_rel_uses_access(self):
        link = _build_external_acquisition_link(
            _edition(key="/books/OL1M"),
            _provider(url="https://example.org", access="borrow"),
        )
        assert link.rel == "http://opds-spec.org/acquisition/borrow"

    def test_rel_fallback_when_access_missing(self):
        link = _build_external_acquisition_link(
            _edition(key="/books/OL1M"),
            _provider(url="https://example.org"),
        )
        assert link.rel == "http://opds-spec.org/acquisition"

    @pytest.mark.parametrize(
        "ol_format,mime",
        [
            ("web", "text/html"),
            ("pdf", "application/pdf"),
            ("epub", "application/epub+zip"),
            ("audio", "audio/mpeg"),
        ],
    )
    def test_type_mapped_from_format(self, ol_format: str, mime: str):
        link = _build_external_acquisition_link(
            _edition(key="/books/OL1M"),
            _provider(url="https://example.org", format=ol_format),
        )
        assert link.type == mime

    def test_type_none_when_no_format(self):
        link = _build_external_acquisition_link(_edition(key="/books/OL1M"), _provider(url="https://example.org"))
        assert link.type is None

    def test_availability_from_ebook_access(self):
        available = _build_external_acquisition_link(
            _edition(key="/books/OL1M", ebook_access="public"),
            _provider(url="https://example.org"),
        )
        unavailable = _build_external_acquisition_link(
            _edition(key="/books/OL2M", ebook_access="printdisabled"),
            _provider(url="https://example.org"),
        )
        assert available.properties["availability"] == "available"
        assert unavailable.properties["availability"] == "unavailable"

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("borrow_available", "available"),
            ("open", "available"),
            ("borrow_unavailable", "unavailable"),
            ("private", "unavailable"),
            ("error", "unavailable"),
        ],
    )
    def test_availability_from_status(self, status: str, expected: str):
        link = _build_external_acquisition_link(
            _edition(key="/books/OL1M", availability={"status": status}),
            _provider(url="https://example.org"),
        )
        assert link.properties["availability"] == expected

    def test_status_overrides_ebook_access(self):
        link = _build_external_acquisition_link(
            _edition(key="/books/OL1M", ebook_access="public", availability={"status": "borrow_unavailable"}),
            _provider(url="https://example.org"),
        )
        assert link.properties["availability"] == "unavailable"

    def test_provider_name_set_as_title(self):
        link = _build_external_acquisition_link(
            _edition(key="/books/OL1M"),
            _provider(url="https://example.org", provider_name="Standard Ebooks"),
        )
        assert link.title == "Standard Ebooks"

    def test_price_parsing_valid(self):
        link = _build_external_acquisition_link(
            _edition(key="/books/OL1M"),
            _provider(url="https://example.org", price="9.99 USD"),
        )
        assert link.properties["price"] == {"value": 9.99, "currency": "USD"}

    def test_price_without_currency_ignored(self):
        link = _build_external_acquisition_link(
            _edition(key="/books/OL1M"),
            _provider(url="https://example.org", price="9.99"),
        )
        assert "price" not in link.properties

    def test_price_invalid_amount_ignored(self):
        link = _build_external_acquisition_link(
            _edition(key="/books/OL1M"),
            _provider(url="https://example.org", price="free USD"),
        )
        assert "price" not in link.properties

    def test_no_price_no_price_property(self):
        link = _build_external_acquisition_link(_edition(key="/books/OL1M"), _provider(url="https://example.org"))
        assert "price" not in link.properties


class TestAcquisitionDispatch:
    def test_raises_value_error_when_url_missing(self):
        with pytest.raises(ValueError):
            ol_acquisition_to_opds_links(_edition(key="/books/OL1M"), _provider(url=None))

    @patch("pyopds2_openlibrary._build_ia_alternate_link")
    def test_ia_provider_dispatches_to_ia_builder(self, mock_builder):
        expected = MagicMock()
        mock_builder.return_value = expected
        links = ol_acquisition_to_opds_links(
            _edition(key="/books/OL1M", ia=["ia-id"]),
            _provider(url="https://archive.org/details/ia-id", provider_name="ia"),
        )
        mock_builder.assert_called_once()
        assert links == [expected]

    @patch("pyopds2_openlibrary._build_external_acquisition_link")
    def test_non_ia_provider_dispatches_external_builder(self, mock_builder):
        expected = MagicMock()
        mock_builder.return_value = expected
        links = ol_acquisition_to_opds_links(
            _edition(key="/books/OL1M"),
            _provider(url="https://example.org", provider_name="standardebooks"),
        )
        mock_builder.assert_called_once()
        assert links == [expected]


class TestImages:
    def test_cover_image_link_present(self):
        links = _record(key="/works/OL1W", title="Book", cover_i=123).images()
        assert links is not None
        assert links[0].href == "https://covers.openlibrary.org/b/id/123-L.jpg"

    def test_no_cover_returns_none(self):
        assert _record(key="/works/OL1W", title="Book").images() is None

    def test_edition_cover_preferred_over_work_cover(self):
        links = _record(
            key="/works/OL1W",
            title="Book",
            cover_i=111,
            editions={"docs": [{"key": "/books/OL1M", "title": "Edition", "cover_i": 222}]},
        ).images()
        assert links[0].href.endswith("/222-L.jpg")


class TestMetadata:
    def test_title_prefers_edition(self):
        metadata = _record(
            key="/works/OL1W",
            title="Work",
            editions={"docs": [{"key": "/books/OL1M", "title": "Edition"}]},
        ).metadata()
        assert metadata.title == "Edition"

    def test_title_falls_back_to_work(self):
        metadata = _record(key="/works/OL1W", title="Work").metadata()
        assert metadata.title == "Work"

    def test_title_falls_back_to_untitled(self):
        metadata = _record(key="/works/OL1W", title=None).metadata()
        assert metadata.title == "Untitled"

    def test_authors_with_keys_include_author_link(self):
        metadata = _record(
            key="/works/OL1W",
            title="Book",
            author_name=["Author"],
            author_key=["OL123A"],
        ).metadata()
        assert metadata.author is not None
        assert metadata.author[0].links[0].href == "https://openlibrary.org/authors/OL123A"

    def test_authors_without_keys_name_only(self):
        metadata = _record(key="/works/OL1W", title="Book", author_name=["Author"], author_key=None).metadata()
        assert metadata.author is not None
        assert metadata.author[0].name == "Author"
        assert metadata.author[0].links is None

    def test_no_authors_returns_none(self):
        metadata = _record(key="/works/OL1W", title="Book", author_name=None, author_key=None).metadata()
        assert metadata.author is None

    @patch("pyopds2_openlibrary.fetch_languages_map", return_value={"eng": "en"})
    def test_language_conversion(self, _):
        metadata = _record(key="/works/OL1W", title="Book", language=["eng"]).metadata()
        assert metadata.language == ["en"]

    def test_number_of_pages_from_number_of_pages_median(self):
        metadata = _record(key="/works/OL1W", title="Book", number_of_pages_median=321).metadata()
        assert metadata.numberOfPages == 321

    def test_description_fallback_edition_then_work(self):
        from_edition = _record(
            key="/works/OL1W",
            title="Book",
            description="Work desc",
            editions={"docs": [{"key": "/books/OL1M", "title": "Edition", "description": "Edition desc"}]},
        ).metadata()
        from_work = _record(key="/works/OL1W", title="Book", description="Work desc").metadata()
        assert from_edition.description == "Edition desc"
        assert from_work.description == "Work desc"


class TestFormatAndLanguageHelpers:
    @pytest.mark.parametrize(
        "ol_format,mime",
        [
            ("web", "text/html"),
            ("pdf", "application/pdf"),
            ("epub", "application/epub+zip"),
            ("audio", "audio/mpeg"),
        ],
    )
    def test_each_format_mapped(self, ol_format: str, mime: str):
        assert map_ol_format_to_mime(ol_format) == mime

    def test_unknown_format_returns_none(self):
        assert map_ol_format_to_mime("unknown") is None

    @patch("pyopds2_openlibrary.requests.get")
    def test_known_marc_code_to_iso_code(self, mock_get):
        fetch_languages_map.cache_clear()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [
            {"key": "/languages/eng", "identifiers": {"iso_639_1": ["en"]}},
        ]
        mock_get.return_value = resp
        assert marc_language_to_iso_639_1("eng") == "en"

    @patch("pyopds2_openlibrary.requests.get")
    def test_unknown_marc_code_returns_none(self, mock_get):
        fetch_languages_map.cache_clear()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [
            {"key": "/languages/eng", "identifiers": {"iso_639_1": ["en"]}},
        ]
        mock_get.return_value = resp
        assert marc_language_to_iso_639_1("fra") is None

    @patch("pyopds2_openlibrary.requests.get")
    def test_languages_without_iso_639_1_skipped(self, mock_get):
        fetch_languages_map.cache_clear()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [
            {"key": "/languages/eng", "identifiers": {"iso_639_1": ["en"]}},
            {"key": "/languages/fra", "identifiers": {}},
            {"key": "/languages/deu", "identifiers": None},
        ]
        mock_get.return_value = resp
        mapping = fetch_languages_map()
        assert mapping == {"eng": "en"}

    @patch("pyopds2_openlibrary.requests.get")
    def test_fetch_languages_map_cache_prevents_second_http_call(self, mock_get):
        fetch_languages_map.cache_clear()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [{"key": "/languages/eng", "identifiers": {"iso_639_1": ["en"]}}]
        mock_get.return_value = resp
        first = fetch_languages_map()
        second = fetch_languages_map()
        assert first == second == {"eng": "en"}
        assert mock_get.call_count == 1


class TestPriceParsing:
    def test_valid_price(self):
        assert _parse_price_amount("9.99 USD") == 9.99

    def test_zero_price(self):
        assert _parse_price_amount("0.00 USD") == 0.0

    def test_invalid_price(self):
        assert _parse_price_amount("free") is None

    def test_empty_string(self):
        assert _parse_price_amount("") is None

    def test_none(self):
        assert _parse_price_amount(None) is None


class TestFilterHelpers:
    def test_has_acquisition_options_true_with_providers_with_urls(self):
        record = _record_with_edition_providers(_provider(url="https://example.org/book"))
        assert _has_acquisition_options(record) is True

    def test_has_acquisition_options_no_edition_false(self):
        assert _has_acquisition_options(_record(key="/works/OL1W", title="Book")) is False

    def test_has_acquisition_options_providers_no_urls_false(self):
        record = _record_with_edition_providers(_provider(url=None), _provider(url=None))
        assert _has_acquisition_options(record) is False

    def test_is_currently_available_public_true(self):
        record = _record(
            key="/works/OL1W",
            title="Book",
            editions={"docs": [{"key": "/books/OL1M", "title": "Ed", "ebook_access": "public", "providers": [{"url": "https://x"}]}]},
        )
        assert _is_currently_available(record) is True

    def test_is_currently_available_status_borrow_unavailable_false(self):
        record = _record(
            key="/works/OL1W",
            title="Book",
            editions={"docs": [{"key": "/books/OL1M", "title": "Ed", "availability": {"status": "borrow_unavailable"}, "providers": [{"url": "https://x"}]}]},
        )
        assert _is_currently_available(record) is False

    def test_is_currently_available_status_borrow_available_true(self):
        record = _record(
            key="/works/OL1W",
            title="Book",
            editions={"docs": [{"key": "/books/OL1M", "title": "Ed", "availability": {"status": "borrow_available"}, "providers": [{"url": "https://x"}]}]},
        )
        assert _is_currently_available(record) is True

    def test_is_currently_available_no_edition_false(self):
        assert _is_currently_available(_record(key="/works/OL1W", title="Book")) is False

    def test_has_buyable_provider_paid_true(self):
        assert _has_buyable_provider(_record_with_edition_providers(_provider(price="9.99 USD"))) is True

    def test_has_buyable_provider_zero_false(self):
        assert _has_buyable_provider(_record_with_edition_providers(_provider(price="0.00 USD"))) is False

    def test_has_buyable_provider_no_providers_false(self):
        record = _record(
            key="/works/OL1W",
            title="Book",
            editions={"docs": [{"key": "/books/OL1M", "title": "Ed", "providers": []}]},
        )
        assert _has_buyable_provider(record) is False

    @pytest.mark.parametrize(
        "value,rank",
        [
            ("public", 3),
            ("borrowable", 2),
            ("printdisabled", 1),
            ("no_ebook", 0),
            ("unknown", 0),
        ],
    )
    def test_ebook_access_rank_values(self, value: str, rank: int):
        assert _ebook_access_rank(value) == rank


class TestResolvePreferredEdition:
    @patch("pyopds2_openlibrary.requests.get")
    def test_returns_matching_edition(self, mock_get):
        first = MagicMock()
        first.raise_for_status.return_value = None
        first.json.return_value = {
            "entries": [
                {"key": "/books/OL999M", "languages": [{"key": "/languages/eng"}]},
            ]
        }
        second = MagicMock()
        second.raise_for_status.return_value = None
        second.json.return_value = {
            "docs": [
                {
                    "author_name": ["Preferred Author"],
                    "author_key": ["OLPA"],
                    "editions": {
                        "docs": [
                            {
                                "key": "/books/OL999M",
                                "title": "Preferred",
                                "providers": [{"url": "https://example.org/book"}],
                            }
                        ]
                    },
                }
            ]
        }
        mock_get.side_effect = [first, second]
        resolved = _resolve_preferred_edition("/works/OL1W", "eng", ["key", "title", "providers"])
        assert resolved is not None
        assert resolved.edition.key == "/books/OL999M"

    @patch("pyopds2_openlibrary.requests.get")
    def test_returns_none_when_no_language_match(self, mock_get):
        first = MagicMock()
        first.raise_for_status.return_value = None
        first.json.return_value = {
            "entries": [
                {"key": "/books/OL999M", "languages": [{"key": "/languages/fra"}]},
            ]
        }
        mock_get.return_value = first
        assert _resolve_preferred_edition("/works/OL1W", "eng", ["key"]) is None

    @patch("pyopds2_openlibrary.requests.get", side_effect=Exception("boom"))
    def test_returns_none_on_http_failure(self, _):
        assert _resolve_preferred_edition("/works/OL1W", "eng", ["key"]) is None

    @patch("pyopds2_openlibrary.requests.get")
    def test_author_data_populated_from_preferred_edition_search(self, mock_get):
        first = MagicMock()
        first.raise_for_status.return_value = None
        first.json.return_value = {"entries": [{"key": "/books/OL111M", "languages": [{"key": "/languages/eng"}]}]}
        second = MagicMock()
        second.raise_for_status.return_value = None
        second.json.return_value = {
            "docs": [
                {
                    "author_name": ["Localized Name"],
                    "author_key": ["OLLOCAL"],
                    "editions": {"docs": [{"key": "/books/OL111M", "title": "Localized", "providers": [{"url": "https://x"}]}]},
                }
            ]
        }
        mock_get.side_effect = [first, second]
        resolved = _resolve_preferred_edition("/works/OL1W", "eng", ["key", "title", "providers"])
        assert resolved.author_name == ["Localized Name"]
        assert resolved.author_key == ["OLLOCAL"]


class TestAvailabilityFacetPrimitive:
    def test_all_modes_included_by_default(self):
        links = _build_availability_links(mode="everything", href_fn=lambda m: f"/search?mode={m}")
        assert [l["title"] for l in links] == ["All", "Available to Borrow", "Open Access", "Buyable"]

    def test_active_mode_gets_self_rel(self):
        links = _build_availability_links(mode="ebooks", href_fn=lambda m: f"/search?mode={m}")
        ebooks = next(l for l in links if l["title"] == "Available to Borrow")
        assert ebooks["rel"] == "self"

    def test_inactive_modes_have_no_rel(self):
        links = _build_availability_links(mode="ebooks", href_fn=lambda m: f"/search?mode={m}")
        non_active = [l for l in links if l["title"] != "Available to Borrow"]
        assert all("rel" not in l for l in non_active)

    def test_custom_labels_applied(self):
        links = _build_availability_links(
            mode="everything",
            href_fn=lambda m: f"/search?mode={m}",
            labels={"everything": "Everything", "ebooks": "Borrowable"},
        )
        assert links[0]["title"] == "Everything"
        assert links[1]["title"] == "Borrowable"

    def test_counts_added_as_number_of_items(self):
        links = _build_availability_links(
            mode="everything",
            href_fn=lambda m: f"/search?mode={m}",
            counts={"everything": 10, "ebooks": 4},
        )
        assert links[0]["properties"]["numberOfItems"] == 10
        assert links[1]["properties"]["numberOfItems"] == 4

    def test_exclude_removes_modes(self):
        links = _build_availability_links(
            mode="everything",
            href_fn=lambda m: f"/search?mode={m}",
            exclude={"buyable"},
        )
        assert len(links) == 3
        assert all(l["title"] != "Buyable" for l in links)


class TestFacetBuilders:
    def test_build_facets_returns_sort_and_availability_groups(self):
        facets = build_facets(base_url="https://example.org/opds", query="cats")
        assert len(facets) == 2
        assert facets[0]["metadata"]["title"] == "Sort"
        assert facets[1]["metadata"]["title"] == "Availability"

    def test_build_facets_sort_links_titles(self):
        facets = build_facets(base_url="https://example.org/opds", query="cats")
        titles = [l["title"] for l in facets[0]["links"]]
        assert titles == ["Trending", "Most Recent", "Relevance"]

    def test_build_facets_active_sort_self_and_sort_rel(self):
        facets = build_facets(base_url="https://example.org/opds", query="cats", sort="new")
        most_recent = next(l for l in facets[0]["links"] if l["title"] == "Most Recent")
        assert most_recent["rel"] == ["self", "http://opds-spec.org/sort/new"]

    def test_build_facets_sort_links_number_of_items(self):
        facets = build_facets(base_url="https://example.org/opds", query="cats", total=123)
        assert all(l["properties"]["numberOfItems"] == 123 for l in facets[0]["links"])

    def test_build_facets_availability_links_point_to_search(self):
        facets = build_facets(base_url="https://example.org/opds", query="cats")
        assert all("/search?" in l["href"] for l in facets[1]["links"])

    def test_build_facets_active_availability_has_self_rel(self):
        facets = build_facets(base_url="https://example.org/opds", query="cats", mode="open_access")
        active = next(l for l in facets[1]["links"] if l["title"] == "Open Access")
        assert active["rel"] == "self"

    def test_build_home_facets_returns_availability_only(self):
        facets = OpenLibraryDataProvider.build_home_facets(base_url="https://example.org/opds", mode="everything")
        assert len(facets) == 1
        assert facets[0]["metadata"]["title"] == "Availability"

    def test_build_home_facets_uses_home_labels(self):
        facets = OpenLibraryDataProvider.build_home_facets(base_url="https://example.org/opds", mode="everything")
        assert [l["title"] for l in facets[0]["links"]] == ["Everything", "Borrowable", "Open Access"]

    def test_build_home_facets_excludes_buyable(self):
        facets = OpenLibraryDataProvider.build_home_facets(base_url="https://example.org/opds", mode="everything")
        assert len(facets[0]["links"]) == 3

    def test_build_home_facets_links_point_to_root_mode(self):
        facets = OpenLibraryDataProvider.build_home_facets(base_url="https://example.org/opds", mode="ebooks")
        assert all("/search" not in l["href"] for l in facets[0]["links"])
        everything = next(l for l in facets[0]["links"] if l["title"] == "Everything")
        assert everything["href"] == "https://example.org/opds/"


class TestFacetCounts:
    @patch("pyopds2_openlibrary.requests.get")
    def test_count_fetch_returns_all_modes(self, mock_get):
        def _side_effect(*args, **kwargs):
            response = MagicMock()
            response.raise_for_status.return_value = None
            q = kwargs["params"]["q"]
            if "ebook_access:public" in q:
                response.json.return_value = {"numFound": 11}
            elif "ebook_access:[printdisabled TO *]" in q:
                response.json.return_value = {"numFound": 22}
            else:
                response.json.return_value = {"numFound": 33}
            return response

        mock_get.side_effect = _side_effect
        counts = fetch_facet_counts("cats")
        assert set(counts.keys()) == {"everything", "ebooks", "open_access", "buyable"}

    def test_count_for_buyable_is_none(self):
        assert OpenLibraryDataProvider._count_for_mode("cats", "buyable") is None

    @patch("pyopds2_openlibrary.OpenLibraryDataProvider._count_for_mode")
    def test_known_mode_known_total_skips_request(self, mock_count):
        mock_count.return_value = 1
        counts = fetch_facet_counts("cats", known_mode="ebooks", known_total=77)
        assert counts["ebooks"] == 77
        called_modes = [call.args[1] for call in mock_count.call_args_list]
        assert "ebooks" not in called_modes

    @patch("pyopds2_openlibrary.requests.get")
    def test_ebooks_mode_appends_ebook_access_filter(self, mock_get):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"numFound": 5}
        mock_get.return_value = resp
        OpenLibraryDataProvider._count_for_mode("cats", "ebooks")
        assert mock_get.call_args.kwargs["params"]["q"] == "cats ebook_access:[printdisabled TO *]"

    @patch("pyopds2_openlibrary.requests.get")
    def test_open_access_mode_appends_public_filter(self, mock_get):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"numFound": 5}
        mock_get.return_value = resp
        OpenLibraryDataProvider._count_for_mode("cats", "open_access")
        assert mock_get.call_args.kwargs["params"]["q"] == "cats ebook_access:public"


class TestSearch:
    @staticmethod
    def _search_doc(
        work_key: str,
        title: str,
        price: str | None,
        status: str,
        ebook_access: str = "printdisabled",
        providers: list[dict] | None = None,
        editions_docs: list[dict] | None = None,
    ) -> dict:
        if editions_docs is not None:
            editions = {"docs": editions_docs}
        else:
            editions = {
                "docs": [
                    {
                        "key": work_key.replace("/works", "/books").replace("W", "M"),
                        "title": title,
                        "ebook_access": ebook_access,
                        "availability": {"status": status},
                        "providers": providers if providers is not None else [{"url": "https://example.org/read", "access": "borrow", "price": price}],
                    }
                ]
            }
        return {
            "key": work_key,
            "title": title,
            "ebook_access": ebook_access,
            "editions": editions,
        }

    @patch("pyopds2_openlibrary.requests.get")
    def test_basic_search_returns_search_response(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "numFound": 1,
            "docs": [self._search_doc("/works/OL1W", "Book", "9.99 USD", "borrow_available")],
        }
        mock_get.return_value = response
        result = OpenLibraryDataProvider.search("cats")
        assert isinstance(result, DataProvider.SearchResponse)

    @patch("pyopds2_openlibrary.requests.get")
    def test_mode_ebooks_appends_filter(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"numFound": 0, "docs": []}
        mock_get.return_value = response
        OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks"})
        assert mock_get.call_args.kwargs["params"]["q"] == "cats ebook_access:[printdisabled TO *]"

    @patch("pyopds2_openlibrary.requests.get")
    def test_mode_open_access_appends_filter(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"numFound": 0, "docs": []}
        mock_get.return_value = response
        OpenLibraryDataProvider.search("cats", facets={"mode": "open_access"})
        assert mock_get.call_args.kwargs["params"]["q"] == "cats ebook_access:public"

    @patch("pyopds2_openlibrary.requests.get")
    def test_mode_buyable_filters_to_non_free_providers(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "numFound": 2,
            "docs": [
                self._search_doc("/works/OL1W", "Paid", "9.99 USD", "borrow_available"),
                self._search_doc("/works/OL2W", "Free", "0.00 USD", "borrow_available"),
            ],
        }
        mock_get.return_value = response
        result = OpenLibraryDataProvider.search("cats", facets={"mode": "buyable"})
        assert [r.title for r in result.records] == ["Paid"]

    @patch("pyopds2_openlibrary.requests.get")
    def test_mode_buyable_total_equals_filtered_len(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "numFound": 99,
            "docs": [
                self._search_doc("/works/OL1W", "Paid", "9.99 USD", "borrow_available"),
                self._search_doc("/works/OL2W", "Free", "0.00 USD", "borrow_available"),
            ],
        }
        mock_get.return_value = response
        result = OpenLibraryDataProvider.search("cats", facets={"mode": "buyable"})
        assert result.total == len(result.records) == 1

    @patch("pyopds2_openlibrary.requests.get")
    def test_mode_everything_no_ebook_filter(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"numFound": 0, "docs": []}
        mock_get.return_value = response
        OpenLibraryDataProvider.search("cats", facets={"mode": "everything"})
        assert mock_get.call_args.kwargs["params"]["q"] == "cats"

    @patch("pyopds2_openlibrary.requests.get")
    def test_records_without_acquisition_options_are_filtered(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "numFound": 2,
            "docs": [
                self._search_doc(
                    "/works/OL1W",
                    "No providers",
                    None,
                    "borrow_available",
                    providers=[],
                ),
                self._search_doc(
                    "/works/OL2W",
                    "Has provider",
                    "0.00 USD",
                    "borrow_available",
                    providers=[{"url": "https://example.org/read", "access": "borrow"}],
                ),
            ],
        }
        mock_get.return_value = response
        result = OpenLibraryDataProvider.search("cats", facets={"mode": "everything"})
        assert [r.title for r in result.records] == ["Has provider"]

    @pytest.mark.parametrize("mode", ["ebooks", "open_access", "buyable"])
    @patch("pyopds2_openlibrary.requests.get")
    def test_available_books_sorted_before_unavailable(self, mock_get, mode: str):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "numFound": 2,
            "docs": [
                self._search_doc("/works/OL1W", "Unavailable", "9.99 USD", "borrow_unavailable"),
                self._search_doc("/works/OL2W", "Available", "9.99 USD", "borrow_available"),
            ],
        }
        mock_get.return_value = response
        result = OpenLibraryDataProvider.search("cats", facets={"mode": mode})
        assert [r.title for r in result.records][:2] == ["Available", "Unavailable"]

    @patch("pyopds2_openlibrary.requests.get")
    def test_language_matched_editions_moved_first(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "numFound": 1,
            "docs": [
                self._search_doc(
                    "/works/OL1W",
                    "Book",
                    "9.99 USD",
                    "borrow_available",
                    editions_docs=[
                        {
                            "key": "/books/OLFRA",
                            "title": "French",
                            "language": ["fre"],
                            "providers": [{"url": "https://example.org/fr"}],
                        },
                        {
                            "key": "/books/OLENG",
                            "title": "English",
                            "language": ["eng"],
                            "providers": [{"url": "https://example.org/en"}],
                        },
                    ],
                )
            ],
        }
        mock_get.return_value = response
        result = OpenLibraryDataProvider.search("cats")
        assert result.records[0].editions.docs[0].key == "/books/OLENG"

    @patch("pyopds2_openlibrary._resolve_preferred_edition")
    @patch("pyopds2_openlibrary.requests.get")
    def test_edition_key_query_substitutes_foreign_language_edition(self, mock_get, mock_resolve):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "numFound": 1,
            "docs": [
                self._search_doc(
                    "/works/OL1W",
                    "Book",
                    "9.99 USD",
                    "borrow_available",
                    editions_docs=[
                        {
                            "key": "/books/OLFRA",
                            "title": "French",
                            "language": ["fre"],
                            "providers": [{"url": "https://example.org/fr"}],
                        }
                    ],
                )
            ],
        }
        mock_get.return_value = response
        mock_resolve.return_value = SimpleNamespace(
            edition=OpenLibraryDataRecord.EditionDoc.model_validate(
                {
                    "key": "/books/OLENG",
                    "title": "English",
                    "language": ["eng"],
                    "providers": [{"url": "https://example.org/en"}],
                }
            ),
            author_name=["Localized Author"],
            author_key=["OLAUTH"],
        )

        result = OpenLibraryDataProvider.search("edition_key:OLFRA")
        assert result.records[0].editions.docs[0].key == "/books/OLENG"
        assert result.records[0].author_name == ["Localized Author"]

    @patch("pyopds2_openlibrary.requests.get")
    def test_title_parameter_propagates_to_response_title(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "numFound": 1,
            "docs": [self._search_doc("/works/OL1W", "Book", "9.99 USD", "borrow_available")],
        }
        mock_get.return_value = response
        result = OpenLibraryDataProvider.search("cats", title="Curated")
        assert result.title == "Curated"

    @patch("pyopds2_openlibrary.requests.get")
    def test_title_injected_into_pagination_params(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "numFound": 1,
            "docs": [self._search_doc("/works/OL1W", "Book", "9.99 USD", "borrow_available")],
        }
        mock_get.return_value = response
        result = OpenLibraryDataProvider.search("cats", limit=10, offset=20, title="Curated")
        assert result.params["title"] == "Curated"

    @patch("pyopds2_openlibrary.requests.get")
    def test_no_title_leaves_params_unchanged(self, mock_get):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "numFound": 1,
            "docs": [self._search_doc("/works/OL1W", "Book", "9.99 USD", "borrow_available")],
        }
        mock_get.return_value = response
        result = OpenLibraryDataProvider.search("cats", limit=10, offset=20)
        assert "title" not in result.params
