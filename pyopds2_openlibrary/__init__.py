import atexit as _atexit
import re as _re
import threading as _threading
import time as _time
import typing
import unicodedata as _unicodedata
from html.parser import HTMLParser as _HTMLParser
from typing import List, Optional, TypedDict, Union, cast
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
    Link,
    Publication,
)


# Force JSON-LD aliases (e.g. ``@type``) when serializing pyopds2 models.
# Upstream models declare ``alias="@type"`` on Metadata.type but do not default
# ``by_alias=True`` in ``model_dump``, so JSON output uses ``"type"`` instead
# of the spec-required ``"@type"``. Patch the base classes here so every
# caller — including app routes that construct ``Catalog`` directly — gets
# spec-compliant output.
def _patch_by_alias(cls):
    original = cls.model_dump

    def model_dump(self, **kwargs):
        kwargs.setdefault("by_alias", True)
        return original(self, **kwargs)

    cls.model_dump = model_dump


_patch_by_alias(Publication)
_patch_by_alias(Catalog)


class Subject(BaseModel):
    """An OPDS 2.0 subject object: a display name plus a browse link.

    Open Library subjects are free-text, not drawn from a controlled
    vocabulary (e.g. Thema), so ``code`` and ``scheme`` are intentionally
    absent — only ``name`` and ``links`` are available.
    """
    name: str
    links: Optional[List[Link]] = None


# pyopds2 declares ``Metadata.subject`` as ``List[str]``, which rejects the
# richer object form OPDS 2.0 also permits. Widen it to accept Subject objects
# while keeping plain strings valid, then rebuild the model so the new
# annotation takes effect.
Metadata.model_fields['subject'].annotation = Optional[List[Union[Subject, str]]]
Metadata.model_rebuild(force=True)
# Models that embed Metadata compiled their nested serializer against the old
# ``List[str]`` schema; rebuild them too so they emit Subject objects without
# Pydantic serialization warnings.
Publication.model_rebuild(force=True)
Catalog.model_rebuild(force=True)


# Matches a single ``ebook_access:`` clause in a Solr query string —
# either a bare value (``ebook_access:public``) or a range
# (``ebook_access:[printdisabled TO *]``). Used to strip a baked-in
# ebook_access clause before re-applying the user-selected availability mode.
_EBOOK_ACCESS_CLAUSE_RE = _re.compile(r'\s*ebook_access:(?:\[[^\]]*\]|\S+)')

_REQUEST_TIMEOUT: float = 30.0

# HTTP status codes that indicate a transient server-side failure worth retrying.
_RETRY_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
# Default delays between attempts (seconds): 3 total attempts — immediate, +0.25 s,
# +0.5 s. These requests are on the critical path of user-facing pages, so the
# total backoff budget is kept under ~1 s — a transient blip should cost a beat,
# not seconds. A 429 Retry-After header overrides the per-attempt delay when
# present (capped below to stay within the same budget).
_RETRY_DELAYS: tuple[float, ...] = (0.0, 0.25, 0.5)
# Cap on Retry-After so a server asking us to wait can't blow the ~1 s budget.
_RETRY_AFTER_MAX: float = 0.5
# Default User-Agent. OpenLibrary's edge blocks the default httpx UA with 403,
# so every outbound request must identify itself. Consumers should override
# via ``OpenLibraryDataProvider.USER_AGENT`` to include contact info.
DEFAULT_USER_AGENT: str = "pyopds2_openlibrary/1.0 (+https://github.com/ArchiveLabs/pyopds2_openlibrary)"


def _user_agent() -> str:
    """Return the active User-Agent string.

    Reads ``OpenLibraryDataProvider.USER_AGENT`` lazily so consumers can
    override it at runtime; falls back to ``DEFAULT_USER_AGENT`` before the
    class is defined or if the attribute is unset.
    """
    cls = globals().get("OpenLibraryDataProvider")
    ua = getattr(cls, "USER_AGENT", None) if cls is not None else None
    return ua or DEFAULT_USER_AGENT


_http_client: Optional[httpx.Client] = None
_http_client_lock = _threading.Lock()


def _get_http_client() -> httpx.Client:
    """Return the shared httpx.Client singleton (thread-safe, lazy init)."""
    global _http_client
    if _http_client is None:
        with _http_client_lock:
            if _http_client is None:
                _http_client = httpx.Client(
                    limits=httpx.Limits(
                        max_keepalive_connections=10,
                        max_connections=20,
                        keepalive_expiry=30.0,
                    ),
                    timeout=httpx.Timeout(
                        connect=5.0,
                        read=_REQUEST_TIMEOUT,
                        write=5.0,
                        pool=2.0,
                    ),
                )
                _atexit.register(_http_client.close)
    return _http_client


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
            r = _get_http_client().get(
                url,
                params=params,
                timeout=httpx.Timeout(connect=5.0, read=timeout, write=5.0, pool=2.0),
                headers={"User-Agent": _user_agent()},
            )
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



# Bounding box the covers server scales its ``-L`` rendition into. Mirrors
# ``image_sizes["L"]`` in openlibrary/coverstore/config.py.
_COVER_L_BOX = (500, 500)


