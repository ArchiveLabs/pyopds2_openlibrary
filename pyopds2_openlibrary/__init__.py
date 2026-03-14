import functools
import typing
from typing_extensions import Literal
from urllib.parse import urlencode
import requests
from typing import List, Optional, TypedDict, cast
from pydantic import BaseModel, Field

from pyopds2 import (
    DataProvider,
    DataProviderRecord,
    Contributor,
    Metadata,
    Link
)

class BookSharedDoc(BaseModel):
    """Fields shared between OpenLibrary works and editions."""
    key: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    description: Optional[str] = None
    cover_i: Optional[int] = None
    ebook_access: Optional[str] = None
    language: Optional[list[str]] = None
    ia: Optional[list[str]] = None


class OpenLibraryDataRecord(BookSharedDoc, DataProviderRecord):

    class EditionAvailability(BaseModel):
        status: Literal["borrow_available", "borrow_unavailable",  "open", "private", "error"]

    class EditionProvider(BaseModel):
        """Basically the acquisition info for an edition."""
        access: Optional[str] = None
        format: Optional[Literal['web', 'pdf', 'epub', 'audio']] = None
        price: Optional[str] = None
        """Book price, eg '0.00 USD'"""
        url: Optional[str] = None
        provider_name: Optional[str] = None

    class EditionDoc(BookSharedDoc):
        """Open Library edition document."""
        availability: Optional["OpenLibraryDataRecord.EditionAvailability"] = None
        providers: Optional[list["OpenLibraryDataRecord.EditionProvider"]] = None

    class EditionsResultSet(BaseModel):
        numFound: Optional[int] = None
        start: Optional[int] = None
        numFoundExact: Optional[bool] = None
        docs: Optional[list["OpenLibraryDataRecord.EditionDoc"]] = None

    author_key: Optional[list[str]] = Field(
        None, description="List of author keys"
    )
    author_name: Optional[list[str]] = Field(
        None, description="List of author names"
    )
    editions: Optional["OpenLibraryDataRecord.EditionsResultSet"] = Field(
        None, description="Editions information (nested structure)"
    )
    number_of_pages_median: Optional[int] = None

    @property
    def type(self) -> str:
        """Type _should_ be improved to dynamically return type based on record data."""
        return "http://schema.org/Book"

    def links(self) -> List[Link]:
        edition = self.editions.docs[0] if self.editions and self.editions.docs else None
        book = edition or self
        opds_base = OpenLibraryDataProvider.OPDS_BASE_URL or f"{OpenLibraryDataProvider.BASE_URL}/opds"

        links: list[Link] = [
            Link(
                rel="self",
                href=f"{opds_base}{book.key}",
                type="application/opds-publication+json",
            ),
            Link(
                rel="alternate",
                href=f"{OpenLibraryDataProvider.BASE_URL}{book.key}",
                type="text/html",
            ),
            Link(
                rel="alternate",
                href=f"{OpenLibraryDataProvider.BASE_URL}{book.key}.json",
                type="application/json",
            ),
        ]

        if not edition or not edition.providers:
            return links

        return links + [
            ol_acquisition_to_opds_acquisition_link(edition, acquisition)
            for acquisition in edition.providers
        ]

    def images(self) -> Optional[List[Link]]:
        edition = self.editions.docs[0] if self.editions and self.editions.docs else None
        book = edition or self
        if book.cover_i:
            return [
                Link(href=f"https://covers.openlibrary.org/b/id/{book.cover_i}-L.jpg", type="image/jpeg", rel="cover"),
            ]
        return None

    def metadata(self) -> Metadata:
        """Return this record as OPDS Metadata."""
        def get_authors() -> Optional[List[Contributor]]:
            if self.author_name and self.author_key:
                return [
                    Contributor(
                        name=name,
                        links=[
                            Link(
                                href=f"{OpenLibraryDataProvider.BASE_URL}/authors/{key}",
                                type="text/html",
                                rel="author"
                            )
                        ]
                    )
                    for name, key in zip(self.author_name, self.author_key)
                ]

        edition = self.editions.docs[0] if self.editions and self.editions.docs else None
        book = edition or self

        return Metadata(
            type=self.type,
            title=book.title,
            subtitle=book.subtitle,
            author=get_authors(),
            description=book.description or self.description,
            language=[lang for marc_lang in (book.language or []) if (lang := marc_language_to_iso_639_1(marc_lang))],
            # TODO: Use the edition-specific pagecount
            numberOfPages=self.number_of_pages_median,
        )


