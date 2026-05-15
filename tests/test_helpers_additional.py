import pytest
from unittest.mock import patch

from pyopds2_openlibrary import (
    map_ol_format_to_mime,
    strip_markdown,
    _parse_price_amount,
    _ebook_access_rank,
    _apply_media_type_filter,
    _build_availability_links,
    _build_language_links,
    ol_acquisition_to_opds_links,
    OpenLibraryDataRecord,
)


def test_map_ol_format_to_mime_known_and_unknown():
    assert map_ol_format_to_mime('epub') == 'application/epub+zip'
    assert map_ol_format_to_mime('web') == 'text/html'
    assert map_ol_format_to_mime('nonexistent-format') == 'application/octet-stream'


def test_strip_markdown_basic():
    md = """# Title\nSome **bold** text and a [link](https://example.org)."""
    out = strip_markdown(md)
    assert 'Title' in out
    assert 'bold' in out
    # markdown-it renders links as text; URLs are not preserved in plain text
    assert 'link' in out


@pytest.mark.parametrize("inp,expected", [
    ("0.99 USD", 0.99),
    ("12.5 EUR", 12.5),
    ("free", None),
    ("", None),
])
def test_parse_price_amount(inp, expected):
    assert _parse_price_amount(inp) == expected


def test_ebook_access_rank_values():
    assert _ebook_access_rank('public') > _ebook_access_rank('borrowable')
    assert _ebook_access_rank(None) == 0


def test_apply_media_type_filter_variants():
    base = "title:foo"
    assert _apply_media_type_filter(base, None) == base
    assert 'id_librivox:*' in _apply_media_type_filter(base, 'audiobook')
    assert 'ebook_access' in _apply_media_type_filter(base, 'ebook')


def test_build_availability_links_counts_and_active():
    def href_fn(val):
        return f"/search?mode={val}"

    counts = {'everything': 5, 'ebooks': 2, 'open_access': 1, 'buyable': None}
    links = _build_availability_links(mode='ebooks', href_fn=href_fn, counts=counts)
    titles = [l['title'] for l in links]
    assert 'Everything' in titles
    # active should be present for 'ebooks'
    active = [l for l in links if l.get('rel') == 'self']
    assert active and active[0]['title'] == 'Available to Borrow'
    # numberOfItems appears where provided
    num_items = [l.get('properties', {}).get('numberOfItems') for l in links]
    assert 2 in num_items


def test_build_language_links_active_and_order():
    with patch('pyopds2_openlibrary.fetch_language_options', return_value=[(None, 'All'), ('en', 'English'), ('es', 'Spanish')]):
        links = _build_language_links(language='es', href_fn=lambda lang: f"/search?language={lang}")
        # first link should be All
        assert links[0]['title'] == 'All'
        # there should be a self rel for 'es'
        rels = {l.get('href'): l.get('rel') for l in links}
        assert '/search?language=es' in rels


def test_ol_acquisition_to_opds_links_raises_on_missing_url():
    edition = OpenLibraryDataRecord.EditionDoc.model_construct(key='/books/OL1M')
    acq = OpenLibraryDataRecord.EditionProvider.model_construct(url=None)
    with pytest.raises(ValueError):
        ol_acquisition_to_opds_links(edition, acq)
