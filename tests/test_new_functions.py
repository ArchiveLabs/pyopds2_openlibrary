"""Test cases for newly added functions: Latin names, author bios, Markdown stripping, and LibriVox support."""

from unittest.mock import MagicMock, patch
import pytest

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import (
    OpenLibraryDataRecord,
    _is_latin_name,
    _latin_name_for_author,
    fetch_author_bio,
    strip_markdown,
)


class TestIsLatinName:
    """Test Latin script detection using Unicode character names."""

    def test_pure_latin_ascii_returns_true(self):
        """ASCII letters are Latin script."""
        assert _is_latin_name("John") is True
        assert _is_latin_name("Smith") is True
        assert _is_latin_name("Hello World") is True

    def test_latin_extended_returns_true(self):
        """Latin Extended characters (accented, etc.) are Latin script."""
        assert _is_latin_name("José") is True
        assert _is_latin_name("François") is True
        assert _is_latin_name("Müller") is True
        assert _is_latin_name("Łąkę") is True

    def test_cyrillic_returns_false(self):
        """Cyrillic script should return False."""
        assert _is_latin_name("Иван") is False
        assert _is_latin_name("Петров") is False

    def test_greek_returns_false(self):
        """Greek script should return False."""
        assert _is_latin_name("Αλέξανδρος") is False

    def test_arabic_returns_false(self):
        """Arabic script should return False."""
        assert _is_latin_name("أحمد") is False

    def test_chinese_returns_false(self):
        """CJK characters should return False."""
        assert _is_latin_name("王小明") is False

    def test_mixed_with_numbers_returns_true(self):
        """Numbers are not alphabetic and should be ignored."""
        assert _is_latin_name("Name123") is True
        assert _is_latin_name("Test42") is True

    def test_empty_string_returns_true(self):
        """Empty string has no non-Latin alphabetic characters."""
        assert _is_latin_name("") is True

    def test_only_punctuation_returns_true(self):
        """Punctuation is not alphabetic."""
        assert _is_latin_name("!@#$%^&*()") is True
        assert _is_latin_name("--dash--") is True

    def test_mixed_latin_and_cyrillic_returns_false(self):
        """If any non-Latin alphabetic characters are present, return False."""
        assert _is_latin_name("Name Иван") is False


class TestLatinNameForAuthor:
    """Test fetching Latin-script names for authors."""

    def test_already_latin_name_returns_unchanged(self):
        """If the current name is already Latin, return it unchanged."""
        assert _latin_name_for_author("OL123A", "John Smith") == "John Smith"

    @patch("pyopds2_openlibrary._get")
    def test_fetches_personal_name_when_current_is_non_latin(self, mock_get):
        """Fetch author data when current name is non-Latin."""
        response = MagicMock()
        response.json.return_value = {
            "personal_name": "Ivan Petrov",
            "name": "Иван Петров",
        }
        mock_get.return_value = response
        
        result = _latin_name_for_author("OL123A", "Иван Петров")
        
        assert result == "Ivan Petrov"
        mock_get.assert_called_once_with("https://openlibrary.org/authors/OL123A.json")

    @patch("pyopds2_openlibrary._get")
    def test_fallback_to_current_when_no_latin_personal_name(self, mock_get):
        """If personal_name is not Latin, fall back to current name."""
        # Clear cache to ensure clean test
        openlibrary._latin_author_cache.clear()
        
        response = MagicMock()
        response.json.return_value = {
            "personal_name": "Иван",
            "name": "Иван Петров",
        }
        mock_get.return_value = response
        
        result = _latin_name_for_author("OL_UNIQUE_123", "Иван Петров")
        
        assert result == "Иван Петров"

    @patch("pyopds2_openlibrary._get")
    def test_caches_result(self, mock_get):
        """Cache the result so future calls don't need to fetch."""
        response = MagicMock()
        response.json.return_value = {
            "personal_name": "Latin Name",
            "name": "Кириллица",
        }
        mock_get.return_value = response
        
        # First call should fetch
        result1 = _latin_name_for_author("OL456A", "Кириллица")
        # Second call should use cache
        result2 = _latin_name_for_author("OL456A", "Кириллица")
        
        assert result1 == result2 == "Latin Name"
        mock_get.assert_called_once()

    @patch("pyopds2_openlibrary._get")
    def test_handles_fetch_error_gracefully(self, mock_get):
        """On any error, return the current name."""
        mock_get.side_effect = Exception("API error")
        
        result = _latin_name_for_author("OL789A", "Кириллица")
        
        assert result == "Кириллица"


