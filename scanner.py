"""Babel library scanner.

Walks the Books folder, groups files of the same book (EPUB + PDF of one
edition), works out title/author/year/publisher from the folder layout and
filenames (plus EPUB/MOBI metadata), makes cover thumbnails, and writes it all
to a SQLite database with a full-text index. The books themselves are never
modified.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from io import BytesIO

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

# Set by the app from the user's chosen library folder (Preferences → General)
BOOKS_ROOT = ""
DATA_DIR = os.environ.get("BABEL_DATA_DIR") or os.path.expanduser("~/.local/share/babel")
DB_PATH = os.path.join(DATA_DIR, "library.db")
COVERS_DIR = os.path.join(DATA_DIR, "covers")

BOOK_EXTS = {"epub", "pdf", "mobi", "azw3", "azw", "cbr", "cbz", "djvu", "fb2"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}
# Preferred format when opening a book
FORMAT_ORDER = ["epub", "azw3", "azw", "mobi", "fb2", "pdf", "djvu", "cbz", "cbr"]
THUMB_SIZE = (240, 360)
# Bump when parsing changes so every book's metadata is rebuilt on the next scan
PARSER_VERSION = "8"
REBUILD_COVERS = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    key TEXT PRIMARY KEY,
    title TEXT, author TEXT, author_sort TEXT, year INTEGER, publisher TEXT,
    collection TEXT, collection_sort TEXT, kind TEXT, genre TEXT, notes TEXT, description TEXT, subjects TEXT,
    cover TEXT, formats TEXT, files TEXT, dir TEXT, size INTEGER,
    sig TEXT, added REAL
);
CREATE TABLE IF NOT EXISTS user (
    key TEXT PRIMARY KEY, fav INTEGER DEFAULT 0, status TEXT DEFAULT '',
    last_opened REAL, open_count INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    key UNINDEXED, title, author, collection, publisher, subjects, notes,
    description, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE INDEX IF NOT EXISTS idx_books_author ON books(author_sort);
CREATE INDEX IF NOT EXISTS idx_books_collection ON books(collection);
"""

MONTHS = ("january february march april may june july august september "
          "october november december spring summer autumn fall winter").split()
MAGAZINE_RE = re.compile(r"magazine|quarterly|journal|monthly|\(\d+ issues\)|"
                         r"scientific american|new yorker|economist", re.I)
PAPER_DIR_RE = re.compile(r"papers|articles|obituar|essays and articles|interviews", re.I)
PAPER_NAME_RE = re.compile(r"^_|obituary|nobel lecture|interview|article on|review of", re.I)


def connect(db_path=None):
    db_path = db_path or DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(books)")}
    if "collection_sort" not in cols:          # databases from before this column existed
        con.execute("ALTER TABLE books ADD COLUMN collection_sort TEXT")
    ucols = {r[1] for r in con.execute("PRAGMA table_info(user)")}
    if "kind" not in ucols:                    # the user's own type corrections
        con.execute("ALTER TABLE user ADD COLUMN kind TEXT")
    if "genre" not in ucols:                   # the user's own genre corrections
        con.execute("ALTER TABLE user ADD COLUMN genre TEXT")
    # genres looked up per author (Wikidata) or set by the user for a whole author
    con.execute("CREATE TABLE IF NOT EXISTS author_genre (author TEXT PRIMARY KEY, genre TEXT, "
                "source TEXT, checked REAL)")
    acols = {r[1] for r in con.execute("PRAGMA table_info(author_genre)")}
    if "user_genre" not in acols:              # your choice for a whole author, kept apart
        con.execute("ALTER TABLE author_genre ADD COLUMN user_genre TEXT")
    if "genre" not in cols:                    # databases from before genres: fill from file tags
        con.execute("ALTER TABLE books ADD COLUMN genre TEXT")
        rows = con.execute("SELECT key, subjects FROM books WHERE subjects != ''").fetchall()
        con.executemany("UPDATE books SET genre=? WHERE key=?",
                        [(genre_from_subjects(sub), k) for k, sub in rows if genre_from_subjects(sub)])
        con.commit()
    return con


# ---------------------------------------------------------------- text cleanup

def clean(s):
    """Undo the filesystem-safe substitutions used in the filenames."""
    s = s.replace("_ndash;", "–").replace("_ndash", "–").replace("_amp;", "&")
    s = re.sub(r"(?<=\s)amp;(?=\s)", "&", s)
    s = re.sub(r"(?<![\w_])_([^_]+?)_(?![\w_])", r"‘\1’", s)   # _Avi_ / _Title_ were quotes
    s = re.sub(r"(\w)_s\b", r"\1's", s)        # Physicist_s -> Physicist's
    s = re.sub(r"(\w)_t\b", r"\1't", s)        # Don_t -> Don't
    s = re.sub(r"_\s", ": ", s)                # Nuclear Folly_ A New -> Nuclear Folly: A New
    s = re.sub(r"^_+|_+$", "", s.strip())
    s = s.replace("_", "'")
    s = re.sub(r"(\S)  (\S)", r"\1 & \2", s)   # "Ward No. 6  Other Stories" ("&" was stripped)
    return re.sub(r"\s+", " ", s).strip(" -")


def front_article(title):
    """'Seagull, The' -> 'The Seagull'."""
    m = re.match(r"^(.*?),\s*(The|A|An)\b(.*)$", title)
    if m and (not m.group(3).strip() or m.group(3).lstrip().startswith((":", "(", "[", ","))):
        return f"{m.group(2)} {m.group(1)}{m.group(3)}".strip()
    return title