def _fit_cover_size(
    width: Optional[int],
    height: Optional[int],
    box: tuple[int, int] = _COVER_L_BOX,
) -> Optional[tuple[int, int]]:
    """Pixel size the covers server serves for an original of *width* x
    *height* scaled into *box*, or ``None`` when the original size is unknown.
    """
    if not width or not height or width < 0 or height < 0:
        return None
    x, y = width, height
    if x > box[0]:
        y = max(y * box[0] // x, 1)
        x = box[0]
    if y > box[1]:
        x = max(x * box[1] // y, 1)
        y = box[1]
    return x, y


class BookSharedDoc(BaseModel):
    """Fields shared between OpenLibrary works and editions."""
    key: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    description: Optional[str] = None
    cover_i: Optional[int] = None
    # Pixel size of the *original* upload behind ``cover_i``. The served
    # ``-L`` rendition is smaller; ``images()`` derives its size via
    # ``_fit_cover_size``.
    cover_width: Optional[int] = None
    cover_height: Optional[int] = None
    ebook_access: Optional[str] = None
    language: Optional[list[str]] = None
    ia: Optional[list[str]] = None
    # Aggregate rating is a work-level signal in OL's Solr index; editions
    # carry no ratings. Populated only on the work record.
    ratings_average: Optional[float] = None
    ratings_count: Optional[int] = None
    # Subjects are work-level display names; editions carry none.
    subject: Optional[list[str]] = None


class OpenLibraryDataRecord(BookSharedDoc, DataProviderRecord):

    class EditionAvailability(BaseModel):
        status: Literal["borrow_available", "borrow_unavailable",  "open", "private", "error"]

    class EditionProvider(BaseModel):
        """Basically the acquisition info for an edition."""
        access: Optional[str] = None
        # Any string: Open Library also says ``daisy``, ``djvu``, ``mobi`` and
        # ``txt``, and a Literal of four rejected the whole edition for one.
        format: Optional[str] = None
        price: Optional[str] = None
        """Book price, eg '0.00 USD'"""
        url: Optional[str] = None
        provider_name: Optional[str] = None

    class EditionAcquisition(BaseModel):
        """One row of Open Library's ``opds_acquisitions``: an OPDS 2.0
        acquisition link as the catalogue states it, synthesized for the
        Internet Archive or harvested from a provider's own feed (then it
        may carry ``properties.price``)."""
        rel: str
        href: str
        type: Optional[str] = None
        provider_name: Optional[str] = None
        properties: Optional[dict] = None

        model_config = {"extra": "allow"}

    class EditionDoc(BookSharedDoc):
        """Open Library edition document."""
        availability: Optional["OpenLibraryDataRecord.EditionAvailability"] = None
        providers: Optional[list["OpenLibraryDataRecord.EditionProvider"]] = None
        publish_year: Optional[list[int]] = None
        opds_acquisitions: Optional[list["OpenLibraryDataRecord.EditionAcquisition"]] = None

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
    # The ``language`` the search that found this record was made with, so a
    # work's self link can echo it: the editions a work lists depend on it.
    # Not a Solr field, never serialised.
    request_language: Optional[str] = Field(None, exclude=True)

    @property
    def type(self) -> str:
        # Prefer the surfaced edition's own provider formats over the work-level
        # ``id_librivox`` flag: a work can have a LibriVox recording on one
        # edition while the edition we are returning is an ebook (epub/pdf).
        edition = self.editions.docs[0] if self.editions and self.editions.docs else None
        if edition and edition.providers:
            has_audio = any(p.format == "audio" for p in edition.providers)
            has_ebook = any(p.format in _DOWNLOADABLE_FORMATS for p in edition.providers)
            if has_ebook and not has_audio:
                return "http://schema.org/Book"
            if has_audio:
                return "http://schema.org/Audiobook"
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
        # Skip the fallback when the surfaced edition is clearly an ebook
        # (has epub/pdf providers): a work-level LibriVox recording belongs on
        # a different edition and would be misleading on an ebook entry.
        has_audio = any(
            lnk.type in ("audio/mpeg", "application/audiobook+json")
            or lnk.href.startswith("https://librivox.org")
            for lnk in links
        )
        edition_has_ebook = bool(
            edition
            and edition.providers
            and any(p.format in _DOWNLOADABLE_FORMATS for p in edition.providers)
        )
        if self.id_librivox and not has_audio and not edition_has_ebook:
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
        if not book.cover_i:
            return None
        # OPDS 2.0 §2.3: ``width``/``height`` describe the linked resource, so
        # report the size of the ``-L`` rendition, not the Solr original.
        size = _fit_cover_size(book.cover_width, book.cover_height)
        dimensions = {"width": size[0], "height": size[1]} if size else {}
        return [
            Link(
                href=f"https://covers.openlibrary.org/b/id/{book.cover_i}-L.jpg",
                type="image/jpeg",
                rel="cover",
                **dimensions,
            ),
        ]

    def _authors(self) -> Optional[List[Contributor]]:
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
                            rel="author",
                        ),
                    ],
                )
                for name, key in zip(self.author_name, self.author_key)
            ]
        if self.author_name:
            return [Contributor(name=name) for name in self.author_name]
        return None

    def _aggregate_rating(self) -> Optional[dict]:
        # Ratings are work-level in OL — read from ``self`` (the work), never
        # the surfaced edition. Omit entirely for unrated works so consumers
        # don't see a meaningless ``ratingValue: 0``. schema.org/AggregateRating
        # (OL uses a 1–5 scale).
        if self.ratings_count and self.ratings_average:
            return {
                "@type": "AggregateRating",
                "ratingValue": round(self.ratings_average, 2),
                "ratingCount": self.ratings_count,
                "bestRating": 5,
                "worstRating": 1,
            }
        return None

    def _subjects(self) -> Optional[List[Subject]]:
        # Subjects are work-level. Emit at most 10 as navigable OPDS subject
        # objects whose link browses the app's /search by subject name. Embedded
        # double-quotes are stripped so the Solr quoted clause can't break; the
        # name keeps its original text. Omit entirely for works with no subjects.
        # Colon-separated subjects (e.g. ``content_warning:cover``) are machine
        # tags, not human-readable, so they are filtered out before slicing.
        if not self.subject:
            return None
        opds_base = OpenLibraryDataProvider.OPDS_BASE_URL or f"{OpenLibraryDataProvider.BASE_URL}/opds"
        subjects = []
        human_readable = [name for name in self.subject if ":" not in name]
        for name in human_readable[:10]:
            query = f'subject:"{name.replace(chr(34), "")}"'
            href = f"{opds_base}/search?" + urlencode({"query": query, "title": name})
            subjects.append(Subject(name=name, links=[Link(type="application/opds+json", href=href)]))
        return subjects or None

    def metadata(self) -> Metadata:
        """This record as the metadata of its surfaced edition."""
        edition = self.editions.docs[0] if self.editions and self.editions.docs else None
        book = edition or self

        desc = book.description
        if not desc and self.description:
            langs = book.language or self.language or []
            if "eng" in langs:
                desc = self.description

        return Metadata(
            type=self.type,
            title=book.title or self.title or "Untitled",
            subtitle=book.subtitle,
            author=self._authors(),
            description=strip_markdown(desc) if desc else None,
            language=[lang for marc_lang in (book.language or []) if (lang := marc_language_to_iso_639_1(marc_lang))],
            published=edition_published(edition) if edition else None,
            # TODO: Use the edition-specific pagecount
            numberOfPages=self.number_of_pages_median,
            aggregateRating=self._aggregate_rating(),
            subject=self._subjects(),
        )

    # -- The work, and the editions it lists ---------------------------------

    def to_edition_publication(self) -> Publication:
        """The surfaced edition as a publication of its own: the document at
        ``/books/OL…M``, terminal — its own metadata, cover and acquisition
        links, and the Archive's document as its one alternate when the
        Archive holds it."""
        return Publication(metadata=self.metadata(), links=self.links(), images=self.images())

    def to_publication(self) -> Publication:
        """A record in a feed is a work (see ``to_work_publication``)."""
        return self.to_work_publication()

    def work_olid(self) -> str:
        return (self.key or "").split("/")[-1]

    def work_self_href(self, language: Optional[str] = None) -> str:
        """``…/opds/works/OL…W``, with the request's ``language`` echoed: the
        editions listed depend on it, so it names the document."""
        opds_base = OpenLibraryDataProvider.OPDS_BASE_URL or f"{OpenLibraryDataProvider.BASE_URL}/opds"
        href = f"{opds_base}/works/{self.work_olid()}"
        return f"{href}?language={language}" if language else href

    def work_metadata(self, editions: list["OpenLibraryDataRecord.EditionDoc"]) -> Metadata:
        """The work's own words, with what a card needs from its editions:
        the first edition's type, and the languages of the editions listed."""
        languages: list[str] = []
        for edition in editions:
            for marc in edition.language or []:
                iso = marc_language_to_iso_639_1(marc)
                if iso and iso not in languages:
                    languages.append(iso)
        if not languages:
            languages = [iso for marc in (self.language or []) if (iso := marc_language_to_iso_639_1(marc))]

        # The first edition's description if it has one, else the work's —
        # under the same guard as an edition: only when English is among the
        # languages on the page, so a French edition is not described in English.
        desc = editions[0].description if editions else None
        if not desc and self.description and (not languages or "en" in languages):
            desc = self.description

        first = editions[0] if editions else None
        return Metadata(
            type=edition_type(first) if first else self.type,
            title=self.title or "Untitled",
            subtitle=self.subtitle,
            author=self._authors(),
            description=strip_markdown(desc) if desc else None,
            language=languages or None,
            numberOfPages=self.number_of_pages_median,
            aggregateRating=self._aggregate_rating(),
            subject=self._subjects(),
        )

    def to_work_publication(
        self,
        editions: Optional[list["OpenLibraryDataRecord.EditionDoc"]] = None,
        language: Optional[str] = None,
    ) -> Publication:
        """This work as an OPDS publication whose ``rel=alternate`` links of
        the publication type are its editions, best offer first.

        *editions* defaults to the ones Solr surfaced on this record (a feed's
        one), ranked; the work route passes the composed list. *language*
        defaults to the one the search was made with.
        """
        if editions is None:
            editions = rank_editions(list(self.editions.docs)) if self.editions and self.editions.docs else []
        if language is None:
            language = self.request_language
        links: list[Link] = [
            Link(rel="self", href=self.work_self_href(language), type=PUBLICATION_TYPE),
            Link(rel="alternate", href=f"{OpenLibraryDataProvider.BASE_URL}{self.key}", type="text/html"),
            Link(rel="alternate", href=f"{OpenLibraryDataProvider.BASE_URL}{self.key}.json", type="application/json"),
        ]
        for edition in editions:
            link = edition_alternate_link(edition, self.author_name)
            if link is not None:
                links.append(link)
        return Publication(
            metadata=self.work_metadata(editions),
            links=links,
            images=self._work_images(editions),
        )

    def _work_images(self, editions: list["OpenLibraryDataRecord.EditionDoc"]) -> Optional[List[Link]]:
        """The cover of the first edition that has one, else the work's."""
        for book in [*editions, self]:
            if book.cover_i:
                size = _fit_cover_size(book.cover_width, book.cover_height)
                dimensions = {"width": size[0], "height": size[1]} if size else {}
                return [Link(href=f"https://covers.openlibrary.org/b/id/{book.cover_i}-L.jpg", type="image/jpeg", rel="cover", **dimensions)]
        return None


class OpenLibraryLanguageStub(TypedDict):
    key: str
    name: Optional[str]
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
    return Link(
        title="Internet Archive",
        href=_ia_webpub_href(edition.ia[0]),
        rel="alternate",
        type=PUBLICATION_TYPE,
        properties={"authenticate": dict(_IA_AUTHENTICATE)},
    )


def _ia_webpub_href(identifier: str) -> str:
    return f"https://archive.org/services/loans/loan/?action=webpub&identifier={identifier}&opds=1"


_IA_AUTHENTICATE = {
    "href": "https://archive.org/services/loans/loan/?action=authentication_document",
    "type": "application/opds-authentication+json",
}


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


# ---------------------------------------------------------------------------
# The editions of a work
# ---------------------------------------------------------------------------
#
# A work is an OPDS publication whose ``rel="alternate"`` links of type
# ``application/opds-publication+json`` are its editions, one per edition,
# best offer first. Each points straight at the document that serves the
# edition — the Internet Archive's webpub for a printing the Archive holds,
# the edition document here otherwise — and carries a hint of what it points
# at under ``properties.edition``, so a client draws the list without
# fetching any of them. The hint is a summary; the document is the truth.
#
# Open Library's search answers one edition per work per call, so a work's
# list is composed: one call per (language, access tier), each asking for
# the best edition under that pair. The tier clause in ``q`` is what keeps
# an edition with no usable offer out of the list; there is no post-filter.

_OPDS_ACQUISITION = "http://opds-spec.org/acquisition"
REL_OPEN_ACCESS = f"{_OPDS_ACQUISITION}/open-access"
REL_BORROW = f"{_OPDS_ACQUISITION}/borrow"
REL_BUY = f"{_OPDS_ACQUISITION}/buy"
PUBLICATION_TYPE = "application/opds-publication+json"
_AUDIOBOOK_TYPES = ("application/audiobook+json",)

# The box the covers server scales its ``-M`` rendition into
# (``image_sizes["M"]`` in openlibrary/coverstore/config.py).
_COVER_M_BOX = (180, 360)

# How a provider is named to a reader. Anything not listed is its
# ``provider_name`` with the underscores as spaces, capitalised.
_DISTRIBUTOR_NAMES = {
    "ia": "Internet Archive",
    "standard_ebooks": "Standard Ebooks",
    "gutenberg": "Project Gutenberg",
    "project_gutenberg": "Project Gutenberg",
    "librivox": "LibriVox",
    "bwb": "Better World Books",
    "better_world_books": "Better World Books",
}

# The access tiers an edition is asked for, best first. Print-disabled
# printings are asked for only under ``access=print_disabled``, as /books
# serves them.
_EDITION_TIERS = ("public", "borrowable")


class EditionOffer(typing.NamedTuple):
    """The one offer an edition is listed for: its relation, who makes it,
    its price if it has one, and the loan state if it is a loan."""
    rel: str
    provider_name: Optional[str]
    price: Optional[dict]
    availability: Optional[str]


def distributor_name(provider_name: Optional[str]) -> Optional[str]:
    if not provider_name:
        return None
    return _DISTRIBUTOR_NAMES.get(provider_name) or provider_name.replace("_", " ").title()


def _availability_state(edition: OpenLibraryDataRecord.EditionDoc) -> Optional[str]:
    status = edition.availability.status if edition.availability else None
    if status in ("open", "borrow_available"):
        return "available"
    if status in ("borrow_unavailable", "private", "error"):
        return "unavailable"
    return None


def _price_in(properties: Optional[dict]) -> Optional[dict]:
    price = (properties or {}).get("price")
    if isinstance(price, dict) and isinstance(price.get("value"), (int, float)) and price.get("currency"):
        return {"value": price["value"], "currency": price["currency"]}
    return None


