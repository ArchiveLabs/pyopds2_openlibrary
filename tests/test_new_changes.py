from unittest.mock import MagicMock, patch

import pyopds2_openlibrary as openlibrary
from pyopds2_openlibrary import OpenLibraryDataProvider


@patch("pyopds2_openlibrary.httpx.get")
def test_count_for_mode_print_disabled_appends_filter(mock_get):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"numFound": 7}
    mock_get.return_value = resp

    # Call internal helper for print_disabled mode
    total = OpenLibraryDataProvider._count_for_mode("cats", "print_disabled")

    # Verify HTTP call used the printdisabled ebook_access filter
    called_params = mock_get.call_args.kwargs["params"]
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


from pyopds2_openlibrary import OpenLibraryDataRecord


class TestResponsiveCoverImages:
    def test_three_sizes_with_legacy_rels(self):
        imgs = OpenLibraryDataRecord(key="/works/OL1W", title="T", cover_i=42).images()
        assert [i.href[-5:] for i in imgs] == ["L.jpg", "M.jpg", "S.jpg"]
        assert all("42" in i.href for i in imgs)
        assert imgs[0].rel == "http://opds-spec.org/image"
        assert imgs[1].rel == "http://opds-spec.org/image/thumbnail"
        assert imgs[2].rel is None  # small variant carries no rel

    def test_no_cover_returns_none(self):
        assert OpenLibraryDataRecord(key="/works/OL1W", title="T").images() is None

    def test_edition_cover_preferred_over_work(self):
        # editions.docs[0].cover_i wins over the work-level cover_i.
        rec = OpenLibraryDataRecord(
            key="/works/OL1W", title="T", cover_i=111,
            editions={"docs": [{"key": "/books/OL1M", "cover_i": 222}]},
        )
        assert rec._displayed_cover_id() == 222

    def test_falls_back_to_work_cover_when_edition_has_none(self):
        rec = OpenLibraryDataRecord(
            key="/works/OL1W", title="T", cover_i=111,
            editions={"docs": [{"key": "/books/OL1M"}]},
        )
        assert rec._displayed_cover_id() == 111


class TestMetadataNewFields:
    def _dump(self, **kwargs):
        rec = OpenLibraryDataRecord(key="/works/OL1W", title="T", **kwargs)
        return rec.metadata().model_dump(by_alias=True, exclude_none=True)

    def test_picks_first_isbn13(self):
        d = self._dump(isbn=["123", "9780306406157", "9791234567896"])
        assert d["identifier"] == "urn:isbn:9780306406157"

    def test_no_isbn13_omits_identifier(self):
        d = self._dump(isbn=["123", "0306406152"])  # only isbn10
        assert "identifier" not in d

    def test_publisher_contributor_with_search_link(self):
        d = self._dump(publisher=["Acme", "Other"])
        pub = d["publisher"][0]
        assert pub["name"] == "Acme"
        assert "publisher%3A%22Acme%22" in pub["links"][0]["href"]  # publisher:"Acme" url-encoded

    def test_first_publish_year_becomes_published_date(self):
        d = self._dump(first_publish_year=1999)
        assert str(d["published"]).startswith("1999-01-01")

    def test_series_belongs_to_with_position_and_link(self):
        d = self._dump(series_name=["Foo"], series_key=["OLseries"], series_position=["2"])
        series = d["belongsTo"]["series"][0]
        assert series["name"] == "Foo"
        assert series["position"] == 2.0
        assert series["links"][0]["href"].endswith("/series/OLseries")

    def test_series_bad_position_skipped(self):
        d = self._dump(series_name=["Foo"], series_position=["not-a-number"])
        assert "position" not in d["belongsTo"]["series"][0]


class TestFeaturedSubjectFeeds:
    def test_drops_standard_ebooks_for_non_english(self):
        subjects = [
            {"presentable_name": "Standard Ebooks", "key": "/x"},
            {"presentable_name": "Art", "key": "/subjects/art"},
        ]
        en = OpenLibraryDataProvider.featured_subject_feeds(subjects, language="en")
        fr = OpenLibraryDataProvider.featured_subject_feeds(subjects, language="fr")
        assert [t for t, _, _ in en] == ["Standard Ebooks", "Art"]
        assert [t for t, _, _ in fr] == ["Art"]

    def test_query_override_and_default_build(self):
        subjects = [
            {"presentable_name": "Custom", "query": 'publisher:"X" ebook_access:public'},
            {"presentable_name": "Art", "key": "/subjects/art"},
        ]
        feeds = OpenLibraryDataProvider.featured_subject_feeds(subjects)
        assert feeds[0] == ("Custom", 'publisher:"X" ebook_access:public', "trending")
        assert feeds[1][1] == 'subject_key:art -subject:"content_warning:cover" ebook_access:[borrowable TO *]'


class _FakeCatalog:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.publications = kwargs.get("publications", [])

    @staticmethod
    def create(*args, **kwargs):
        return _FakeCatalog(metadata={"title": "Group"}, publications=[{"metadata": {"title": "Book"}}])

    def model_dump(self):
        return self.kwargs


class TestBuildHomePages:
    @patch("pyopds2_openlibrary.OpenLibraryDataProvider.search")
    @patch("pyopds2_openlibrary.Catalog")
    def test_single_fanout_produces_all_pages(self, mock_catalog_cls, mock_search):
        mock_catalog_cls.side_effect = lambda **kwargs: _FakeCatalog(**kwargs)
        mock_catalog_cls.create.side_effect = _FakeCatalog.create
        mock_search.return_value = object()

        num_groups = len(OpenLibraryDataProvider._home_groups_config("everything", language="en"))
        per_page = OpenLibraryDataProvider.GROUPS_PER_PAGE
        expected_pages = (num_groups + per_page - 1) // per_page

        pages = OpenLibraryDataProvider.build_home_pages(
            base="https://example.org/opds", mode="everything", language="en",
        )

        assert len(pages) == expected_pages
        # One fan-out total, not one per page.
        assert mock_search.call_count == num_groups
        # Page 1 has navigation; later pages don't.
        assert pages[0]["navigation"]
        assert pages[1]["navigation"] == []

    @patch("pyopds2_openlibrary.OpenLibraryDataProvider.search")
    @patch("pyopds2_openlibrary.Catalog")
    def test_pages_cap(self, mock_catalog_cls, mock_search):
        mock_catalog_cls.side_effect = lambda **kwargs: _FakeCatalog(**kwargs)
        mock_catalog_cls.create.side_effect = _FakeCatalog.create
        mock_search.return_value = object()

        pages = OpenLibraryDataProvider.build_home_pages(
            base="https://example.org/opds", mode="everything", language="en", pages=2,
        )
        assert len(pages) == 2
