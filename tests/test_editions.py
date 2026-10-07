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
