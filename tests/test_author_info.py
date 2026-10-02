"""The author page's raw material: name, bio and photo from ``/authors/<olid>.json``.

The photo is linked as the ``-L`` rendition; its advertised size must be the
size of that exact file, so it comes from the covers server's metadata run
through the same box fit as book covers, and is omitted when unknown.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import (
    AuthorInfo,
    REL_PORTRAIT,
    _first_photo_id,
    author_photo_links,
    fetch_author_bio,
    fetch_author_info,
)

AUTHOR_URL = "https://openlibrary.org/authors/OL31353A.json"
PHOTO_URL = "https://covers.openlibrary.org/a/id/15165689.json"


def _responses(author: dict, photo: dict | Exception | None = None):
    """A ``_get`` side effect answering the author and photo metadata URLs."""
    def get(url, **_):
        if url == AUTHOR_URL:
            r = MagicMock(); r.json.return_value = author; return r
        if url == PHOTO_URL:
            if isinstance(photo, Exception):
                raise photo
            r = MagicMock(); r.json.return_value = photo; return r
        raise AssertionError(f"unexpected url {url}")
    return get


class TestFirstPhotoId:
    def test_first_positive_id_wins(self):
        assert _first_photo_id([15165689, 14853829]) == 15165689

    def test_minus_one_means_no_photo_and_is_skipped(self):
        assert _first_photo_id([-1, 123]) == 123
        assert _first_photo_id([5543033, -1]) == 5543033

    def test_none_when_there_is_no_usable_photo(self):
        assert _first_photo_id([-1]) is None
        assert _first_photo_id([]) is None
        assert _first_photo_id(None) is None
        assert _first_photo_id("15165689") is None


class TestFetchAuthorInfo:
    @patch("pyopds2_openlibrary._get")
    def test_name_bio_and_sized_photo(self, mock_get):
        mock_get.side_effect = _responses(
            {"name": "Ursula K. Le Guin", "bio": "An *American* author.", "photos": [15165689, -1]},
            {"id": 15165689, "width": 3185, "height": 3791},
        )
        info = fetch_author_info("OL31353A")
        assert info == AuthorInfo("Ursula K. Le Guin", "An American author.", 15165689, 420, 500)

    @patch("pyopds2_openlibrary._get")
    def test_bio_may_be_a_text_object(self, mock_get):
        mock_get.side_effect = _responses(
            {"name": "A", "bio": {"type": "/type/text", "value": "Bio text"}},
        )
        assert fetch_author_info("OL31353A").bio == "Bio text"

    @patch("pyopds2_openlibrary._get")
    def test_no_photo_means_no_photo_fields_and_no_second_request(self, mock_get):
        mock_get.side_effect = _responses({"name": "A", "photos": [-1]})
        info = fetch_author_info("OL31353A")
        assert (info.photo_id, info.photo_width, info.photo_height) == (None, None, None)
        assert mock_get.call_count == 1

    @patch("pyopds2_openlibrary._get")
    def test_missing_photos_key_is_fine(self, mock_get):
        mock_get.side_effect = _responses({"name": "A"})
        assert fetch_author_info("OL31353A").photo_id is None

    @patch("pyopds2_openlibrary._get")
    def test_a_failed_size_lookup_keeps_the_photo_but_drops_the_size(self, mock_get):
        mock_get.side_effect = _responses(
            {"name": "A", "bio": "B", "photos": [15165689]}, RuntimeError("covers down"),
        )
        info = fetch_author_info("OL31353A")
        assert info == AuthorInfo("A", "B", 15165689, None, None)

    @patch("pyopds2_openlibrary._get")
    def test_unknown_size_is_not_guessed(self, mock_get):
        mock_get.side_effect = _responses(
            {"name": "A", "photos": [15165689]}, {"id": 15165689},
        )
        info = fetch_author_info("OL31353A")
        assert (info.photo_width, info.photo_height) == (None, None)

    @patch("pyopds2_openlibrary._get")
    def test_a_failed_author_fetch_gives_all_none(self, mock_get):
        mock_get.side_effect = RuntimeError("API error")
        assert fetch_author_info("OL31353A") == AuthorInfo(None, None, None, None, None)

    @patch("pyopds2_openlibrary._get")
    def test_prefers_a_latin_personal_name_and_seeds_the_cache(self, mock_get):
        mock_get.side_effect = _responses(
            {"name": "Иван Петров", "personal_name": "Ivan Petrov"},
        )
        openlibrary._latin_author_cache.pop("OL31353A", None)
        assert fetch_author_info("OL31353A").name == "Ivan Petrov"
        assert openlibrary._latin_author_cache["OL31353A"] == "Ivan Petrov"

    @patch("pyopds2_openlibrary._get")
    def test_fetch_author_bio_is_the_name_and_bio_pair(self, mock_get):
        mock_get.side_effect = _responses(
            {"name": "A", "bio": "B", "photos": [15165689]}, {"width": 100, "height": 100},
        )
        assert fetch_author_bio("OL31353A") == ("A", "B")


class TestAuthorPhotoLinks:
    def test_the_portrait_link_with_its_size(self):
        [link] = author_photo_links(AuthorInfo("A", None, 15165689, 420, 500))
        assert link.model_dump(exclude_none=True) == {
            "href": "https://covers.openlibrary.org/a/id/15165689-L.jpg",
            "type": "image/jpeg",
            "rel": REL_PORTRAIT,
            "width": 420,
            "height": 500,
        }

    def test_the_size_is_left_out_when_unknown(self):
        [link] = author_photo_links(AuthorInfo("A", None, 15165689, None, None))
        dumped = link.model_dump(exclude_none=True)
        assert "width" not in dumped and "height" not in dumped

    def test_no_photo_no_images(self):
        assert author_photo_links(AuthorInfo("A", "bio", None, None, None)) is None

    def test_the_portrait_relation_is_a_url_under_the_rel_path(self):
        assert REL_PORTRAIT.startswith("https://openlibrary.org/opds/rel/")
