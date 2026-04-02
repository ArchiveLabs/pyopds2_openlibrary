"""Tests for OpenLibrary OPDS Catalog creation."""

import pytest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch, MagicMock
from pyopds2 import Catalog
import pyopds2_openlibrary as openlibrary

from pyopds2_openlibrary import (
    _REQUEST_TIMEOUT,
    OpenLibraryDataProvider,
    OpenLibraryDataRecord,
    _has_acquisition_options,
    _has_buyable_provider,
    _parse_price_amount,
    _resolve_preferred_edition,
    fetch_languages_map,
    ol_acquisition_to_opds_links,
)

build_facets = OpenLibraryDataProvider.build_facets
fetch_facet_counts = OpenLibraryDataProvider.fetch_facet_counts


def _reset_languages_map_cache() -> None:
    openlibrary._languages_map_cache = None
    openlibrary._languages_map_fetched_at = 0.0


class TestOpenLibraryCatalogCreation:
    """Test catalog creation using OpenLibraryDataProvider."""

    @patch('pyopds2_openlibrary.fetch_languages_map')
    @patch('pyopds2_openlibrary.httpx.get')
    def test_create_catalog_from_search(self, mock_get, mock_lang_map):
        """Test creating a catalog from search results."""
        # Mock the language map
        mock_lang_map.return_value = {"eng": "en"}
        
        # Mock the search API response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "numFound": 2,
            "docs": [
                {
                    "key": "/works/OL45804W",
                    "title": "Fantastic Mr Fox",
                    "author_name": ["Roald Dahl"],
                    "author_key": ["OL34184A"],
                    "cover_i": 8739161,
                    "language": ["eng"],
                    "editions": {
                        "numFound": 1,
                        "docs": [
                            {
                                "key": "/books/OL7353617M",
                                "title": "Fantastic Mr Fox",
                                "cover_i": 8739161,
                                "format": "epub",
                                "providers": [
                                    {
                                        "url": "https://openlibrary.org/books/OL7353617M",
                                        "format": "epub",
                                        "access": "open",
                                        "provider_name": "openlibrary"
                                    }
                                ]
                            }
                        ]
                    }
                },
                {
                    "key": "/works/OL45805W",
                    "title": "Charlie and the Chocolate Factory",
                    "author_name": ["Roald Dahl"],
                    "author_key": ["OL34184A"],
                }
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        # Create catalog from search
        catalog = Catalog.create(OpenLibraryDataProvider.search("roald dahl", limit=10))

        # Verify the catalog was created
        assert catalog is not None
        assert isinstance(catalog, Catalog)
        
        # Check metadata
        assert catalog.metadata is not None
        # Note: The catalog title may be set by pyopds2 based on provider
        
        # Check publications
        assert catalog.publications is not None
        assert len(catalog.publications) == 1
        
        # Verify first publication
        first_pub = catalog.publications[0]
        assert first_pub.metadata.title == "Fantastic Mr Fox"
        assert first_pub.metadata.author is not None
        assert len(first_pub.metadata.author) == 1
        assert first_pub.metadata.author[0].name == "Roald Dahl"
        
        # Verify links
        assert first_pub.links is not None
        assert len(first_pub.links) > 0

    @patch('pyopds2_openlibrary.httpx.get')
    def test_search_with_pagination(self, mock_get):
        """Test search with pagination parameters."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "numFound": 100,
            "docs": [
                {
                    "key": "/works/OL45804W",
                    "title": "Test Book",
                    "cover_i": 456,
                    "author_name": ["Test Author"],
                    "author_key": ["OL12345A"],
                    "editions": {
                        "docs": [
                            {
                                "key": "/books/OL123M",
                                "title": "Test Book",
                                "cover_i": 456,
                                "providers": [{"url": "https://example.org/read", "format": "epub"}],
                            }
                        ]
                    },
                }
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        # Perform search with pagination
        result = OpenLibraryDataProvider.search("test", limit=10, offset=20)

        # Verify search response
        assert result is not None
        assert result.total == 100
        assert result.query == "test"
        assert result.limit == 10
        assert result.offset == 20
        assert len(result.records) == 1

    @patch('pyopds2_openlibrary.httpx.get')
    def test_empty_search_results(self, mock_get):
        """Test catalog creation with empty search results."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "numFound": 0,
            "docs": []
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        # Create catalog with empty results
        catalog = Catalog.create(OpenLibraryDataProvider.search("nonexistent"))

        # Verify catalog is created even with no results
        assert catalog is not None
        assert catalog.publications is None or len(catalog.publications) == 0


class TestOpenLibraryDataRecord:
    """Test OpenLibraryDataRecord functionality."""

    @patch('pyopds2_openlibrary.fetch_languages_map')
    def test_record_metadata(self, mock_lang_map):
        """Test metadata extraction from a record."""
        # Mock the language map
        mock_lang_map.return_value = {"eng": "en"}
        
        record = OpenLibraryDataRecord(
            key="/works/OL45804W",
            title="Test Book",
            subtitle="A Test Subtitle",
            description="A test description",
            author_name=["Test Author"],
            author_key=["OL12345A"],
            language=["eng"],
            number_of_pages_median=200
        )

        metadata = record.metadata()

        assert metadata.title == "Test Book"
        assert metadata.subtitle == "A Test Subtitle"
        assert metadata.description == "A test description"
        assert metadata.author is not None
        assert len(metadata.author) == 1
        assert metadata.author[0].name == "Test Author"
        assert metadata.numberOfPages == 200

    def test_record_links(self):
        """Test link generation from a record."""
        record = OpenLibraryDataRecord(
            key="/works/OL45804W",
            title="Test Book"
        )

        links = record.links()

        assert links is not None
        assert len(links) >= 3  # Should have self, alternate HTML, and alternate JSON
        
        # Check for self link
        self_links = [link for link in links if link.rel == "self"]
        assert len(self_links) == 1
        assert "opds" in self_links[0].href

    def test_record_metadata_falls_back_when_title_missing(self):
        """Metadata title should be resilient when edition or work title is missing."""
        record = OpenLibraryDataRecord(
            key="/works/OL45804W",
            title="Work Title",
            editions=OpenLibraryDataRecord.EditionsResultSet(
                docs=[
                    OpenLibraryDataRecord.EditionDoc(
                        key="/books/OL123M",
                        title=None,
                    )
                ]
            )
        )

        metadata = record.metadata()
        assert metadata.title == "Work Title"

        untitled_record = OpenLibraryDataRecord(
            key="/works/OL00000W",
            title=None,
        )

        untitled_metadata = untitled_record.metadata()
        assert untitled_metadata.title == "Untitled"

    def test_record_metadata_with_author_name_only(self):
        """Metadata should not fail when author_name exists without author_key."""
        record = OpenLibraryDataRecord(
            key="/works/OL45804W",
            title="Test Book",
            author_name=["Author Without Key"],
            author_key=None,
        )

        metadata = record.metadata()
        assert metadata.author is not None
        assert metadata.author[0].name == "Author Without Key"

    def test_record_links_skips_provider_without_url(self):
        """Acquisition links should skip providers missing URL instead of failing."""
        record = OpenLibraryDataRecord(
            key="/works/OL45804W",
            title="Test Book",
            editions=OpenLibraryDataRecord.EditionsResultSet(
                docs=[
                    OpenLibraryDataRecord.EditionDoc(
                        key="/books/OL123M",
                        title="Test Book",
                        providers=[
                            OpenLibraryDataRecord.EditionProvider(
                                provider_name="ia",
                                access="borrow",
                                format="web",
                                url=None,
                            )
                        ],
                    )
                ]
            ),
        )

        links = record.links()
        acquisition_links = [link for link in links if '/acquisition/' in link.rel]
        assert len(acquisition_links) == 0

    def test_record_links_handles_malformed_price_and_missing_ia(self):
        """Acquisition mapping should tolerate malformed price and absent IA ids."""
        record = OpenLibraryDataRecord(
            key="/works/OL45804W",
            title="Test Book",
            editions=OpenLibraryDataRecord.EditionsResultSet(
                docs=[
                    OpenLibraryDataRecord.EditionDoc(
                        key="/books/OL123M",
                        title="Test Book",
                        ia=None,
                        providers=[
                            OpenLibraryDataRecord.EditionProvider(
                                provider_name="ia",
                                access="borrow",
                                format="web",
                                url="https://archive.org/details/example",
                                price="not-a-price",
                            )
                        ],
                    )
                ]
            ),
        )

        links = record.links()
        acquisition_links = [link for link in links if '/acquisition/' in link.rel]
        assert len(acquisition_links) == 0

    def test_record_images(self):
        """Test image link generation from a record."""
        # Record with cover
        record_with_cover = OpenLibraryDataRecord(
            key="/works/OL45804W",
            title="Test Book",
            cover_i=8739161
        )

        images = record_with_cover.images()
        assert images is not None
        assert len(images) == 1
        # Verify cover URL structure
        assert images[0].href.startswith("https://covers.openlibrary.org/")
        assert "8739161" in images[0].href

        # Record without cover
        record_without_cover = OpenLibraryDataRecord(
            key="/works/OL45805W",
            title="Test Book No Cover"
        )

        images_none = record_without_cover.images()
        assert images_none is None

    def test_record_type(self):
        """Test the type property of a record."""
        record = OpenLibraryDataRecord(
            key="/works/OL45804W",
            title="Test Book"
        )

        assert record.type == "http://schema.org/Book"


class TestOpenLibraryDataProvider:
    """Test OpenLibraryDataProvider functionality."""

    def test_provider_constants(self):
        """Test provider class constants."""
        assert OpenLibraryDataProvider.BASE_URL == "https://openlibrary.org"
        assert OpenLibraryDataProvider.TITLE == "OpenLibrary.org OPDS Service"
        assert OpenLibraryDataProvider.SEARCH_URL == "/opds/search{?query}"

    @patch('pyopds2_openlibrary.httpx.get')
    def test_search_method(self, mock_get):
        """Test the search method directly."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "numFound": 1,
            "docs": [
                {
                    "key": "/works/OL45804W",
                    "title": "Test Book",
                    "cover_i": 789,
                    "editions": {
                        "docs": [
                            {
                                "key": "/books/OL123M",
                                "title": "Test Book",
                                "cover_i": 789,
                                "providers": [{"url": "https://example.org/read", "format": "epub"}],
                            }
                        ]
                    },
                }
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = OpenLibraryDataProvider.search("test query")

        assert result is not None
        assert result.provider == OpenLibraryDataProvider
        assert result.query == "test query"
        assert len(result.records) == 1

    @patch('pyopds2_openlibrary.httpx.get')
    def test_search_with_sort(self, mock_get):
        """Test search with sort parameter."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "numFound": 1,
            "docs": [{"key": "/works/OL45804W", "title": "Test"}]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = OpenLibraryDataProvider.search("test", sort="rating")

        assert result.sort == "rating"
        # Verify sort was passed in the request
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert call_args[1]['params']['sort'] == "rating"

    @patch('pyopds2_openlibrary.fetch_languages_map')
    @patch('pyopds2_openlibrary.httpx.get')
    def test_availability(self, mock_get, mock_lang_map):
        """Availability is computed correctly without making real API calls."""
        mock_lang_map.return_value = {"eng": "en"}

        def get_availability(ebook_access, availability_status):
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "numFound": 1,
                "docs": [
                    {
                        "key": "/works/OL1W",
                        "title": "Test Book",
                        "cover_i": 321,
                        "ebook_access": ebook_access,
                        "editions": {
                            "docs": [
                                {
                                    "key": "/books/OL1M",
                                    "title": "Test Book",
                                    "cover_i": 321,
                                    "ebook_access": ebook_access,
                                    "availability": {"status": availability_status},
                                    "providers": [
                                        {
                                            "url": "https://example.org/read",
                                            "format": "epub",
                                            "access": "borrow",
                                            "provider_name": "standardebooks",
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                ],
            }
            mock_response.raise_for_status.return_value = None
            mock_get.return_value = mock_response

            catalog = Catalog.create(OpenLibraryDataProvider.search("any query"))
            publication = catalog.publications[0]
            acquisition_link = next((link for link in publication.links if '/acquisition/' in link.rel), None)
            return acquisition_link.properties['availability']

        assert get_availability("public", "open") == 'available'
        assert get_availability("printdisabled", "borrow_available") == 'available'
        assert get_availability("printdisabled", "borrow_unavailable") == 'unavailable'


def _record_with_providers(*providers: OpenLibraryDataRecord.EditionProvider) -> OpenLibraryDataRecord:
    edition = OpenLibraryDataRecord.EditionDoc(
        key="/books/OL1M",
        title="Edition",
        providers=list(providers),
    )
    return OpenLibraryDataRecord(
        key="/works/OL1W",
        title="Work",
        editions=OpenLibraryDataRecord.EditionsResultSet(docs=[edition]),
    )


class TestHasAcquisitionOptions:
    def test_returns_false_when_edition_providers_is_none(self):
        record = OpenLibraryDataRecord(
            key="/works/OL10W",
            title="No providers field",
            editions=OpenLibraryDataRecord.EditionsResultSet(
                docs=[OpenLibraryDataRecord.EditionDoc(key="/books/OL10M", title="Edition", providers=None)]
            ),
        )
        assert _has_acquisition_options(record) is False

    def test_returns_false_when_edition_providers_is_empty(self):
        record = OpenLibraryDataRecord(
            key="/works/OL11W",
            title="Empty providers",
            editions=OpenLibraryDataRecord.EditionsResultSet(
                docs=[OpenLibraryDataRecord.EditionDoc(key="/books/OL11M", title="Edition", providers=[])]
            ),
        )
        assert _has_acquisition_options(record) is False

    def test_returns_false_when_all_provider_urls_are_none(self):
        record = _record_with_providers(
            OpenLibraryDataRecord.EditionProvider(provider_name="a", url=None),
            OpenLibraryDataRecord.EditionProvider(provider_name="b", url=None),
        )
        assert _has_acquisition_options(record) is False

    def test_returns_false_when_multiple_providers_all_urls_none(self):
        record = _record_with_providers(
            OpenLibraryDataRecord.EditionProvider(provider_name="a", access="borrow", url=None),
            OpenLibraryDataRecord.EditionProvider(provider_name="b", access="open-access", url=None),
            OpenLibraryDataRecord.EditionProvider(provider_name="c", format="epub", url=None),
        )
        assert _has_acquisition_options(record) is False

    def test_returns_true_when_at_least_one_provider_has_url(self):
        record = _record_with_providers(
            OpenLibraryDataRecord.EditionProvider(provider_name="a", url="https://example.org/a", format="epub"),
        )
        assert _has_acquisition_options(record) is True

    def test_returns_true_when_mix_of_none_and_valid_urls(self):
        record = _record_with_providers(
            OpenLibraryDataRecord.EditionProvider(provider_name="a", url=None),
            OpenLibraryDataRecord.EditionProvider(provider_name="b", url="https://example.org/b", format="epub"),
        )
        assert _has_acquisition_options(record) is True

    def test_returns_false_when_record_has_no_editions(self):
        record = OpenLibraryDataRecord(key="/works/OL12W", title="No editions")
        assert _has_acquisition_options(record) is False


class TestAcquisitionLinkRelFallback:
    def test_access_none_uses_generic_acquisition_rel(self):
        edition = OpenLibraryDataRecord.EditionDoc(key="/books/OL20M", title="Edition")
        acq = OpenLibraryDataRecord.EditionProvider(url="https://example.org/generic", access=None, format="epub")

        link = ol_acquisition_to_opds_links(edition, acq)[0]
        assert link.rel == "http://opds-spec.org/acquisition"

    def test_access_borrow_uses_borrow_rel(self):
        edition = OpenLibraryDataRecord.EditionDoc(key="/books/OL21M", title="Edition")
        acq = OpenLibraryDataRecord.EditionProvider(url="https://example.org/borrow", access="borrow", format="epub")

        link = ol_acquisition_to_opds_links(edition, acq)[0]
        assert link.rel == "http://opds-spec.org/acquisition/borrow"

    def test_access_open_access_uses_open_access_rel(self):
        edition = OpenLibraryDataRecord.EditionDoc(key="/books/OL22M", title="Edition")
        acq = OpenLibraryDataRecord.EditionProvider(url="https://example.org/open", access="open-access", format="epub")

        link = ol_acquisition_to_opds_links(edition, acq)[0]
        assert link.rel == "http://opds-spec.org/acquisition/open-access"


class TestPriceAndBuyableHelpers:
    def test_parse_price_amount(self):
        assert _parse_price_amount("9.99 USD") == 9.99
        assert _parse_price_amount("0.00 USD") == 0.0
        assert _parse_price_amount("0.99 USD") == 0.99
        assert _parse_price_amount("") is None
        assert _parse_price_amount(None) is None
        assert _parse_price_amount("free") is None
        assert _parse_price_amount("15") == 15.0

    def test_has_buyable_provider_true_for_paid(self):
        record = _record_with_providers(OpenLibraryDataRecord.EditionProvider(price="9.99 USD"))
        assert _has_buyable_provider(record) is True

    def test_has_buyable_provider_false_for_zero_price(self):
        record = _record_with_providers(OpenLibraryDataRecord.EditionProvider(price="0.00 USD"))
        assert _has_buyable_provider(record) is False

    def test_has_buyable_provider_true_for_point_ninety_nine(self):
        # Guards against regressions from string checks like startswith("0").
        record = _record_with_providers(OpenLibraryDataRecord.EditionProvider(price="0.99 USD"))
        assert _has_buyable_provider(record) is True

    def test_has_buyable_provider_false_no_providers(self):
        record = OpenLibraryDataRecord(
            key="/works/OL2W",
            title="No Providers",
            editions=OpenLibraryDataRecord.EditionsResultSet(
                docs=[OpenLibraryDataRecord.EditionDoc(key="/books/OL2M", title="Edition", providers=[])]
            ),
        )
        assert _has_buyable_provider(record) is False

    def test_has_buyable_provider_false_no_editions(self):
        record = OpenLibraryDataRecord(key="/works/OL3W", title="No Editions")
        assert _has_buyable_provider(record) is False

    def test_has_buyable_provider_true_when_any_provider_paid(self):
        record = _record_with_providers(
            OpenLibraryDataRecord.EditionProvider(price="0.00 USD"),
            OpenLibraryDataRecord.EditionProvider(price="12.50 USD"),
        )
        assert _has_buyable_provider(record) is True

    def test_has_buyable_provider_false_when_price_none(self):
        record = _record_with_providers(OpenLibraryDataRecord.EditionProvider(price=None))
        assert _has_buyable_provider(record) is False


class TestFacetCountsAndBuilder:
    @patch("pyopds2_openlibrary.httpx.get")
    def test_count_for_mode_buyable_returns_none(self, mock_get):
        assert OpenLibraryDataProvider._count_for_mode("cats", "buyable") is None
        mock_get.assert_not_called()

    @patch("pyopds2_openlibrary.httpx.get")
    def test_count_for_mode_everything_uses_unmodified_query(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 7}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        total = OpenLibraryDataProvider._count_for_mode("cats", "everything")
        assert total == 7
        assert mock_get.call_args[1]["params"]["q"] == "cats"

    @patch("pyopds2_openlibrary.httpx.get")
    def test_count_for_mode_ebooks_appends_filter(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 8}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        OpenLibraryDataProvider._count_for_mode("cats", "ebooks")
        assert mock_get.call_args[1]["params"]["q"] == "cats ebook_access:[printdisabled TO *]"

    @patch("pyopds2_openlibrary.httpx.get")
    def test_count_for_mode_open_access_appends_filter(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 9}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        OpenLibraryDataProvider._count_for_mode("cats", "open_access")
        assert mock_get.call_args[1]["params"]["q"] == "cats ebook_access:public"

    @patch("pyopds2_openlibrary.httpx.get")
    def test_count_for_mode_does_not_append_if_ebook_access_already_present(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 10}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        existing = "cats ebook_access:public"
        OpenLibraryDataProvider._count_for_mode(existing, "ebooks")
        assert mock_get.call_args[1]["params"]["q"] == existing

    @patch("pyopds2_openlibrary.OpenLibraryDataProvider._count_for_mode")
    def test_fetch_facet_counts_shape_and_buyable_none(self, mock_count_for_mode):
        mock_count_for_mode.side_effect = lambda query, mode: None if mode == "buyable" else {
            "everything": 100,
            "ebooks": 50,
            "open_access": 25,
        }[mode]

        counts = fetch_facet_counts("cats")
        assert list(counts.keys()) == ["everything", "ebooks", "open_access", "buyable"]
        assert counts["buyable"] is None

    @patch("pyopds2_openlibrary.OpenLibraryDataProvider._count_for_mode")
    def test_fetch_facet_counts_uses_known_mode_total_and_skips_call(self, mock_count_for_mode):
        mock_count_for_mode.return_value = 1

        counts = fetch_facet_counts("cats", known_mode="ebooks", known_total=42)

        assert counts["ebooks"] == 42
        called_modes = [call.args[1] for call in mock_count_for_mode.call_args_list]
        assert "ebooks" not in called_modes

    def test_build_facets_groups_and_links_and_rels(self):
        facets = build_facets(base_url="https://example.org/opds", query="fox", sort="new", mode="ebooks")

        assert len(facets) == 1
        assert facets[0]["metadata"]["title"] == "Availability"

        availability_titles = [l["title"] for l in facets[0]["links"]]
        assert availability_titles == ["Everything", "Available to Borrow", "Open Access", "Available to Purchase"]

        for link in facets[0]["links"]:
            assert link["type"] == "application/opds+json"
            assert "title" in link
            assert "href" in link

        active = next(l for l in facets[0]["links"] if l["title"] == "Available to Borrow")
        assert active["rel"] == "self"

        everything = next(l for l in facets[0]["links"] if l["title"] == "Everything")
        parsed = parse_qs(urlparse(everything["href"]).query)
        assert parsed.get("query") == ["fox"]
        assert parsed.get("language") == ["eng"]

    def test_build_facets_number_of_items_and_none_behavior(self):
        counts = {
            "everything": 100,
            "ebooks": 80,
            "open_access": 30,
            "buyable": None,
        }
        facets = build_facets(
            base_url="https://example.org/opds",
            query="fox",
            sort="trending",
            mode="everything",
            total=123,
            availability_counts=counts,
        )

        for link in facets[0]["links"]:
            if "properties" in link:
                assert link["properties"]["numberOfItems"] in {100, 80, 30}

        availability_links = {l["title"]: l for l in facets[0]["links"]}
        assert availability_links["Everything"]["properties"]["numberOfItems"] == 100
        assert availability_links["Available to Borrow"]["properties"]["numberOfItems"] == 80
        assert availability_links["Open Access"]["properties"]["numberOfItems"] == 30
        assert "properties" not in availability_links["Available to Purchase"]

    def test_build_facets_href_mode_and_sort_params(self):
        facets = build_facets(base_url="https://example.org/opds", query="my query", sort="", mode="everything")
        all_link = next(l for l in facets[0]["links"] if l["title"] == "Everything")
        parsed_all = parse_qs(urlparse(all_link["href"]).query)
        assert "mode" not in parsed_all
        assert parsed_all.get("language") == ["eng"]
        assert parsed_all.get("query") == ["my query"]

        buyable_link = next(l for l in facets[0]["links"] if l["title"] == "Available to Purchase")
        parsed_buyable = parse_qs(urlparse(buyable_link["href"]).query)
        assert parsed_buyable.get("mode") == ["buyable"]


class TestSearchModeHandling:
    @staticmethod
    def _solr_doc(
        key: str,
        title: str,
        ebook_access: str,
        availability_status: str,
        providers: list[dict],
    ) -> dict:
        return {
            "key": key,
            "title": title,
            "cover_i": 123,
            "ebook_access": ebook_access,
            "editions": {
                "docs": [
                    {
                        "key": key.replace("/works", "/books").replace("W", "M"),
                        "title": title,
                        "cover_i": 123,
                        "ebook_access": ebook_access,
                        "availability": {"status": availability_status},
                        "providers": providers,
                    }
                ]
            },
        }

    def _mock_search_response(self, mock_get):
        docs = [
            self._solr_doc(
                key="/works/OLP1W",
                title="Paid Available",
                ebook_access="printdisabled",
                availability_status="borrow_available",
                providers=[{"price": "9.99 USD", "access": "borrow", "format": "epub", "url": "https://x/1"}],
            ),
            self._solr_doc(
                key="/works/OLF1W",
                title="Free Unavailable",
                ebook_access="printdisabled",
                availability_status="borrow_unavailable",
                providers=[{"price": "0.00 USD", "access": "borrow", "format": "epub", "url": "https://x/2"}],
            ),
            self._solr_doc(
                key="/works/OLN1W",
                title="No Providers",
                ebook_access="printdisabled",
                availability_status="borrow_available",
                providers=[],
            ),
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 99, "docs": docs}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

    @patch("pyopds2_openlibrary.httpx.get")
    def test_search_default_query_unmodified(self, mock_get):
        self._mock_search_response(mock_get)

        OpenLibraryDataProvider.search("cats")
        assert mock_get.call_args[1]["params"]["q"] == "cats"

    @patch("pyopds2_openlibrary.httpx.get")
    def test_search_everything_query_unmodified(self, mock_get):
        self._mock_search_response(mock_get)

        OpenLibraryDataProvider.search("cats", facets={"mode": "everything"})
        assert mock_get.call_args[1]["params"]["q"] == "cats"

    @patch("pyopds2_openlibrary.httpx.get")
    def test_search_ebooks_appends_filter(self, mock_get):
        self._mock_search_response(mock_get)

        OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks"})
        assert mock_get.call_args[1]["params"]["q"] == "cats ebook_access:[printdisabled TO *]"

    @patch("pyopds2_openlibrary.httpx.get")
    def test_search_open_access_appends_filter(self, mock_get):
        self._mock_search_response(mock_get)

        OpenLibraryDataProvider.search("cats", facets={"mode": "open_access"})
        assert mock_get.call_args[1]["params"]["q"] == "cats ebook_access:public"

    @patch("pyopds2_openlibrary.httpx.get")
    def test_search_buyable_appends_ebooks_filter_with_guard(self, mock_get):
        self._mock_search_response(mock_get)

        OpenLibraryDataProvider.search("cats", facets={"mode": "buyable"})
        assert mock_get.call_args[1]["params"]["q"] == "cats ebook_access:[printdisabled TO *]"

    @patch("pyopds2_openlibrary.httpx.get")
    def test_search_buyable_does_not_duplicate_existing_ebook_access_clause(self, mock_get):
        self._mock_search_response(mock_get)
        query = "cats ebook_access:[printdisabled TO *]"

        OpenLibraryDataProvider.search(query, facets={"mode": "buyable"})
        assert mock_get.call_args[1]["params"]["q"] == query

    @patch("pyopds2_openlibrary.httpx.get")
    def test_search_buyable_filters_records_and_total(self, mock_get):
        self._mock_search_response(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "buyable"})
        assert result.total == 1
        assert len(result.records) == 1
        assert all(_has_buyable_provider(r) for r in result.records)

    @patch("pyopds2_openlibrary.httpx.get")
    def test_search_ebooks_removes_records_without_acquisition_options(self, mock_get):
        self._mock_search_response(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks"})
        assert len(result.records) == 2
        assert all(r.editions and r.editions.docs and r.editions.docs[0].providers for r in result.records)

    @patch("pyopds2_openlibrary.httpx.get")
    def test_filtered_modes_sort_available_before_unavailable(self, mock_get):
        self._mock_search_response(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks"})
        assert result.records[0].title == "Paid Available"
        assert result.records[1].title == "Free Unavailable"


class TestSearchAcquisitionFilterAllModes:
    @staticmethod
    def _doc_with_providers(
        key: str,
        title: str,
        providers: list[dict] | None,
        description: str | None = None,
    ) -> dict:
        return {
            "key": key,
            "title": title,
            "cover_i": 123,
            "description": description,
            "ebook_access": "printdisabled",
            "editions": {
                "docs": [
                    {
                        "key": key.replace("/works", "/books").replace("W", "M"),
                        "title": title,
                        "cover_i": 123,
                        "providers": providers,
                    }
                ]
            },
        }

    def _mock_response_with_mixed_acquisition(self, mock_get):
        docs = [
            self._doc_with_providers(
                key="/works/OL31W",
                title="No providers",
                providers=[],
            ),
            self._doc_with_providers(
                key="/works/OL32W",
                title="Null urls only",
                providers=[{"url": None}, {"url": None}],
            ),
            self._doc_with_providers(
                key="/works/OL33W",
                title="Valid provider",
                providers=[{"url": "https://example.org/read", "access": "borrow", "format": "epub"}],
            ),
            self._doc_with_providers(
                key="/works/OL34W",
                title="Has description only",
                providers=[],
                description="Should still be filtered",
            ),
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 44, "docs": docs}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

    @patch("pyopds2_openlibrary.httpx.get")
    def test_mode_everything_excludes_record_with_empty_providers(self, mock_get):
        self._mock_response_with_mixed_acquisition(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "everything"})
        titles = [r.title for r in result.records]
        assert "No providers" not in titles

    @patch("pyopds2_openlibrary.httpx.get")
    def test_mode_everything_excludes_record_with_all_null_provider_urls(self, mock_get):
        self._mock_response_with_mixed_acquisition(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "everything"})
        titles = [r.title for r in result.records]
        assert "Null urls only" not in titles

    @patch("pyopds2_openlibrary.httpx.get")
    def test_mode_everything_keeps_record_with_valid_provider_url(self, mock_get):
        self._mock_response_with_mixed_acquisition(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "everything"})
        titles = [r.title for r in result.records]
        assert "Valid provider" in titles

    @patch("pyopds2_openlibrary.httpx.get")
    def test_mode_everything_excludes_described_record_without_providers(self, mock_get):
        self._mock_response_with_mixed_acquisition(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "everything"})
        titles = [r.title for r in result.records]
        assert "Has description only" not in titles

    @patch("pyopds2_openlibrary.httpx.get")
    def test_mode_ebooks_excludes_record_without_providers(self, mock_get):
        self._mock_response_with_mixed_acquisition(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks"})
        titles = [r.title for r in result.records]
        assert "No providers" not in titles

    @patch("pyopds2_openlibrary.httpx.get")
    def test_mode_open_access_excludes_record_without_providers(self, mock_get):
        self._mock_response_with_mixed_acquisition(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "open_access"})
        titles = [r.title for r in result.records]
        assert "No providers" not in titles


class TestRequestTimeoutUsage:
    @patch("pyopds2_openlibrary.httpx.get")
    def test_fetch_languages_map_passes_timeout(self, mock_get):
        _reset_languages_map_cache()
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        fetch_languages_map()

        assert mock_get.call_args[1]["timeout"] == 30.0
        assert mock_get.call_args[1]["timeout"] == _REQUEST_TIMEOUT

    @patch("pyopds2_openlibrary.httpx.get")
    def test_resolve_preferred_edition_editions_request_passes_timeout(self, mock_get):
        first_response = MagicMock()
        first_response.raise_for_status.return_value = None
        first_response.json.return_value = {
            "entries": [
                {
                    "key": "/books/OL40M",
                    "languages": [{"key": "/languages/eng"}],
                }
            ]
        }

        second_response = MagicMock()
        second_response.raise_for_status.return_value = None
        second_response.json.return_value = {
            "docs": [
                {
                    "editions": {
                        "docs": [
                            {
                                "key": "/books/OL40M",
                                "title": "Preferred",
                                "providers": [{"url": "https://example.org/book"}],
                            }
                        ]
                    }
                }
            ]
        }
        mock_get.side_effect = [first_response, second_response]

        _resolve_preferred_edition("/works/OL40W", "eng", ["key", "title", "providers"])

        assert mock_get.call_args_list[0].kwargs["timeout"] == 30.0
        assert mock_get.call_args_list[0].kwargs["timeout"] == _REQUEST_TIMEOUT

    @patch("pyopds2_openlibrary.httpx.get")
    def test_resolve_preferred_edition_search_request_passes_timeout(self, mock_get):
        first_response = MagicMock()
        first_response.raise_for_status.return_value = None
        first_response.json.return_value = {
            "entries": [
                {
                    "key": "/books/OL41M",
                    "languages": [{"key": "/languages/eng"}],
                }
            ]
        }

        second_response = MagicMock()
        second_response.raise_for_status.return_value = None
        second_response.json.return_value = {
            "docs": [
                {
                    "editions": {
                        "docs": [
                            {
                                "key": "/books/OL41M",
                                "title": "Preferred",
                                "providers": [{"url": "https://example.org/book"}],
                            }
                        ]
                    }
                }
            ]
        }
        mock_get.side_effect = [first_response, second_response]

        _resolve_preferred_edition("/works/OL41W", "eng", ["key", "title", "providers"])

        assert mock_get.call_args_list[1].kwargs["timeout"] == 30.0
        assert mock_get.call_args_list[1].kwargs["timeout"] == _REQUEST_TIMEOUT

    @patch("pyopds2_openlibrary.httpx.get")
    def test_count_for_mode_passes_timeout(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 1}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        OpenLibraryDataProvider._count_for_mode("cats", "everything")

        assert mock_get.call_args[1]["timeout"] == 30.0
        assert mock_get.call_args[1]["timeout"] == _REQUEST_TIMEOUT

    @patch("pyopds2_openlibrary.httpx.get")
    def test_search_passes_timeout(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "numFound": 1,
            "docs": [
                {
                    "key": "/works/OL42W",
                    "title": "Book",
                    "cover_i": 123,
                    "editions": {
                        "docs": [
                            {
                                "key": "/books/OL42M",
                                "title": "Book",
                                "cover_i": 123,
                                "providers": [{"url": "https://example.org/book"}],
                            }
                        ]
                    },
                }
            ],
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        OpenLibraryDataProvider.search("cats", facets={"mode": "everything"})

        assert mock_get.call_args[1]["timeout"] == 30.0
        assert mock_get.call_args[1]["timeout"] == _REQUEST_TIMEOUT


class TestSearchTotalsByMode:
    @staticmethod
    def _mock_docs_with_buyable_and_non_buyable():
        return [
            {
                "key": "/works/OL51W",
                "title": "Paid",
                "cover_i": 123,
                "ebook_access": "printdisabled",
                "editions": {
                    "docs": [
                        {
                            "key": "/books/OL51M",
                            "title": "Paid",
                            "cover_i": 123,
                            "availability": {"status": "borrow_available"},
                            "providers": [{"url": "https://example.org/paid", "format": "epub", "price": "5.00 USD"}],
                        }
                    ]
                },
            },
            {
                "key": "/works/OL52W",
                "title": "Free",
                "cover_i": 123,
                "ebook_access": "printdisabled",
                "editions": {
                    "docs": [
                        {
                            "key": "/books/OL52M",
                            "title": "Free",
                            "cover_i": 123,
                            "availability": {"status": "borrow_available"},
                            "providers": [{"url": "https://example.org/free", "format": "epub", "price": "0.00 USD"}],
                        }
                    ]
                },
            },
        ]

    @patch("pyopds2_openlibrary.httpx.get")
    def test_mode_buyable_total_is_filtered_len(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 99, "docs": self._mock_docs_with_buyable_and_non_buyable()}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "buyable"})
        assert result.total == 1

    @patch("pyopds2_openlibrary.httpx.get")
    def test_mode_ebooks_total_uses_openlibrary_numfound(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 77, "docs": self._mock_docs_with_buyable_and_non_buyable()}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks"})
        assert result.total == 77

    @patch("pyopds2_openlibrary.httpx.get")
    def test_mode_everything_total_uses_openlibrary_numfound(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 66, "docs": self._mock_docs_with_buyable_and_non_buyable()}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "everything"})
        assert result.total == 66