def offer_rank(rel: str, availability: Optional[str], price: Optional[dict]) -> tuple[int, float]:
    """Where an offer sorts: free, then a loan that can be taken now, then a
    loan whose state is unknown, then one that is checked out, then for sale
    cheapest first, then anything else."""
    if rel == REL_OPEN_ACCESS:
        return (0, 0.0)
    if rel == REL_BORROW:
        return ({"available": 1, None: 2, "unavailable": 3}[availability], 0.0)
    if rel == REL_BUY:
        return (4, float(price["value"]) if price else float("inf"))
    return (5, 0.0)


def edition_offer(edition: OpenLibraryDataRecord.EditionDoc) -> Optional[EditionOffer]:
    """The best offer an edition has, or None when it has none to list.

    Open Library's own ``opds_acquisitions`` first; a printing the Archive
    holds that came without them is still an Archive offer; and an older
    record with only ``providers`` is read through the same converter the
    edition document uses.
    """
    state = _availability_state(edition)
    candidates: list[EditionOffer] = []
    for row in edition.opds_acquisitions or []:
        if row.rel in (REL_OPEN_ACCESS, REL_BORROW, REL_BUY):
            candidates.append(EditionOffer(
                row.rel, row.provider_name, _price_in(row.properties),
                state if row.rel == REL_BORROW else None,
            ))
    if not candidates and edition.ia and edition.ebook_access in ("public", "borrowable", "printdisabled"):
        rel = REL_OPEN_ACCESS if edition.ebook_access == "public" else REL_BORROW
        candidates.append(EditionOffer(rel, "ia", None, state if rel == REL_BORROW else None))
    if not candidates:
        for provider in edition.providers or []:
            if not provider.url:
                continue
            for link in ol_acquisition_to_opds_links(edition, provider):
                if link.rel in (REL_OPEN_ACCESS, REL_BORROW, REL_BUY):
                    candidates.append(EditionOffer(
                        cast(str, link.rel), provider.provider_name, _price_in(link.properties),
                        state if link.rel == REL_BORROW else None,
                    ))
    if not candidates:
        return None
    return min(candidates, key=lambda o: offer_rank(o.rel, o.availability, o.price))


def edition_type(edition: OpenLibraryDataRecord.EditionDoc) -> str:
    """schema.org Audiobook when any of the edition's offers is audio, else Book."""
    for row in edition.opds_acquisitions or []:
        if row.type and (row.type.startswith("audio/") or row.type in _AUDIOBOOK_TYPES):
            return "http://schema.org/Audiobook"
    if any(p.format == "audio" for p in edition.providers or []):
        return "http://schema.org/Audiobook"
    return "http://schema.org/Book"


def edition_published(edition: OpenLibraryDataRecord.EditionDoc) -> Optional[str]:
    """The earliest year the printing says, as an ISO date: ``2010-01-01``."""
    years = [y for y in (edition.publish_year or []) if isinstance(y, int) and y > 0]
    return f"{min(years):04d}-01-01" if years else None


def _edition_cover(edition: OpenLibraryDataRecord.EditionDoc) -> Optional[dict]:
    if not edition.cover_i:
        return None
    cover: dict = {"href": f"https://covers.openlibrary.org/b/id/{edition.cover_i}-M.jpg", "type": "image/jpeg"}
    size = _fit_cover_size(edition.cover_width, edition.cover_height, _COVER_M_BOX)
    if size:
        cover["width"], cover["height"] = size
    return cover


def edition_hint(
    edition: OpenLibraryDataRecord.EditionDoc,
    author_names: Optional[list[str]],
    offer: EditionOffer,
) -> dict:
    """What a client needs to draw the edition's row without fetching it."""
    hint: dict = {
        "key": (edition.key or "").split("/")[-1] or None,
        "@type": edition_type(edition),
        "author": list(author_names) if author_names else None,
        "published": edition_published(edition),
        "language": [iso for marc in (edition.language or []) if (iso := marc_language_to_iso_639_1(marc))] or None,
        "cover": _edition_cover(edition),
        "distributor": distributor_name(offer.provider_name),
        "offer": {
            "rel": offer.rel,
            **({"price": offer.price} if offer.price else {}),
            **({"availability": {"state": offer.availability}} if offer.availability else {}),
        },
    }
    return {k: v for k, v in hint.items() if v is not None}


def edition_target(edition: OpenLibraryDataRecord.EditionDoc, offer: EditionOffer) -> str:
    """Where the edition's alternate points: the Archive's document for a
    printing the Archive serves, else the edition document here."""
    if edition.ia and offer.provider_name == "ia":
        return _ia_webpub_href(edition.ia[0])
    opds_base = OpenLibraryDataProvider.OPDS_BASE_URL or f"{OpenLibraryDataProvider.BASE_URL}/opds"
    return f"{opds_base}{edition.key}"


def edition_alternate_link(
    edition: OpenLibraryDataRecord.EditionDoc,
    author_names: Optional[list[str]] = None,
) -> Optional[Link]:
    """The edition as one ``rel=alternate`` link on its work, or None when
    it has no offer to list (nothing to point at)."""
    offer = edition_offer(edition)
    if offer is None or not edition.key:
        return None
    href = edition_target(edition, offer)
    properties: dict = {}
    if href.startswith("https://archive.org/"):
        properties["authenticate"] = dict(_IA_AUTHENTICATE)
    properties["edition"] = edition_hint(edition, author_names, offer)
    return Link(rel="alternate", type=PUBLICATION_TYPE, href=href, title=edition.title, properties=properties)


def rank_editions(
    editions: list[OpenLibraryDataRecord.EditionDoc],
) -> list[OpenLibraryDataRecord.EditionDoc]:
    """One of each edition, best offer first (``offer_rank``), the given
    order kept within a rank; an edition with nothing to offer is left out
    because there is no link to make for it."""
    seen: set[str] = set()
    ranked: list[tuple[OpenLibraryDataRecord.EditionDoc, EditionOffer]] = []
    for edition in editions:
        if not edition.key or edition.key in seen:
            continue
        seen.add(edition.key)
        offer = edition_offer(edition)
        if offer is not None:
            ranked.append((edition, offer))
    ranked.sort(key=lambda pair: offer_rank(pair[1].rel, pair[1].availability, pair[1].price))
    return [edition for edition, _ in ranked]


def _edition_queries(
    work_olid: str,
    language: Optional[str],
    access: Optional[str],
) -> list[tuple[str, Optional[str]]]:
    """The ``(q, lang)`` of each one-edition call, tiers outer, the reader's
    languages inner, so the composed list is already in rank order within a
    tier."""
    tiers = ("printdisabled",) if access == "print_disabled" else _EDITION_TIERS
    pairs = [(iso, iso_639_1_to_marc(iso)) for iso in parse_languages(canonical_language(language))]
    pairs = [(iso, marc) for iso, marc in pairs if marc] or [(None, None)]
    queries: list[tuple[str, Optional[str]]] = []
    for tier in tiers:
        for iso, marc in pairs:
            q = f"key:/works/{work_olid} ebook_access:{tier}"
            if marc:
                q = f"{q} language:{marc}"
            queries.append((q, iso))
    return queries


def _edition_for_query(q: str, iso_lang: Optional[str]) -> Optional[OpenLibraryDataRecord.EditionDoc]:
    r = _get(
        f"{OpenLibraryDataProvider.BASE_URL}/search.json",
        params={
            "q": q,
            "editions": "true",
            "limit": 1,
            "fields": "key,editions," + ",".join(_EDITION_RESOLVE_FIELDS),
            **({"lang": iso_lang} if iso_lang else {}),
        },
    )
    docs = r.json().get("docs", [])
    edition_docs = docs[0].get("editions", {}).get("docs", []) if docs else []
    if not edition_docs:
        return None
    return OpenLibraryDataRecord.EditionDoc.model_validate(edition_docs[0])


def editions_of_work(
    work_olid: str,
    language: Optional[str] = None,
    access: Optional[str] = None,
) -> list[OpenLibraryDataRecord.EditionDoc]:
    """The editions a work lists for a reader: the best edition under each
    (language, access tier), composed from one call each and ranked.

    A call that fails raises; a work page with a silently shorter list would
    read as the catalogue's answer.
    """
    from concurrent.futures import ThreadPoolExecutor

    queries = _edition_queries(work_olid, language, access)
    with ThreadPoolExecutor(max_workers=min(len(queries), 6)) as pool:
        found = list(pool.map(lambda pair: _edition_for_query(*pair), queries))
    return rank_editions([edition for edition in found if edition is not None])


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


class AuthorInfo(typing.NamedTuple):
    """What the author page needs from ``/authors/<olid>.json``.

    ``photo_width``/``photo_height`` are the pixel size of the ``-L``
    rendition at ``https://covers.openlibrary.org/a/id/<photo_id>-L.jpg``,
    or ``None`` when unknown.
    """
    name: Optional[str]
    bio: Optional[str]
    photo_id: Optional[int]
    photo_width: Optional[int]
    photo_height: Optional[int]


_NO_AUTHOR_INFO = AuthorInfo(None, None, None, None, None)


def _first_photo_id(photos: object) -> Optional[int]:
    """First usable id in an author record's ``photos``; ``-1`` means none."""
    if not isinstance(photos, list):
        return None
    return next((p for p in photos if isinstance(p, int) and p > 0), None)


def _fetch_photo_size(photo_id: int) -> Optional[tuple[int, int]]:
    """Pixel size of the ``-L`` rendition of author photo *photo_id*.

    The covers server reports the original's size; the ``-L`` file is that
    scaled into the same box as book covers, so ``_fit_cover_size`` gives
    the exact size of the linked file. ``None`` when the size is unknown.
    """
    data = _get(f"https://covers.openlibrary.org/a/id/{photo_id}.json").json()
    return _fit_cover_size(data.get("width"), data.get("height"))


