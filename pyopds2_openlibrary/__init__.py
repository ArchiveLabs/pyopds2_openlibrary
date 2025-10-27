import requests
from typing import List, Optional
from pydantic import BaseModel, Field

from opds2 import (
    DataProvider,
    DataProviderRecord,
    Contributor,
    Metadata,
    Link
)

class OpenLibraryDataRecord(DataProviderRecord):

    class EditionProvider(BaseModel):
        access: Optional[str] = None
        format: Optional[str] = None
        price: Optional[float] = None
        url: Optional[str] = None
        provider_name: Optional[str] = None

    class EditionDoc(BaseModel):
        key: Optional[str] = None
        title: Optional[str] = None
        cover_i: Optional[int] = None
        ebook_access: Optional[str] = None
        providers: Optional[list["OpenLibraryDataRecord.EditionProvider"]] = None

    class EditionsInfo(BaseModel):
        numFound: Optional[int] = None
        start: Optional[int] = None
        numFoundExact: Optional[bool] = None
        docs: Optional[list["OpenLibraryDataRecord.EditionDoc"]] = None

    key: str = Field(..., description="Unique key for the work (e.g. /works/OL27448W)")
    title: str = Field(..., min_length=1, description="Title of the work")
    subtitle: Optional[str] = Field(None, description="Subtitle of the work")
    author_key: Optional[list[str]] = Field(None, description="List of author keys")
    author_name: Optional[list[str]] = Field(None, description="List of author names")
    cover_i: Optional[int] = Field(None, description="Cover image ID")
    ebook_access: Optional[str] = Field(None, description="Ebook access type (e.g. borrowable)")
    editions: Optional["OpenLibraryDataRecord.EditionsInfo"] = Field(None, description="Editions information (nested structure)")
    description: Optional[str] = Field(None, description="Description of the work")
    providers: Optional[list] = Field(None, description="List of providers")
    availability: Optional[dict] = Field(None, description="Availability information")
    language: Optional[list[str]] = Field(None, description="Languages of the work")
    
    @property
    def type(self) -> str:
        """Type _should_ be improved to dynamically return type based on record data."""
        return "http://schema.org/Book"
    
    def links(self) -> List[Link]:
        return []
    
    def images(self) -> Optional[List[Link]]:
        if self.cover_i:
            cover_url = f"https://covers.openlibrary.org/b/id/{self.cover_i}-L.jpg"
            return [Link(href=cover_url, type="image/jpeg", rel="cover")]
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
                                href=f"/authors/{key}",
                                type="text/html",
                                rel="author"
                            )
                        ]
                    )
                    for name, key in zip(self.author_name, self.author_key)
                ]

        return Metadata(
            type=self.type,
            title=self.title,
            subtitle=self.subtitle,
            author=get_authors(),
            description=self.description,
            language=self.language
        )


class OpenLibraryDataProvider(DataProvider):
    """Data provider for Open Library records."""
    URL: str = "https://openlibrary.org"
    TITLE: str = "OpenLibrary.org OPDS Service"
    CATALOG_URL: str = "/opds/catalog"
    SEARCH_URL: str = "/opds/search{?query}"

    @staticmethod
    def search(
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[OpenLibraryDataRecord], int]:
        fields = [
            "key", "title", "editions", "description", "providers", "author_name",
            "cover_i", "availability", "ebook_access", "author_key", "subtitle", "language"
        ]
        params = {
            "editions": "true",
            "q": query,
            "page": (offset // limit) + 1 if limit else 1,
            "limit": limit,
            "fields": ",".join(fields),
        }
        r = requests.get(f"{}/search.json", params=params)
        r.raise_for_status()
        data = r.json()
        docs = data.get("docs", [])
        records = []
        for doc in docs:
            # Unpack editions field if present
            if "editions" in doc and isinstance(doc["editions"], dict):
                doc = dict(doc)
                doc["editions"] = OpenLibraryDataRecord.EditionsInfo.model_validate(doc["editions"])
            records.append(OpenLibraryDataRecord.model_validate(doc))
        return records, data.get("numFound", 0)
