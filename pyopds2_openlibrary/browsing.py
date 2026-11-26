"""The browsing categories and the reading interests: what the front page
lists to browse, what a category's page offers as its subcategories, and
what a new reader is asked to pick.

The source is the design's "Open books browsing and reading interests" of
24 November 2026: 17 categories, 95 visible subcategories and 15 hidden
source rows, and 26 interests. A category's books are all its rows together
(OR), the hidden ones included; a subcategory's are its own row; an
interest's are the one category or the one subcategory it names.

Each row is a complete Solr clause as the design gives it, conditions and
all (``subject_key:("biography" AND "history")``): it is never taken apart,
only joined to the others with OR. In a non-fiction category, each row
also leaves out the books tagged as fiction (``NOT_FICTION``), except the
rows of a literary form (Poetry and Theater under Arts). The availability
and language filters are added after, to the whole, by the code that
searches.
"""

from __future__ import annotations

import re
from typing import Optional

# One row of a category: the code and label of the subcategory it is, or
# None for a hidden row, which only adds to the category's own books.
_Row = tuple[Optional[str], Optional[str], str]

# What a non-fiction row leaves out. Open Library splits a heading such as
# "Spain -- Politics and government -- Fiction" into separate subjects, so a
# novel carries the subjects of a non-fiction category too.
NOT_FICTION = 'NOT subject_key:("children\'s_fiction" OR "fiction" OR "juvenile_fiction")'

# The categories whose rows leave out fiction, and in them the rows of a
# literary form, which do not.
_NON_FICTION = {
    "biography", "history", "philosophy-and-religion", "arts", "health-and-self-help",
    "food-and-lifestyle", "business", "politics-and-law", "science-and-learning",
}
_LITERARY_FORMS = {("arts", "poetry"), ("arts", "theater")}


def _key(*keys: str) -> str:
    """``subject_key:"a"`` for one key, ``subject_key:("a" OR "b")`` for more."""
    quoted = [f'"{k}"' for k in keys]
    return f"subject_key:{quoted[0]}" if len(quoted) == 1 else f'subject_key:({" OR ".join(quoted)})'


def _all(*keys: str) -> str:
    """``subject_key:("a" AND "b")``: a book with every one of the keys."""
    return f'subject_key:({" AND ".join(f"{chr(34)}{k}{chr(34)}" for k in keys)})'


