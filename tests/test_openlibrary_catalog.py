"""Tests for OpenLibrary OPDS Catalog creation."""

import pytest
from unittest.mock import patch, MagicMock
from pyopds2 import Catalog
from pyopds2_openlibrary import OpenLibraryDataProvider, OpenLibraryDataRecord


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