class OpenLibraryLanguageStub(TypedDict):
    key: str
    identifiers: dict[str, list[str]] | None


def ol_acquisition_to_opds_acquisition_link(
    edition: OpenLibraryDataRecord.EditionDoc,
    acq: OpenLibraryDataRecord.EditionProvider
) -> Link:
    link = Link(
        href=acq.url,
        rel=f'http://opds-spec.org/acquisition/{acq.access}',
        type=map_ol_format_to_mime(acq.format) if acq.format else None,
        properties={}
    )

    if edition.ebook_access:
        # Default availability to `unavailable`
        link.properties["availability"] = "unavailable"
        if edition.ebook_access == "public":
            link.properties["availability"] = "available"
    if edition.availability:
        status = edition.availability.status
        if status == "open" or status == "borrow_available":
            link.properties["availability"] = "available"
        elif status == "private" or status == "error" or status == "borrow_unavailable":
            link.properties["availability"] = "unavailable"

    if acq.provider_name == "ia" and edition.ia:
        link.properties['more'] = {
            "href": f"https://archive.org/services/loans/loan/?action=webpub&identifier={edition.ia[0]}&opds=1",
            "rel": "http://opds-spec.org/acquisition/",
            "type": "application/opds-publication+json"
        }
    elif acq.provider_name:
        link.title = acq.provider_name

    if acq.price:
        amount, currency = acq.price.split(" ")
        link.properties["price"] = {
            "value": float(amount),
            "currency": currency,
        }        

    return link


def map_ol_format_to_mime(ol_format: Literal['web', 'pdf', 'epub', 'audio']) -> Optional[str]:
    """Map Open Library format strings to MIME types."""
    mapping = {
        'web': 'text/html',
        'pdf': 'application/pdf',
        'epub': 'application/epub+zip',
        'audio': 'audio/mpeg',
    }
    return mapping.get(ol_format)


def marc_language_to_iso_639_1(marc_code: str) -> Optional[str]:
    """
    Convert a MARC language code to an iso_639_1 language code using
    the cached languages map.
    """
    return fetch_languages_map().get(marc_code)


@functools.cache
def fetch_languages_map() -> dict[str, str]:
    """
    Get a map of MARC language codes (as saved in Open Library search results)
    to iso_639_1 language names.
    """
    r = requests.get("https://openlibrary.org/query.json?type=/type/language&key&identifiers&limit=1000")
    r.raise_for_status()
    data = cast(List[OpenLibraryLanguageStub], r.json())
    languages = {}
    for lang in data:
        marc_code = lang["key"].split("/")[-1]
        identifiers = lang.get("identifiers")
        if not identifiers:
            continue
        iso_codes = identifiers.get("iso_639_1", [])
        if iso_codes:
            languages[marc_code] = iso_codes[0]
    return languages


def _has_acquisition_options(record: OpenLibraryDataRecord) -> bool:
    """Check if a record's edition has any acquisition options (providers).

    Books without providers have no way for the user to interact with them
    (no borrow, no sample, no download) and should be hidden from results.
    """
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if not edition:
        return False
    return bool(edition.providers)


def _is_currently_available(record: OpenLibraryDataRecord) -> bool:
    """Check if a record's edition is currently available (not checked out)."""
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if not edition:
        return False
    if edition.ebook_access == "public":
        return True
    if edition.availability and edition.availability.status == "borrow_unavailable":
        return False
    return True


def build_facets(
    base_url: str,
    query: str,
    sort: Optional[str] = None,
    mode: str = "everything",
    total: Optional[int] = None,
    availability_counts: Optional[dict[str, int]] = None,
) -> list[dict]:
    """Build OPDS 2.0 facets for sort and availability filtering.

    Prefer ``OpenLibraryDataProvider.build_facets`` which delegates here.
    """
    return OpenLibraryDataProvider.build_facets(
        base_url=base_url, query=query, sort=sort, mode=mode,
        total=total, availability_counts=availability_counts,
    )


def fetch_facet_counts(query: str, known_mode: Optional[str] = None, known_total: Optional[int] = None) -> dict[str, int]:
    """Fetch facet counts.  Prefer ``OpenLibraryDataProvider.fetch_facet_counts``."""
    return OpenLibraryDataProvider.fetch_facet_counts(query, known_mode, known_total)