# (code, name, rows), in the order the front page lists them.
_CATEGORIES: list[tuple[str, str, list[_Row]]] = [
    ("crime-and-thrillers", "Crime and thrillers", [
        ("crime", "Crime", _key("crime_fiction", "detective_and_mystery_stories", "fiction_mystery__detective_police_procedural")),
        ("police-procedurals", "Police procedurals", _key("fiction_mystery__detective_police_procedural")),
        ("true-crime", "True crime", _key("true_crime")),
        ("thrillers", "Thrillers", _key("suspense_fiction", "thriller")),
        ("horror", "Horror", _key("horror", "horror_tales")),
    ]),
    ("romance", "Romance", [
        (None, None, _key("fiction_romance_general", "love_stories", "romance", "romance_fiction")),
        ("contemporary", "Contemporary", _key("fiction_romance_contemporary")),
        ("historical", "Historical", _key("fiction_romance_historical_general")),
        ("suspense", "Suspense", _key("fiction_romance_suspense")),
        ("paranormal", "Paranormal", _key("fiction_romance_paranormal_general")),
    ]),
    ("fantasy", "Fantasy", [
        (None, None, _key(
            "fantasy", "fantasy_fiction", "fiction_fantasy_epic", "fiction_fantasy_general",
            "fiction_fantasy_historical", "fiction_fantasy_urban", "fiction_romance_fantasy",
        )),
        ("epic", "Epic", _key("fiction_fantasy_epic")),
        ("urban", "Urban", _key("fiction_fantasy_urban")),
        ("historical", "Historical", _key("fiction_fantasy_historical")),
        ("romance", "Romance", _key("fiction_romance_fantasy")),
    ]),
    ("sci-fi", "Sci-fi", [
        (None, None, _key(
            "fiction_science_fiction_action__adventure", "fiction_science_fiction_general",
            "fiction_science_fiction_hard_science_fiction", "fiction_science_fiction_space_opera",
            "hard_science_fiction", "science-fiction", "science_fiction",
        )),
        ("space-opera", "Space opera", _key("fiction_science_fiction_space_opera")),
        ("hard-sci-fi", "Hard sci-fi", _key("fiction_science_fiction_hard_science_fiction", "hard_science_fiction")),
    ]),
    ("fiction", "Fiction", [
        (None, None, _key("fiction", "fiction_general")),
        ("literary", "Literary", _key("general__literary_fiction", "literary_fiction")),
        ("historical", "Historical", _key("historical_fiction")),
        ("short-stories", "Short stories", _key("short_stories")),
        ("adventure", "Adventure", _key("action__adventure", "adventure", "adventure_stories", "fiction_action__adventure")),
        ("westerns", "Westerns", _key("fiction_westerns")),
        (None, None, _key("fiction_psychological")),
        ("war", "War", _key("fiction_war__military")),
    ]),
    ("classics", "Classics", [
        (None, None, _key("classic_literature")),
    ]),
    ("childrens-books", "Children’s books", [
        ("stories", "Stories", _key("children's_fiction", "children's_stories", "juvenile_fiction")),
        (None, None, _key("juvenile_literature")),
        ("poetry", "Poetry", _key("children's_poetry", "juvenile_poetry", "stories_in_rhyme")),
        ("bedtime", "Bedtime", _key("bedtime")),
        ("picture-books", "Picture books", _key("picture_books")),
        ("nonfiction", "Nonfiction", _key("juvenile_nonfiction")),
        ("funny", "Funny", _all("children's_fiction", "humorous_stories")),
    ]),
    ("humor", "Humor", [
        (None, None, _key("humor")),
    ]),
    ("biography", "Biography", [
        (None, None, _key("biographies", "biography", "biography__autobiography")),
        ("memoirs", "Memoirs", _key("autobiography", "autobiography_and_memoir", "memoirs", "personal_memoirs", "personal_narratives")),
        ("historical", "Historical", _all("biography", "history")),
        ("politics", "Politics", _all("biography", "politics_and_government")),
        ("world-war-ii", "World War II", _all("biography", "world_war_1939-1945")),
        ("women", "Women", _all("biography", "women")),
        ("artists", "Artists", _all("artists", "biography")),
    ]),
    ("history", "History", [
        (None, None, _key("history")),
        ("ancient", "Ancient", _key("ancient_civilization", "ancient_history")),
        ("archaeology", "Archaeology", _key("antiquities", "archaeology")),
        ("world-war-ii", "World War II",
            f'({_all("biography", "world_war_1939-1945")} OR {_all("history", "world_war_1939-1945")})'),
        ("world-history", "World history", _key("world_history")),
        ("modern", "Modern", _key("modern_history")),
        ("military", "Military", _key("military_history")),
        (None, None, _key("social_history")),
        ("world-war-i", "World War I", _key("world_war_1914-1918")),
    ]),
    ("philosophy-and-religion", "Philosophy and religion", [
        ("philosophy", "Philosophy", _key("ethics", "logic", "philosophy")),
        ("ethics", "Ethics", _key("ethics")),
        ("religion", "Religion", _key(
            "buddhism", "christianity", "hinduism", "islam", "judaism", "religion", "spirituality", "theology",
        )),
        ("christianity", "Christianity", _key("christianity")),
        ("islam", "Islam", _key("islam")),
        ("judaism", "Judaism", _key("judaism")),
        ("buddhism", "Buddhism", _key("buddhism")),
        ("hinduism", "Hinduism", _key("hinduism")),
        ("spirituality", "Spirituality", _key("spirituality")),
    ]),
    ("arts", "Arts", [
        ("architecture", "Architecture", _key("architecture")),
        ("art", "Art", _key("art", "art__art_instruction")),
        ("art-history", "Art history", _key("history_of_art__art__design_styles")),
        ("dance", "Dance", _key("dance")),
        ("design", "Design", _key("design", "graphic_design")),
        ("fashion", "Fashion", _key("fashion")),
        ("film", "Film", _key("film", "motion_pictures")),
        ("music", "Music", _key("music", "music_theory")),
        ("drawing-and-painting", "Drawing and painting", _key("drawing", "painting__paintings")),
        ("photography", "Photography", _key("photography")),
        ("theater", "Theater", _key("drama", "plays", "theater")),
        ("poetry", "Poetry", _key("poetry")),
        (None, None, _key("sculpture")),
    ]),
    ("health-and-self-help", "Health and self-help", [
        ("self-help", "Self-help", _key("mental_health", "self-help", "self-help_techniques")),
        ("mental-health", "Mental health", _key("mental_health")),
        ("health", "Health", _key("health")),
        ("nutrition", "Nutrition", _key("nutrition")),
        ("fitness", "Fitness", _key("exercise", "physical_fitness")),
        ("parenting", "Parenting", _key("parenting")),
    ]),
    ("food-and-lifestyle", "Food and lifestyle", [
        ("cooking", "Cooking", _key("cookbooks", "cookery", "cooking", "recipes")),
        ("travel", "Travel", _key("description_and_travel", "travel")),
        ("hobbies", "Hobbies", _key("handicraft", "hobbies")),
        ("gardening", "Gardening", _key("gardening")),
        ("sports", "Sports", _key("sports")),
    ]),
    ("business", "Business", [
        ("management", "Management", _key("management")),
        ("entrepreneurship", "Entrepreneurship", _key("entrepreneurship")),
        ("economics", "Economics", _key("business__economics", "economics")),
        ("success", "Success", _key("success_in_business")),
        ("finance", "Finance", _key("finance")),
        (None, None,
            f'({_key("accounting", "business", "business__economics", "commerce", "economics", "entrepreneurship", "finance", "management", "marketing", "success_in_business")}'
            f' OR (subject:"Business" AND subject_key:"textbooks"))'),
        ("marketing", "Marketing", _key("marketing")),
    ]),
    ("politics-and-law", "Politics and law", [
        ("politics", "Politics", _key("political_science")),
        ("law", "Law", _key("law")),
        ("foreign-affairs", "Foreign affairs", _key("international_relations")),
        (None, None, _key("economic_policy")),
    ]),
    ("science-and-learning", "Science and learning", [
        ("biology", "Biology", _key("biology")),
        ("chemistry", "Chemistry", _key("chemistry")),
        ("mathematics", "Mathematics", f'(subject_key:"mathematics" OR {_all("algebra", "textbooks")})'),
        ("physics", "Physics", _key("physics")),
        ("computing", "Computing", _key("computer_science", "programming")),
        ("psychology", "Psychology", _key("psychology")),
        ("science", "Science",
            f'({_key("astronomy", "biology", "chemistry", "computer_science", "ecology", "engineering", "geology", "mathematics", "medicine", "natural_history", "physics", "programming", "psychology", "technology")}'
            f' OR {_all("algebra", "textbooks")} OR {_all("geography", "textbooks")} OR {_all("science", "textbooks")})'),
        ("medicine", "Medicine", _key("medicine")),
        ("technology", "Technology", _key("engineering", "technology")),
        ("astronomy", "Astronomy", _key("astronomy")),
        ("ecology", "Ecology", _key("ecology")),
        ("nature", "Nature", _key("natural_history")),
        ("education", "Education", _all("education", "textbooks")),
        ("languages", "Languages",
            f'({_key("foreign_language_study", "language_and_languages")} OR {_all("english_language", "textbooks")})'),
        (None, None, _key("linguistics")),
    ]),
]