def fetch_author_info(olid: str) -> AuthorInfo:
    """Fetch name, bio and photo of an author from the OpenLibrary author API.

    The bio has been stripped of Markdown/HTML. A failure to size the photo
    only drops the size; any other failure returns an all-``None``
    ``AuthorInfo``. Never raises.
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
        photo_id = _first_photo_id(data.get("photos"))
    except Exception:
        return _NO_AUTHOR_INFO
    size: Optional[tuple[int, int]] = None
    if photo_id:
        try:
            size = _fetch_photo_size(photo_id)
        except Exception:
            size = None
    width, height = size if size else (None, None)
    return AuthorInfo(name, bio, photo_id, width, height)


def fetch_author_bio(olid: str) -> tuple[Optional[str], Optional[str]]:
    """Fetch author name and bio from the OpenLibrary author API.

    Returns ``(name, bio)`` where bio has been stripped of Markdown/HTML.
    Returns ``(None, None)`` on any failure — never raises.
    """
    info = fetch_author_info(olid)
    return info.name, info.bio


def author_photo_links(info: AuthorInfo) -> Optional[List[Link]]:
    """The author page's ``images`` collection: the ``-L`` photo, carrying
    ``REL_PORTRAIT`` and, when known, the size of that exact file. ``None``
    when the author has no photo.
    """
    if not info.photo_id:
        return None
    dimensions = (
        {"width": info.photo_width, "height": info.photo_height}
        if info.photo_width and info.photo_height else {}
    )
    return [
        Link(
            href=f"https://covers.openlibrary.org/a/id/{info.photo_id}-L.jpg",
            type="image/jpeg",
            rel=REL_PORTRAIT,
            **dimensions,
        ),
    ]


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
_languages_names_cache: dict[str, str] = {}  # iso_639_1 -> display name
_languages_map_fetched_at: float = 0.0
_LANGUAGES_MAP_TTL: float = 7 * 24 * 60 * 60  # 7 days — MARC↔ISO mappings essentially never change.


def fetch_languages_map() -> dict[str, str]:
    """Return a map of MARC language codes to ISO 639-1 codes.

    Also populates ``_languages_names_cache`` (iso_639_1 → display name) as a
    side-effect so ``fetch_language_options`` can build the full language list
    without a second API call.

    Results are cached for ``_LANGUAGES_MAP_TTL`` seconds.  Unlike
    ``@functools.cache``, a failure to fetch does **not** poison the cache —
    the next request will retry the OL API rather than returning stale ``{}``.
    """
    global _languages_map_cache, _languages_names_cache, _languages_map_fetched_at
    now = _time.monotonic()
    if _languages_map_cache is not None and (now - _languages_map_fetched_at) < _LANGUAGES_MAP_TTL:
        return _languages_map_cache
    try:
        r = _get("https://openlibrary.org/query.json?type=/type/language&key&name&identifiers&limit=1000")
    except Exception:
        if _languages_map_cache is not None:
            return _languages_map_cache
        raise
    data = cast(List[OpenLibraryLanguageStub], r.json())
    languages: dict[str, str] = {}
    names: dict[str, str] = {}
    for lang in data:
        marc_code = lang["key"].split("/")[-1]
        identifiers = lang.get("identifiers")
        if not identifiers:
            continue
        iso_codes = identifiers.get("iso_639_1", [])
        if iso_codes:
            iso = iso_codes[0]
            languages[marc_code] = iso
            name = lang.get("name")
            if name:
                names[iso] = name
    _languages_map_cache = languages
    _languages_names_cache = names
    _languages_map_fetched_at = now
    return languages


_FALLBACK_LANGUAGE_OPTIONS: list[tuple[Optional[str], str]] = [
    (None, "All"),
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("hi", "Hindi"),
]


def fetch_language_options() -> list[tuple[Optional[str], str]]:
    """Return all available language options sorted alphabetically by display name.

    Each entry is ``(iso_639_1_code, display_name)``.  The first entry is
    always ``(None, "All")`` meaning no language filter.  Falls back to a
    small hardcoded list if the OL API is unavailable and no cached data exists.
    """
    try:
        fetch_languages_map()
    except Exception:
        pass
    if not _languages_names_cache:
        return list(_FALLBACK_LANGUAGE_OPTIONS)
    options: list[tuple[Optional[str], str]] = [(None, "All")]
    options.extend(sorted(_languages_names_cache.items(), key=lambda x: x[1]))
    return options


_iso_to_marc_cache: dict[str, str] = {}


def iso_639_1_to_marc(iso_code: str) -> Optional[str]:
    """Convert an ISO 639-1 code (e.g. 'en') to a MARC language code (e.g. 'eng').

    Uses a reverse-lookup cache built from ``fetch_languages_map()`` to avoid
    a linear scan on every call.  Returns ``None`` if no mapping is found.
    """
    lang_map = fetch_languages_map()  # MARC → ISO
    if iso_code in lang_map:
        return iso_code
    if iso_code in _iso_to_marc_cache:
        return _iso_to_marc_cache[iso_code]
    # Rebuild reverse cache from the latest map.
    _iso_to_marc_cache.clear()
    for marc, iso in lang_map.items():
        _iso_to_marc_cache[iso] = marc
    return _iso_to_marc_cache.get(iso_code)


def parse_languages(raw: Optional[str]) -> list[str]:
    """Split a ``language`` parameter into its codes.

    ``language`` is a comma-separated list of ISO 639-1 codes, the reader's
    primary language first: ``"en,fr"``.  Codes are lowercased and stripped,
    empties dropped, duplicates removed with the first occurrence kept, so
    ``" EN, fr,,en"`` reads as ``["en", "fr"]``.
    """
    if not raw:
        return []
    seen: list[str] = []
    for part in raw.split(","):
        code = part.strip().lower()
        if code and code not in seen:
            seen.append(code)
    return seen


def canonical_language(raw: Optional[str]) -> Optional[str]:
    """The one spelling of a ``language`` parameter: ``"en,fr"`` or ``None``.

    Idempotent.  Every entry point canonicalises first, so the hrefs a feed
    emits and the keys a cache is filed under are the same for ``"EN, fr"``
    and ``"en,fr"``.
    """
    codes = parse_languages(raw)
    return ",".join(codes) if codes else None


# The values a ``mode`` list may name, in their canonical order. ``everything``
# is not one of them: it is what an empty list means.
_MODE_VALUES: tuple[str, ...] = ("open_access", "ebooks", "buyable", "print_disabled")

# The values a ``media_type`` list may name, in their canonical order.
_MEDIA_TYPE_VALUES: tuple[str, ...] = ("ebook", "audiobook")


def _parse_listed(raw: Optional[str], allowed: tuple[str, ...]) -> list[str]:
    """Split a comma-separated parameter into the allowed values it names.

    Values are lowercased and stripped; unknown ones and duplicates are
    dropped.  The result is in ``allowed``'s order rather than the caller's,
    since the values of these lists (unlike languages) carry no priority —
    so ``"buyable,ebooks"`` and ``"ebooks, buyable"`` read the same.
    """
    if not raw:
        return []
    named = {part.strip().lower() for part in raw.split(",")}
    return [value for value in allowed if value in named]


def parse_modes(raw: Optional[str]) -> list[str]:
    """The availability modes a ``mode`` parameter names: ``"ebooks,buyable"``
    is ``["ebooks", "buyable"]``; ``"everything"``, empty and unknown values
    name none, which is everything."""
    return _parse_listed(raw, _MODE_VALUES)


def canonical_mode(raw: Optional[str]) -> str:
    """The one spelling of a ``mode`` parameter: ``"ebooks,buyable"``, or
    ``"everything"`` for none.  Idempotent, like ``canonical_language``."""
    modes = parse_modes(raw)
    return ",".join(modes) if modes else "everything"


def parse_media_types(raw: Optional[str]) -> list[str]:
    """The media types a ``media_type`` parameter names, in canonical order."""
    return _parse_listed(raw, _MEDIA_TYPE_VALUES)


def canonical_media_type(raw: Optional[str]) -> Optional[str]:
    """The one spelling of a ``media_type`` parameter, or ``None`` for all."""
    types = parse_media_types(raw)
    return ",".join(types) if types else None


def _language_solr_clause(raw: Optional[str]) -> Optional[str]:
    """The Solr clause that keeps works in any of the given languages.

    Each ISO code is mapped to its MARC code; one that maps to nothing is
    ignored.  None mapped is no clause; one is ``language:eng``; several are
    ``language:(eng OR fre)``.
    """
    marcs: list[str] = []
    for code in parse_languages(raw):
        marc = iso_639_1_to_marc(code)
        if marc and marc not in marcs:
            marcs.append(marc)
    if not marcs:
        return None
    if len(marcs) == 1:
        return f"language:{marcs[0]}"
    return f"language:({' OR '.join(marcs)})"


def _is_english_or_all(language: Optional[str]) -> bool:
    """Whether the front page keeps its English rules for this selection."""
    languages = parse_languages(language)
    return not languages or "en" in languages


def _has_acquisition_options(record: OpenLibraryDataRecord) -> bool:
    """Check if a record's edition would produce at least one usable OPDS link.

    Mirrors the filtering logic in ``ol_acquisition_to_opds_links`` so that
    books with no actionable link are hidden from results.

    A work-level ``id_librivox`` is deliberately *not* enough on its own: the
    LibriVox fallback in ``links()`` is a plain ``rel=alternate`` catalog page,
    which no OPDS client can acquire or open.  Such an entry must come from the
    edition's own providers (IA webpub, epub/pdf download, or a purchase link)
    to be servable.
    """
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


def _resolve_latin_author_names(records: list[OpenLibraryDataRecord]) -> None:
    """Resolve non-Latin author names (e.g. Cyrillic) to their Latin personal_name.

    OL's search index stores author_name from the author record's `name` field,
    which for authors like Chekhov is in their native script. We fetch the author
    record once (cached) and use personal_name when it is Latin-script.
    """
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


# The edition-level ebook_access values each availability mode's Solr clause
# admits. 'buyable' admits what 'ebooks' does; its real test is client-side.
_MODE_ACCESS_VALUES: dict[str, tuple[str, ...]] = {
    "ebooks":         ("borrowable", "printdisabled"),
    "print_disabled": ("printdisabled",),
    "open_access":    ("public",),
    "buyable":        ("borrowable", "printdisabled"),
}


def _mode_ebook_access_clause(mode: str) -> str:
    """Return the Solr ``ebook_access`` clause for an availability *mode*.

    *mode* may be a list (``"ebooks,open_access"``): the clause admits what
    any of the listed modes admits, an OR of their values.

    Single source of truth shared by ``search`` and ``_count_for_mode`` so the
    displayed results and the per-mode facet counts can never drift apart.

    Values are enumerated rather than using a Solr range: lexicographic order is
    ``borrowable < no_ebook < printdisabled < public``, so a range like
    ``[borrowable TO *]`` would sweep in ``no_ebook`` (print-only, unservable
    over OPDS) and ``[printdisabled TO *]`` would sweep in ``public``.

    ``everything`` floors to the three *servable* values (anything with an
    actual ebook). Without this, an unfiltered query returns ``no_ebook``
    print-only works that ``_has_acquisition_options`` then silently drops,
    leaving an empty feed with an inflated ``numFound`` total.
    """
    modes = parse_modes(mode)
    if not modes:
        # everything
        return "ebook_access:(borrowable OR printdisabled OR public)"
    values: list[str] = []
    for m in modes:
        for value in _MODE_ACCESS_VALUES[m]:
            if value not in values:
                values.append(value)
    # The servable values in the order the single-mode clauses always spelled
    # them, so a list's clause and a single mode's never differ in spelling.
    values.sort(key=["borrowable", "printdisabled", "public"].index)
    if len(values) == 1:
        return f"ebook_access:{values[0]}"
    return f"ebook_access:({' OR '.join(values)})"


def _passes_mode(record: "OpenLibraryDataRecord", mode: str) -> bool:
    """Whether a record belongs under one availability mode, after edition
    resolution — the post-filter ``search`` applies, per listed mode.

    ``open_access`` keeps the work-level fallback: a public-domain work whose
    language-preferred edition is borrowable is still genuinely open.  The
    others use the displayed edition only — the work-level fallback leaked
    open-access editions into "Borrow".  ``buyable`` is a test of providers;
    Solr has no field for it.
    """
    if mode == "buyable":
        return _has_buyable_provider(record)
    allowed = _EBOOK_MODE_ALLOWED[mode]
    if mode == "open_access":
        return _get_edition_ebook_access(record) in allowed or record.ebook_access in allowed
    return _get_edition_ebook_access(record) in allowed

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
                # OpenLibrary search results may surface edition.language as either
                # MARC ("eng") or ISO ("en") depending on the endpoint/path.
                if ed.language and (marc_language in ed.language or (iso_lang and iso_lang in ed.language)):
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
    "key", "title", "subtitle", "description", "cover_i", "cover_width", "cover_height",
    "ebook_access", "language", "ia", "availability", "providers", "publish_year",
    "editions.opds_acquisitions",
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
    # ``language`` is ISO 639-1 (e.g. "en"), but edition ``language`` fields
    # store MARC codes (e.g. "eng"). Convert once so all comparisons match.
    marc_lang: Optional[str] = iso_639_1_to_marc(language)
    for record in records:
        if not (record.editions and record.editions.docs):
            continue
        if len(record.editions.docs) > 1:
            matched = [d for d in record.editions.docs if d.language and marc_lang and marc_lang in d.language]
            others = [d for d in record.editions.docs if not (d.language and marc_lang and marc_lang in d.language)]
            if matched:
                record.editions.docs = matched + others
        elif resolve_mismatched:
            ed = record.editions.docs[0]
            if not ed.language or not marc_lang or marc_lang not in ed.language:
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

# Link relations the feed emits beyond the registered ones (``self``, ``next``,
# ``search``, …). ``REL_SORT_POPULAR`` is OPDS's own; the facet relations are
# extension relations (RFC 8288), so each is a URL under a host we control that
# resolves to the page documenting it, served by opds.openlibrary.org's ``/rel/``
# route. They are fixed strings, not built from the deployment's base URL: a
# relation names a kind of link, the same on every host.
#
# Every option link of a facet group carries that group's relation, so a client
# can find the Availability picker by relation whatever its title says, and the
# applied option carries ``["self", <group relation>]``.
REL_FACET_AVAILABILITY: str = "https://openlibrary.org/opds/rel/facet/availability"
REL_FACET_LANGUAGE: str = "https://openlibrary.org/opds/rel/facet/language"
REL_FACET_MEDIA_TYPE: str = "https://openlibrary.org/opds/rel/facet/media-type"
REL_FACET_ACCESS: str = "https://openlibrary.org/opds/rel/facet/access"
# The trending carousel's self link also carries this registered relation,
# as does the Popular Books carousel on an author page.
REL_SORT_POPULAR: str = "http://opds-spec.org/sort/popular"
# A feed-level ``images`` entry that pictures the person the feed is about:
# the author's photo on ``/authors/<olid>``.
REL_PORTRAIT: str = "https://openlibrary.org/opds/rel/portrait"


def _set_facet_rel(link: dict, rel: str, active: bool) -> None:
    """Give a facet option link its group's relation, plus ``self`` when it is
    the applied option (OPDS 2.0 §2.4), as a list of the two."""
    link["rel"] = ["self", rel] if active else rel


# Single canonical label per mode — used by both build_facets and build_home_facets.
_AVAILABILITY_MODES: list[tuple[str, str]] = [
    ("everything",     "Everything"),
    ("ebooks",         "Borrow"),
    ("open_access",    "Open Access"),
    ("buyable",        "Buy"),
]

# All homepage groups are always attempted regardless of language corpus size;
# the empty-publications filter at the end of ``build_home_feed`` is what drops
# carousels that came back zero. Pruning by corpus size was too aggressive and
# hid groups that would have filled fine for mid-tier languages.

# Media type options for the Media Type facet group (OPDS 2.0 §2.4).
# ``None`` means "no media type filter" (All).
_MEDIA_TYPE_OPTIONS: list[tuple[Optional[str], str]] = [
    (None, "All"),
    ("ebook", "Books"),
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

    Each option's link narrows to that one mode; with a list applied, every
    listed mode's link is marked ``rel: "self"``, and "Everything" only when
    none is. Every link carries ``REL_FACET_AVAILABILITY`` as well.

    Args:
        mode: Currently active mode value (e.g. ``"ebooks"``), or a list of
            them (``"ebooks,buyable"``).
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
    selected = parse_modes(mode)
    links = []
    for val, _ in _AVAILABILITY_MODES:
        if val in exclude:
            continue
        link: dict = {
            "title": resolved[val],
            "href": href_fn(val),
            "type": "application/opds+json",
        }
        active = (val == "everything" and not selected) or val in selected
        _set_facet_rel(link, REL_FACET_AVAILABILITY, active)
        if active:
            link.setdefault("properties", {})["active"] = True
        count = counts.get(val)
        if count is not None:
            link.setdefault("properties", {})["numberOfItems"] = count
        links.append(link)
    return links


def _build_language_links(
    language: Optional[str],
    href_fn: typing.Callable[[Optional[str]], str],
    counts: Optional[dict[str, int]] = None,
) -> list[dict]:
    """Build the list of language facet link dicts per OPDS 2.0 §2.4.

    Uses ``fetch_language_options()`` to return all languages available in OL,
    sorted alphabetically.  Falls back to a small hardcoded list if OL is down.

    The currently active language is indicated by ``rel: "self"`` on its link,
    as required by the OPDS 2.0 specification, and every link carries
    ``REL_FACET_LANGUAGE``.  ``language=None`` means
    "All Languages" (no filter); that entry is always first in the list.

    The links stay single-select, as OPDS facets are: each narrows to one
    language and "All" clears.  A selection of several languages (``"en,fr"``)
    marks no entry active — it is not any one of them — but keeps every
    selected language listed whatever its count.

    Args:
        language: Active ISO 639-1 code (e.g. ``"en"``), a comma-separated
            list of them, or ``None`` for the "All Languages" (unfiltered)
            selection.
        href_fn: Converts a language code (or ``None``) to a full URL.
        counts: Optional ``{iso_639_1: numFound}`` map. When supplied, only
            languages with ``count > 0`` are emitted (the active language is
            kept regardless so the UI can still show it as selected) and the
            count is exposed via ``properties.numberOfItems``. When ``None``
            the full language list is emitted unfiltered — used as the
            fallback path when the count request fails.
    """
    selected = parse_languages(language)
    links = []
    for lang_code, label in fetch_language_options():
        if counts is not None and lang_code is not None and lang_code not in selected:
            if counts.get(lang_code, 0) <= 0:
                continue
        link: dict = {
            "title": label,
            "href": href_fn(lang_code),
            "type": "application/opds+json",
        }
        if counts is not None and lang_code is not None:
            n = counts.get(lang_code)
            if n is not None and n > 0:
                link.setdefault("properties", {})["numberOfItems"] = n
        is_active = (lang_code is None and not selected) or (lang_code is not None and selected == [lang_code])
        _set_facet_rel(link, REL_FACET_LANGUAGE, is_active)
        if is_active:
            link.setdefault("properties", {})["active"] = True
        links.append(link)
    return links

def _apply_media_type_filter(query: str, media_type: Optional[str]) -> str:
    """Return *query* with a Solr clause for the requested media type.

    - ``media_type="audiobook"`` appends ``id_librivox:*`` to restrict to
      works that have a LibriVox audio recording on the Internet Archive.
    - ``media_type="ebook"`` appends ``ebook_access:[printdisabled TO *]``
      when that filter is not already present.
    - ``media_type=None`` returns the query unchanged, and so does the list
      of both: every servable work is one or the other.
    """
    types = parse_media_types(media_type)
    if types == ["audiobook"]:
        return f"{query} id_librivox:*".strip()
    if types == ["ebook"] and "ebook_access:" not in query:
        return f"{query} ebook_access:[printdisabled TO *]".strip()
    return query


def _build_media_type_links(
    media_type: Optional[str],
    href_fn: typing.Callable[[Optional[str]], str],
) -> list[dict]:
    """Build the list of media type facet link dicts per OPDS 2.0 §2.4.

    As with availability: each link narrows to one type, and with a list
    applied every listed type is marked ``rel: "self"``. Every link carries
    ``REL_FACET_MEDIA_TYPE``.
    """
    selected = parse_media_types(media_type)
    links = []
    for mt_code, label in _MEDIA_TYPE_OPTIONS:
        link: dict = {
            "title": label,
            "href": href_fn(mt_code),
            "type": "application/opds+json",
        }
        active = (mt_code is None and not selected) or mt_code in selected
        _set_facet_rel(link, REL_FACET_MEDIA_TYPE, active)
        if active:
            link.setdefault("properties", {})["active"] = True
        links.append(link)
    return links


# Access options for the Access facet group (OPDS 2.0 §2.4).
# "general" (default) hides print-disabled content; "print_disabled" shows only that content.
_ACCESS_OPTIONS: list[tuple[str, str]] = [
    ("general",        "General"),
    ("print_disabled", "Print Disabled"),
]


def _build_access_links(
    access: Optional[str],
    href_fn: typing.Callable[[str], str],
) -> list[dict]:
    """Build the list of access facet link dicts per OPDS 2.0 §2.4.

    Two options: 'General' (default, excludes print-disabled) and 'Print Disabled'
    (shows only print-disabled content). Print-disabled is hidden by default.
    Every link carries ``REL_FACET_ACCESS``.
    """
    active = access or "general"
    links = []
    for ac_code, label in _ACCESS_OPTIONS:
        link: dict = {
            "title": label,
            "href": href_fn(ac_code),
            "type": "application/opds+json",
        }
        _set_facet_rel(link, REL_FACET_ACCESS, ac_code == active)
        if ac_code == active:
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
    require_trending: bool = True,
) -> tuple[str, str, str]:
    """Build a subject-genre carousel group tuple.

    When *require_trending* is False, the ``trending_score_hourly_sum:[1 TO *]``
    filter is omitted — useful for non-English languages whose books rarely
    have non-zero trending scores in OL's English-biased ranking signals.
    """
    parts = [subject_filter, ea, '-subject:"content_warning:cover"']
    if require_trending:
        parts.insert(2, 'trending_score_hourly_sum:[1 TO *]')
    if extra:
        parts.append(extra)
    return (title, " ".join(parts), sort)


def _kids_group(ea: str, require_trending: bool = True) -> tuple[str, str, str]:
    """Build the Kids carousel group tuple for a given ebook_access filter."""
    trending = 'trending_score_hourly_sum:[1 TO *] ' if require_trending else ''
    return ("Kids", f'{ea} {trending}{_KIDS_SUBJECT_FILTER} -subject:"content_warning:cover"', "random.hourly")


_GROUP_DESCRIPTIONS: dict[str, str] = {
    "Standard Ebooks": (
        "Standard Ebooks is a volunteer-run project that produces free, carefully "
        "typeset public-domain ebooks formatted to a consistent standard for modern "
        "e-readers."
    ),
    "Classic Books": (
        "Beloved works from before 1950 that have been digitized and made "
        "available to the public as ebooks."
    ),
    "Kids": (
        "Stories, picture books, and non-fiction for young readers, available on "
        "Open Library."
    ),
}


# Relations a carousel's self link carries besides ``self``, keyed by the
# group's title like ``_GROUP_DESCRIPTIONS``. Only the trending carousel has
# one for now: a client finds it by relation rather than by title.
_GROUP_RELS: dict[str, str] = {
    "Trending Books": REL_SORT_POPULAR,
}


# What every search asks Solr for, work and edition alike.
_SEARCH_FIELDS = [
    "key", "title", "editions", "description", "providers", "author_name", "ia",
    "cover_i", "cover_width", "cover_height", "availability", "ebook_access",
    "author_key", "subtitle", "language", "number_of_pages_median", "id_librivox",
    "ratings_average", "ratings_count", "subject", "publish_year",
    # Dotted on purpose: a price belongs to a printing, and OL serves the
    # field for editions only.
    "editions.opds_acquisitions",
]


class OpenLibraryDataProvider(DataProvider):
    """Data provider for Open Library records."""
    BASE_URL: str = "https://openlibrary.org"
    OPDS_BASE_URL: Optional[str] = None
    TITLE: str = "OpenLibrary.org OPDS Service"
    SEARCH_URL: str = "/opds/search{?query}"
    USER_AGENT: str = DEFAULT_USER_AGENT

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
    def _count_for_mode(query: str, mode: str, language: Optional[str] = None) -> Optional[int]:
        """Run a lightweight ``limit=0`` search to get the total count for a mode.

        Returns ``None`` for modes that require client-side filtering (like
        ``buyable``) since Solr cannot provide an accurate count.

        When *language* is set the count is scoped with ``language:<marc>`` to
        mirror ``search`` — otherwise non-active mode counts would be global
        (all languages) while the active mode's count is language-filtered,
        letting a subset mode report a larger count than the superset.
        """
        if 'buyable' in parse_modes(mode):
            # Buyable is filtered client-side (_has_buyable_provider); Solr
            # has no field for it so we cannot produce an accurate count.
            return None

        # Mode wins: strip any baked-in ebook_access clause (group queries
        # like Standard Ebooks add ebook_access:public) so the selected mode
        # is what Solr filters on. Counts must match the actual filtered
        # result set; without this, "Available to Borrow" on Standard Ebooks
        # would report the open-access count.
        internal_query = _EBOOK_ACCESS_CLAUSE_RE.sub('', query).strip()
        internal_query = f"{internal_query} {_mode_ebook_access_clause(mode)}".strip()

        # Scope the count to the active languages, matching search().
        if language and 'language:' not in internal_query:
            clause = _language_solr_clause(language)
            if clause:
                internal_query = f"{internal_query} {clause}"

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
        language: Optional[str] = None,
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
                futures = {pool.submit(OpenLibraryDataProvider._count_for_mode, base_query, m, language): m for m in to_fetch}
                for future in as_completed(futures):
                    counts[futures[future]] = future.result()

        return counts

    @staticmethod
    def fetch_language_counts(
        query: str = "",
        mode: str = "everything",
        media_type: Optional[str] = None,
        access: Optional[str] = None,
    ) -> Optional[dict[str, int]]:
        """Return ``{iso_639_1: ebook_edition_count}`` for languages with ebooks.

        Hits ``https://openlibrary.org/languages.json?limit=500`` which is the
        only OL endpoint that returns per-language counts. The OL search API
        strips facet parameters, so a per-query Solr facet is not possible.

        Counts are global (not narrowed by the current query/mode/media_type/
        access context) — this is intentional: the goal is to hide languages
        that have **no ebooks in OL at all**, not to compute exact per-search
        counts. The args are accepted for forward-compatibility but ignored.

        Returns ``None`` on any failure so callers can fall back to the
        unfiltered language list rather than 500ing the page.
        """
        try:
            # ``languages.json`` is hard-capped at ~480 records server-side and
            # silently ignores ``offset``, so a single ``limit=1000`` call
            # returns every language that has at least one ebook. No
            # pagination loop needed.
            r = _get(
                f"{OpenLibraryDataProvider.BASE_URL}/languages.json",
                params={"limit": 1000},
            )
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, list):
            return None
        try:
            marc_to_iso = fetch_languages_map()
        except Exception:
            return None
        iso_counts: dict[str, int] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            marc = entry.get("marc_code")
            ebook_count = entry.get("ebook_edition_count") or 0
            if not isinstance(marc, str) or not isinstance(ebook_count, int):
                continue
            if ebook_count <= 0:
                continue
            iso = marc_to_iso.get(marc)
            if iso:
                iso_counts[iso] = iso_counts.get(iso, 0) + ebook_count
        return iso_counts

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
        access: Optional[str] = None,
        language_counts: Optional[dict[str, int]] = None,
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
            language: Comma-separated ISO 639-1 codes for the active selection
                (e.g. ``"en"`` or ``"en,fr"``), or ``None`` for "All
                Languages" (no filter). This value is preserved in every facet
                href so that switching availability mode keeps the current
                language selection, and vice-versa.
            title: Display title for the search results page (e.g. ``"Art"``).
                Preserved in every facet href so switching facets keeps the
                page title instead of falling back to "Search Results".
            total: Reserved for Sort links when re-enabled.
            availability_counts: ``{mode_value: item_count}`` per OPDS 2.0 §2.4.
        """
        language = canonical_language(language)
        mode = canonical_mode(mode)
        media_type = canonical_media_type(media_type)

        def search_href(
            sort_val: Optional[str] = sort,
            mode_val: str = mode,
            lang_val: Optional[str] = language,
            mt_val: Optional[str] = media_type,
            ac_val: Optional[str] = access,
        ) -> str:
            # Strip ebook_access filters from the query for "everything" mode
            # so the facet link truly returns all results regardless of how
            # the user arrived at the search page.
            # Handles both simple values (ebook_access:public) and range
            # queries (ebook_access:[borrowable TO *]).
            q = _EBOOK_ACCESS_CLAUSE_RE.sub('', query).strip() if mode_val == "everything" else query
            params: dict[str, str] = {"query": q}
            if sort_val:
                params["sort"] = sort_val
            if mode_val and mode_val != "everything":
                params["mode"] = mode_val
            if lang_val:
                params["language"] = lang_val
            if mt_val:
                params["media_type"] = mt_val
            if ac_val and ac_val != "general":
                params["access"] = ac_val
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
                    counts=language_counts,
                ),
            },
            {
                "metadata": {"title": "Media Type"},
                "links": _build_media_type_links(
                    media_type=media_type,
                    href_fn=lambda mt: search_href(mt_val=mt),
                ),
            },
            {
                "metadata": {"title": "Access"},
                "links": _build_access_links(
                    access=access,
                    href_fn=lambda ac: search_href(ac_val=ac),
                ),
            },
        ]

    @staticmethod
    def build_home_facets(
        base_url: str,
        mode: str = "everything",
        language: Optional[str] = None,
        media_type: Optional[str] = None,
        access: Optional[str] = None,
        language_counts: Optional[dict[str, int]] = None,
    ) -> list[dict]:
        """Build Availability, Language, and Media Type facet groups for the OPDS homepage.

        Uses the same canonical labels as ``build_facets``.
        Links point to ``<base_url>/?mode=<value>`` / ``<base_url>/?language=<code>``.
        Each active facet link is marked with ``rel: "self"`` per OPDS 2.0 §2.4.

        Args:
            base_url: Base URL of the OPDS service (no trailing slash).
            mode: Currently active availability mode.
            language: Comma-separated ISO 639-1 codes (e.g. ``"en"`` or
                ``"en,fr"``), or ``None`` for "All Languages" (no filter).
            media_type: Active media type (e.g. ``"ebook"`` or ``"audiobook"``),
                or ``None`` for all media types.
        """
        language = canonical_language(language)
        mode = canonical_mode(mode)
        media_type = canonical_media_type(media_type)

        def home_href(val: str) -> str:
            params: dict[str, str] = {}
            if val != "everything":
                params["mode"] = val
            if language:
                params["language"] = language
            if media_type:
                params["media_type"] = media_type
            if access and access != "general":
                params["access"] = access
            return f"{base_url}/?{urlencode(params)}" if params else f"{base_url}/"

        def lang_href(lang: Optional[str]) -> str:
            params: dict[str, str] = {}
            if mode != "everything":
                params["mode"] = mode
            if lang:
                params["language"] = lang
            if media_type:
                params["media_type"] = media_type
            if access and access != "general":
                params["access"] = access
            return f"{base_url}/?{urlencode(params)}" if params else f"{base_url}/"

        def mt_href(mt: Optional[str]) -> str:
            params: dict[str, str] = {}
            if mode != "everything":
                params["mode"] = mode
            if language:
                params["language"] = language
            if mt:
                params["media_type"] = mt
            if access and access != "general":
                params["access"] = access
            return f"{base_url}/?{urlencode(params)}" if params else f"{base_url}/"

        def ac_href(ac: str) -> str:
            params: dict[str, str] = {}
            if mode != "everything":
                params["mode"] = mode
            if language:
                params["language"] = language
            if media_type:
                params["media_type"] = media_type
            if ac != "general":
                params["access"] = ac
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
                    counts=language_counts,
                ),
            },
            {
                "metadata": {"title": "Media Type"},
                "links": _build_media_type_links(
                    media_type=media_type,
                    href_fn=mt_href,
                ),
            },
            {
                "metadata": {"title": "Access"},
                "links": _build_access_links(
                    access=access,
                    href_fn=ac_href,
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
        access: Optional[str] = None,
        sort: Optional[str] = None,
        path: Optional[str] = None,
    ) -> list[dict]:
        """Build Availability, Language, and Media Type facet groups for an author catalog page.

        Links point to ``<base_url><path>?mode=<value>&...``, preserving the
        current page, limit, language, media_type and sort selections when
        switching between facets. *path* is the feed's path under
        *base_url*, ``/authors/<olid>`` unless given (a service that lists an
        author's books at ``/authors/<olid>/books`` passes that).
        """
        language = canonical_language(language)
        mode = canonical_mode(mode)
        media_type = canonical_media_type(media_type)
        if path is None:
            path = f"/authors/{olid}"

        def author_href(
            mode_val: str = mode,
            lang_val: Optional[str] = language,
            mt_val: Optional[str] = media_type,
            ac_val: Optional[str] = access,
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
            if ac_val and ac_val != "general":
                params["access"] = ac_val
            if sort:
                params["sort"] = sort
            return f"{base_url}{path}?{urlencode(params)}" if params else f"{base_url}{path}"

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
            {
                "metadata": {"title": "Access"},
                "links": _build_access_links(
                    access=access,
                    href_fn=lambda ac: author_href(ac_val=ac),
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
    def _home_groups_config(
        mode: str = "everything",
        language: Optional[str] = None,
        language_counts: Optional[dict[str, int]] = None,
    ) -> list[tuple[str, str, str]]:
        """Return the full list of homepage group definitions.

        Each entry is ``(title, solr_query, sort)``.  The *mode* parameter
        controls the ``ebook_access`` filter baked into each query.

        Language handling:
        - ``_STANDARD_EBOOKS_GROUP`` is omitted for non-English languages —
          Standard Ebooks only publishes English public-domain books.
        - The ``trending_score_hourly_sum:[1 TO *]`` and ``readinglog_count``
          filters are dropped for non-English languages because OL's trending
          and reading-log signals are heavily English-biased; keeping them
          would cause most genre groups to return 0 results.
        - A list of languages counts as English when English is in it: with
          ``en`` in the OR clause the English corpus fills every group, and
          without it the pool is still a minority-language one.
        """
        is_english_or_all = _is_english_or_all(language)
        include_standard_ebooks = is_english_or_all
        # Non-English: drop trending_score / readinglog gates so groups have content.
        require_trending = is_english_or_all
        trending_filter = 'trending_score_hourly_sum:[1 TO *] ' if require_trending else ''
        readinglog_filter = ' readinglog_count:[4 TO *]' if require_trending else ''
        # Non-English: drop English-biased year windows so Romance / Textbooks
        # don't silently empty out. The corresponding pre-1930 translations
        # and older textbooks dominate the non-English ebook corpus on OL.
        romance_subject = "subject:romance" + (" first_publish_year:[1930 TO *]" if require_trending else "")
        textbooks_subject = "subject_key:textbooks" + (" publish_year:[1990 TO *]" if require_trending else "")

        if mode == "open_access":
            # Public-domain-friendly groups ordered by reliability.
            # Romance, Thrillers, and Textbooks are excluded because post-1928
            # books in those genres are mostly still under copyright.
            # Trending drops readinglog_count so older public-domain classics
            # (which accumulate fewer logs) still surface.
            oa = "ebook_access:public"
            groups: list[tuple[str, str, str]] = [
                _CLASSIC_BOOKS_GROUP,
            ]
            if include_standard_ebooks:
                groups.append(_STANDARD_EBOOKS_GROUP)
            groups += [
                ("Trending Books", f'{trending_filter}-subject:"content_warning:cover" {oa}'.strip(), "trending"),
                _kids_group(oa, require_trending=require_trending),
                _subject_group("Science", "subject_key:science", oa, require_trending=require_trending),
                _subject_group("History", "subject_key:history", oa, require_trending=require_trending),
                _subject_group("Philosophy", "subject_key:philosophy", oa, require_trending=require_trending),
            ]
            return groups

        if mode == "print_disabled":
            # Print-disabled groups ordered by density of printdisabled books.
            # Standard Ebooks is excluded (ebook_access:public, filtered out by post-filter).
            # Trending drops readinglog_count because print-disabled titles
            # accumulate fewer reading logs than borrowable books.
            pd = "ebook_access:printdisabled"
            return [
                _CLASSIC_BOOKS_GROUP,
                _kids_group(pd, require_trending=require_trending),
                ("Textbooks", f'{textbooks_subject} {pd}', "trending"),
                ("Trending Books", f'{trending_filter}-subject:"content_warning:cover" {pd}'.strip(), "trending"),
                _subject_group("Romance", romance_subject, pd, sort="trending,trending_score_hourly_sum", require_trending=require_trending),
                _subject_group("Thrillers", "subject:thrillers", pd, sort="trending,trending_score_hourly_sum", require_trending=require_trending),
                _subject_group("Science", "subject_key:science", pd, require_trending=require_trending),
            ]

        ea = "ebook_access:[borrowable TO *]"
        groups = [
            ("Trending Books", f'{trending_filter}-subject:"content_warning:cover" {ea}{readinglog_filter}'.strip(), "trending"),
            _CLASSIC_BOOKS_GROUP,
            _subject_group("Romance", romance_subject, ea, sort="trending,trending_score_hourly_sum", require_trending=require_trending),
            _kids_group(ea, require_trending=require_trending),
            _subject_group("Thrillers", "subject:thrillers", ea, sort="trending,trending_score_hourly_sum", require_trending=require_trending),
            ("Textbooks", f'{textbooks_subject} {ea}', "trending"),
        ]
        if include_standard_ebooks:
            groups.append(_STANDARD_EBOOKS_GROUP)
        return groups

    @staticmethod
    def _home_page_href(
        base: str, mode: str, language: Optional[str], page: int,
        media_type: Optional[str] = None,
        access: Optional[str] = None,
    ) -> str:
        """Build a homepage href with pagination."""
        params: dict[str, str] = {}
        if mode != "everything":
            params["mode"] = mode
        if language:
            params["language"] = language
        if media_type:
            params["media_type"] = media_type
        if access and access != "general":
            params["access"] = access
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
        access: Optional[str] = None,
        language_counts: Optional[dict[str, int]] = None,
        limit: int = 0,
    ) -> dict:
        """Build a complete OPDS 2.0 homepage catalog dict.

        Fetches the current page's batch of groups from Open Library,
        builds navigation (page 1 only), facets (page 1 only), and
        pagination links (``next`` / ``previous``).

        Args:
            base: OPDS base URL (no trailing slash).
            mode: Availability filter (``everything``, ``ebooks``,
                ``open_access``, ``buyable``).
            language: Comma-separated ISO 639-1 codes, the preferred one
                first, or ``None`` for all.
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

        language = canonical_language(language)
        mode = canonical_mode(mode)
        media_type = canonical_media_type(media_type)
        subjects = featured_subjects if featured_subjects is not None else cls.FEATURED_SUBJECTS
        media = cls.OPDS_MEDIA_TYPE
        search_url = cls.SEARCH_URL
        all_groups = cls._home_groups_config(mode, language, language_counts=language_counts)

        # Fetch every configured group, then paginate only the non-empty ones.
        # Pagination must come *after* the empty-group filter: slicing first and
        # dropping empties afterwards (the old order) let a page whose slice
        # happened to contain empty carousels render with fewer than
        # GROUPS_PER_PAGE groups — minority-language homepages routinely
        # collapsed to just "Trending Books" this way.
        per_page = cls.GROUPS_PER_PAGE

        # Fetch groups in parallel.
        # Non-English: drop the cover requirement and widen the Solr pool so
        # carousels still fill. International books often lack ``cover_i``
        # metadata, and a smaller intersection with ``language:<MARC>`` would
        # otherwise empty most subject groups. Empty carousels are dropped
        # entirely by the publications-check below, so a placeholder cover
        # is a better UX than a missing carousel.
        is_english_or_all = _is_english_or_all(language)
        # Caller-provided limit (> 0) overrides the language-default cap; lets
        # a client tune per-carousel size via ``?limit=N`` on the home route.
        default_limit = 25 if is_english_or_all else 50
        group_limit = limit if limit > 0 else default_limit
        group_require_cover = is_english_or_all

        def fetch_one(title: str, query: str, sort: str) -> Optional[Catalog]:
            try:
                resp = cls.search(
                    query=query, sort=sort, limit=group_limit,
                    language=language, facets={"mode": mode}, title=title,
                    media_type=media_type, access=access,
                    require_cover=group_require_cover,
                )
                desc = _GROUP_DESCRIPTIONS.get(title)
                group = Catalog.create(metadata=Metadata(title=title, description=desc), response=resp)
            except Exception:
                return None
            # Outside the try: a slip here is a bug to surface, not a fetch
            # failure to drop the carousel for.
            rel = _GROUP_RELS.get(title)
            if rel:
                for link in group.links:
                    if link.rel == "self":
                        link.rel = ["self", rel]
            return group

        non_empty: list[Catalog] = []
        if all_groups:
            with ThreadPoolExecutor(max_workers=len(all_groups)) as pool:
                futures = {
                    pool.submit(fetch_one, t, q, s): i
                    for i, (t, q, s) in enumerate(all_groups)
                }
                results: list[Optional[Catalog]] = [None] * len(all_groups)
                for future in as_completed(futures):
                    results[futures[future]] = future.result()

            non_empty = [
                g for g in results
                if g is not None and g.publications
            ]

        # Paginate the surviving (non-empty) groups.
        start = (page - 1) * per_page
        loaded_groups = non_empty[start : start + per_page]
        has_next = start + per_page < len(non_empty)

        # Navigation — only on page 1 when groups loaded
        navigation: list[Navigation] = []
        if page == 1 and loaded_groups:
            # Standard Ebooks is English-only; hide its nav link for other languages.
            visible_subjects = [
                s for s in subjects
                if s.get("presentable_name") != "Standard Ebooks" or _is_english_or_all(language)
            ]
            for subject in visible_subjects:
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
                if mode != "everything":
                    nav_params["mode"] = mode
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
            Link(rel="self", href=cls._home_page_href(base, mode, language, page, media_type, access), type=media),
            Link(rel="start", href=f"{base}/", type=media),
            Link(rel="search", href=f"{base}/search{{?query,language,mode,media_type}}", type=media, templated=True),
            cls.bookshelf_link(),
            cls.profile_link(),
        ]
        if has_next:
            links.append(Link(rel="next", href=cls._home_page_href(base, mode, language, page + 1, media_type, access), type=media))
        if page > 1:
            links.append(Link(rel="previous", href=cls._home_page_href(base, mode, language, page - 1, media_type, access), type=media))

        catalog = Catalog(
            metadata=Metadata(title="Open Library"),
            publications=[],
            navigation=navigation,
            groups=loaded_groups,
            facets=cls.build_home_facets(base, mode, language, media_type, access=access, language_counts=language_counts),
            links=links,
        )
        return catalog.model_dump()

    @typing.override
    @staticmethod
    def fetch_work(work_olid: str, language: Optional[str] = None) -> Optional[OpenLibraryDataRecord]:
        """The work record itself, unfiltered: ``search()`` keeps only records
        whose surfaced edition has an offer, and a work whose best edition in
        the reader's first language has none may still list one in another."""
        language = canonical_language(language)
        languages = parse_languages(language)
        r = _get(
            f"{OpenLibraryDataProvider.BASE_URL}/search.json",
            params={
                "q": f"key:/works/{work_olid}",
                "editions": "true",
                "limit": 1,
                "fields": ",".join(_SEARCH_FIELDS),
                **({"lang": languages[0]} if languages else {}),
            },
        )
        docs = r.json().get("docs", [])
        if not docs:
            return None
        doc = dict(docs[0])
        if "editions" in doc and isinstance(doc["editions"], dict):
            doc["editions"] = OpenLibraryDataRecord.EditionsResultSet.model_validate(doc["editions"])
        record = OpenLibraryDataRecord.model_validate(doc)
        record.request_language = language
        _resolve_latin_author_names([record])
        return record

    @staticmethod
    def work(
        work_olid: str,
        language: Optional[str] = None,
        access: Optional[str] = None,
    ) -> Optional[Publication]:
        """The work as an OPDS publication listing its editions for a reader
        of *language*, or None when there is no such work or nothing to list."""
        record = OpenLibraryDataProvider.fetch_work(work_olid, language)
        if record is None:
            return None
        editions = editions_of_work(work_olid, language, access)
        if not editions:
            return None
        return record.to_work_publication(editions, canonical_language(language))

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
        access: Optional[str] = None,
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
            language: Comma-separated ISO 639-1 codes, the preferred one
                first (e.g. ``"en"`` or ``"en,fr"``), or ``None`` to return
                results in all languages without any language filter.  When
                set, works in any of the languages are kept, OL is asked to
                surface editions in the first and, for ``edition_key:``
                queries, the preferred edition is resolved via the work's
                editions endpoint.
        """
        language = canonical_language(language)
        languages = parse_languages(language)
        primary_language = languages[0] if languages else None
        media_type = canonical_media_type(media_type)

        fields = _SEARCH_FIELDS

        internal_query = query
        # ``mode`` may be a list: a record is kept if it belongs under any of
        # the listed modes.
        mode = canonical_mode(facets.get('mode') if facets else None)
        modes = parse_modes(mode)

        # Mode wins: strip any ebook_access clause baked into the query
        # (group queries like Standard Ebooks add ebook_access:public,
        # subject nav links add [borrowable TO *]) before applying the
        # selected mode's own clause. Without this, "Available to Borrow"
        # on an open-access group would silently keep returning open-access
        # records — Solr would see ebook_access:public and never even
        # consider the borrow range.
        internal_query = _EBOOK_ACCESS_CLAUSE_RE.sub('', internal_query).strip()
        # Apply the mode's ebook_access clause (shared with _count_for_mode so
        # results and facet counts stay in lockstep). ``everything`` floors to
        # servable books only — see _mode_ebook_access_clause.
        internal_query = f"{internal_query} {_mode_ebook_access_clause(mode)}".strip()

        # Apply media_type filter (audiobook/ebook) on top of the mode filter.
        internal_query = _apply_media_type_filter(internal_query, media_type)

        # When a language filter is active, add language:<MARC> (or an OR of
        # them) to the Solr query so non-matching works are excluded (the
        # `lang` param only influences edition preference / ranking, it does
        # not filter).
        if language and 'language:' not in internal_query:
            clause = _language_solr_clause(language)
            if clause:
                internal_query = f"{internal_query} {clause}"

        params = {
            "editions": "true",
            "q": internal_query,
            "page": (offset // limit) + 1 if limit else 1,
            "limit": limit,
            **({'sort': sort} if sort else {}),
            "fields": ",".join(fields),
            # Also pass lang to prefer editions in the reader's first language.
            **({'lang': primary_language} if primary_language else {}),
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
            record = OpenLibraryDataRecord.model_validate(doc)
            record.request_language = language
            records.append(record)

        total = data.get("numFound", 0)

        # Ensure the displayed edition matches the selected language.
        # - Multiple editions: reorder so language-matching ones come first.
        # - Single mismatched edition: resolve the preferred edition from OL.
        # This also handles edition_key: queries where OL ignores the lang param.
        if primary_language:
            records = _align_editions_to_language(
                records, primary_language,
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
        if media_type == "ebook":  # canonical: "ebook" alone, not with audiobook
            records = [r for r in records if not r.id_librivox]

        # Access filter: control print-disabled visibility.
        # "general" (default) hides print-disabled books; "print_disabled" shows only them.
        if access == "print_disabled":
            records = [r for r in records
                       if _get_edition_ebook_access(r) == "printdisabled"
                       or r.ebook_access == "printdisabled"]
        else:
            records = [r for r in records
                       if _get_edition_ebook_access(r) != "printdisabled"
                       and r.ebook_access != "printdisabled"]

        # Strict post-filter: enforce each listed mode's boundary after all
        # edition resolution (see _passes_mode), keeping a record that belongs
        # under any of them. Open-access books must never appear under
        # borrow-only filters, and 'buyable' is only testable here.
        if access != "print_disabled" and modes:
            records = [r for r in records if any(_passes_mode(r, m) for m in modes)]
            # Sort available books before unavailable, preserving order within each group
            records.sort(key=lambda r: (0 if _is_currently_available(r) else 1))
        elif access == "print_disabled":
            records.sort(key=lambda r: (0 if _is_currently_available(r) else 1))

        # Set total for buyable after ALL filters — client-side filtering means Solr
        # cannot produce an accurate count; use the final post-filter record count.
        if 'buyable' in modes:
            total = len(records)

        _resolve_latin_author_names(records)

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
        # The pagination links carry every parameter the search was made with,
        # with the same omit-when-default rules as the facet hrefs so the two
        # agree: pyopds2's own params know only query, limit, page and sort,
        # and following ``next`` used to drop the language, the mode, the
        # media type and the access filter. ``params`` is a
        # functools.cached_property; setting it on the instance before first
        # access caches our version.
        base_params = {
            **({"query": query} if query else {}),
            **({"limit": str(limit)} if limit else {}),
            **({"sort": sort} if sort else {}),
        }
        if resp.page > 1:
            base_params["page"] = str(resp.page)
        if mode and mode != "everything":
            base_params["mode"] = mode
        if language:
            base_params["language"] = language
        if media_type:
            base_params["media_type"] = media_type
        if access and access != "general":
            base_params["access"] = access
        if title:
            base_params["title"] = title
        resp.params = base_params
        return resp
