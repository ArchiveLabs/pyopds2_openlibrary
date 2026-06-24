from unittest.mock import MagicMock, patch

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import OpenLibraryDataProvider


@patch("pyopds2_openlibrary._get_http_client")
def test_count_for_mode_print_disabled_appends_filter(mock_get_client):
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"numFound": 7}
    mock_client.get.return_value = resp

    # Call internal helper for print_disabled mode
    total = OpenLibraryDataProvider._count_for_mode("cats", "print_disabled")

    # Verify HTTP call used the printdisabled ebook_access filter
    called_params = mock_client.get.call_args.kwargs["params"]
    assert "q" in called_params
    assert "ebook_access:printdisabled" in called_params["q"]
    assert called_params["limit"] == 0
    assert called_params["fields"] == "key"
    assert total == 7


@patch("pyopds2_openlibrary.OpenLibraryDataProvider._count_for_mode")
def test_fetch_facet_counts_includes_print_disabled(mock_count):
    mock_count.side_effect = lambda *args: None if args[1] == "buyable" else {
        "everything": 100,
        "ebooks": 50,
        "print_disabled": 35,
        "open_access": 25,
    }[args[1]]

    counts = OpenLibraryDataProvider.fetch_facet_counts("cats")
    assert set(counts.keys()) == {"everything", "ebooks", "print_disabled", "open_access", "buyable"}
    assert counts["print_disabled"] == 35
    assert counts["buyable"] is None
