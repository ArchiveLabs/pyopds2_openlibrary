# pyopds2_openlibrary
An Open Library DataProvider for the pyopds2 library

## Simple Example

```
virtualenv env
source ./env/bin/activate
pip install pyopds2 requests
git clone https://github.com/ArchiveLabs/pyopds2_openlibrary.git
python
```

```python:
from pyopds2 import Catalog
from pyopds2_openlibrary import OpenLibraryDataProvider
catalog = Catalog.create(OpenLibraryProvider.search("Libraries of the future by J C R Licklider"))
catalog.model_dump_json()
```
