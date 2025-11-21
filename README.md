# pyopds2_openlibrary
An Open Library DataProvider for the pyopds2 library

## Simple Example

```
git clone https://github.com/ArchiveLabs/pyopds2_openlibrary.git
cd pyopds2_openlibrary
virtualenv env
source ./env/bin/activate
pip install pyopds2 requests
python
```

```python:
from pyopds2 import Catalog
from pyopds2_openlibrary import OpenLibraryDataProvider
catalog = Catalog.create(OpenLibraryDataProvider.search("Libraries of the future by J C R Licklider"))
catalog.model_dump_json()
```
