"""Shared fixtures."""
from unittest.mock import patch

import pytest

import pyopds2_openlibrary as openlibrary


@pytest.fixture(autouse=True)
def _feeds_list_no_editions(request):
    """A feed composes its cards' editions with calls of its own after the
    search (``list_editions``). Tests of the search's own request read its
    last call, so composition is off unless a test asks for it with the
    ``lists_editions`` marker."""
    if request.node.get_closest_marker("lists_editions"):
        yield
        return
    with patch.object(openlibrary, "list_editions"):
        yield


def pytest_configure(config):
    config.addinivalue_line("markers", "lists_editions: let a feed compose its cards' editions")