def flip_name(name):
    """'Bioy Casares, Adolfo' -> 'Adolfo Bioy Casares'. Leaves other forms alone."""
    name = name.strip()
    if name.count(",") == 1 and "&" not in name and ";" not in name:
        last, first = [p.strip() for p in name.split(",")]
        if first and last and len(first.split()) <= 4:
            return f"{first} {last}"
    return name


def pull_brackets(s):
    """Split '[tr. Katz]' style notes out of a string."""
    notes = re.findall(r"\[([^\]]*)\]", s)
    s = re.sub(r"\s*\[[^\]]*\]", "", s).strip()
    return s, notes


def split_tail_paren(s):
    """'Title (Publisher, 2003)' -> ('Title', publisher, year)."""
    m = re.search(r"\s*\(([^()]*)\)\s*$", s)
    if not m:
        return s, "", None
    inner = m.group(1)
    ym = re.search(r"\b(1[4-9]\d\d|20\d\d)\b", inner)
    if not ym:
        return s, "", None
    year = int(ym.group(1))
    publisher = ""
    if "," in inner:
        pub = inner.rsplit(",", 1)[0].strip()
        if pub and not re.search(r"\d", pub) and pub.split()[0].lower() not in MONTHS:
            publisher = pub
    elif not re.search(r"\d", inner.replace(ym.group(1), "")) and inner.strip() != ym.group(1):
        pass
    return s[: m.start()].strip(), publisher, year


# ------------------------------------------------------------ filename parsing

UNSORTED = "Unsorted"     # books that aren't inside any collection folder
# Name of a top-level folder whose subfolders are author collections ("Carson, Anne",
# "Anne Carson - Poems (12 books)") holding files named "Last, First - Title (Publisher, Year)".
# Set from Preferences; empty means every folder is treated the same way.
COLLECTIONS_FOLDER = ""

GENRES = ["Fiction", "Poetry", "Drama", "Essays & Criticism", "Philosophy", "History",
          "Politics & Society", "Science", "Mind & Psychology", "Biography & Memoir", "Religion", "Art"]

# file tags (EPUB subjects) -> genre; first match wins, so the more specific come first
SUBJECT_RULES = [
    ("Poetry", r"poetry|poems|verse"),
    ("Drama", r"drama|plays\b|theat"),
    ("Essays & Criticism", r"literary criticism|criticism|essays"),
    ("Biography & Memoir", r"biograph|memoir|autobiograph|diaries|letters"),
    ("Fiction", r"fiction|novel|short stor|literary$|classics"),
    ("Philosophy", r"philosoph"),
    ("Mind & Psychology", r"psycholog|psychiatr|neuroscien"),
    ("Science", r"science|physics|mathemat|biolog|chemist|astronom|cosmolog"),
    ("History", r"histor"),
    ("Politics & Society", r"politic|social|sociolog|econom|feminis|current affairs"),
    ("Religion", r"religio|theolog|spiritual|buddhis|christian|islam"),
    ("Art", r"\bart\b|music|photograph|architect|painting"),
]


def genre_from_subjects(subjects):
    """One broad genre from a book's own (messy) subject tags, or '' if none fits."""
    tags = [t.strip().lower() for t in re.split(r"[,;/]", subjects or "") if t.strip()]
    for genre, pattern in SUBJECT_RULES:
        hits = [t for t in tags if re.search(pattern, t)]
        if genre == "Fiction":
            hits = [t for t in hits if not re.search(r"non-?\s?fiction", t)]
        if hits:
            return genre
    return ""


# occupation words (Wikidata) -> genre. A person's occupations are read in the order
# Wikidata lists them (main one first); the first that maps to a genre wins.
OCCUPATION_RULES = [
    ("Politics & Society", r"political scientist|social scientist|political|politician|economist|"
                           r"sociologist|journalist|activist|anthropologist|revolutionary|lawyer|diplomat|columnist"),
    ("Fiction", r"novelist|short story writer|science fiction writer|children's writer|fiction writer"),
    ("Poetry", r"\bpoet\b|poet$"),
    ("Drama", r"playwright|dramatist"),
    ("Philosophy", r"philosopher|logician"),
    ("Mind & Psychology", r"psycholog|psychiatr|psychoanalyst|neurolog|neuroscientist"),
    ("Science", r"physicist|mathematician|biologist|chemist|astronomer|astrophysicist|cosmologist|"
                r"computer scientist|naturalist|geologist|zoologist|botanist|scientist$|engineer|statistician|"
                r"science writer|science communicator|astrobiologist|exobiologist|science popularizer"),
    ("History", r"historian|archaeologist"),
    ("Religion", r"theologian|priest|monk|rabbi|religious"),
    ("Art", r"painter|artist|art historian|photographer|architect|composer|musician"),
    ("Essays & Criticism", r"essayist|literary critic|critic|literary scholar"),
    ("Biography & Memoir", r"biographer|memoirist|diarist"),
]
GENERIC_WRITER = r"^(writer|author|prose writer|novelist and writer)$"
# when occupations come unordered (the query service), the genre that ranks first here wins:
# literary work beats day jobs, so a poet-diplomat is still a poet
GENRE_PRIORITY = ["Fiction", "Poetry", "Drama", "Philosophy", "Mind & Psychology", "Science", "History",
                  "Religion", "Art", "Essays & Criticism", "Politics & Society", "Biography & Memoir"]
SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "Babel/1.2 (personal desktop book library)"


LITERARY = {"Fiction", "Poetry", "Drama"}


def genre_for_occupation_set(labels):
    """Every occupation votes for its genre (literary ones count double); ties go by priority.
    So an astronomer who wrote one novel is Science, and a poet-diplomat is still a poet."""
    score = {}
    for l in {x.lower().strip() for x in labels if x}:
        for genre, pattern in OCCUPATION_RULES:
            if re.search(pattern, l):
                score[genre] = score.get(genre, 0) + (2 if genre in LITERARY else 1)
                break
    if score:
        best = max(score.values())
        for g in GENRE_PRIORITY:
            if score.get(g) == best:
                return g
    if any(re.search(GENERIC_WRITER, l.lower().strip()) for l in labels if l):
        return "Fiction"
    return ""


def lookup_genres_fuzzy(names):
    """For names an exact match missed ('G. W. F. Hegel'): Wikidata's own name search,
    run inside the query service so a whole batch is still one request."""
    import urllib.parse
    import urllib.request
    esc = lambda n: n.replace("\\", "\\\\").replace('"', '\\"')
    values = " ".join(f'"{esc(n)}"' for n in names)
    query = f"""SELECT ?name ?p ?links ?occLabel WHERE {{
      VALUES ?name {{ {values} }}
      SERVICE wikibase:mwapi {{
        bd:serviceParam wikibase:endpoint "www.wikidata.org"; wikibase:api "EntitySearch";
                        mwapi:search ?name; mwapi:language "en"; mwapi:limit "3".
        ?p wikibase:apiOutputItem mwapi:item.
      }}
      ?p wdt:P31 wd:Q5 ; wdt:P106 ?occ ; wikibase:sitelinks ?links .
      ?occ rdfs:label ?occLabel . FILTER(LANG(?occLabel) = "en")
    }}"""
    return _run_people_query(query)


