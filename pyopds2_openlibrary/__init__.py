import functools
import typing
from typing_extensions import Literal
import requests
from typing import List, Optional, TypedDict, cast
from pydantic import BaseModel, Field

from opds2 import (
    DataProvider,
    DataProviderRecord,
    SearchRequest,
    SearchResponse,
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

class OpenLibraryDataRecord(BookSharedDoc, DataProviderRecord):

    class EditionProvider(BaseModel):
        """Basically the acquisition info for an edition."""
        access: Optional[str] = None
        format: Optional[Literal['web', 'pdf', 'epub', 'audio']] = None
        price: Optional[float] = None
        url: Optional[str] = None
        provider_name: Optional[str] = None

    class EditionDoc(BookSharedDoc):
        """Open Library edition document."""
        providers: Optional[list["OpenLibraryDataRecord.EditionProvider"]] = None

    class EditionsResultSet(BaseModel):
        numFound: Optional[int] = None
        start: Optional[int] = None
        numFoundExact: Optional[bool] = None
        docs: Optional[list["OpenLibraryDataRecord.EditionDoc"]] = None

    author_key: Optional[list[str]] = Field(None, description="List of author keys")
    author_name: Optional[list[str]] = Field(None, description="List of author names")
    editions: Optional["OpenLibraryDataRecord.EditionsResultSet"] = Field(None, description="Editions information (nested structure)")
    number_of_pages_median: Optional[int] = None
    
    @property
    def type(self) -> str:
        """Type _should_ be improved to dynamically return type based on record data."""
        return "http://schema.org/Book"
    
    def links(self) -> List[Link]:
        edition = self.editions.docs[0] if self.editions and self.editions.docs else None
        book = edition or self
        links: list[Link] = [
            Link(
                rel="self",
                href=f"{OpenLibraryDataProvider.URL}{book.key}",
                type="text/html",
            ),
            Link(
                rel="alternate",
                href=f"{OpenLibraryDataProvider.URL}{book.key}.json",
                type="application/json",
            ),
        ]

        if not edition or not edition.providers:
            return links

        return links + [
            Link(
                href=acquisition.url,
                rel=f'http://opds-spec.org/acquisition/{acquisition.access}',
                type=map_ol_format_to_mime(acquisition.format) if acquisition.format else None,
            )
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
            if self.author_name:
                return [
                    Contributor(
                        name=name,
                        links=[
                            Link(
                                href=f"{OpenLibraryDataProvider.URL}/authors/{key}",
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
            description=book.description,
            language=[lang for marc_lang in (book.language or []) if (lang := marc_language_to_iso_639_1(marc_lang))],
            # TODO: Use the edition-specific pagecount
            numberOfPages=self.number_of_pages_median,
        )


class OpenLibraryLanguageStub(TypedDict):
    key: str
    identifiers: dict[str, list[str]] | None


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
    r = requests.get("http://openlibrary.org/query.json?type=/type/language&key&identifiers&limit=1000")
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


class OpenLibraryDataProvider(DataProvider):
    """Data provider for Open Library records."""
    URL: str = "https://openlibrary.org"
    TITLE: str = "OpenLibrary.org OPDS Service"
    CATALOG_URL: str = "/opds/catalog"
    SEARCH_URL: str = "/opds/search{?query}"

    @typing.override
    @staticmethod
    def search(
        query: str,
        limit: int = 50,
        offset: int = 0,
        sort: Optional[str] = None,
    ) -> SearchResponse:
        fields = [
            "key", "title", "editions", "description", "providers", "author_name",
            "cover_i", "availability", "ebook_access", "author_key", "subtitle", "language",
            "number_of_pages_median",
        ]
        params = {
            "editions": "true",
            "q": query,
            "page": (offset // limit) + 1 if limit else 1,
            "limit": limit,
            **( {"sort": sort} if sort else {} ),
            "fields": ",".join(fields),
        }
        r = requests.get(f"{OpenLibraryDataProvider.URL}/search.json", params=params)
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
        return SearchResponse(records, data.get("numFound", 0), SearchRequest(query, limit, offset, sort))