# (code, name, category, subcategory or None for the whole category), in the
# order the onboarding lists them: related interests together, not by
# popularity.
_INTERESTS: list[tuple[str, str, str, Optional[str]]] = [
    ("crime", "Crime", "crime-and-thrillers", "crime"),
    ("true-crime", "True crime", "crime-and-thrillers", "true-crime"),
    ("thrillers", "Thrillers", "crime-and-thrillers", "thrillers"),
    ("horror", "Horror", "crime-and-thrillers", "horror"),
    ("romance", "Romance", "romance", None),
    ("fantasy", "Fantasy", "fantasy", None),
    ("fantasy-romance", "Fantasy romance", "fantasy", "romance"),
    ("sci-fi", "Sci-fi", "sci-fi", None),
    ("fiction", "Fiction", "fiction", None),
    ("historical-fiction", "Historical fiction", "fiction", "historical"),
    ("adventure", "Adventure", "fiction", "adventure"),
    ("classics", "Classics", "classics", None),
    ("childrens-books", "Children’s books", "childrens-books", None),
    ("humor", "Humor", "humor", None),
    ("biography", "Biography", "biography", None),
    ("history", "History", "history", None),
    ("philosophy", "Philosophy", "philosophy-and-religion", "philosophy"),
    ("religion", "Religion", "philosophy-and-religion", "religion"),
    ("arts", "Arts", "arts", None),
    ("poetry", "Poetry", "arts", "poetry"),
    ("cooking", "Cooking", "food-and-lifestyle", "cooking"),
    ("self-help", "Self-help", "health-and-self-help", "self-help"),
    ("travel", "Travel", "food-and-lifestyle", "travel"),
    ("business", "Business", "business", None),
    ("politics", "Politics", "politics-and-law", None),
    ("science", "Science", "science-and-learning", "science"),
]


