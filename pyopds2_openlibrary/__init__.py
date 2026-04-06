import functools
import typing
from typing_extensions import Literal
from urllib.parse import urlencode
import requests

_REQUEST_TIMEOUT: float = 30.0
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

        for acquisition in edition.providers:
            if acquisition.url:
                links.extend(ol_acquisition_to_opds_links(edition, acquisition))
        return links

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
            if self.author_name:
                return [Contributor(name=name) for name in self.author_name]

        edition = self.editions.docs[0] if self.editions and self.editions.docs else None
        book = edition or self

        return Metadata(
            type=self.type,
            title=book.title or self.title or "Untitled",
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


def _build_ia_alternate_link(edition: OpenLibraryDataRecord.EditionDoc) -> Link:
    """Build an alternate link for an Internet Archive provider.

    Instead of an acquisition link pointing to the IA details page, we produce
    a single ``rel=alternate`` link whose href is the webpub manifest endpoint.
    An ``authenticate`` property tells the client where to obtain credentials.

    Link schema: https://github.com/readium/webpub-manifest/blob/master/schema/link.schema.json
    """
    identifier = edition.ia[0]
    return Link(
        title="Internet Archive",
        href=f"https://archive.org/services/loans/loan/?action=webpub&identifier={identifier}&opds=1",
        rel="alternate",
        type="application/opds-publication+json",
        properties={
            "authenticate": {
                "href": "https://archive.org/services/loans/loan/?action=authentication_document",
                "type": "application/opds-authentication+json",
            }
        },
    )


def _build_external_acquisition_link(
    edition: OpenLibraryDataRecord.EditionDoc,
    acq: OpenLibraryDataRecord.EditionProvider,
) -> Link:
    """Build an acquisition link for a non-IA provider (e.g. Standard Ebooks)."""
    link = Link(
        href=acq.url,
        rel=f'http://opds-spec.org/acquisition/{acq.access}' if acq.access else 'http://opds-spec.org/acquisition',
        type=map_ol_format_to_mime(acq.format) if acq.format else None,
        properties={}
    )

    if edition.ebook_access:
        link.properties["availability"] = "unavailable"
        if edition.ebook_access == "public":
            link.properties["availability"] = "available"
    if edition.availability:
        status = edition.availability.status
        if status == "open" or status == "borrow_available":
            link.properties["availability"] = "available"
        elif status == "private" or status == "error" or status == "borrow_unavailable":
            link.properties["availability"] = "unavailable"

    if acq.provider_name:
        link.title = acq.provider_name

    if acq.price:
        amount = _parse_price_amount(acq.price)
        price_parts = acq.price.split(maxsplit=1)
        currency = price_parts[1] if len(price_parts) > 1 else None
        if amount is not None and currency:
            link.properties["price"] = {
                "value": amount,
                "currency": currency,
            }

    return link


def ol_acquisition_to_opds_links(
    edition: OpenLibraryDataRecord.EditionDoc,
    acq: OpenLibraryDataRecord.EditionProvider,
) -> List[Link]:
    """Convert an OL provider into one or more OPDS links.

    For IA providers this replaces the acquisition link with a single
    ``rel=alternate`` webpub manifest link (per issue #50).
    For all other providers a standard acquisition link is returned.
    """
    if not acq.url:
        raise ValueError("Provider URL is required for acquisition links")

    if acq.provider_name == "ia" and edition.ia:
        return [_build_ia_alternate_link(edition)]

    return [_build_external_acquisition_link(edition, acq)]


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
    r = requests.get("https://openlibrary.org/query.json?type=/type/language&key&identifiers&limit=1000", timeout=_REQUEST_TIMEOUT)
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
    if not edition or not edition.providers:
        return False
    return any(p.url for p in edition.providers)


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


_EBOOK_ACCESS_RANK = {
    "public": 3,
    "borrowable": 2,
    "printdisabled": 1,
    "no_ebook": 0,
}


def _ebook_access_rank(ebook_access: Optional[str]) -> int:
    """Return a numeric rank for an ebook_access value (higher = more accessible)."""
    return _EBOOK_ACCESS_RANK.get(ebook_access or "no_ebook", 0)


class _ResolvedEdition(typing.NamedTuple):
    """Result of _resolve_preferred_edition."""
    edition: "OpenLibraryDataRecord.EditionDoc"
    author_name: Optional[list[str]]
    author_key: Optional[list[str]]


def _resolve_preferred_edition(
    work_key: str,
    language: str,
    edition_fields: list[str],
) -> Optional["_ResolvedEdition"]:
    """Find a full EditionDoc for *work_key* in the preferred MARC language code.

    Makes up to two requests:
    1. Work editions endpoint to find an edition key matching *language*.
    2. Search endpoint to get the full edition data (providers, availability, etc.).

    Returns a ``_ResolvedEdition`` namedtuple (edition + author data) or
    ``None`` if no matching edition is found or any request fails.
    """
    try:
        r = requests.get(
            f"{OpenLibraryDataProvider.BASE_URL}{work_key}/editions.json",
            params={"limit": 50, "fields": "key,languages"},
            timeout=_REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        preferred_olid: Optional[str] = None
        for entry in r.json().get("entries", []):
            langs = [lang["key"].split("/")[-1] for lang in entry.get("languages", [])]
            if language in langs:
                # entry["key"] is like "/books/OL27945116M"
                preferred_olid = entry["key"].split("/")[-1]
                break

        if not preferred_olid:
            return None

        # Include author fields so we can fix author name for the preferred edition.
        search_fields = "editions,author_name,author_key," + ",".join(edition_fields)
        r2 = requests.get(
            f"{OpenLibraryDataProvider.BASE_URL}/search.json",
            params={
                "q": f"edition_key:{preferred_olid}",
                "editions": "true",
                "fields": search_fields,
                "limit": 1,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        r2.raise_for_status()
        docs = r2.json().get("docs", [])
        if not docs:
            return None
        edition_docs = docs[0].get("editions", {}).get("docs", [])
        if not edition_docs:
            return None
        edition = OpenLibraryDataRecord.EditionDoc.model_validate(edition_docs[0])
        author_name = docs[0].get("author_name") or None
        author_key = docs[0].get("author_key") or None
        return _ResolvedEdition(edition=edition, author_name=author_name, author_key=author_key)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shared availability-facet primitives
# ---------------------------------------------------------------------------

# Canonical ordering and labels for availability modes.
# Each entry: (mode_value, search_page_label, home_page_label)
_AVAILABILITY_MODES: list[tuple[str, str, str]] = [
    ("everything",  "All",                "Everything"),
    ("ebooks",      "Available to Borrow", "Borrowable"),
    ("open_access", "Open Access",         "Open Access"),
    ("buyable",     "Buyable",             "Purchasable"),
]


def _build_availability_links(
    mode: str,
    href_fn: typing.Callable[[str], str],
    labels: Optional[dict[str, str]] = None,
    counts: Optional[dict[str, Optional[int]]] = None,
    exclude: Optional[set[str]] = None,
) -> list[dict]:
    """Build the list of availability facet link dicts (single implementation).

    Each caller supplies its own ``href_fn`` (Open/Closed) so this function
    never needs to change when a new page type needs availability facets.

    Args:
        mode: Currently active mode value (e.g. ``"ebooks"``).
        href_fn: Converts a mode value string to a full URL.
        labels: ``{mode_value: display_label}`` overrides.  Unspecified modes
            fall back to the search-page label in ``_AVAILABILITY_MODES``.
        counts: ``{mode_value: item_count}`` for ``numberOfItems`` (OPDS 2.0 §2.4).
        exclude: Mode values to omit from the facet list.
    """
    default_labels = {val: slabel for val, slabel, _ in _AVAILABILITY_MODES}
    resolved = {**default_labels, **(labels or {})}
    counts = counts or {}
    exclude = exclude or set()
    links = []
    for val, _, _ in _AVAILABILITY_MODES:
        if val in exclude:
            continue
        link: dict = {
            "title": resolved[val],
            "href": href_fn(val),
            "type": "application/opds+json",
        }
        if val == mode:
            link["rel"] = "self"
        count = counts.get(val)
        if count is not None:
            link.setdefault("properties", {})["numberOfItems"] = count
        links.append(link)
    return links


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
            timeout=_REQUEST_TIMEOUT,
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

        Availability links point to ``/search``; for homepage facets use
        ``build_home_facets``.

        Args:
            total: Total result count; attached to every Sort link as
                   ``numberOfItems``.
            availability_counts: ``{mode_value: item_count}`` for
                   ``numberOfItems`` per OPDS 2.0 §2.4.
        """
        def search_href(sort_val: Optional[str] = sort, mode_val: str = "everything") -> str:
            params: dict[str, str] = {"query": query}
            if sort_val:
                params["sort"] = sort_val
            if mode_val and mode_val != "everything":
                params["mode"] = mode_val
            return f"{base_url}/search?{urlencode(params)}"

        def sort_link(
            title: str,
            active: bool,
            rel: Optional[str],
            sort_val: Optional[str],
        ) -> dict:
            link: dict = {
                "type": "application/opds+json",
                "title": title,
                "href": search_href(sort_val=sort_val),
            }
            if active:
                link["rel"] = ["self", rel] if rel else "self"
            elif rel:
                link["rel"] = rel
            if total is not None:
                link.setdefault("properties", {})["numberOfItems"] = total
            return link

        active_sort = sort or ""

        return [
            {
                "metadata": {"title": "Sort"},
                "links": [
                    sort_link("Trending",    active_sort == "trending",
                              "http://opds-spec.org/sort/popular", "trending"),
                    sort_link("Most Recent", active_sort == "new",
                              "http://opds-spec.org/sort/new",     "new"),
                    sort_link("Relevance",   active_sort == "",
                              None,                                ""),
                ],
            },
            {
                "metadata": {"title": "Availability"},
                "links": _build_availability_links(
                    mode=mode,
                    href_fn=lambda val: search_href(sort_val=sort, mode_val=val),
                    counts=availability_counts,
                ),
            },
        ]

    @staticmethod
    def build_home_facets(base_url: str, mode: str = "everything") -> list[dict]:
        """Build the Availability facet group for the OPDS homepage.

        Links point to ``<base_url>/?mode=<value>`` so the filter resets when
        the user navigates to ``/search``.

        Args:
            base_url: Base URL of the OPDS service (no trailing slash).
            mode: Currently active availability mode.
        """
        home_labels = {val: hlabel for val, _, hlabel in _AVAILABILITY_MODES}

        def home_href(val: str) -> str:
            return f"{base_url}/" if val == "everything" else f"{base_url}/?mode={val}"

        return [{
            "metadata": {"title": "Availability"},
            "links": _build_availability_links(
                mode=mode,
                href_fn=home_href,
                labels=home_labels,
                exclude={"buyable"},
            ),
        }]

    @typing.override
    @staticmethod
    def search(
        query: str,
        limit: int = 50,
        offset: int = 0,
        sort: Optional[str] = None,
        facets: Optional[dict[str, str]] = None,
        language: str = "eng",
        title: Optional[str] = None,
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
            language: Reserved for future use. Currently only ``"eng"``
                (English) is supported; any other value is ignored and
                English is used instead. Multi-language support will be
                added via browser-language / facet selection in a future
                release.
        """
        # Only English is supported for now. Override any other value so
        # non-English content is never accidentally displayed.
        language = "eng"

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
            # Ask OL to prefer editions in the requested language.
            # This works for general queries but is ignored when edition_key: is present.
            **({'lang': language} if language else {}),
        }
        r = requests.get(f"{OpenLibraryDataProvider.BASE_URL}/search.json", params=params, timeout=_REQUEST_TIMEOUT)
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

        # When the query targets a specific edition (edition_key:), OL ignores the
        # lang param and always returns that edition.  If its language doesn't match
        # the preference, resolve the correct edition from the work.
        if language and "edition_key:" in query:
            edition_fields = [
                "key", "title", "subtitle", "description", "cover_i",
                "ebook_access", "language", "ia", "availability", "providers",
            ]
            for record in records:
                if record.editions and record.editions.docs:
                    ed = record.editions.docs[0]
                    if not ed.language or language not in ed.language:
                        preferred = _resolve_preferred_edition(
                            record.key or "", language, edition_fields
                        )
                        # Always prefer the language-matched edition over a foreign-language
                        # one, even if its ebook-access rank is lower. Language
                        # correctness takes priority; the user can still follow
                        # the alternate link to the original edition.
                        if preferred:
                            record.editions.docs[0] = preferred.edition
                            # Fix author name/key if the edition-level search
                            # returned localised author data.
                            if preferred.author_name:
                                record.author_name = preferred.author_name
                            if preferred.author_key:
                                record.author_key = preferred.author_key

        # When OL returns multiple editions, move language-matching ones first
        # (fallback for cases where lang param alone isn't sufficient).
        if language:
            for record in records:
                if record.editions and record.editions.docs and len(record.editions.docs) > 1:
                    matched = [d for d in record.editions.docs if d.language and language in d.language]
                    others = [d for d in record.editions.docs if not (d.language and language in d.language)]
                    if matched:
                        record.editions.docs = matched + others

        # Always filter out records with no acquisition options (issue #36).
        # An OPDS feed is for book acquisition; without providers there is nothing
        # the user can do with the entry. This also covers carousels that use
        # mode='everything' and previously received no filtering.
        records = [r for r in records if _has_acquisition_options(r)]

        if mode in ('ebooks', 'open_access', 'buyable'):

            if mode == 'buyable':
                # Keep only records that have a non-free provider.
                # This is a client-side filter (Solr has no buyable field),
                # so total cannot be counted accurately by Solr.
                records = [r for r in records if _has_buyable_provider(r)]
                # Buyable is filtered client-side; Solr cannot count it
                # accurately, so fall back to the current page count.
                total = len(records)
            # Sort available books before unavailable, preserving order within each group
            records.sort(key=lambda r: (0 if _is_currently_available(r) else 1))

        response_kwargs = {
            "provider": OpenLibraryDataProvider,
            "records": records,
            "total": total,
            "query": query,
            "limit": limit,
            "offset": offset,
            "sort": sort,
        }
        # Some pyopds2 versions include title in SearchResponse, some do not.
        if title is not None and "title" in getattr(DataProvider.SearchResponse, "__dataclass_fields__", {}):
            response_kwargs["title"] = title

        resp = DataProvider.SearchResponse(**response_kwargs)

        # Backward-compatible fallback for pyopds2 versions that lack title.
        if title is not None and "title" not in getattr(DataProvider.SearchResponse, "__dataclass_fields__", {}):
            resp.title = title
        # Inject title into pagination params so links carry it.
        # params is a functools.cached_property; setting it on the
        # instance before first access caches our version.
        if title:
            base_params = {
                **({"query": query} if query else {}),
                **({"limit": str(limit)} if limit else {}),
                **({"sort": sort} if sort else {}),
                "title": title,
            }
            if resp.page > 1:
                base_params["page"] = str(resp.page)
            resp.params = base_params
        return resp
