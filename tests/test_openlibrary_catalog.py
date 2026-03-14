"""Tests for OpenLibrary OPDS Catalog creation."""

import pytest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch, MagicMock
from pyopds2 import Catalog
from pyopds2_openlibrary import (
    OpenLibraryDataProvider,
    OpenLibraryDataRecord,
    _has_buyable_provider,
    _parse_price_amount,
    build_facets,
    fetch_facet_counts,
)


class TestOpenLibraryCatalogCreation:
    """Test catalog creation using OpenLibraryDataProvider."""

    @patch('pyopds2_openlibrary.fetch_languages_map')
    @patch('pyopds2_openlibrary.requests.get')
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
                                "providers": [
                                    {
                                        "url": "https://openlibrary.org/books/OL7353617M",
                                        "format": "web",
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
        assert len(catalog.publications) == 2
        
        # Verify first publication
        first_pub = catalog.publications[0]
        assert first_pub.metadata.title == "Fantastic Mr Fox"
        assert first_pub.metadata.author is not None
        assert len(first_pub.metadata.author) == 1
        assert first_pub.metadata.author[0].name == "Roald Dahl"
        
        # Verify links
        assert first_pub.links is not None
        assert len(first_pub.links) > 0

    @patch('pyopds2_openlibrary.requests.get')
    def test_search_with_pagination(self, mock_get):
        """Test search with pagination parameters."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "numFound": 100,
            "docs": [
                {
                    "key": "/works/OL45804W",
                    "title": "Test Book",
                    "author_name": ["Test Author"],
                    "author_key": ["OL12345A"],
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

    @patch('pyopds2_openlibrary.requests.get')
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

    @patch('pyopds2_openlibrary.requests.get')
    def test_search_method(self, mock_get):
        """Test the search method directly."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "numFound": 1,
            "docs": [
                {
                    "key": "/works/OL45804W",
                    "title": "Test Book",
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

    @patch('pyopds2_openlibrary.requests.get')
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
    def test_availability(self, mock_lang_map):
        """Minimal availability test using real provider calls (no mocking of responses)."""
        # keep language map patch to match file style; it doesn't affect the network calls
        mock_lang_map.return_value = {"eng": "en"}

        def get_availability(book_id):
            catalog = Catalog.create(OpenLibraryDataProvider.search(book_id))
            publication = catalog.publications[0]
            acquisition_link = next((link for link in publication.links if '/acquisition/' in link.rel), None)
            return acquisition_link.properties['availability']

        standardebooks_openaccess_book = "OL51733541M"
        assert get_availability(standardebooks_openaccess_book) == 'available'
        internetarchive_lendable_book = "OL59176589M"
        assert get_availability(internetarchive_lendable_book) == 'available'
        internetarchive_printdisabled_book = "OL30032673M"
        assert get_availability(internetarchive_printdisabled_book) == 'unavailable'


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
    @patch("pyopds2_openlibrary.requests.get")
    def test_count_for_mode_buyable_returns_none(self, mock_get):
        assert OpenLibraryDataProvider._count_for_mode("cats", "buyable") is None
        mock_get.assert_not_called()

    @patch("pyopds2_openlibrary.requests.get")
    def test_count_for_mode_everything_uses_unmodified_query(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 7}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        total = OpenLibraryDataProvider._count_for_mode("cats", "everything")
        assert total == 7
        assert mock_get.call_args[1]["params"]["q"] == "cats"

    @patch("pyopds2_openlibrary.requests.get")
    def test_count_for_mode_ebooks_appends_filter(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 8}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        OpenLibraryDataProvider._count_for_mode("cats", "ebooks")
        assert mock_get.call_args[1]["params"]["q"] == "cats ebook_access:[printdisabled TO *]"

    @patch("pyopds2_openlibrary.requests.get")
    def test_count_for_mode_open_access_appends_filter(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"numFound": 9}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        OpenLibraryDataProvider._count_for_mode("cats", "open_access")
        assert mock_get.call_args[1]["params"]["q"] == "cats ebook_access:public"

    @patch("pyopds2_openlibrary.requests.get")
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

        assert len(facets) == 2
        assert facets[0]["metadata"]["title"] == "Sort"
        assert facets[1]["metadata"]["title"] == "Availability"

        sort_titles = [l["title"] for l in facets[0]["links"]]
        assert sort_titles == ["Trending", "Most Recent", "Relevance"]

        availability_titles = [l["title"] for l in facets[1]["links"]]
        assert availability_titles == ["All", "Available to Borrow", "Open Access", "Buyable"]

        for group in facets:
            for link in group["links"]:
                assert link["type"] == "application/opds+json"
                assert "title" in link
                assert "href" in link

        most_recent = next(l for l in facets[0]["links"] if l["title"] == "Most Recent")
        assert most_recent["rel"] == ["self", "http://opds-spec.org/sort/new"]

        available_to_borrow = next(l for l in facets[1]["links"] if l["title"] == "Available to Borrow")
        assert available_to_borrow["rel"] == "self"

        trending = next(l for l in facets[0]["links"] if l["title"] == "Trending")
        assert trending["rel"] == "http://opds-spec.org/sort/popular"

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

        for sort_link in facets[0]["links"]:
            assert sort_link["properties"]["numberOfItems"] == 123

        availability_links = {l["title"]: l for l in facets[1]["links"]}
        assert availability_links["All"]["properties"]["numberOfItems"] == 100
        assert availability_links["Available to Borrow"]["properties"]["numberOfItems"] == 80
        assert availability_links["Open Access"]["properties"]["numberOfItems"] == 30
        assert "properties" not in availability_links["Buyable"]

    def test_build_facets_href_mode_and_sort_params(self):
        facets = build_facets(base_url="https://example.org/opds", query="my query", sort="", mode="everything")
        all_link = next(l for l in facets[1]["links"] if l["title"] == "All")
        parsed_all = parse_qs(urlparse(all_link["href"]).query)
        assert "mode" not in parsed_all
        assert "sort" not in parsed_all

        buyable_link = next(l for l in facets[1]["links"] if l["title"] == "Buyable")
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
            "ebook_access": ebook_access,
            "editions": {
                "docs": [
                    {
                        "key": key.replace("/works", "/books").replace("W", "M"),
                        "title": title,
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
                providers=[{"price": "9.99 USD", "access": "borrow", "format": "web", "url": "https://x/1"}],
            ),
            self._solr_doc(
                key="/works/OLF1W",
                title="Free Unavailable",
                ebook_access="printdisabled",
                availability_status="borrow_unavailable",
                providers=[{"price": "0.00 USD", "access": "borrow", "format": "web", "url": "https://x/2"}],
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

    @patch("pyopds2_openlibrary.requests.get")
    def test_search_default_query_unmodified(self, mock_get):
        self._mock_search_response(mock_get)

        OpenLibraryDataProvider.search("cats")
        assert mock_get.call_args[1]["params"]["q"] == "cats"

    @patch("pyopds2_openlibrary.requests.get")
    def test_search_everything_query_unmodified(self, mock_get):
        self._mock_search_response(mock_get)

        OpenLibraryDataProvider.search("cats", facets={"mode": "everything"})
        assert mock_get.call_args[1]["params"]["q"] == "cats"

    @patch("pyopds2_openlibrary.requests.get")
    def test_search_ebooks_appends_filter(self, mock_get):
        self._mock_search_response(mock_get)

        OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks"})
        assert mock_get.call_args[1]["params"]["q"] == "cats ebook_access:[printdisabled TO *]"

    @patch("pyopds2_openlibrary.requests.get")
    def test_search_open_access_appends_filter(self, mock_get):
        self._mock_search_response(mock_get)

        OpenLibraryDataProvider.search("cats", facets={"mode": "open_access"})
        assert mock_get.call_args[1]["params"]["q"] == "cats ebook_access:public"

    @patch("pyopds2_openlibrary.requests.get")
    def test_search_buyable_appends_ebooks_filter_with_guard(self, mock_get):
        self._mock_search_response(mock_get)

        OpenLibraryDataProvider.search("cats", facets={"mode": "buyable"})
        assert mock_get.call_args[1]["params"]["q"] == "cats ebook_access:[printdisabled TO *]"

    @patch("pyopds2_openlibrary.requests.get")
    def test_search_buyable_does_not_duplicate_existing_ebook_access_clause(self, mock_get):
        self._mock_search_response(mock_get)
        query = "cats ebook_access:[printdisabled TO *]"

        OpenLibraryDataProvider.search(query, facets={"mode": "buyable"})
        assert mock_get.call_args[1]["params"]["q"] == query

    @patch("pyopds2_openlibrary.requests.get")
    def test_search_buyable_filters_records_and_total(self, mock_get):
        self._mock_search_response(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "buyable"})
        assert result.total == len(result.records)
        assert result.total == 1
        assert all(_has_buyable_provider(r) for r in result.records)

    @patch("pyopds2_openlibrary.requests.get")
    def test_search_ebooks_removes_records_without_acquisition_options(self, mock_get):
        self._mock_search_response(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks"})
        assert len(result.records) == 2
        assert all(r.editions and r.editions.docs and r.editions.docs[0].providers for r in result.records)

    @patch("pyopds2_openlibrary.requests.get")
    def test_filtered_modes_sort_available_before_unavailable(self, mock_get):
        self._mock_search_response(mock_get)

        result = OpenLibraryDataProvider.search("cats", facets={"mode": "ebooks"})
        assert result.records[0].title == "Paid Available"
        assert result.records[1].title == "Free Unavailable"