def _parse_price_amount(price: str) -> Optional[float]:
    """Parse the leading numeric portion of a price string (e.g. '0.99 USD') into a float.

    Returns None if parsing fails.
    """
    if not price:
        return None
    numeric_part = price.split(maxsplit=1)[0]
    try:
        return float(numeric_part)
    except ValueError:
        return None


def _has_buyable_provider(record: OpenLibraryDataRecord) -> bool:
    """Check if a record has at least one provider with a non-zero price."""
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if not edition or not edition.providers:
        return False
    for p in edition.providers:
        if not p.price:
            continue
        amount = _parse_price_amount(p.price)
        if amount is not None and amount > 0:
            return True
    return False


class OpenLibraryDataProvider(DataProvider):
    """Data provider for Open Library records."""
    BASE_URL: str = "https://openlibrary.org"
    OPDS_BASE_URL: Optional[str] = None
    TITLE: str = "OpenLibrary.org OPDS Service"
    SEARCH_URL: str = "/opds/search{?query}"

    @classmethod
    def bookshelf_link(cls, host="https://archive.org"):
        return Link(
            rel="http://opds-spec.org/shelf",
            href=f"{host}/services/loans/loan/?action=user_bookshelf",
            type="application/opds+json",
        )

    @classmethod
    def profile_link(cls, host="https://archive.org"):
        return Link(
            rel="profile",
            href=f"{host}/services/loans/loan/?action=user_profile",
            type="application/opds-profile+json",
        )

    @staticmethod
    def _count_for_mode(query: str, mode: str) -> Optional[int]:
        """Run a lightweight ``limit=0`` search to get the total count for a mode.

        Returns ``None`` for modes that require client-side filtering (like
        ``buyable``) since Solr cannot provide an accurate count.
        """
        if mode == 'buyable':
            # Buyable is filtered client-side (_has_buyable_provider); Solr
            # has no field for it so we cannot produce an accurate count.
            return None

        internal_query = query
        if mode == 'ebooks' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:[printdisabled TO *]"
        elif mode == 'open_access' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:public"

        r = requests.get(
            f"{OpenLibraryDataProvider.BASE_URL}/search.json",
            params={"q": internal_query, "limit": 0, "fields": "key"},
        )
        r.raise_for_status()
        return r.json().get("numFound", 0)

    @staticmethod
    def fetch_facet_counts(query: str, known_mode: Optional[str] = None, known_total: Optional[int] = None) -> dict[str, Optional[int]]:
        """Fetch ``numberOfItems`` counts for every availability mode.

        If *known_mode* and *known_total* are provided the count request for
        that mode is skipped (we already have it from the main search).

        Modes that cannot be counted server-side (e.g. ``buyable``) will have
        a ``None`` value unless supplied via *known_mode*/*known_total*.
        """
        modes = ["everything", "ebooks", "open_access", "buyable"]
        counts: dict[str, Optional[int]] = {}
        for m in modes:
            if known_mode and m == known_mode and known_total is not None:
                counts[m] = known_total
            else:
                counts[m] = OpenLibraryDataProvider._count_for_mode(query, m)
        return counts

    @staticmethod
    def build_facets(
        base_url: str,
        query: str,
        sort: Optional[str] = None,
        mode: str = "everything",
        total: Optional[int] = None,
        availability_counts: Optional[dict[str, int]] = None,
    ) -> list[dict]:
        """Build OPDS 2.0 facets for sort and availability filtering.

        Args:
            total: Total number of results for the current search.  Used as
                   ``numberOfItems`` on every Sort facet link (sorting does not
                   change the result count).
            availability_counts: Mapping of mode name to item count.  When
                   provided, every Availability facet link gets a
                   ``numberOfItems`` property per OPDS 2.0 §2.4.
        """

        def href(sort_val: Optional[str] = sort, mode_val: str = mode) -> str:
            params: dict[str, str] = {"query": query}
            if sort_val:
                params["sort"] = sort_val
            if mode_val and mode_val != "everything":
                params["mode"] = mode_val
            return f"{base_url}/search?{urlencode(params)}"

        def facet_link(
            title: str,
            active: bool,
            rel: Optional[str] = None,
            sort_val: Optional[str] = sort,
            mode_val: str = mode,
            number_of_items: Optional[int] = None,
        ) -> dict:
            link: dict = {
                "type": "application/opds+json",
                "title": title,
                "href": href(sort_val=sort_val, mode_val=mode_val),
            }
            if active:
                link["rel"] = ["self", rel] if rel else "self"
            elif rel:
                link["rel"] = rel
            if number_of_items is not None:
                link.setdefault("properties", {})["numberOfItems"] = number_of_items
            return link

        active_sort = sort or ""
        counts = availability_counts or {}

        return [
            {
                "metadata": {"title": "Sort"},
                "links": [
                    facet_link("Trending", active_sort == "trending",
                               rel="http://opds-spec.org/sort/popular", sort_val="trending",
                               number_of_items=total),
                    facet_link("Most Recent", active_sort == "new",
                               rel="http://opds-spec.org/sort/new", sort_val="new",
                               number_of_items=total),
                    facet_link("Relevance", active_sort == "", sort_val="",
                               number_of_items=total),
                ],
            },
            {
                "metadata": {"title": "Availability"},
                "links": [
                    facet_link("All", mode == "everything", mode_val="everything",
                               number_of_items=counts.get("everything")),
                    facet_link("Available to Borrow", mode == "ebooks", mode_val="ebooks",
                               number_of_items=counts.get("ebooks")),
                    facet_link("Open Access", mode == "open_access", mode_val="open_access",
                               number_of_items=counts.get("open_access")),
                    facet_link("Buyable", mode == "buyable", mode_val="buyable",
                               number_of_items=counts.get("buyable")),
                ],
            },
        ]

    @typing.override
    @staticmethod
    def search(
        query: str,
        limit: int = 50,
        offset: int = 0,
        sort: Optional[str] = None,
        facets: Optional[dict[str, str]] = None,
    ) -> DataProvider.SearchResponse:
        """
        Search Open Library.

        Args:
            query: The search query string.
            limit: Maximum number of results to return.
            offset: Number of results to skip.
            sort: Sort order for results.
            facets: Optional facets to apply. Supported facets:
                - 'mode':
                    * 'everything' (default): return all matching results
                      with no ebook filter.
                    * 'ebooks': filter to records with any ebook access
                      (``ebook_access:[printdisabled TO *]``), then hide
                      records without acquisition options.
                    * 'open_access': filter to open-access/public ebooks
                      (``ebook_access:public``), then hide records without
                      acquisition options.
                    * 'buyable': filter to records with ebook access
                      (``ebook_access:[printdisabled TO *]``), then hide
                      records without acquisition options and keep only
                      those that have at least one non-free provider.
        """
        fields = [
            "key", "title", "editions", "description", "providers", "author_name", "ia",
            "cover_i", "availability", "ebook_access", "author_key", "subtitle", "language",
            "number_of_pages_median",
        ]

        internal_query = query
        if facets:
            mode = facets.get('mode', 'everything')
        else:
            mode = 'everything'

        if mode == 'ebooks' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:[printdisabled TO *]"
        elif mode == 'open_access' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:public"
        elif mode == 'buyable' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:[printdisabled TO *]"

        params = {
            "editions": "true",
            "q": internal_query,
            "page": (offset // limit) + 1 if limit else 1,
            "limit": limit,
            **({'sort': sort} if sort else {}),
            "fields": ",".join(fields),
        }
        r = requests.get(f"{OpenLibraryDataProvider.BASE_URL}/search.json", params=params)
        r.raise_for_status()
        data = r.json()
        docs = data.get("docs", [])
        records = []
        for doc in docs:
            # Unpack editions field if present
            if "editions" in doc and isinstance(doc["editions"], dict):
                doc = dict(doc)
                doc["editions"] = OpenLibraryDataRecord.EditionsResultSet.model_validate(doc["editions"])
            records.append(OpenLibraryDataRecord.model_validate(doc))

        total = data.get("numFound", 0)

        if mode in ('ebooks', 'open_access', 'buyable'):
            # Hide books with no acquisition options (issue #23)
            records = [r for r in records if _has_acquisition_options(r)]

            if mode == 'buyable':
                # Keep only records that have a non-free provider.
                # This is a client-side filter (Solr has no buyable field),
                # so total must reflect the filtered count.
                records = [r for r in records if _has_buyable_provider(r)]
                total = len(records)

            # Sort available books before unavailable, preserving order within each group
            records.sort(key=lambda r: (0 if _is_currently_available(r) else 1))

        return DataProvider.SearchResponse(
            provider=OpenLibraryDataProvider,
            records=records,
            total=total,
            query=query,
            limit=limit,
            offset=offset,
            sort=sort
        )