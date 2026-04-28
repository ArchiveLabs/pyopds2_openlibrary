import re as _re
import time as _time
import typing
import unicodedata as _unicodedata
from html.parser import HTMLParser as _HTMLParser
from typing import List, Optional, TypedDict, cast
from typing_extensions import Literal
from urllib.parse import urlencode

import httpx
from markdown_it import MarkdownIt as _MarkdownIt
from pydantic import BaseModel, Field

from pyopds2 import (
    Catalog,
    DataProvider,
    DataProviderRecord,
    Contributor,
    Metadata,
    Navigation,
    Link
)

_REQUEST_TIMEOUT: float = 30.0

# HTTP status codes that indicate a transient server-side failure worth retrying.
_RETRY_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
# Default delays between attempts (seconds): 3 total attempts — immediate, +1 s, +2 s.
# A 429 Retry-After header overrides the per-attempt delay when present.
_RETRY_DELAYS: tuple[float, ...] = (0.0, 1.0, 2.0)
# Cap on Retry-After to avoid holding a thread-pool thread for too long.
_RETRY_AFTER_MAX: float = 10.0


def _get(url: str, *, params=None, timeout: float = _REQUEST_TIMEOUT) -> httpx.Response:
    """``httpx.get`` with automatic retry on transient HTTP/network errors.

    Retries up to ``len(_RETRY_DELAYS) - 1`` times (default: 2 retries) for
    status codes in ``_RETRY_STATUS_CODES`` or for transport-level errors.
    Non-retryable HTTP errors (e.g. 404) are raised immediately.

    Respects the ``Retry-After`` response header on 429 replies, capped at
    ``_RETRY_AFTER_MAX`` seconds to avoid holding thread-pool threads too long.
    """
    delays = list(_RETRY_DELAYS)  # mutable copy so Retry-After can adjust future delays
    for i, delay in enumerate(delays):
        if delay:
            _time.sleep(delay)
        try:
            r = httpx.get(url, params=params, timeout=timeout)
            is_last = i == len(delays) - 1
            if r.status_code in _RETRY_STATUS_CODES and not is_last:
                # Honour Retry-After on 429; overwrite the *next* scheduled delay.
                if r.status_code == 429:
                    try:
                        delays[i + 1] = min(float(r.headers.get("Retry-After", "")), _RETRY_AFTER_MAX)
                    except (ValueError, IndexError):
                        pass
                continue
            r.raise_for_status()
            return r
        except httpx.TransportError:
            if i == len(delays) - 1:
                raise
    # All retries exhausted — raise_for_status surfaces the last response error.
    r.raise_for_status()
    return r  # unreachable; satisfies type checker



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
    id_librivox: Optional[list[str]] = None

    @property
    def type(self) -> str:
        if self.id_librivox:
            return "http://schema.org/Audiobook"
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

        seen: set[tuple[str, str | None]] = set()
        if edition and edition.providers:
            for acquisition in edition.providers:
                if not acquisition.url:
                    continue
                for link in ol_acquisition_to_opds_links(edition, acquisition):
                    key = (link.href, link.type)
                    if key not in seen:
                        seen.add(key)
                        links.append(link)

        # When no audio link was produced by the edition providers but the work
        # has a LibriVox recording, add the LibriVox catalog page as a fallback.
        has_audio = any(
            lnk.type in ("audio/mpeg", "application/audiobook+json")
            or lnk.href.startswith("https://librivox.org")
            for lnk in links
        )
        if self.id_librivox and not has_audio:
            links.append(Link(
                rel="alternate",
                href=f"https://librivox.org/{self.id_librivox[0]}",
                type="text/html",
                title="LibriVox",
            ))

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
                opds_base = OpenLibraryDataProvider.OPDS_BASE_URL or OpenLibraryDataProvider.BASE_URL
                return [
                    Contributor(
                        name=name,
                        links=[
                            Link(
                                href=f"{OpenLibraryDataProvider.BASE_URL}/authors/{key}",
                                type="text/html",
                                rel="author",
                            ),
                            Link(
                                href=f"{opds_base}/authors/{key}",
                                type="application/opds+json",
                            ),
                        ],
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
            description=strip_markdown(book.description) if book.description else None,
            language=[lang for marc_lang in (book.language or []) if (lang := marc_language_to_iso_639_1(marc_lang))],
            # TODO: Use the edition-specific pagecount
            numberOfPages=self.number_of_pages_median,
        )


class OpenLibraryLanguageStub(TypedDict):
    key: str
    identifiers: dict[str, list[str]] | None


# Non-IA provider formats that produce acquisition links.
# epub and pdf are direct downloads.  "web" is normally a plain website link
# and is excluded — except when access="buy", which indicates a purchase link
# to a bookstore (e.g. Better World Books).  "audio" is omitted because
# audio providers (e.g. Librivox) are already surfaced via the IA webpub link.
_DOWNLOADABLE_FORMATS: frozenset[str] = frozenset({"epub", "pdf"})



def _build_ia_alternate_link(edition: OpenLibraryDataRecord.EditionDoc) -> Link:
    """Build an alternate link for an Internet Archive provider.

    Produces a ``rel=alternate`` link to the IA webpub manifest endpoint.
    An ``authenticate`` property tells the client where to obtain credentials.

    Link schema: https://github.com/readium/webpub-manifest/blob/master/schema/link.schema.json
    """
    if not edition.ia:
        raise ValueError("edition.ia must be non-empty to build an IA alternate link")
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
    """Build an acquisition link for a non-IA provider (e.g. Standard Ebooks).

    ``availability`` is only set for borrow/loan links — purchase links
    (``access="buy"``) are independent of the edition's loan state.

    ``indirectAcquisition`` is only set for downloadable formats (epub, pdf)
    where the link leads to a file rather than a web page.  Setting it on
    web purchase links is circular (``text/html`` → ``text/html``) and
    confuses OPDS clients.
    """
    rel = f'http://opds-spec.org/acquisition/{acq.access}' if acq.access else 'http://opds-spec.org/acquisition'
    if edition.ebook_access == "public":
        rel = "http://opds-spec.org/acquisition/open-access"

    link = Link(
        href=acq.url,
        rel=rel,
        type=map_ol_format_to_mime(acq.format) if acq.format else None,
        properties={}
    )

    # availability reflects loan/access state — not meaningful for purchase links.
    if acq.access != "buy":
        if edition.ebook_access:
            link.properties["availability"] = "unavailable"
            if edition.ebook_access == "public":
                link.properties["availability"] = "available"
        # availability.status represents loan state (checked in/out) — only applies to
        # borrowable books. Public/open-access books are always available regardless of
        # loan state, so we skip this block for them to avoid overriding the correct value.
        if edition.availability and edition.ebook_access != "public":
            status = edition.availability.status
            if status == "open" or status == "borrow_available":
                link.properties["availability"] = "available"
            elif status in ("private", "error", "borrow_unavailable"):
                link.properties["availability"] = "unavailable"

    if acq.provider_name:
        link.title = acq.provider_name
        # indirectAcquisition describes a DRM acquisition chain (e.g. ACSM → epub).
        # Only set it for downloadable formats — not for web purchase pages where
        # the type would be text/html → text/html (circular and meaningless).
        if acq.format in _DOWNLOADABLE_FORMATS:
            link.properties["indirectAcquisition"] = [
                {
                    "type": link.type,
                    "title": acq.provider_name,
                }
            ]

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
    """Convert an OL provider into zero or more OPDS links.

    IA providers → webpub alternate link (consistent reader UX).
    epub / pdf → acquisition download link.
    web + access="buy" → purchase link to an external bookstore.
    other web / audio → omitted (generic website links or Librivox audio
        already covered by the IA webpub alternate).
    """
    if not acq.url:
        raise ValueError("Provider URL is required for acquisition links")

    if acq.provider_name == "ia" and edition.ia:
        # All IA providers for the same edition share the same webpub URL.
        # Deduplication in links() (keyed on href+type) keeps only one copy.
        return [_build_ia_alternate_link(edition)]

    # Include web-format links only when they are explicit purchase links
    # (access="buy").  Generic web links (read online, browse) are excluded.
    if acq.format == "web" and acq.access != "buy":
        return []

    if acq.format not in _DOWNLOADABLE_FORMATS and acq.format != "web":
        return []

    return [_build_external_acquisition_link(edition, acq)]


def map_ol_format_to_mime(ol_format: Literal['web', 'pdf', 'epub', 'audio', 'daisy', 'djvu', 'mobi', 'txt'] | str) -> Optional[str]:
    """Map Open Library format strings to MIME types."""
    mapping = {
        'web': 'text/html',
        'pdf': 'application/pdf',
        'epub': 'application/epub+zip',
        'audio': 'audio/mpeg',
        'daisy': 'application/daisy+zip',
        'djvu': 'image/vnd.djvu',
        'mobi': 'application/x-mobipocket-ebook',
        'txt': 'text/plain',
    }
    return mapping.get(ol_format, 'application/octet-stream')


class _HTMLStripper(_HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


_md = _MarkdownIt()


def strip_markdown(text: str) -> str:
    """Convert Markdown/HTML to plain text using markdown-it-py.

    OpenLibrary descriptions may contain Markdown links, horizontal rules,
    emphasis, headings, and occasional inline HTML.  This function strips
    all of that to produce clean readable text suitable for OPDS 2.0
    ``description`` fields.
    """
    html = _md.render(text)
    stripper = _HTMLStripper()
    stripper.feed(html)
    result = stripper.get_text()
    result = result.replace('\r\n', '\n')
    result = _re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def _is_latin_name(text: str) -> bool:
    """Return True if every alphabetic character in *text* is Latin-script.

    Uses Unicode character names (e.g. "LATIN SMALL LETTER A") rather than
    a codepoint range so that IPA extensions and all Latin Extended blocks
    are handled correctly, and non-Latin scripts (Cyrillic, Arabic, CJK, …)
    are reliably detected regardless of their codepoint position.
    """
    return all(
        _unicodedata.name(c, "").startswith("LATIN") or not c.isalpha()
        for c in text
    )


# olid -> Latin personal_name, or None when no Latin alternative was found.
_latin_author_cache: dict[str, Optional[str]] = {}


def _latin_name_for_author(olid: str, current_name: str) -> str:
    """Return a Latin-script display name for *olid*.

    When *current_name* is already Latin it is returned unchanged.
    Otherwise the author record is fetched (once, then cached) and
    ``personal_name`` is returned if it is Latin.  Falls back to
    *current_name* if no Latin alternative can be found.
    """
    if _is_latin_name(current_name):
        return current_name
    if olid in _latin_author_cache:
        return _latin_author_cache[olid] or current_name
    try:
        data = _get(f"{OpenLibraryDataProvider.BASE_URL}/authors/{olid}.json").json()
        personal = data.get("personal_name")
        if personal and _is_latin_name(personal):
            _latin_author_cache[olid] = personal
            return personal
    except Exception:
        pass
    _latin_author_cache[olid] = None
    return current_name


def fetch_author_bio(olid: str) -> tuple[Optional[str], Optional[str]]:
    """Fetch author name and bio from the OpenLibrary author API.

    Returns ``(name, bio)`` where bio has been stripped of Markdown/HTML.
    Returns ``(None, None)`` on any failure — never raises.
    """
    try:
        r = _get(f"{OpenLibraryDataProvider.BASE_URL}/authors/{olid}.json")
        data = r.json()
        name: Optional[str] = data.get("name") or data.get("personal_name")
        # Prefer a Latin-script name when the OL primary name is in another script.
        if name and not _is_latin_name(name):
            personal = data.get("personal_name")
            if personal and _is_latin_name(personal):
                _latin_author_cache[olid] = personal
                name = personal
            else:
                _latin_author_cache[olid] = None
        raw_bio = data.get("bio")
        if isinstance(raw_bio, dict):
            raw_bio = raw_bio.get("value")
        bio: Optional[str] = strip_markdown(raw_bio) if raw_bio else None
        return name, bio
    except Exception:
        return None, None


def marc_language_to_iso_639_1(marc_code: str) -> Optional[str]:
    """Convert a MARC language code to an ISO 639-1 code using the cached languages map.

    Returns ``None`` (rather than raising) if the languages map cannot be fetched,
    so that book metadata is still returned without a language field on OL API errors.
    """
    try:
        return fetch_languages_map().get(marc_code)
    except Exception:
        return None


_languages_map_cache: Optional[dict[str, str]] = None
_languages_map_fetched_at: float = 0.0
_LANGUAGES_MAP_TTL: float = 24 * 60 * 60  # 1 day


def fetch_languages_map() -> dict[str, str]:
    """Return a map of MARC language codes to ISO 639-1 codes.

    Results are cached for ``_LANGUAGES_MAP_TTL`` seconds.  Unlike
    ``@functools.cache``, a failure to fetch does **not** poison the cache —
    the next request will retry the OL API rather than returning stale ``{}``.
    """
    global _languages_map_cache, _languages_map_fetched_at
    now = _time.monotonic()
    if _languages_map_cache is not None and (now - _languages_map_fetched_at) < _LANGUAGES_MAP_TTL:
        return _languages_map_cache
    try:
        r = _get("https://openlibrary.org/query.json?type=/type/language&key&identifiers&limit=1000")
    except Exception:
        if _languages_map_cache is not None:
            return _languages_map_cache
        raise
    data = cast(List[OpenLibraryLanguageStub], r.json())
    languages: dict[str, str] = {}
    for lang in data:
        marc_code = lang["key"].split("/")[-1]
        identifiers = lang.get("identifiers")
        if not identifiers:
            continue
        iso_codes = identifiers.get("iso_639_1", [])
        if iso_codes:
            languages[marc_code] = iso_codes[0]
    _languages_map_cache = languages
    _languages_map_fetched_at = now
    return languages


_iso_to_marc_cache: dict[str, str] = {}


def iso_639_1_to_marc(iso_code: str) -> Optional[str]:
    """Convert an ISO 639-1 code (e.g. 'en') to a MARC language code (e.g. 'eng').

    Uses a reverse-lookup cache built from ``fetch_languages_map()`` to avoid
    a linear scan on every call.  Returns ``None`` if no mapping is found.
    """
    if iso_code in _iso_to_marc_cache:
        return _iso_to_marc_cache[iso_code]
    lang_map = fetch_languages_map()  # MARC → ISO
    # Rebuild reverse cache from the latest map.
    _iso_to_marc_cache.clear()
    for marc, iso in lang_map.items():
        _iso_to_marc_cache[iso] = marc
    return _iso_to_marc_cache.get(iso_code)


def _has_acquisition_options(record: OpenLibraryDataRecord) -> bool:
    """Check if a record's edition would produce at least one usable OPDS link.

    Mirrors the filtering logic in ``ol_acquisition_to_opds_links`` so that
    books with no actionable link are hidden from results.
    """
    # Works with a LibriVox recording always have audio content, even when the
    # edition returned by OL's search API is a print/ebook edition with no providers.
    if record.id_librivox:
        return True
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if not edition or not edition.providers:
        return False
    for p in edition.providers:
        if not p.url:
            continue
        # Any IA provider produces a webpub alternate link.
        if p.provider_name == "ia" and edition.ia:
            return True
        # epub/pdf → download link.
        if p.format in _DOWNLOADABLE_FORMATS:
            return True
        # web + access="buy" → purchase link to an external bookstore.
        if p.format == "web" and p.access == "buy":
            return True
    return False


def _has_cover(record: OpenLibraryDataRecord) -> bool:
    """Check if a record has a cover image at the edition or work level."""
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if edition and edition.cover_i:
        return True
    return bool(record.cover_i)


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


def _get_edition_ebook_access(record: OpenLibraryDataRecord) -> Optional[str]:
    """Return the edition-level ebook_access, falling back to the work-level value.

    Used by the availability post-filter to enforce mode boundaries after
    language-based edition resolution may have swapped in a different edition.
    """
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if edition and edition.ebook_access:
        return edition.ebook_access
    return record.ebook_access


# Strict allowlist of edition-level ebook_access values per availability mode.
# The Solr query for 'ebooks' uses a range that includes 'public'; this post-filter
# enforces the correct boundary after all edition resolution is complete.
# 'buyable' is intentionally excluded: it is filtered client-side via
# _has_buyable_provider, and an open-access book with a priced provider is a valid result.
_EBOOK_MODE_ALLOWED: dict[str, frozenset[str]] = {
    "ebooks":          frozenset({"borrowable", "printdisabled"}),
    "open_access":     frozenset({"public"}),
    "print_disabled":  frozenset({"printdisabled"}),
}

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
    marc_language: str,
    edition_fields: list[str],
) -> Optional["_ResolvedEdition"]:
    """Find a full EditionDoc for *work_key* in the preferred MARC language code.

    Uses a single search call with ``language:<marc>`` + ``lang=<iso>`` so
    OL returns the work with the preferred-language edition directly.

    Falls back to the two-request approach (editions endpoint → search by
    edition key) if the single-call result doesn't match, ensuring backward
    compatibility with older OL API behaviour.

    Returns a ``_ResolvedEdition`` namedtuple (edition + author data) or
    ``None`` if no matching edition is found or any request fails.
    """
    # work_key is like "/works/OL123W" — extract the OLID for a key: query.
    work_olid = work_key.split("/")[-1]
    iso_lang = fetch_languages_map().get(marc_language)
    search_fields = "editions,author_name,author_key," + ",".join(edition_fields)

    try:
        # Single-call approach: search by work key with language filter.
        r = _get(
            f"{OpenLibraryDataProvider.BASE_URL}/search.json",
            params={
                "q": f"key:/works/{work_olid} language:{marc_language}",
                "editions": "true",
                **({'lang': iso_lang} if iso_lang else {}),
                "fields": search_fields,
                "limit": 1,
            },
        )
        docs = r.json().get("docs", [])
        if docs:
            edition_docs = docs[0].get("editions", {}).get("docs", [])
            if edition_docs:
                ed = OpenLibraryDataRecord.EditionDoc.model_validate(edition_docs[0])
                # Verify the returned edition actually matches the language.
                if ed.language and iso_lang and iso_lang in ed.language:
                    return _ResolvedEdition(
                        edition=ed,
                        author_name=docs[0].get("author_name") or None,
                        author_key=docs[0].get("author_key") or None,
                    )

        # Fallback: editions endpoint → search by edition key (2 requests).
        # Handles cases where the single-call approach returns a mismatched
        # edition or OL's lang param doesn't produce the expected result.
        r2 = _get(
            f"{OpenLibraryDataProvider.BASE_URL}{work_key}/editions.json",
            params={"limit": 50, "fields": "key,languages"},
        )
        preferred_olid: Optional[str] = None
        for entry in r2.json().get("entries", []):
            langs = [lang["key"].split("/")[-1] for lang in entry.get("languages", [])]
            if marc_language in langs:
                preferred_olid = entry["key"].split("/")[-1]
                break

        if not preferred_olid:
            return None

        r3 = _get(
            f"{OpenLibraryDataProvider.BASE_URL}/search.json",
            params={
                "q": f"edition_key:{preferred_olid}",
                "editions": "true",
                "fields": search_fields,
                "limit": 1,
            },
        )
        docs = r3.json().get("docs", [])
        if not docs:
            return None
        edition_docs = docs[0].get("editions", {}).get("docs", [])
        if not edition_docs:
            return None
        edition = OpenLibraryDataRecord.EditionDoc.model_validate(edition_docs[0])
        return _ResolvedEdition(
            edition=edition,
            author_name=docs[0].get("author_name") or None,
            author_key=docs[0].get("author_key") or None,
        )
    except Exception:
        return None


_EDITION_RESOLVE_FIELDS = [
    "key", "title", "subtitle", "description", "cover_i",
    "ebook_access", "language", "ia", "availability", "providers",
]


def _align_editions_to_language(
    records: list[OpenLibraryDataRecord],
    language: str,
    resolve_mismatched: bool = False,
) -> list[OpenLibraryDataRecord]:
    """Reorder or resolve editions so the first one matches *language* (ISO 639-1).

    - Multiple editions: move language-matching ones to the front (free).
    - Single mismatched edition: when *resolve_mismatched* is ``True``,
      call ``_resolve_preferred_edition`` (2 HTTP requests per record).
      This is expensive and should only be used for targeted queries
      (e.g. ``edition_key:``).  For general searches the Solr
      ``language:`` filter + ``lang`` param already ensure the right
      edition in almost all cases.
    """
    marc_lang: Optional[str] = None
    if resolve_mismatched:
        marc_lang = iso_639_1_to_marc(language)
    for record in records:
        if not (record.editions and record.editions.docs):
            continue
        if len(record.editions.docs) > 1:
            matched = [d for d in record.editions.docs if d.language and language in d.language]
            others = [d for d in record.editions.docs if not (d.language and language in d.language)]
            if matched:
                record.editions.docs = matched + others
        elif resolve_mismatched:
            ed = record.editions.docs[0]
            if not ed.language or language not in ed.language:
                if marc_lang and record.key:
                    preferred = _resolve_preferred_edition(
                        record.key, marc_lang, _EDITION_RESOLVE_FIELDS
                    )
                    if preferred:
                        record.editions.docs[0] = preferred.edition
                        if preferred.author_name:
                            record.author_name = preferred.author_name
                        if preferred.author_key:
                            record.author_key = preferred.author_key
    return records


# ---------------------------------------------------------------------------
# Shared availability-facet primitives
# ---------------------------------------------------------------------------

# Single canonical label per mode — used by both build_facets and build_home_facets.
_AVAILABILITY_MODES: list[tuple[str, str]] = [
    ("everything",     "Everything"),
    ("ebooks",         "Available to Borrow"),
    ("print_disabled", "Print Disabled"),
    ("open_access",    "Open Access"),
    ("buyable",        "Available for Purchase"),
]

# Language options for the Language facet group (OPDS 2.0 §2.4).
# ``None`` means "no language filter" (All Languages).
# Language codes follow BCP 47 / ISO 639-1 (e.g. "en" for English).
_LANGUAGE_OPTIONS: list[tuple[Optional[str], str]] = [
    (None, "All"),
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("hi", "Hindi"),
]

# Media type options for the Media Type facet group (OPDS 2.0 §2.4).
# ``None`` means "no media type filter" (All).
_MEDIA_TYPE_OPTIONS: list[tuple[Optional[str], str]] = [
    (None, "All"),
    ("ebook", "Ebooks"),
    ("audiobook", "Audiobooks"),
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
    default_labels = {val: label for val, label in _AVAILABILITY_MODES}
    resolved = {**default_labels, **(labels or {})}
    counts = counts or {}
    exclude = exclude or set()
    links = []
    for val, _ in _AVAILABILITY_MODES:
        if val in exclude:
            continue
        link: dict = {
            "title": resolved[val],
            "href": href_fn(val),
            "type": "application/opds+json",
        }
        if val == mode:
            link["rel"] = "self"
            link.setdefault("properties", {})["active"] = True
        count = counts.get(val)
        if count is not None:
            link.setdefault("properties", {})["numberOfItems"] = count
        links.append(link)
    return links


def _build_language_links(
    language: Optional[str],
    href_fn: typing.Callable[[Optional[str]], str],
) -> list[dict]:
    """Build the list of language facet link dicts per OPDS 2.0 §2.4.

    Iterates ``_LANGUAGE_OPTIONS`` so adding a new language only requires
    appending to that list — no logic changes needed here.

    The currently active language is indicated by ``rel: "self"`` on its link,
    as required by the OPDS 2.0 specification.  ``language=None`` means
    "All Languages" (no filter); that entry is always first in the list.

    Args:
        language: Active BCP 47 language code (e.g. ``"en"``), or ``None``
            for the "All Languages" (unfiltered) selection.
        href_fn: Converts a language code (or ``None``) to a full URL.
    """
    links = []
    for lang_code, label in _LANGUAGE_OPTIONS:
        link: dict = {
            "title": label,
            "href": href_fn(lang_code),
            "type": "application/opds+json",
        }
        if lang_code == language:
            link["rel"] = "self"
            link.setdefault("properties", {})["active"] = True
        links.append(link)
    return links

def _apply_media_type_filter(query: str, media_type: Optional[str]) -> str:
    """Return *query* with a Solr clause for the requested media type.

    - ``media_type="audiobook"`` appends ``id_librivox:*`` to restrict to
      works that have a LibriVox audio recording on the Internet Archive.
    - ``media_type="ebook"`` appends ``ebook_access:[printdisabled TO *]``
      when that filter is not already present.
    - ``media_type=None`` returns the query unchanged.
    """
    if media_type == "audiobook":
        return f"{query} id_librivox:*".strip()
    if media_type == "ebook" and "ebook_access:" not in query:
        return f"{query} ebook_access:[printdisabled TO *]".strip()
    return query


def _build_media_type_links(
    media_type: Optional[str],
    href_fn: typing.Callable[[Optional[str]], str],
) -> list[dict]:
    """Build the list of media type facet link dicts per OPDS 2.0 §2.4."""
    links = []
    for mt_code, label in _MEDIA_TYPE_OPTIONS:
        link: dict = {
            "title": label,
            "href": href_fn(mt_code),
            "type": "application/opds+json",
        }
        if mt_code == media_type:
            link["rel"] = "self"
            link.setdefault("properties", {})["active"] = True
        links.append(link)
    return links


# ---------------------------------------------------------------------------
# Homepage carousel group helpers
# ---------------------------------------------------------------------------

_CLASSIC_BOOKS_GROUP: tuple[str, str, str] = (
    "Classic Books",
    'ddc:8* first_publish_year:[* TO 1950] publish_year:[2000 TO *] NOT public_scan_b:false -subject:"content_warning:cover"',
    "trending",
)

_STANDARD_EBOOKS_GROUP: tuple[str, str, str] = (
    "Standard Ebooks",
    'publisher:"Standard Ebooks" ebook_access:public',
    "random.hourly",
)

_KIDS_SUBJECT_FILTER: str = (
    '(subject_key:(juvenile_audience OR children\'s_fiction OR juvenile_nonfiction OR juvenile_encyclopedias OR '
    'juvenile_riddles OR juvenile_poetry OR juvenile_wit_and_humor OR juvenile_limericks OR juvenile_dictionaries OR '
    'juvenile_non-fiction) OR subject:("Juvenile literature" OR "Juvenile fiction" OR "pour la jeunesse" OR "pour enfants"))'
)


def _subject_group(
    title: str,
    subject_filter: str,
    ea: str,
    sort: str = "trending",
    extra: str = "",
) -> tuple[str, str, str]:
    """Build a subject-genre carousel group tuple."""
    parts = [subject_filter, ea, 'trending_score_hourly_sum:[1 TO *]', '-subject:"content_warning:cover"']
    if extra:
        parts.append(extra)
    return (title, " ".join(parts), sort)


def _kids_group(ea: str) -> tuple[str, str, str]:
    """Build the Kids carousel group tuple for a given ebook_access filter."""
    return ("Kids", f'{ea} trending_score_hourly_sum:[1 TO *] {_KIDS_SUBJECT_FILTER} -subject:"content_warning:cover"', "random.hourly")


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
        elif mode == 'print_disabled' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:printdisabled"
        elif mode == 'open_access' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:public"

        r = _get(
            f"{OpenLibraryDataProvider.BASE_URL}/search.json",
            params={"q": internal_query, "limit": 0, "fields": "key"},
        )
        return r.json().get("numFound", 0)

    @staticmethod
    def fetch_facet_counts(
        query: str,
        known_mode: Optional[str] = None,
        known_total: Optional[int] = None,
        media_type: Optional[str] = None,
    ) -> dict[str, Optional[int]]:
        """Fetch ``numberOfItems`` counts for every availability mode.

        If *known_mode* and *known_total* are provided the count request for
        that mode is skipped (we already have it from the main search).

        Modes that cannot be counted server-side (e.g. ``buyable``) will have
        a ``None`` value unless supplied via *known_mode*/*known_total*.

        Count requests run in parallel using a thread pool for speed.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Apply media_type filter to base query before per-mode count requests.
        base_query = _apply_media_type_filter(query, media_type)

        modes = ["everything", "ebooks", "print_disabled", "open_access", "buyable"]
        counts: dict[str, Optional[int]] = {}
        to_fetch: list[str] = []
        for m in modes:
            if known_mode and m == known_mode and known_total is not None:
                counts[m] = known_total
            elif m == "buyable":
                counts[m] = None
            else:
                to_fetch.append(m)

        if to_fetch:
            with ThreadPoolExecutor(max_workers=len(to_fetch)) as pool:
                futures = {pool.submit(OpenLibraryDataProvider._count_for_mode, base_query, m): m for m in to_fetch}
                for future in as_completed(futures):
                    counts[futures[future]] = future.result()

        return counts

    @staticmethod
    def build_facets(
        base_url: str,
        query: str,
        sort: Optional[str] = None,
        mode: str = "everything",
        language: Optional[str] = None,
        title: Optional[str] = None,
        total: Optional[int] = None,
        availability_counts: Optional[dict[str, int]] = None,
        media_type: Optional[str] = None,
    ) -> list[dict]:
        """Build OPDS 2.0 facets for availability and language filtering.

        Returns two facet groups per OPDS 2.0 §2.4:
        - **Availability** – links to ``/search`` with the appropriate mode.
        - **Language** – links to ``/search`` with the appropriate language code.

        Each active facet link is marked with ``rel: "self"`` as required by
        the specification.  Availability links point to ``/search``; for
        homepage facets use ``build_home_facets``.

        The Sort group is retained in ``_build_sort_links`` but not included here.
        To re-enable sort: add the Sort group dict to the returned list.

        Args:
            language: BCP 47 language code for the active selection (e.g.
                ``"en"``), or ``None`` for "All Languages" (no filter).
                This value is preserved in every facet href so that switching
                availability mode keeps the current language selection, and
                vice-versa.
            title: Display title for the search results page (e.g. ``"Art"``).
                Preserved in every facet href so switching facets keeps the
                page title instead of falling back to "Search Results".
            total: Reserved for Sort links when re-enabled.
            availability_counts: ``{mode_value: item_count}`` per OPDS 2.0 §2.4.
        """
        def search_href(
            sort_val: Optional[str] = sort,
            mode_val: str = mode,
            lang_val: Optional[str] = language,
            mt_val: Optional[str] = media_type,
        ) -> str:
            # Strip ebook_access filters from the query for "everything" mode
            # so the facet link truly returns all results regardless of how
            # the user arrived at the search page.
            # Handles both simple values (ebook_access:public) and range
            # queries (ebook_access:[borrowable TO *]).
            q = _re.sub(r'\s*ebook_access:(?:\[[^\]]*\]|\S+)', '', query).strip() if mode_val == "everything" else query
            params: dict[str, str] = {"query": q}
            if sort_val:
                params["sort"] = sort_val
            if mode_val and mode_val != "everything":
                params["mode"] = mode_val
            if lang_val:
                params["language"] = lang_val
            if mt_val:
                params["media_type"] = mt_val
            if title:
                params["title"] = title
            return f"{base_url}/search?{urlencode(params)}"

        # Sort group is unplugged but preserved — re-enable by adding:
        # {"metadata": {"title": "Sort"},
        #  "links": _build_sort_links(sort, lambda sv: search_href(sort_val=sv), total)}

        return [
            {
                "metadata": {"title": "Availability"},
                "links": _build_availability_links(
                    mode=mode,
                    href_fn=lambda val: search_href(mode_val=val),
                    counts=availability_counts,
                ),
            },
            {
                "metadata": {"title": "Language"},
                "links": _build_language_links(
                    language=language,
                    href_fn=lambda lang: search_href(lang_val=lang),
                ),
            },
            {
                "metadata": {"title": "Media Type"},
                "links": _build_media_type_links(
                    media_type=media_type,
                    href_fn=lambda mt: search_href(mt_val=mt),
                ),
            },
        ]

    @staticmethod
    def build_home_facets(
        base_url: str,
        mode: str = "everything",
        language: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> list[dict]:
        """Build Availability, Language, and Media Type facet groups for the OPDS homepage.

        Uses the same canonical labels as ``build_facets``.
        Links point to ``<base_url>/?mode=<value>`` / ``<base_url>/?language=<code>``.
        Each active facet link is marked with ``rel: "self"`` per OPDS 2.0 §2.4.

        Args:
            base_url: Base URL of the OPDS service (no trailing slash).
            mode: Currently active availability mode.
            language: Active BCP 47 language code (e.g. ``"en"``), or ``None``
                for "All Languages" (no filter).
            media_type: Active media type (e.g. ``"ebook"`` or ``"audiobook"``),
                or ``None`` for all media types.
        """
        def home_href(val: str) -> str:
            params: dict[str, str] = {}
            if val != "everything":
                params["mode"] = val
            if language:
                params["language"] = language
            if media_type:
                params["media_type"] = media_type
            return f"{base_url}/?{urlencode(params)}" if params else f"{base_url}/"

        def lang_href(lang: Optional[str]) -> str:
            params: dict[str, str] = {}
            if mode != "everything":
                params["mode"] = mode
            if lang:
                params["language"] = lang
            if media_type:
                params["media_type"] = media_type
            return f"{base_url}/?{urlencode(params)}" if params else f"{base_url}/"

        def mt_href(mt: Optional[str]) -> str:
            params: dict[str, str] = {}
            if mode != "everything":
                params["mode"] = mode
            if language:
                params["language"] = language
            if mt:
                params["media_type"] = mt
            return f"{base_url}/?{urlencode(params)}" if params else f"{base_url}/"

        return [
            {
                "metadata": {"title": "Availability"},
                "links": _build_availability_links(
                    mode=mode,
                    href_fn=home_href,
                    exclude={"buyable"},
                ),
            },
            {
                "metadata": {"title": "Language"},
                "links": _build_language_links(
                    language=language,
                    href_fn=lang_href,
                ),
            },
            {
                "metadata": {"title": "Media Type"},
                "links": _build_media_type_links(
                    media_type=media_type,
                    href_fn=mt_href,
                ),
            },
        ]

    @staticmethod
    def build_author_facets(
        base_url: str,
        olid: str,
        mode: str = "everything",
        language: Optional[str] = None,
        media_type: Optional[str] = None,
        page: int = 1,
        limit: int = 25,
    ) -> list[dict]:
        """Build Availability, Language, and Media Type facet groups for an author catalog page.

        Links point to ``<base_url>/authors/<olid>?mode=<value>&...``, preserving
        the current page, limit, language, and media_type selections when switching
        between facets.
        """
        def author_href(
            mode_val: str = mode,
            lang_val: Optional[str] = language,
            mt_val: Optional[str] = media_type,
        ) -> str:
            params: dict[str, str] = {}
            if page > 1:
                params["page"] = str(page)
            if limit != 25:
                params["limit"] = str(limit)
            if mode_val != "everything":
                params["mode"] = mode_val
            if lang_val:
                params["language"] = lang_val
            if mt_val:
                params["media_type"] = mt_val
            return f"{base_url}/authors/{olid}?{urlencode(params)}" if params else f"{base_url}/authors/{olid}"

        return [
            {
                "metadata": {"title": "Availability"},
                "links": _build_availability_links(
                    mode=mode,
                    href_fn=lambda val: author_href(mode_val=val),
                    exclude={"buyable"},
                ),
            },
            {
                "metadata": {"title": "Language"},
                "links": _build_language_links(
                    language=language,
                    href_fn=lambda lang: author_href(lang_val=lang),
                ),
            },
            {
                "metadata": {"title": "Media Type"},
                "links": _build_media_type_links(
                    media_type=media_type,
                    href_fn=lambda mt: author_href(mt_val=mt),
                ),
            },
        ]

    # -- Homepage group definitions & pagination ---------------------------

    GROUPS_PER_PAGE: int = 3

    OPDS_MEDIA_TYPE: str = "application/opds+json"

    FEATURED_SUBJECTS: list[dict[str, str]] = [
        {"key": "/subjects/art",                           "presentable_name": "Art"},
        {"key": "/subjects/science_fiction",               "presentable_name": "Science Fiction"},
        {"key": "/subjects/fantasy",                       "presentable_name": "Fantasy"},
        {"key": "/subjects/biographies",                   "presentable_name": "Biographies"},
        {"key": "/subjects/recipes",                       "presentable_name": "Recipes"},
        {"key": "/subjects/romance",                       "presentable_name": "Romance"},
        {"key": "/subjects/textbooks",                     "presentable_name": "Textbooks"},
        {"key": "/subjects/children",                      "presentable_name": "Children"},
        {"key": "/subjects/history",                       "presentable_name": "History"},
        {"key": "/subjects/medicine",                      "presentable_name": "Medicine"},
        {"key": "/subjects/religion",                      "presentable_name": "Religion"},
        {"key": "/subjects/mystery_and_detective_stories", "presentable_name": "Mystery and Detective Stories"},
        {"key": "/subjects/plays",                         "presentable_name": "Plays"},
        {"key": "/subjects/music",                         "presentable_name": "Music"},
        {"key": "/subjects/science",                       "presentable_name": "Science"},
        {"presentable_name": "Standard Ebooks",            "query": 'publisher:"Standard Ebooks" ebook_access:public'},
    ]

    @staticmethod
    def _home_groups_config(mode: str = "everything") -> list[tuple[str, str, str]]:
        """Return the full list of homepage group definitions.

        Each entry is ``(title, solr_query, sort)``.  The *mode* parameter
        controls the ``ebook_access`` filter baked into each query.
        """
        if mode == "open_access":
            # Public-domain-friendly groups ordered by reliability.
            # Romance, Thrillers, and Textbooks are excluded because post-1928
            # books in those genres are mostly still under copyright.
            # Trending drops readinglog_count so older public-domain classics
            # (which accumulate fewer logs) still surface.
            oa = "ebook_access:public"
            return [
                _CLASSIC_BOOKS_GROUP,
                _STANDARD_EBOOKS_GROUP,
                ("Trending Books", f'trending_score_hourly_sum:[1 TO *] -subject:"content_warning:cover" {oa}', "trending"),
                _kids_group(oa),
                _subject_group("Science", "subject_key:science", oa),
                _subject_group("History", "subject_key:history", oa),
                _subject_group("Philosophy", "subject_key:philosophy", oa),
            ]

        if mode == "print_disabled":
            # Print-disabled groups ordered by density of printdisabled books.
            # Standard Ebooks is excluded (ebook_access:public, filtered out by post-filter).
            # Trending drops readinglog_count because print-disabled titles
            # accumulate fewer reading logs than borrowable books.
            pd = "ebook_access:printdisabled"
            return [
                _CLASSIC_BOOKS_GROUP,
                _kids_group(pd),
                ("Textbooks", f'subject_key:textbooks publish_year:[1990 TO *] {pd}', "trending"),
                ("Trending Books", f'trending_score_hourly_sum:[1 TO *] -subject:"content_warning:cover" {pd}', "trending"),
                _subject_group("Romance", "subject:romance first_publish_year:[1930 TO *]", pd, sort="trending,trending_score_hourly_sum"),
                _subject_group("Thrillers", "subject:thrillers", pd, sort="trending,trending_score_hourly_sum"),
                _subject_group("Science", "subject_key:science", pd),
            ]

        ea = "ebook_access:[borrowable TO *]"
        return [
            ("Trending Books", f'trending_score_hourly_sum:[1 TO *] -subject:"content_warning:cover" {ea} readinglog_count:[4 TO *]', "trending"),
            _CLASSIC_BOOKS_GROUP,
            _subject_group("Romance", "subject:romance first_publish_year:[1930 TO *]", ea, sort="trending,trending_score_hourly_sum"),
            _kids_group(ea),
            _subject_group("Thrillers", "subject:thrillers", ea, sort="trending,trending_score_hourly_sum"),
            ("Textbooks", f'subject_key:textbooks publish_year:[1990 TO *] {ea}', "trending"),
            _STANDARD_EBOOKS_GROUP,
        ]

    @staticmethod
    def _home_page_href(
        base: str, mode: str, language: Optional[str], page: int,
        media_type: Optional[str] = None,
    ) -> str:
        """Build a homepage href with pagination."""
        params: dict[str, str] = {}
        if mode != "everything":
            params["mode"] = mode
        if language:
            params["language"] = language
        if media_type:
            params["media_type"] = media_type
        if page > 1:
            params["page"] = str(page)
        return f"{base}/?{urlencode(params)}" if params else f"{base}/"

    @classmethod
    def build_home_feed(
        cls,
        base: str,
        mode: str = "everything",
        language: Optional[str] = None,
        page: int = 1,
        featured_subjects: Optional[list[dict[str, str]]] = None,
        media_type: Optional[str] = None,
    ) -> dict:
        """Build a complete OPDS 2.0 homepage catalog dict.

        Fetches the current page's batch of groups from Open Library,
        builds navigation (page 1 only), facets (page 1 only), and
        pagination links (``next`` / ``previous``).

        Args:
            base: OPDS base URL (no trailing slash).
            mode: Availability filter (``everything``, ``ebooks``,
                ``open_access``, ``buyable``).
            language: BCP 47 language code or ``None`` for all.
            page: 1-based page number for group pagination.
            featured_subjects: Override the default ``FEATURED_SUBJECTS``
                list.  Each entry needs ``presentable_name`` and either
                ``key`` or ``query``.
            media_type: Media type filter (``"ebook"``, ``"audiobook"``,
                or ``None`` for all).

        Returns:
            A dict ready to be serialised as JSON (via ``Catalog.model_dump``).
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        subjects = featured_subjects if featured_subjects is not None else cls.FEATURED_SUBJECTS
        media = cls.OPDS_MEDIA_TYPE
        search_url = cls.SEARCH_URL
        all_groups = cls._home_groups_config(mode)

        # Paginate
        per_page = cls.GROUPS_PER_PAGE
        start = (page - 1) * per_page
        page_groups = all_groups[start : start + per_page]
        has_next = start + per_page < len(all_groups)

        # Fetch groups in parallel
        def fetch_one(title: str, query: str, sort: str) -> Optional[Catalog]:
            try:
                resp = cls.search(
                    query=query, sort=sort, limit=25,
                    language=language, facets={"mode": mode}, title=title,
                    media_type=media_type,
                )
                return Catalog.create(metadata=Metadata(title=title), response=resp)
            except Exception:
                return None

        loaded_groups: list[Catalog] = []
        if page_groups:
            with ThreadPoolExecutor(max_workers=len(page_groups)) as pool:
                futures = {
                    pool.submit(fetch_one, t, q, s): i
                    for i, (t, q, s) in enumerate(page_groups)
                }
                results: list[Optional[Catalog]] = [None] * len(page_groups)
                for future in as_completed(futures):
                    results[futures[future]] = future.result()

            loaded_groups = [
                g for g in results
                if g is not None and g.publications
            ]

        # Navigation — only on page 1 when groups loaded
        navigation: list[Navigation] = []
        if page == 1 and loaded_groups:
            for subject in subjects:
                q = subject.get("query") or (
                    f'subject_key:{subject["key"].split("/")[-1]}'
                    f' -subject:"content_warning:cover"'
                    f' ebook_access:[borrowable TO *]'
                )
                nav_params: dict[str, str] = {
                    "sort": "trending",
                    "title": subject["presentable_name"],
                    "query": q,
                }
                if language:
                    nav_params["language"] = language
                if media_type:
                    nav_params["media_type"] = media_type
                navigation.append(Navigation(
                    type=media,
                    title=subject["presentable_name"],
                    href=f"{search_url}?{urlencode(nav_params)}",
                ))

        # Links
        links = [
            Link(rel="self", href=cls._home_page_href(base, mode, language, page, media_type), type=media),
            Link(rel="start", href=f"{base}/", type=media),
            Link(rel="search", href=f"{base}/search{{?query}}", type=media, templated=True),
            cls.bookshelf_link(),
            cls.profile_link(),
        ]
        if has_next:
            links.append(Link(rel="next", href=cls._home_page_href(base, mode, language, page + 1, media_type), type=media))
        if page > 1:
            links.append(Link(rel="previous", href=cls._home_page_href(base, mode, language, page - 1, media_type), type=media))

        catalog = Catalog(
            metadata=Metadata(title="Open Library"),
            publications=[],
            navigation=navigation,
            groups=loaded_groups,
            facets=cls.build_home_facets(base, mode, language, media_type),
            links=links,
        )
        return catalog.model_dump()

    @typing.override
    @staticmethod
    def search(
        query: str,
        limit: int = 50,
        offset: int = 0,
        sort: Optional[str] = None,
        facets: Optional[dict[str, str]] = None,
        language: Optional[str] = None,
        title: Optional[str] = None,
        require_cover: bool = True,
        media_type: Optional[str] = None,
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
            language: BCP 47 language code to prefer (e.g. ``"en"`` for
                English), or ``None`` to return results in all languages
                without any language filter.  When set, OL is asked to
                surface editions in that language and, for
                ``edition_key:`` queries, the preferred edition is
                resolved via the work's editions endpoint.
        """
        fields = [
            "key", "title", "editions", "description", "providers", "author_name", "ia",
            "cover_i", "availability", "ebook_access", "author_key", "subtitle", "language",
            "number_of_pages_median", "id_librivox",
        ]

        internal_query = query
        if facets:
            mode = facets.get('mode', 'everything')
        else:
            mode = 'everything'

        # "Everything" means no ebook_access restriction — strip any filter
        # that may have been baked into the query by navigation links.
        if mode == 'everything':
            internal_query = _re.sub(r'\s*ebook_access:(?:\[[^\]]*\]|\S+)', '', internal_query).strip()
        elif mode == 'ebooks' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:[printdisabled TO *]"
        elif mode == 'print_disabled' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:printdisabled"
        elif mode == 'open_access' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:public"
        elif mode == 'buyable' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:[printdisabled TO *]"

        # Apply media_type filter (audiobook/ebook) on top of the mode filter.
        internal_query = _apply_media_type_filter(internal_query, media_type)

        # When a language filter is active, add language:<MARC> to the Solr
        # query so non-matching works are excluded (the `lang` param only
        # influences edition preference / ranking, it does not filter).
        if language and 'language:' not in internal_query:
            marc = iso_639_1_to_marc(language)
            if marc:
                internal_query = f"{internal_query} language:{marc}"

        params = {
            "editions": "true",
            "q": internal_query,
            "page": (offset // limit) + 1 if limit else 1,
            "limit": limit,
            **({'sort': sort} if sort else {}),
            "fields": ",".join(fields),
            # Also pass lang to prefer editions in the requested language.
            **({'lang': language} if language else {}),
        }
        r = _get(f"{OpenLibraryDataProvider.BASE_URL}/search.json", params=params)
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

        # Ensure the displayed edition matches the selected language.
        # - Multiple editions: reorder so language-matching ones come first.
        # - Single mismatched edition: resolve the preferred edition from OL.
        # This also handles edition_key: queries where OL ignores the lang param.
        if language:
            records = _align_editions_to_language(
                records, language,
                resolve_mismatched="edition_key:" in query,
            )

        # Always filter out records with no usable OPDS links.
        # When require_cover is True (homepage groups, navigation), also
        # filter out records without a cover image or description to avoid
        # broken "Cover Unavailable" cards.  Search results keep these so
        # users can find all available books.
        if require_cover:
            records = [r for r in records if _has_acquisition_options(r) and _has_cover(r)]
        else:
            records = [r for r in records if _has_acquisition_options(r)]

        # When the user explicitly filters by ebook media type, exclude audiobook
        # records (works whose primary content is a LibriVox audio recording).
        # LibriVox works have ebook_access:public so they survive mode filters,
        # but they belong in the audiobook facet, not the ebook facet.
        if media_type == "ebook":
            records = [r for r in records if not r.id_librivox]

        if mode in ('ebooks', 'print_disabled', 'open_access', 'buyable'):
            if mode == 'buyable':
                # Keep only records that have a non-free provider.
                # This is a client-side filter (Solr has no buyable field).
                records = [r for r in records if _has_buyable_provider(r)]
            # Sort available books before unavailable, preserving order within each group
            records.sort(key=lambda r: (0 if _is_currently_available(r) else 1))

        # Strict post-filter: enforce ebook_access boundaries after all edition resolution.
        # We check BOTH edition-level AND work-level so that language-based edition swaps
        # cannot cause false negatives. Example: a public-domain work (work.ebook_access=
        # "public") whose language-preferred edition happens to have ebook_access=
        # "borrowable" should still appear in open_access mode — the Solr query already
        # guaranteed the work is public. For ebooks mode, the work-level OR means a
        # borrowable work whose language-swapped edition is public will still be included.
        if mode in _EBOOK_MODE_ALLOWED:
            allowed = _EBOOK_MODE_ALLOWED[mode]
            records = [r for r in records if _get_edition_ebook_access(r) in allowed or r.ebook_access in allowed]

        # Set total for buyable after ALL filters — client-side filtering means Solr
        # cannot produce an accurate count; use the final post-filter record count.
        if mode == 'buyable':
            total = len(records)

        # Resolve non-Latin author names (e.g. Cyrillic) to their Latin personal_name.
        # OL's search index stores author_name from the author record's `name` field,
        # which for authors like Chekhov is in their native script. We fetch the author
        # record once (cached) and use personal_name when it is Latin-script.
        olid_to_nonlatin: dict[str, str] = {}  # olid -> first non-Latin name seen
        for r in records:
            if r.author_name and r.author_key:
                for name, key in zip(r.author_name, r.author_key):
                    if not _is_latin_name(name) and key not in _latin_author_cache and key not in olid_to_nonlatin:
                        olid_to_nonlatin[key] = name
        if olid_to_nonlatin:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=min(len(olid_to_nonlatin), 4)) as pool:
                futures = {pool.submit(_latin_name_for_author, olid, name): olid for olid, name in olid_to_nonlatin.items()}
                for future in as_completed(futures):
                    future.result()
        for record in records:
            if record.author_name and record.author_key:
                resolved = [
                    (_latin_author_cache.get(key) or name) if not _is_latin_name(name) else name
                    for name, key in zip(record.author_name, record.author_key)
                ]
                # Preserve any trailing names that have no corresponding author_key entry.
                resolved.extend(record.author_name[len(resolved):])
                record.author_name = resolved

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