def _run_people_query(query):
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({"query": query, "format": "json"}).encode()
    for attempt in range(4):
        req = urllib.request.Request(SPARQL, data=data, headers={
            "User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                rows = json.load(r)["results"]["bindings"]
            break
        except Exception:
            time.sleep(10 * (attempt + 1))
    else:
        return None
    people = {}                                   # name -> {person: [sitelinks, occupations]}
    for b in rows:
        name, p = b["name"]["value"], b["p"]["value"]
        people.setdefault(name, {}).setdefault(p, [int(b["links"]["value"]), []])[1].append(b["occLabel"]["value"])
    out = {}
    for name, cands in people.items():           # several people share a name: take the best known
        links, occ = max(cands.values(), key=lambda v: v[0])
        out[name] = genre_for_occupation_set(occ)
    return out


def lookup_genres_batch(names):
    """Occupations for up to ~100 names in one Wikidata query -> {name: genre}."""
    import urllib.parse
    import urllib.request
    esc = lambda n: n.replace("\\", "\\\\").replace('"', '\\"')
    values = " ".join(f'"{esc(n)}"@en' for n in names)
    query = f"""SELECT ?name ?p ?links ?occLabel WHERE {{
      VALUES ?name {{ {values} }}
      ?p rdfs:label|skos:altLabel ?name ; wdt:P31 wd:Q5 ; wdt:P106 ?occ ; wikibase:sitelinks ?links .
      ?occ rdfs:label ?occLabel . FILTER(LANG(?occLabel) = "en")
    }}"""
    return _run_people_query(query)


def lookup_genres(db_path=None, progress=None, only_missing=True, workers=1):
    """Fill author_genre for every author in the library (only author names are sent)."""
    import time as _t
    con = connect(db_path)
    authors = [a for (a,) in con.execute("SELECT DISTINCT author FROM books WHERE author != '' "
                                         "AND kind IN ('Book', 'Paper')")]
    if only_missing:
        done = {a for (a,) in con.execute("SELECT author FROM author_genre WHERE checked IS NOT NULL")}
        authors = [a for a in authors if a not in done]
    con.close()
    results = {}
    step = 80
    for i in range(0, len(authors), step):
        batch = authors[i:i + step]
        got = lookup_genres_batch(batch)
        if got is not None:
            missed = [a for a in batch if not got.get(a)]
            for i2 in range(0, len(missed), 20):
                fz = lookup_genres_fuzzy(missed[i2:i2 + 20]) or {}
                got.update({a: g for a, g in fz.items() if g})
                _t.sleep(1)
            for a in batch:
                results[a] = got.get(a, "")       # "" = not found / unsure; it's remembered as checked
        if progress:
            progress(min(i + step, len(authors)), len(authors), batch[-1])
        _t.sleep(1)
    con = connect(db_path)
    now = _t.time()
    con.executemany("INSERT INTO author_genre(author, genre, source, checked) VALUES (?, ?, 'wikidata', ?) "
                    "ON CONFLICT(author) DO UPDATE SET genre=excluded.genre, source='wikidata', "
                    "checked=excluded.checked",
                    [(a, g, now) for a, g in results.items()])
    con.commit()
    con.close()
    return len(authors), sum(1 for g in results.values() if g)

PARTICLES = {"de", "da", "di", "del", "della", "der", "den", "van", "von", "la", "le", "du", "dos", "das",
             "bin", "ibn", "al", "el", "ten", "ter", "y"}
NOT_A_PERSON = re.compile(r"magazine|journal|quarterly|monthly|weekly|edition|collection|complete|"
                          r"\bbest\b|review|issues|series|anthology|library|scien|history|american\b|"
                          r"\bthe\b|\band\b|\bof\b|news|times|digest|guide|world", re.I)


SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def surname_first(name):
    """'Vincent van Gogh' -> 'van Gogh, Vincent'; 'Hugh Everett III' -> 'Everett, Hugh III'."""
    words = name.split()
    suffix = ""
    if len(words) > 2 and words[-1].lower() in SUFFIXES:
        suffix = " " + words.pop()
    i = len(words) - 1
    while i > 1 and words[i - 1].lower() in PARTICLES:
        i -= 1
    return " ".join(words[i:]) + ", " + " ".join(words[:i]) + suffix


def looks_like_person(name):
    words = name.split()
    if not 2 <= len(words) <= 5 or re.search(r"\d", name) or NOT_A_PERSON.search(name):
        return False
    return all(w[0].isupper() or w.lower() in PARTICLES or w[0] in "'\"(‘“" for w in words)


def normalize_collection(raw):
    """Folder name -> (display name, person name in 'Last, First' form or '').

    'Carson, Anne'                        -> ('Anne Carson', 'Carson, Anne')
    'Amichai, Yehuda - Collected Poetry'  -> ('Yehuda Amichai - Collected Poetry', 'Amichai, Yehuda')
    'Ian Hacking - On the Philosophy…'    -> unchanged, ('Hacking, Ian')
    """
    c = clean(raw)
    head, sep, rest = c.partition(" - ")
    note = re.search(r"\s*\([^)]*\)\s*$", head)            # 'Mahmoud Darwish (Palestine)'
    note = note.group(0) if note else ""
    h = head[: len(head) - len(note)].strip() if note else head.strip()
    if h.count(",") == 1 and not re.search(r"\d", h):
        last, first = [p.strip() for p in h.split(",")]
        if last and first and len((first + " " + last).split()) <= 6 and not NOT_A_PERSON.search(h):
            return f"{first} {last}{note}{sep}{rest}", f"{last}, {first}"
    if looks_like_person(h):
        return c, surname_first(h)
    return front_article(c), ""


def collection_sort_key(display, person, kind):
    """File person-named collections under the surname; magazines and comics keep their own name."""
    if person and kind not in ("Magazine", "Comic"):
        head, sep, rest = display.partition(" - ")
        return person + (sep + rest if sep else "")
    return display


def parse_collection_file(stem, rel_parts, ext):
    """Filenames like 'Bioy Casares, Adolfo - Invention of Morel (NYRB, 2003)'."""
    collection_raw = rel_parts[1] if len(rel_parts) > 2 else ""
    collection, coll_person = normalize_collection(collection_raw)
    coll_author = ""
    if coll_person:
        coll_author = flip_name(coll_person)
    elif " - " in collection:
        coll_author = flip_name(re.sub(r"\s*\([^)]*\)", "", collection.split(" - ")[0]))
    author_raw = ""

    folders = " / ".join(rel_parts[2:-1])
    is_magazine = bool(MAGAZINE_RE.search(collection_raw))
    is_comic = ext in ("cbr", "cbz")

    notes = []
    if is_magazine or is_comic:
        title, publisher, year = split_tail_paren(clean(stem))
        if not year:
            ym = re.search(r"\b(1[89]\d\d|20\d\d)\b", stem)
            year = int(ym.group(1)) if ym else None
        title = clean(stem)
        author = ""
        kind = "Comic" if is_comic else "Magazine"
    else:
        s = clean(stem)
        if " - " in s:
            author, rest = s.split(" - ", 1)
            author, a_notes = pull_brackets(author)
            author_raw = re.sub(r"\s*\([^)]*\)", "", author).strip()
            author = flip_name(author_raw)
            notes += a_notes
        else:
            author, rest = coll_author, s
            author_raw = coll_person
        rest, t_notes = pull_brackets(rest)
        rest = rest.strip(" -–")
        notes += t_notes
        title, publisher, year = split_tail_paren(rest)
        is_paper = (PAPER_DIR_RE.search(folders) or PAPER_NAME_RE.search(stem)
                    or (ext == "pdf" and " - " not in s and not publisher))
        kind = "Paper" if is_paper else "Book"
        if kind == "Book" and not publisher and ext == "pdf" and year and " - " in s \
                and PAPER_DIR_RE.search(folders):
            kind = "Paper"
    if not year:
        ym = re.findall(r"\b(1[5-9]\d\d|20\d\d)\b", stem)
        year = int(ym[-1]) if ym else None
    return dict(title=front_article(title or clean(stem)), author=author, year=year,
                publisher=publisher, collection=collection, coll_person=coll_person, kind=kind,
                author_raw=author_raw if author_raw.count(",") == 1 else "",
                notes="; ".join(n for n in notes if n))


def parse_loose(stem, rel_parts):
    """Other naming styles: 'Title -- Author -- … ' fields, or 'Author - Title-Publisher (Year)'."""
    if len(rel_parts) > 1:
        collection, coll_person = normalize_collection(rel_parts[0])
    else:
        collection, coll_person = UNSORTED, ""
    s = stem
    if " -- " in s:                                   # 'Title -- Author -- edition -- …' fields
        parts = [p.strip() for p in s.split(" -- ")]
        title = clean(parts[0])
        author = clean(parts[1]).split(";")[0] if len(parts) > 1 else ""
        author = re.sub(r"\(.*?\)", "", author).strip(" ,")
        year = None
        for p in parts[2:]:
            ym = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", p)
            if ym:
                year = int(ym.group(1))
                break
        publisher = parts[3] if len(parts) > 4 and not re.search(r"\d{6}", parts[3]) else ""
        if re.search(r"\b(19|20)\d\d\b", publisher):
            publisher = ""
        return dict(title=title, author=flip_name(author), year=year,
                    publisher=clean(publisher), collection=collection, coll_person=coll_person,
                    kind="Book", notes="")
    notes = []
    m = re.match(r"^\(([^)]*)\)\s*(.*)$", s)          # leading '(Series) ...'
    if m:
        notes.append(clean(m.group(1)))
        s = m.group(2)
    rest, publisher, year = split_tail_paren(s)
    author, title = "", rest
    if " - " in rest:
        author, title = rest.split(" - ", 1)
    # 'Title-Publisher' (no spaces around the last hyphen)
    m = re.match(r"^(.*\S)-([A-Z][^-]+)$", title)
    if m and len(m.group(2)) > 3:
        title, publisher = m.group(1), m.group(2)
    return dict(coll_person=coll_person, title=clean(title), author=clean(author).split(",")[0].strip(),
                year=year, publisher=clean(publisher), collection=collection,
                kind="Book", notes="; ".join(notes))


# ------------------------------------------------------------- file metadata

DC = "{http://purl.org/dc/elements/1.1/}"
OPF = "{http://www.idpf.org/2007/opf}"


def epub_info(path):
    """Return (metadata dict, cover image bytes or None) from an EPUB."""
    meta, cover = {}, None
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            opf_path = None
            if "META-INF/container.xml" in names:
                c = ET.fromstring(z.read("META-INF/container.xml"))
                rf = c.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
                if rf is not None:
                    opf_path = rf.get("full-path")
            if not opf_path:
                opf_path = next((n for n in names if n.endswith(".opf")), None)
            if not opf_path or opf_path not in names:
                return meta, None
            root = ET.fromstring(z.read(opf_path))
            md = root.find(f"{OPF}metadata")
            if md is None:
                md = root.find("metadata")

            def text(tag):
                if md is None:
                    return ""
                e = md.find(f"{DC}{tag}")
                return (e.text or "").strip() if e is not None and e.text else ""

            meta["title"] = text("title")
            meta["author"] = text("creator")
            meta["publisher"] = text("publisher")
            d = text("date")
            ym = re.search(r"(1[5-9]\d\d|20\d\d)", d)
            meta["year"] = int(ym.group(1)) if ym else None
            desc = text("description")
            desc = re.sub(r"<[^>]+>", " ", unescape(desc))
            meta["description"] = re.sub(r"\s+", " ", unescape(desc)).strip()
            if md is not None:
                subs = [e.text.strip() for e in md.findall(f"{DC}subject") if e.text]
                meta["subjects"] = ", ".join(dict.fromkeys(subs))

            # cover
            base = os.path.dirname(opf_path)
            manifest = root.find(f"{OPF}manifest")
            if manifest is None:
                manifest = root.find("manifest")
            items = list(manifest) if manifest is not None else []
            href = None
            cover_id = None
            if md is not None:
                for m in md.iter():
                    if m.tag.endswith("meta") and m.get("name") == "cover":
                        cover_id = m.get("content")
            img_items = [i for i in items if (i.get("media-type") or "").startswith("image/")]
            for i in items:
                if "cover-image" in (i.get("properties") or ""):
                    href = i.get("href")
            if not href and cover_id:
                for i in items:
                    if i.get("id") == cover_id:
                        href = i.get("href")
            if not href:
                for i in img_items:
                    if "cover" in (i.get("id", "") + i.get("href", "")).lower():
                        href = i.get("href")
                        break
            if not href and img_items:
                href = img_items[0].get("href")
            if href:
                from urllib.parse import unquote
                p = os.path.normpath(os.path.join(base, unquote(href))).replace("\\", "/")
                if p in names:
                    cover = z.read(p)
    except Exception:
        pass
    return meta, cover


def ebook_meta(path, cover_out):
    """Calibre's ebook-meta for MOBI/AZW3: metadata + cover."""
    meta = {}
    try:
        r = subprocess.run(["ebook-meta", path, f"--get-cover={cover_out}"],
                           capture_output=True, text=True, timeout=60)
        for line in r.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k == "title":
                    meta["title"] = v
                elif k.startswith("author"):
                    meta["author"] = re.sub(r"\s*\[.*\]$", "", v)
                elif k == "publisher":
                    meta["publisher"] = v
                elif k == "published":
                    ym = re.search(r"(1[5-9]\d\d|20\d\d)", v)
                    meta["year"] = int(ym.group(1)) if ym else None
                elif k == "comments":
                    meta["description"] = re.sub(r"<[^>]+>", " ", v).strip()
    except Exception:
        pass
    return meta


def pdf_cover(path):
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "p")
        try:
            subprocess.run(["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-jpeg",
                            "-scale-to", "480", path, out],
                           capture_output=True, timeout=60)
            with open(out + ".jpg", "rb") as f:
                return f.read()
        except Exception:
            return None


def comic_cover(path):
    try:
        r = subprocess.run(["bsdtar", "-tf", path], capture_output=True, text=True, timeout=60)
        imgs = sorted(n for n in r.stdout.splitlines()
                      if n.rsplit(".", 1)[-1].lower() in IMAGE_EXTS)
        if not imgs:
            return None
        r = subprocess.run(["bsdtar", "-xOf", path, imgs[0]], capture_output=True, timeout=60)
        return r.stdout or None
    except Exception:
        return None


def save_thumb(data_or_path, key):
    out = os.path.join(COVERS_DIR, hashlib.sha1(key.encode()).hexdigest() + ".jpg")
    try:
        src = BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else data_or_path
        with Image.open(src) as im:
            im.draft("RGB", (THUMB_SIZE[0] * 2, THUMB_SIZE[1] * 2))
            im = im.convert("RGB")
            if im.width < 20 or im.height < 20:
                return ""
            im.thumbnail(THUMB_SIZE, Image.LANCZOS)
            im.save(out, "JPEG", quality=85, optimize=True)
        return out
    except Exception:
        return ""


# ------------------------------------------------------------------ the scan

def discover(root, recursive=True):
    """Yield (key, files, cover_image_path_or_None, dir) for every book group."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")] if recursive else []
        groups, images = {}, {}
        for fn in filenames:
            if "." not in fn:
                continue
            stem, ext = fn.rsplit(".", 1)
            ext = ext.lower()
            if ext in BOOK_EXTS:
                groups.setdefault(stem, []).append(os.path.join(dirpath, fn))
            elif ext in IMAGE_EXTS:
                images[stem] = os.path.join(dirpath, fn)
        for stem, files in groups.items():
            img = images.get(stem)
            if not img and len(groups) == 1 and len(images) == 1:
                img = next(iter(images.values()))
            if not img:   # 'Title (Pub, 2003).jpg' next to 'Title (Pub, 2003) - copy.pdf' etc.
                for istem, ipath in images.items():
                    if istem.startswith(stem[:40]) or stem.startswith(istem[:40]):
                        img = ipath
                        break
            key = os.path.join(dirpath, stem)
            yield key, sorted(files, key=lambda p: FORMAT_ORDER.index(p.rsplit(".", 1)[1].lower())), img, dirpath


def signature(files, img):
    parts = []
    for p in files + ([img] if img else []):
        try:
            st = os.stat(p)
            parts.append(f"{p}|{st.st_size}|{int(st.st_mtime)}")
        except OSError:
            pass
    parts.append(PARSER_VERSION)
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def build_record(root, key, files, img, dirpath):
    parts = files[0].split(os.sep)
    cf = COLLECTIONS_FOLDER
    if cf and cf in parts:            # works whether root is the books folder or the collections folder
        rel = parts[parts.index(cf):]
    else:
        rel = os.path.relpath(files[0], root).split(os.sep)
    stem = os.path.basename(key)
    exts = [p.rsplit(".", 1)[1].lower() for p in files]
    if cf and rel[0] == cf and len(rel) <= 2:
        # loose in the collections folder itself, not in a collection: some other naming style
        rec = parse_loose(stem, rel[-1:])
        trust_embedded = True
    elif cf and rel[0] == cf:
        rec = parse_collection_file(stem, rel, exts[0])
        trust_embedded = False
    else:
        rec = parse_loose(stem, rel)
        trust_embedded = True

    meta, cover_bytes, cover_path = {}, None, None
    for p, ext in zip(files, exts):
        if ext == "epub":
            meta, cover_bytes = epub_info(p)
            break
    if not meta and not img:
        for p, ext in zip(files, exts):
            if ext in ("mobi", "azw3", "azw"):
                with tempfile.TemporaryDirectory() as td:
                    co = os.path.join(td, "c.jpg")
                    meta = ebook_meta(p, co)
                    if os.path.exists(co):
                        with open(co, "rb") as f:
                            cover_bytes = f.read()
                break

    # Fill gaps (or, for loose files, prefer embedded metadata)
    for k in ("title", "author", "publisher", "year"):
        v = meta.get(k)
        if v and (trust_embedded or not rec.get(k)) and str(v).lower() not in ("unknown", "untitled"):
            if k == "author" and v.count(",") >= 2:
                v = v.split(",")[0]          # 'Plato, Cooper, John M., …' lists several people: keep the first
            rec[k] = flip_name(v) if k == "author" else v
    rec["description"] = meta.get("description", "")
    rec["subjects"] = meta.get("subjects", "")

    # Cover: sidecar image > embedded > rendered first page
    thumb = ""
    existing = os.path.join(COVERS_DIR, hashlib.sha1(key.encode()).hexdigest() + ".jpg")
    if os.path.exists(existing) and not REBUILD_COVERS:
        thumb = existing
    if not thumb and img:
        thumb = save_thumb(img, key)
    if not thumb and cover_bytes:
        thumb = save_thumb(cover_bytes, key)
    if not thumb:
        for p, ext in zip(files, exts):
            data = None
            if ext == "pdf":
                data = pdf_cover(p)
            elif ext in ("cbr", "cbz"):
                data = comic_cover(p)
            if data:
                thumb = save_thumb(data, key)
                break

    size = 0
    for p in files:
        try:
            size += os.path.getsize(p)
        except OSError:
            pass
    author = rec.get("author") or ""
    a_sort = author
    if rec.get("author_raw") and flip_name(rec["author_raw"]) == author:
        a_sort = rec["author_raw"]                 # filename already had 'Bioy Casares, Adolfo'
    elif author and "," not in author and " " in author:
        a_sort = surname_first(author)
    rec["collection_sort"] = collection_sort_key(rec["collection"], rec.get("coll_person", ""), rec.get("kind"))
    rec["genre"] = genre_from_subjects(rec.get("subjects", ""))
    rec.update(key=key, author=author, author_sort=a_sort, cover=thumb,
               formats=",".join(dict.fromkeys(e.upper() for e in exts)),
               files=json.dumps(files), dir=dirpath, size=size)
    return rec


COLS = ["key", "title", "author", "author_sort", "year", "publisher", "collection", "collection_sort",
        "kind", "genre", "notes", "description", "subjects", "cover", "formats", "files",
        "dir", "size", "sig", "added"]


def scan(root=None, db_path=None, progress=None, workers=4, full=False, only=None,
         shallow=None):
    """Scan the library. progress(done, total, message) is called as it goes.

    With `only` (a list of folders inside root), just those subtrees are
    re-checked — used when the folder watcher sees something change. `shallow`
    folders are checked for their own files only, not their subfolders.
    """
    root = root or BOOKS_ROOT
    db_path = db_path or DB_PATH
    if not root or not os.path.isdir(root):   # no folder / drive not mounted: change nothing
        return 0, 0, 0
    os.makedirs(COVERS_DIR, exist_ok=True)
    con = connect(db_path)
    existing = dict(con.execute("SELECT key, sig FROM books"))
    if progress:
        progress(0, 0, "Finding books…")
    if only is not None or shallow is not None:
        # drop folders nested inside another folder in the list
        dirs = sorted({os.path.normpath(d) for d in only or []})
        dirs = [d for d in dirs if not any(d.startswith(o + os.sep) for o in dirs if o != d)]
        flat = {os.path.normpath(d) for d in shallow or []}
        found = [g for d in dirs if os.path.isdir(d) for g in discover(d)]
        found += [g for d in flat if os.path.isdir(d) for g in discover(d, recursive=False)]
        existing_in_scope = {k: v for k, v in existing.items()
                             if any(k.startswith(d + os.sep) for d in dirs)
                             or os.path.dirname(k) in flat}
    else:
        found = list(discover(root))
        existing_in_scope = existing
        if not found and existing:       # looks unmounted/empty: don't wipe the library
            con.close()
            return 0, 0, 0
    seen = set()
    todo = []
    for key, files, img, d in found:
        seen.add(key)
        sig = signature(files, img)
        if full or existing.get(key) != sig:
            todo.append((key, files, img, d, sig))

    removed = set(existing_in_scope) - seen
    if removed:
        con.executemany("DELETE FROM books WHERE key=?", [(k,) for k in removed])

    total, done, now = len(todo), 0, time.time()
    if progress:
        progress(0, total, f"{len(found):,} books found, {total:,} to process")
    batch = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(build_record, root, k, f, i, d): (k, s) for k, f, i, d, s in todo}
        for fut in as_completed(futs):
            key, sig = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:
                print("failed:", key, e, file=sys.stderr)
                done += 1
                continue
            rec["sig"] = sig
            rec["added"] = now
            batch.append(rec)
            done += 1
            if len(batch) >= 200:
                _write(con, batch)
                batch = []
            if progress and (done % 25 == 0 or done == total):
                progress(done, total, rec.get("title", ""))
    _write(con, batch)

    # rebuild the search index (fast; ~16k rows)
    con.execute("DELETE FROM books_fts")
    con.execute("""INSERT INTO books_fts(key, title, author, collection, publisher, subjects, notes, description)
                   SELECT key, title, author, collection, publisher, subjects, notes, description FROM books""")
    con.commit()
    con.close()
    if progress:
        progress(total, total, "done")
    return len(found), total, len(removed)


def is_book_file(name):
    return "." in name and name.rsplit(".", 1)[1].lower() in BOOK_EXTS


def _write(con, batch):
    if not batch:
        return
    sql = (f"INSERT INTO books ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))}) "
           f"ON CONFLICT(key) DO UPDATE SET " +
           ",".join(f"{c}=excluded.{c}" for c in COLS if c not in ("key", "added")))
    con.executemany(sql, [[r.get(c) for c in COLS] for r in batch])
    con.commit()


if __name__ == "__main__":
    def cli_progress(done, total, msg):
        print(f"\r[{done}/{total}] {msg[:70]:<70}", end="", flush=True)
    t = time.time()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        BOOKS_ROOT = os.path.abspath(args[0])
    else:   # fall back to the folder chosen in Babel
        from PyQt6.QtCore import QSettings
        BOOKS_ROOT = QSettings("Babel", "Babel").value("prefs/library_root", "") or ""
        COLLECTIONS_FOLDER = QSettings("Babel", "Babel").value("prefs/collections_folder", "") or ""
    if not os.path.isdir(BOOKS_ROOT):
        sys.exit("usage: scanner.py [books folder] [--full]")
    n, processed, removed = scan(root=BOOKS_ROOT, progress=cli_progress, full="--full" in sys.argv)
    print(f"\n{n} books, {processed} processed, {removed} removed in {time.time() - t:.0f}s")


# ------------------------------------------------------------ exact duplicates
# Byte-for-byte identical book files. Nothing here deletes anything: duplicates
# are moved to the Trash, so they can be restored.

def _sha256(path, partial=False):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if partial:                       # first and last 64 KB: a cheap first pass
            h.update(f.read(65536))
            try:
                f.seek(-65536, os.SEEK_END)
            except OSError:
                f.seek(0)
            h.update(f.read(65536))
        else:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def _collection_dir(root, path):
    """Top folder a file belongs to (inside the collections folder, the folder below it)."""
    parts = os.path.relpath(path, root).split(os.sep)
    if COLLECTIONS_FOLDER and parts[0] == COLLECTIONS_FOLDER and len(parts) > 2:
        return os.path.join(root, parts[0], parts[1])
    return os.path.join(root, parts[0]) if len(parts) > 1 else root


def find_exact_duplicates(root, progress=None):
    """Plan which identical copies to remove: [{'keep', 'drop', 'size'}]. Read-only."""
    by_size, coll_size = {}, {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if is_book_file(fn):
                p = os.path.join(dirpath, fn)
                try:
                    by_size.setdefault(os.path.getsize(p), []).append(p)
                except OSError:
                    continue
                c = _collection_dir(root, p)
                coll_size[c] = coll_size.get(c, 0) + 1
    candidates = [ps for size, ps in by_size.items() if size > 0 and len(ps) > 1]
    groups = []
    for i, paths in enumerate(candidates):
        if progress:
            progress(i, len(candidates))
        quick = {}
        for p in paths:
            try:
                quick.setdefault(_sha256(p, partial=True), []).append(p)
            except OSError:
                pass
        for same_quick in quick.values():
            if len(same_quick) < 2:
                continue
            full = {}
            for p in same_quick:
                try:
                    full.setdefault(_sha256(p), []).append(p)
                except OSError:
                    pass
            groups += [g for g in full.values() if len(g) > 1]

    def nested(p):
        parts = os.path.relpath(p, root).split(os.sep)[:-1]
        return len(parts) != len(set(parts))          # a folder copied inside itself

    def keep_score(p):
        return (bool(COLLECTIONS_FOLDER) and os.path.relpath(p, root).startswith(COLLECTIONS_FOLDER + os.sep),
                coll_size.get(_collection_dir(root, p), 0),   # fuller collection keeps its copy
                not nested(p), -p.count(os.sep), -len(p))

    plan = []
    for g in groups:
        ranked = sorted(g, key=keep_score, reverse=True)
        plan += [{"keep": ranked[0], "drop": d, "size": os.path.getsize(d)} for d in ranked[1:]]
    if progress:
        progress(len(candidates), len(candidates))
    return plan


def move_to_trash(paths):
    """Move files/folders to the desktop Trash. Returns the paths that were moved."""
    moved = []
    for i in range(0, len(paths), 100):
        batch = [p for p in paths[i:i + 100] if os.path.exists(p)]
        if not batch:
            continue
        if shutil.which("gio"):
            subprocess.run(["gio", "trash", "--"] + batch, capture_output=True)
        moved += [p for p in batch if not os.path.exists(p)]
    return moved


def apply_duplicate_plan(root, plan, db_path=None):
    """Carry favourites/history to the kept copy, then trash the duplicates, their
    now-orphaned covers and any folders left with nothing but covers/notes."""
    con = connect(db_path)
    key = lambda p: p.rsplit(".", 1)[0]
    for item in plan:
        row = con.execute("SELECT fav, status, last_opened, open_count FROM user WHERE key=?",
                          (key(item["drop"]),)).fetchone()
        if row:
            con.execute("INSERT OR IGNORE INTO user(key) VALUES (?)", (key(item["keep"]),))
            con.execute("UPDATE user SET fav=max(fav, ?), status=CASE WHEN status='' THEN ? ELSE status END, "
                        "last_opened=max(coalesce(last_opened, 0), coalesce(?, 0)), "
                        "open_count=open_count+? WHERE key=?", row + (key(item["keep"]),))
    con.commit()
    con.close()

    # only trash a copy if the kept one is still there and still the same size
    safe = [i["drop"] for i in plan
            if os.path.isfile(i["keep"]) and os.path.isfile(i["drop"])
            and os.path.getsize(i["keep"]) == os.path.getsize(i["drop"]) == i["size"]]
    moved = move_to_trash(safe)

    covers, dirs = [], set()
    for d in moved:
        dp, stem = os.path.dirname(d), os.path.basename(d).rsplit(".", 1)[0]
        dirs.add(dp)
        try:
            names = os.listdir(dp)
        except OSError:
            continue
        if not any(n.rsplit(".", 1)[0] == stem and is_book_file(n) for n in names):
            covers += [os.path.join(dp, n) for n in names
                       if n.rsplit(".", 1)[0] == stem and n.rsplit(".", 1)[-1].lower() in IMAGE_EXTS]
    moved_covers = move_to_trash(covers)

    def only_leftovers(d):
        for _, _, fns in os.walk(d):
            for f in fns:
                ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
                notes = f.startswith("_") and ext == "txt"          # download notes, not books
                if not notes and ext not in IMAGE_EXTS | {"opf", "nfo", "url"}:
                    return False
        return True
    up = set()
    for d in dirs:
        while d.startswith(root + os.sep):
            up.add(d)
            d = os.path.dirname(d)
    empty = [d for d in sorted(up, key=lambda p: p.count(os.sep))
             if os.path.isdir(d) and only_leftovers(d)]
    # outermost first, skipping folders inside one already being removed
    empty = [d for d in empty if not any(d.startswith(o + os.sep) for o in empty if o != d)]
    moved_dirs = move_to_trash(empty)
    return len(moved), sum(i["size"] for i in plan if i["drop"] in set(moved)), len(moved_covers), len(moved_dirs)