_PLAIN_KEYS = re.compile(r'subject_key:(\((?:"[^"]*"(?: OR )?)+\)|"[^"]*")')


def plain_keys(clause: str) -> list[str]:
    """The subject keys a clause takes alone, any one of them enough: those of
    its ``subject_key:"a"`` and ``subject_key:("a" OR "b")`` parts. A key
    that only counts with another (``("biography" AND "history")``) is not
    one: history alone is not a biography."""
    keys: list[str] = []
    for group in _PLAIN_KEYS.findall(clause):
        keys += re.findall(r'"([^"]*)"', group)
    return keys


def _source(category_code: str, sub: Optional[str], row: str) -> str:
    """The clause a row searches: the row itself, or in a non-fiction
    category the row without the books tagged as fiction."""
    if category_code in _NON_FICTION and (category_code, sub) not in _LITERARY_FORMS:
        return f"({row} AND {NOT_FICTION})"
    return row


def _category(code: str, name: str, rows: list[_Row]) -> dict:
    # The keys come from the rows as the design gives them: the fiction
    # keys a row leaves out do not name the category.
    keys = list(dict.fromkeys(k for _, _, q in rows for k in plain_keys(q)))
    return {
        "code": code,
        "presentable_name": name,
        "icon": f"browse-{code}",
        "sources": [_source(code, sub, q) for sub, _, q in rows],
        "keys": keys,
        "address": {"category": code},
        "subcategories": [
            {
                "code": sub,
                "presentable_name": label,
                "sources": [_source(code, sub, q)],
                "address": {"category": code, "subcategory": sub},
            }
            for sub, label, q in rows
            if sub is not None
        ],
    }


BROWSE_CATEGORIES: list[dict] = [_category(*c) for c in _CATEGORIES]


def category(code: Optional[str]) -> Optional[dict]:
    """The browsing category named by ``code``, or None."""
    return next((c for c in BROWSE_CATEGORIES if c["code"] == code), None)


def subcategory(category_code: Optional[str], code: Optional[str]) -> Optional[dict]:
    """The subcategory ``code`` of the category ``category_code``, or None."""
    found = category(category_code)
    if found is None:
        return None
    return next((s for s in found["subcategories"] if s["code"] == code), None)


def _interest(code: str, name: str, category_code: str, sub: Optional[str]) -> dict:
    target = subcategory(category_code, sub) if sub else category(category_code)
    assert target is not None, f"the interest {code} names no {category_code}/{sub}"
    return {
        "code": code,
        "presentable_name": name,
        "icon": f"interest-{code}",
        "sources": list(target["sources"]),
        # Its own address, not its category's: an interest's page is its
        # books alone, with no All and no subcategories to choose from.
        "address": {"interest": code},
        # The category or subcategory it is, by its codes.
        "target": dict(target["address"]),
    }


READING_INTERESTS: list[dict] = [_interest(*i) for i in _INTERESTS]


def reading_interest(code: Optional[str]) -> Optional[dict]:
    """The reading interest named by ``code``, or None."""
    return next((i for i in READING_INTERESTS if i["code"] == code), None)