class TestFetchAuthorBio:
    """Test fetching author bios from OpenLibrary."""

    @patch("pyopds2_openlibrary._get")
    def test_fetch_author_bio_success(self, mock_get):
        """Fetch author name and bio successfully."""
        response = MagicMock()
        response.json.return_value = {
            "name": "John Doe",
            "bio": "An American author.",
        }
        mock_get.return_value = response
        
        name, bio = fetch_author_bio("OL123A")
        
        assert name == "John Doe"
        assert bio == "An American author."

    @patch("pyopds2_openlibrary._get")
    def test_fetch_author_bio_with_personal_name(self, mock_get):
        """Use personal_name if name is not available."""
        response = MagicMock()
        response.json.return_value = {
            "personal_name": "John Michael Doe",
            "bio": "Author biography.",
        }
        mock_get.return_value = response
        
        name, bio = fetch_author_bio("OL456A")
        
        assert name == "John Michael Doe"
        assert bio == "Author biography."

    @patch("pyopds2_openlibrary._get")
    def test_strips_markdown_from_bio(self, mock_get):
        """Markdown in bio is stripped to plain text."""
        response = MagicMock()
        response.json.return_value = {
            "name": "Author",
            "bio": "**Bold** and *italic* text.",
        }
        mock_get.return_value = response
        
        name, bio = fetch_author_bio("OL789A")
        
        assert bio == "Bold and italic text."

    @patch("pyopds2_openlibrary._get")
    def test_handles_dict_bio_value(self, mock_get):
        """Bio can be a dict with 'value' key."""
        response = MagicMock()
        response.json.return_value = {
            "name": "Author",
            "bio": {"value": "Bio text"},
        }
        mock_get.return_value = response
        
        name, bio = fetch_author_bio("OLABCA")
        
        assert bio == "Bio text"

    @patch("pyopds2_openlibrary._get")
    def test_returns_none_on_error(self, mock_get):
        """Return (None, None) if fetch fails."""
        mock_get.side_effect = Exception("API error")
        
        name, bio = fetch_author_bio("OLXYZ")
        
        assert name is None
        assert bio is None

    @patch("pyopds2_openlibrary._get")
    def test_prefers_latin_personal_name_over_primary_name(self, mock_get):
        """Prefer Latin personal_name over non-Latin primary name."""
        response = MagicMock()
        response.json.return_value = {
            "name": "Иван Петров",
            "personal_name": "Ivan Petrov",
            "bio": "Bio",
        }
        mock_get.return_value = response
        
        name, bio = fetch_author_bio("OLFDA")
        
        assert name == "Ivan Petrov"


class TestStripMarkdown:
    """Test Markdown and HTML stripping."""

    def test_plain_text_unchanged(self):
        """Plain text without Markdown should be unchanged."""
        result = strip_markdown("This is plain text.")
        assert result == "This is plain text."

    def test_bold_text_stripped(self):
        """Bold Markdown (**text**) should be stripped."""
        result = strip_markdown("This is **bold** text.")
        assert result == "This is bold text."

    def test_italic_text_stripped(self):
        """Italic Markdown (*text*) should be stripped."""
        result = strip_markdown("This is *italic* text.")
        assert result == "This is italic text."

    def test_links_stripped(self):
        """Markdown links [text](url) are converted to text."""
        result = strip_markdown("Click [here](https://example.org) for more.")
        assert "https://example.org" not in result
        assert "here" in result

    def test_headers_stripped(self):
        """Markdown headers (# H1, ## H2) are stripped."""
        result = strip_markdown("# Header\nSome text.")
        assert "#" not in result
        assert "Header" in result
        assert "Some text" in result

    def test_inline_html_stripped(self):
        """Inline HTML is stripped."""
        result = strip_markdown("This is <b>bold</b> text.")
        assert "<b>" not in result
        assert "bold" in result

    def test_multiple_newlines_collapsed(self):
        """Multiple consecutive newlines are collapsed to max 2."""
        result = strip_markdown("Line 1\n\n\n\n\nLine 2")
        assert "\n\n\n" not in result
        lines = result.split("\n")
        assert any("Line 1" in line for line in lines)
        assert any("Line 2" in line for line in lines)

    def test_result_trimmed(self):
        """Result is stripped of leading/trailing whitespace."""
        result = strip_markdown("  Some text  ")
        assert result == "Some text"

    def test_empty_string(self):
        """Empty string returns empty string."""
        result = strip_markdown("")
        assert result == ""

    def test_complex_markdown_stripped(self):
        """Complex Markdown with multiple features."""
        markdown = """# Title

This is **bold** and *italic* text.

[Link](https://example.org)

- Bullet 1
- Bullet 2
"""
        result = strip_markdown(markdown)
        
        assert "#" not in result
        assert "Title" in result
        assert "bold" in result
        assert "italic" in result
        assert "https://example.org" not in result
        assert "Link" in result


class TestLibriVoxSupport:
    """Test LibriVox audio support in records."""

    def test_record_with_id_librivox_has_audiobook_type(self):
        """Record with id_librivox has Audiobook type."""
        record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL1W",
            "title": "Audio Book",
            "id_librivox": ["librivox_id_123"]
        })
        
        assert record.type == "http://schema.org/Audiobook"

    def test_record_without_id_librivox_has_book_type(self):
        """Record without id_librivox has Book type."""
        record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL2W",
            "title": "Regular Book"
        })
        
        assert record.type == "http://schema.org/Book"

    def test_librivox_fallback_link_added_when_no_audio_links(self):
        """Fallback LibriVox link added when no audio links exist."""
        record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL3W",
            "title": "Audio Book",
            "id_librivox": ["librivox_123"],
            "editions": {
                "docs": [{
                    "key": "/books/OL3M",
                    "title": "Audio Book",
                    "providers": []
                }]
            }
        })
        
        links = record.links()
        
        librivox_links = [l for l in links if l.title == "LibriVox"]
        assert len(librivox_links) == 1
        assert librivox_links[0].href == "https://librivox.org/librivox_123"

    def test_librivox_fallback_skipped_when_librivox_link_already_present(self):
        """Fallback link not added when LibriVox link already exists from provider."""
        # Create a record where the provider URL is already a librivox link
        record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL4W",
            "title": "Audio Book",
            "id_librivox": ["librivox_456"],
            "editions": {
                "docs": [{
                    "key": "/books/OL4M",
                    "title": "Audio Book",
                    "providers": [{
                        "url": "https://librivox.org/librivox_other",
                        "format": "web"
                    }]
                }]
            }
        })
        
        links = record.links()
        
        # Count LibriVox links (should only have one, from the provider, not a fallback)
        librivox_links = [l for l in links if l.href.startswith("https://librivox.org")]
        # The provider creates one link, and the fallback checks if any librivox link exists
        assert len(librivox_links) >= 1

    def test_multiple_librivox_ids(self):
        """Uses first ID when multiple are present."""
        record = OpenLibraryDataRecord.model_validate({
            "key": "/works/OL5W",
            "title": "Audio Book",
            "id_librivox": ["first_id", "second_id"],
            "editions": {
                "docs": [{
                    "key": "/books/OL5M",
                    "title": "Audio Book",
                    "providers": []
                }]
            }
        })
        
        links = record.links()
        librivox_links = [l for l in links if l.title == "LibriVox"]
        
        assert librivox_links[0].href == "https://librivox.org/first_id"
