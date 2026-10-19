#!/usr/bin/env python3
"""Babel — a desktop library for the Books folder."""

import json
import os
import random
import re
import shutil
import hashlib
import tempfile
import unicodedata
import sqlite3
import subprocess
import sys
import time
from collections import OrderedDict

from PyQt6.QtCore import (QSizeF, QStorageInfo, QAbstractListModel, QEvent, QFileSystemWatcher, QPoint, QModelIndex, QObject, QRect, QRectF,
                          QRunnable, QSettings, QSize, Qt, QThread, QThreadPool,
                          QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QFont,
                         QFontMetrics, QIcon, QImage, QKeySequence, QLinearGradient,
                         QPainter, QPainterPath, QPen, QPixmap, QShortcut, QTextOption)
from PyQt6.QtWidgets import (QStyleOptionViewItem, QAbstractButton, QDialog, QFileDialog, QMenu, QMessageBox,
                             QStackedWidget, QToolButton, QAbstractItemView, QApplication, QComboBox, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListView,
                             QListWidget, QListWidgetItem, QMainWindow, QProgressBar,
                             QPushButton, QScrollArea, QSizePolicy, QSplitter, QStatusBar,
                             QStyle, QStyledItemDelegate, QTabWidget, QVBoxLayout,
                             QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner  # noqa: E402

FROZEN = getattr(sys, "frozen", False)
# assets live next to the script, or inside the bundle when running as a single binary
APP_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
ICON_PATH = os.path.join(APP_DIR, "babel.svg")
CHEVRON = os.path.join(APP_DIR, "chevron-down.svg")
CLOSE_ICON = os.path.join(APP_DIR, "window-close.svg")
MENU_ICON = os.path.join(APP_DIR, "open-menu.svg")
DICE_ICON = os.path.join(APP_DIR, "dice.svg")
_THEMED = []      # (widget, icon file) pairs to recolour when the theme changes


def themed_icon_path(path):
    """The icon file recoloured for the current theme (cached in Babel's data folder)."""
    pal = PALETTES[THEME]
    tag = hashlib.sha1(f"{pal['TEXT']}{pal['MUTED']}{pal['ACCENT_TEXT']}".encode()).hexdigest()[:8]
    out_dir = os.path.join(scanner.DATA_DIR, "theme")
    out = os.path.join(out_dir, f"{THEME}-{tag}-{os.path.basename(path)}")
    if not os.path.exists(out):
        os.makedirs(out_dir, exist_ok=True)
        svg = open(path).read()
        for dark, role in (("#ece6dc", "TEXT"), ("#9a9189", "MUTED"), ("#e3ab52", "ACCENT_TEXT")):
            svg = svg.replace(dark, PALETTES[THEME][role])
        with open(out, "w") as f:
            f.write(svg)
    return out


def themed_icon(widget, path):
    widget.setIcon(QIcon(themed_icon_path(path)))
    _THEMED.append((widget, path))
REFRESH_ICON = os.path.join(APP_DIR, "refresh.svg")
VERSION = "1.3.1"
WINDOW_RADIUS = 12      # GNOME's window corner radius
CHEVRON_ACCENT = os.path.join(APP_DIR, "chevron-down-accent.svg")

# ------------------------------------------------------------------- palette
# Two palettes, swapped at runtime (see apply_theme). Code reads these globals
# when it paints, so changing them and restyling is enough to switch modes.
PALETTES = {
    # Close to Claude's own interface (warm charcoal / ivory). Accent: a soft amber for
    # large areas (buttons, switches), the icon's bright amber for small highlights
    "dark": dict(
        BG="#262624", PANEL="#1f1e1d", PANEL_2="#30302e", SELECT="#3a3936",
        LINE="#3e3e38", LINE_STRONG="#55544e",
        TEXT="#faf9f5", MUTED="#9c9a92", DESC="#c2c0b6",
        ACCENT="#e3ab52", ACCENT_TEXT="#e8b562", ACCENT_HOVER="#efbc69", ACCENT_DOWN="#d39a42",
        ACCENT_BRIGHT="#ffb300",
        ON_ACCENT="#1f1a10", TEAL="#8fc7b8", DANGER_TEXT="#ff8a7a", WASH=255),
    "light": dict(
        BG="#faf9f5", PANEL="#f5f4ed", PANEL_2="#ffffff", SELECT="#ece9df",
        LINE="#e6e3d9", LINE_STRONG="#cfccc1",
        TEXT="#141413", MUTED="#73726c", DESC="#3d3d3a",
        ACCENT="#e3ab52", ACCENT_TEXT="#9a6200", ACCENT_HOVER="#efbc69", ACCENT_DOWN="#d39a42",
        ACCENT_BRIGHT="#ffb300",
        ON_ACCENT="#1f1a10", TEAL="#2f7d6a", DANGER_TEXT="#b3261e", WASH=0),
}
THEME = "dark"


def wanted_theme():
    """'light' or 'dark', from the Style preference or GNOME's own setting."""
    choice = prefs().get("theme") if _PREFS is not None or QApplication.instance() else "system"
    if choice in ("light", "dark"):
        return choice
    app = QApplication.instance()
    if app is not None and app.styleHints().colorScheme() == Qt.ColorScheme.Light:
        return "light"
    return "dark"


def set_palette(name):
    global THEME
    THEME = name
    globals().update(PALETTES[name])


set_palette("dark")


def wash(a):
    """A see-through highlight: white on dark, black on light."""
    return QColor(WASH, WASH, WASH, int(a * 255))
SERIF = "Noto Serif"

CARD_W, CARD_H = 168, 300
COVER_W, COVER_H = 148, 222
COVER_SIZES = {            # card w, card h, cover w, cover h
    "small": (136, 252, 118, 177),
    "medium": (168, 300, 148, 222),
    "large": (204, 358, 182, 273),
}


def apply_cover_size(name):
    global CARD_W, CARD_H, COVER_W, COVER_H
    CARD_W, CARD_H, COVER_W, COVER_H = COVER_SIZES.get(name, COVER_SIZES["medium"])


class Prefs(QObject):
    """User preferences, stored with QSettings. `changed` fires with the key."""
    changed = pyqtSignal(str)
    DEFAULTS = {
        "start_view": "kind:Book",
        "default_sort": "relevance",
        "library_root": "",
        "collections_folder": "",   # top-level folder of "Last, First" author collections
        "watch": True,
        "scan_on_start": True,
        "reader_epub": "foliate",
        # GNOME's Document Viewer: Papers on current GNOME, Evince on older releases
        "reader_pdf": "papers" if shutil.which("papers") else "evince" if shutil.which("evince") else "default",
        "reader_kindle": "default",
        "reader_comic": "default",
        "prefer_format": "epub",
        "cover_size": "medium",
        "show_format_badges": True,
        "show_type_badges": True,
        "merge_duplicates": True,
        "theme": "system",          # system / light / dark
        "auto_genres": True,        # look up genres for new authors (sends author names to Wikidata)
        "kobo_kepub": True,
        "kobo_convert_kindle": True,
    }

    def __init__(self):
        super().__init__()
        self.s = QSettings("Babel", "Babel")

    def get(self, key):
        d = self.DEFAULTS[key]
        v = self.s.value("prefs/" + key, d)
        if isinstance(d, bool):
            return v in (True, "true", "1", 1)
        return v

    def set(self, key, value):
        if self.get(key) == value:
            return
        self.s.setValue("prefs/" + key, value)
        self.changed.emit(key)


_PREFS = None


def prefs():
    global _PREFS
    if _PREFS is None:
        _PREFS = Prefs()
    return _PREFS

KINDS = ["Book", "Paper", "Magazine", "Comic"]

STATUSES = [("", "Not started"), ("want", "Want to Read"),
            ("reading", "Reading"), ("finished", "Finished")]
STATUS_LABEL = dict(STATUSES)

ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.I)


def title_key(t):
    return ARTICLE_RE.sub("", (t or "").lower()).strip("\"'“‘(")


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------- data layer

class Library:
    # Genre, best source first: your own choice for this book, your choice for the author,
    # the book's own tags, the author's occupation (Wikidata), then the collection's person.
    COLL_HEAD = ("CASE WHEN instr(b.collection, ' - ') > 0 "
                 "THEN substr(b.collection, 1, instr(b.collection, ' - ') - 1) ELSE b.collection END")
    GENRE = ("COALESCE(NULLIF(u.genre, ''), NULLIF(ag.user_genre, ''), NULLIF(b.genre, ''), "
             "NULLIF(ag.genre, ''), NULLIF(ac.user_genre, ''), NULLIF(ac.genre, ''), '')")
    GENRE_JOINS = (" LEFT JOIN author_genre ag ON ag.author = b.author"
                   " LEFT JOIN author_genre ac ON ac.author = " + COLL_HEAD)
    # a type you set yourself (user.kind) wins over Babel's guess (books.kind)
    LIST_COLS = ("b.key, b.title, b.author, b.author_sort, b.year, b.collection, COALESCE(NULLIF(u.kind, ''), b.kind) AS kind, "
                 "b.formats, b.cover, b.added, COALESCE(u.fav,0) AS fav, "
                 "COALESCE(u.status,'') AS status, u.last_opened, " + GENRE + " AS genre")

    def __init__(self, path=None):
        self.con = scanner.connect(path or scanner.DB_PATH)
        self.con.row_factory = sqlite3.Row

    def count(self):
        return self.con.execute("SELECT COUNT(*) FROM books").fetchone()[0]

    @staticmethod
    def fts_query(text):
        toks = re.findall(r"\w+", text, re.UNICODE)
        return " ".join(f'"{t}"*' for t in toks)

    def query(self, search="", shelf="all", kind=None, fmt=None, collection=None, author=None,
              merge=True, genre=None):
        where, args = [], []
        join = ""
        if search and self.fts_query(search):
            join = ("JOIN (SELECT key, bm25(books_fts, 0, 10, 7, 3, 1, 2, 2, 0.5) AS rank "
                    "FROM books_fts WHERE books_fts MATCH ?) f ON f.key = b.key")
            args.append(self.fts_query(search))
        if shelf == "fav":
            where.append("u.fav = 1")
        elif shelf in ("want", "reading", "finished"):
            where.append("u.status = ?")
            args.append(shelf)
        elif shelf == "opened":
            where.append("u.last_opened IS NOT NULL")
        elif shelf == "added":
            where.append("b.added > (SELECT MIN(added) FROM books) + 600")
        if kind:
            where.append("COALESCE(NULLIF(u.kind, ''), b.kind) = ?")
            args.append(kind)
        if fmt:
            where.append("(" + " OR ".join("b.formats LIKE ?" for _ in fmt) + ")")
            args += [f"%{f}%" for f in fmt]
        if collection:
            where.append("b.collection = ?")
            args.append(collection)
        if author:
            where.append("b.author = ?")
            args.append(author)
        if genre:
            where.append(self.GENRE + " = ?")
            args.append(genre)
        rank = ", f.rank" if join else ", 0 AS rank"
        sql = (f"SELECT {self.LIST_COLS}{rank} FROM books b LEFT JOIN user u ON u.key = b.key "
               f"{self.GENRE_JOINS} {join} {'WHERE ' + ' AND '.join(where) if where else ''}")
        try:
            rows = [dict(r) for r in self.con.execute(sql, args)]
        except sqlite3.OperationalError:
            return []
        if join:
            # which matches are by title, author or collection; the rest only mention it
            primary = {k for (k,) in self.con.execute(
                "SELECT key FROM books_fts WHERE books_fts MATCH ?",
                ("{title author collection} : (" + self.fts_query(search) + ")",))}
            for r in rows:
                r["via_desc"] = r["key"] not in primary
        if not merge and shelf != "dups":
            for r in rows:
                r["copies"] = 1
            return rows
        out = self.merge(rows)
        if shelf == "dups":
            out = [r for r in out if r["copies"] > 1]
        return out

    @staticmethod
    def dedupe_key(r):
        return (title_key(r["title"]), (r["author"] or "").lower(), r["year"], r["formats"])

    def merge(self, rows):
        """The same book can sit in several folders: keep one row per book,
        preferring the copy that carries favourite/status/history."""
        rows = sorted(rows, key=lambda r: (not (r["fav"] or r["status"] or r["last_opened"]), len(r["key"])))
        seen, out = {}, []
        for r in rows:
            k = self.dedupe_key(r)
            if k in seen:
                seen[k]["copies"] += 1
                continue
            r = dict(r)
            r["copies"] = 1
            seen[k] = r
            out.append(r)
        return out

    def duplicate_stats(self):
        """(extra copies, bytes they take up)."""
        groups = {}
        for r in self.con.execute("SELECT title, author, year, formats, size FROM books"):
            k = (title_key(r[0]), (r[1] or "").lower(), r[2], r[3])
            groups.setdefault(k, []).append(r[4] or 0)
        extra = [sorted(sizes)[:-1] for sizes in groups.values() if len(sizes) > 1]
        return sum(len(e) for e in extra), sum(sum(e) for e in extra)

    def unique_count(self):
        return len({(title_key(r[0]), (r[1] or "").lower(), r[2], r[3]) for r in
                    self.con.execute("SELECT title, author, year, formats FROM books")})

    def clear_history(self):
        self.con.execute("UPDATE user SET last_opened = NULL, open_count = 0")
        self.con.commit()

    def match_snippet(self, key, search):
        """The bit of text that made a book match, with the match in «»."""
        q = self.fts_query(search)
        if not q:
            return ""
        r = self.con.execute("SELECT snippet(books_fts, -1, '«', '»', '…', 14) FROM books_fts "
                             "WHERE books_fts MATCH ? AND key = ?", (q, key)).fetchone()
        return r[0] if r else ""

    def copies(self, b):
        """Other locations of the same book."""
        return [dict(r) for r in self.con.execute(
            "SELECT key, collection, files FROM books WHERE key != ? AND title = ? "
            "AND author = ? AND year IS ? AND formats = ?",
            (b["key"], b["title"], b["author"], b["year"], b["formats"]))]

    def book(self, key):
        r = self.con.execute(
            "SELECT b.*, u.kind AS kind_override, " + self.GENRE + " AS genre_now, u.genre AS genre_override, "
            "COALESCE(u.fav,0) AS fav, COALESCE(u.status,'') AS status, "
            "u.last_opened, COALESCE(u.open_count,0) AS open_count "
            "FROM books b LEFT JOIN user u ON u.key=b.key" + self.GENRE_JOINS + " WHERE b.key=?", (key,)).fetchone()
        if not r:
            return None
        d = dict(r)
        if d.get("kind_override"):
            d["kind"] = d["kind_override"]      # your own choice wins over Babel's guess
        d["genre"] = d.pop("genre_now") or ""
        return d

    def collections(self):
        """[(collection, count)] in shelf order: people by surname, Unsorted last."""
        sort_votes, counts = {}, {}
        for coll, key, n in self.con.execute(
                "SELECT collection, COALESCE(collection_sort, collection), COUNT(*) FROM books "
                "GROUP BY collection, collection_sort"):
            counts[coll] = counts.get(coll, 0) + n
            votes = sort_votes.setdefault(coll, {})
            votes[key] = votes.get(key, 0) + n

        def order(coll):
            # if any book is by the person the folder is named after, file it under their surname
            person_keys = [k for k in sort_votes[coll] if k != coll]
            key = max(person_keys, key=sort_votes[coll].get) if person_keys else coll
            plain = unicodedata.normalize("NFKD", key).encode("ascii", "ignore").decode().casefold()
            return (coll == scanner.UNSORTED, ARTICLE_RE.sub("", plain))
        return [(c, counts[c]) for c in sorted(counts, key=order)]

    def authors(self):
        return self.con.execute(
            "SELECT author, MIN(author_sort), COUNT(*) FROM books WHERE author != '' "
            "GROUP BY author ORDER BY MIN(author_sort) COLLATE NOCASE").fetchall()

    def counts(self, merge=True):
        """Sidebar numbers, counted exactly the way the grid shows them."""
        raw = self.query(merge=False)
        if raw:
            first_added = min(r["added"] or 0 for r in raw)
        n = (lambda rows: len(self.merge(rows))) if merge else len
        c = {"all": n(raw),
             "fav": n([r for r in raw if r["fav"]]),
             "opened": n([r for r in raw if r["last_opened"]]),
             "added": n([r for r in raw if (r["added"] or 0) > first_added + 600]) if raw else 0}
        for s in ("want", "reading", "finished"):
            c[s] = n([r for r in raw if r["status"] == s])
        for k in {r["kind"] for r in raw}:
            c["kind:" + k] = n([r for r in raw if r["kind"] == k])
        for g in scanner.GENRES:
            c["genre:" + g] = n([r for r in raw if r["genre"] == g])
        for f in ("EPUB", "PDF", "MOBI", "AZW3", "CBR", "CBZ", "DJVU"):
            c["fmt:" + f] = n([r for r in raw if f in (r["formats"] or "")])
        # one combined count for the Kindle row, so a MOBI+AZW3 book isn't counted twice
        c["fmt:MOBI,AZW3"] = n([r for r in raw if "MOBI" in (r["formats"] or "") or "AZW3" in (r["formats"] or "")])
        # collections and authors: a book counts once per collection/author it appears in
        coll, auth = {}, {}
        for r in raw:
            k = self.dedupe_key(r) if merge else r["key"]
            coll.setdefault(r["collection"], set()).add(k)
            if r["author"]:
                auth.setdefault(r["author"], set()).add(k)
        self.coll_counts = {c_: len(v) for c_, v in coll.items()}
        self.auth_counts = {a: len(v) for a, v in auth.items()}
        return c

    def _ensure_user(self, key):
        self.con.execute("INSERT OR IGNORE INTO user(key) VALUES (?)", (key,))

    def set_fav(self, key, fav):
        self._ensure_user(key)
        self.con.execute("UPDATE user SET fav=? WHERE key=?", (int(fav), key))
        self.con.commit()

    def set_kind(self, keys, kind):
        """Your own type for these books ('' = back to Babel's guess). Survives rescans."""
        for k in keys:
            self._ensure_user(k)
            self.con.execute("UPDATE user SET kind=? WHERE key=?", (kind or None, k))
        self.con.commit()

    def set_genre(self, keys, genre):
        """Your genre for these books ('' = automatic again). Survives rescans."""
        for k in keys:
            self._ensure_user(k)
            self.con.execute("UPDATE user SET genre=? WHERE key=?", (genre or None, k))
        self.con.commit()

    def set_author_genre(self, author, genre):
        """Your genre for everything by this author ('' = back to the automatic one)."""
        self.con.execute("INSERT OR IGNORE INTO author_genre(author, genre, source) VALUES (?, '', 'none')",
                         (author,))
        self.con.execute("UPDATE author_genre SET user_genre=? WHERE author=?", (genre or None, author))
        self.con.commit()

    def genre_stats(self):
        authors = self.con.execute("SELECT COUNT(DISTINCT author) FROM books WHERE author != '' "
                                   "AND kind IN ('Book', 'Paper')").fetchone()[0]
        known = self.con.execute("SELECT COUNT(*) FROM author_genre WHERE genre != '' "
                                 "OR user_genre != ''").fetchone()[0]
        return known, authors

    def set_status(self, key, status):
        self._ensure_user(key)
        self.con.execute("UPDATE user SET status=? WHERE key=?", (status, key))
        self.con.commit()

    def mark_opened(self, key):
        self._ensure_user(key)
        self.con.execute("UPDATE user SET last_opened=?, open_count=open_count+1 WHERE key=?",
                         (time.time(), key))
        self.con.commit()


# ------------------------------------------------------------ cover loading

class CoverSignals(QObject):
    loaded = pyqtSignal(str, QImage)


class CoverTask(QRunnable):
    def __init__(self, key, path, size, signals):
        super().__init__()
        self.key, self.path, self.size, self.signals = key, path, size, signals

    def run(self):
        img = QImage(self.path)
        if not img.isNull():
            img = img.scaled(self.size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
        self.signals.loaded.emit(self.key, img)


class BookModel(QAbstractListModel):
    KeyRole = Qt.ItemDataRole.UserRole + 1
    RowRole = Qt.ItemDataRole.UserRole + 2

    def __init__(self, dpr=1.0):
        super().__init__()
        self.rows = []
        self.index_of = {}
        self.cache = OrderedDict()
        self.pending = set()
        self.dpr = dpr
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(4)
        self.signals = CoverSignals()
        self.signals.loaded.connect(self._on_loaded)

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.index_of = {r["key"]: i for i, r in enumerate(rows)}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return r["title"]
        if role == Qt.ItemDataRole.ToolTipRole:
            bits = [r["title"]]
            if r["author"]:
                bits.append(r["author"])
            if r["year"]:
                bits.append(str(r["year"]))
            return " — ".join(bits)
        if role == self.KeyRole:
            return r["key"]
        if role == self.RowRole:
            return r
        return None

    def update_row(self, key, **changes):
        i = self.index_of.get(key)
        if i is not None:
            self.rows[i].update(changes)
            idx = self.index(i)
            self.dataChanged.emit(idx, idx)

    def cover(self, row):
        key, path = row["key"], row["cover"]
        if not path:
            return None
        pix = self.cache.get(key)
        if pix is not None:
            self.cache.move_to_end(key)
            return pix
        if key not in self.pending:
            self.pending.add(key)
            size = QSize(int(COVER_W * self.dpr), int(COVER_H * self.dpr))
            self.pool.start(CoverTask(key, path, size, self.signals))
        return None

    def _on_loaded(self, key, img):
        self.pending.discard(key)
        if img.isNull():
            return
        pix = QPixmap.fromImage(img)
        pix.setDevicePixelRatio(self.dpr)
        self.cache[key] = pix
        while len(self.cache) > 1500:
            self.cache.popitem(last=False)
        i = self.index_of.get(key)
        if i is not None:
            idx = self.index(i)
            self.dataChanged.emit(idx, idx)


def placeholder_colors(text):
    h = (sum(ord(c) * (i + 1) for i, c in enumerate(text or "?")) * 37) % 360
    return QColor.fromHsl(h, 70, 62), QColor.fromHsl((h + 25) % 360, 80, 34)


def paint_placeholder(p, rect, title, author, title_px=15):
    c1, c2 = placeholder_colors(title)
    g = QLinearGradient(rect.topLeft().toPointF(), rect.bottomRight().toPointF())
    g.setColorAt(0, c2)
    g.setColorAt(1, c2.darker(160))
    p.fillRect(rect, g)
    p.setPen(QPen(QColor(255, 255, 255, 60), 1))
    inner = rect.adjusted(8, 8, -8, -8)
    p.drawRect(inner)
    p.setPen(QColor("#f6efe3"))
    f = QFont(SERIF)
    f.setPixelSize(title_px)
    f.setWeight(QFont.Weight.DemiBold)
    p.setFont(f)
    opt = QTextOption(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
    opt.setWrapMode(QTextOption.WrapMode.WordWrap)
    t_rect = QRectF(inner.adjusted(10, 18, -10, -int(inner.height() * 0.3)))
    p.drawText(t_rect, title or "", opt)
    if author:
        f.setPixelSize(max(10, title_px - 4))
        f.setWeight(QFont.Weight.Normal)
        f.setItalic(True)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 190))
        a_rect = QRectF(inner.adjusted(10, int(inner.height() * 0.72), -10, -10))
        p.drawText(a_rect, author, opt)


class BookDelegate(QStyledItemDelegate):
    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.title_font = QFont()
        self.title_font.setPixelSize(13)
        self.title_font.setWeight(QFont.Weight.DemiBold)
        self.author_font = QFont()
        self.author_font.setPixelSize(12)
        self.badge_font = QFont()
        self.badge_font.setPixelSize(9)
        self.badge_font.setWeight(QFont.Weight.Bold)

    def sizeHint(self, option, index):
        return QSize(CARD_W, CARD_H)

    def paint(self, p, option, index):
        r = index.data(BookModel.RowRole)
        if r is None:
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        if selected or hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(SELECT) if selected else wash(0.04))
            p.drawRoundedRect(QRectF(rect.adjusted(1, 1, -1, -1)), 10, 10)

        cover = QRect(rect.x() + (CARD_W - COVER_W) // 2, rect.y() + 10, COVER_W, COVER_H)
        # shadow
        p.setPen(Qt.PenStyle.NoPen)
        for i, a in enumerate((40, 25, 12)):
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(QRectF(cover.adjusted(-i, 2 - i, i, 4 + i)), 6, 6)

        path = QPainterPath()
        path.addRoundedRect(QRectF(cover), 5, 5)
        p.setClipPath(path)
        pix = self.model.cover(r)
        if pix is not None:
            pw = pix.width() / pix.devicePixelRatio()
            ph = pix.height() / pix.devicePixelRatio()
            sx = (pw - COVER_W) / 2
            sy = (ph - COVER_H) / 2
            p.drawPixmap(QRectF(cover), pix,
                         QRectF(sx * pix.devicePixelRatio(), sy * pix.devicePixelRatio(),
                                COVER_W * pix.devicePixelRatio(), COVER_H * pix.devicePixelRatio()))
        else:
            paint_placeholder(p, cover, r["title"], r["author"], 14)
        p.setClipping(False)

        if selected:
            p.setPen(QPen(QColor(ACCENT_BRIGHT), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cover).adjusted(-1, -1, 1, 1), 6, 6)

        # badges: formats bottom-left, kind top-left, favorite top-right
        p.setFont(self.badge_font)
        fm = QFontMetrics(self.badge_font)
        x = cover.x() + 6
        show_fmt = prefs().get("show_format_badges")
        for fmt in (r["formats"] or "").split(",")[:3] if show_fmt else []:
            w = fm.horizontalAdvance(fmt) + 10
            br = QRectF(x, cover.bottom() - 22, w, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(15, 13, 20, 200))
            p.drawRoundedRect(br, 4, 4)
            p.setPen(QColor("#f1e7d6"))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, fmt)
            x += w + 4
        if r["kind"] and r["kind"] != "Book" and prefs().get("show_type_badges"):
            label = r["kind"].upper()
            w = fm.horizontalAdvance(label) + 10
            br = QRectF(cover.x() + 6, cover.y() + 6, w, 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(br, 4, 4)
            p.setPen(QColor(ON_ACCENT))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, label)
        if r.get("via_desc"):
            label = "IN DESCRIPTION"
            w = fm.horizontalAdvance(label) + 10
            y = cover.y() + (28 if (r["kind"] and r["kind"] != "Book" and prefs().get("show_type_badges")) else 6)
            br = QRectF(cover.x() + 6, y, min(w, COVER_W - 12), 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(15, 13, 20, 215))
            p.drawRoundedRect(br, 4, 4)
            p.setPen(QColor("#d9cfc0"))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, label)
        if r["fav"]:
            br = QRectF(cover.right() - 26, cover.y() + 6, 20, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(15, 13, 20, 200))
            p.drawEllipse(br)
            p.setPen(QColor(ACCENT))
            f = QFont()
            f.setPixelSize(13)
            p.setFont(f)
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, "★")

        # title + author
        tx = QRect(rect.x() + 8, cover.bottom() + 9, CARD_W - 16, 36)
        p.setFont(self.title_font)
        p.setPen(QColor(TEXT))
        fm = QFontMetrics(self.title_font)
        lines = self._wrap(r["title"] or "", fm, tx.width(), 2)
        for i, line in enumerate(lines):
            p.drawText(QRect(tx.x(), tx.y() + i * 17, tx.width(), 17),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)
        p.setFont(self.author_font)
        p.setPen(QColor(MUTED))
        fm2 = QFontMetrics(self.author_font)
        sub = r["author"] or r["collection"] or ""
        if r["year"]:
            sub = f"{sub} · {r['year']}" if sub else str(r["year"])
        p.drawText(QRect(tx.x(), tx.y() + len(lines) * 17 + 2, tx.width(), 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm2.elidedText(sub, Qt.TextElideMode.ElideRight, tx.width()))
        p.restore()

    @staticmethod
    def _wrap(text, fm, width, max_lines):
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if fm.horizontalAdvance(trial) <= width:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
                if len(lines) == max_lines:
                    break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        if len(lines) == max_lines:
            consumed = " ".join(lines)
            if len(consumed) < len(text):
                lines[-1] = fm.elidedText(lines[-1] + " " + text[len(consumed):],
                                          Qt.TextElideMode.ElideRight, width)
        return [fm.elidedText(l, Qt.TextElideMode.ElideRight, width) for l in lines] or [""]


# ------------------------------------------------------------- scan worker

# ---------------------------------------------------------------------- Kobo

KOBO_FOLDER = "Babel"               # where Babel puts books on the device
KOBO_FORMATS = ("epub", "pdf", "mobi", "cbz", "cbr")   # what a Kobo can read as-is


def find_kobo():
    """(mount root, device path, free bytes) of a connected Kobo, or None."""
    override = os.environ.get("BABEL_KOBO_PATH")      # for testing without a device
    if override and os.path.isdir(os.path.join(override, ".kobo")):
        return override, "", shutil.disk_usage(override).free
    for vol in QStorageInfo.mountedVolumes():
        root = vol.rootPath()
        if vol.isValid() and vol.isReady() and os.path.isdir(os.path.join(root, ".kobo")):
            return root, bytes(vol.device()).decode(errors="ignore"), vol.bytesAvailable()
    return None


def kobo_name(b, ext):
    """'Author - Title.ext', safe for the Kobo's FAT filesystem."""
    name = f"{b['author']} - {b['title']}" if b.get("author") else b["title"]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")[:150] or "Untitled"
    return name + ext


def kobo_plan(b, kobo_root):
    """Pick the file to send and where it lands: (source, dest, needs) where needs is
    '' (copy), 'kepub' or 'convert'. None if nothing suitable."""
    files = json.loads(b["files"])
    by_ext = {p.rsplit(".", 1)[-1].lower(): p for p in files}
    use_kepub = prefs().get("kobo_kepub") and shutil.which("kepubify")
    dest_dir = os.path.join(kobo_root, KOBO_FOLDER)
    if "epub" in by_ext:
        if use_kepub:
            return by_ext["epub"], os.path.join(dest_dir, kobo_name(b, ".kepub.epub")), "kepub"
        return by_ext["epub"], os.path.join(dest_dir, kobo_name(b, ".epub")), ""
    for e in KOBO_FORMATS[1:]:
        if e in by_ext:
            return by_ext[e], os.path.join(dest_dir, kobo_name(b, "." + e)), ""
    for e in ("azw3", "azw", "fb2"):
        if e in by_ext and prefs().get("kobo_convert_kindle") and shutil.which("ebook-convert"):
            return by_ext[e], os.path.join(dest_dir, kobo_name(b, ".epub")), "convert"
    return None


def on_kobo(b, kobo_root):
    plan = kobo_plan(b, kobo_root) if kobo_root else None
    return bool(plan) and os.path.exists(plan[1])


class KoboSender(QThread):
    progress = pyqtSignal(int, int, str)
    finished_send = pyqtSignal(int, int, list)     # sent, already there, failures

    def __init__(self, books, kobo_root):
        super().__init__()
        self.books, self.root = books, kobo_root

    def run(self):
        sent, skipped, failed = 0, 0, []
        os.makedirs(os.path.join(self.root, KOBO_FOLDER), exist_ok=True)
        for i, b in enumerate(self.books):
            self.progress.emit(i, len(self.books), b["title"])
            plan = kobo_plan(b, self.root)
            if not plan:
                failed.append(f"{b['title']} (no format a Kobo can read)")
                continue
            src, dest, needs = plan
            if os.path.exists(dest):
                skipped += 1
                continue
            try:
                with tempfile.TemporaryDirectory() as td:
                    if needs == "convert":
                        out = os.path.join(td, "book.epub")
                        subprocess.run(["ebook-convert", src, out], capture_output=True, timeout=600, check=True)
                        src = out
                    if needs == "kepub":
                        out = os.path.join(td, "book.kepub.epub")
                        subprocess.run(["kepubify", "-o", out, src], capture_output=True, timeout=300, check=True)
                        src = out
                    shutil.copyfile(src, dest + ".part")
                    os.replace(dest + ".part", dest)
                sent += 1
            except Exception as e:
                failed.append(f"{b['title']} ({e.__class__.__name__})")
                try:
                    os.remove(dest + ".part")
                except OSError:
                    pass
        os.sync()          # make sure everything is on the device before it's unplugged
        self.progress.emit(len(self.books), len(self.books), "")
        self.finished_send.emit(sent, skipped, failed)


class GenreWorker(QThread):
    """Looks up genres for authors that don't have one yet (only author names are sent)."""
    progress = pyqtSignal(int, int)
    done = pyqtSignal(int, int)

    def run(self):
        try:
            n, found = scanner.lookup_genres(progress=lambda d, t, a: self.progress.emit(d, t))
        except Exception:
            n, found = 0, 0
        self.done.emit(n, found)


class DupFinder(QThread):
    """Finds byte-identical book files (read-only)."""
    progress = pyqtSignal(int, int)
    found = pyqtSignal(list)

    def run(self):
        self.found.emit(scanner.find_exact_duplicates(
            scanner.BOOKS_ROOT, lambda d, t: self.progress.emit(d, t)))


class DupTrasher(QThread):
    """Moves the planned duplicate copies to the Trash."""
    done = pyqtSignal(int, float, int, int)

    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def run(self):
        self.done.emit(*scanner.apply_duplicate_plan(scanner.BOOKS_ROOT, self.plan))


class ScanWorker(QThread):
    progress = pyqtSignal(int, int, str)
    finished_scan = pyqtSignal(int, int, int)

    def __init__(self, only=None, shallow=None, full=False, rebuild=False):
        super().__init__()
        self.only, self.shallow, self.full, self.rebuild = only, shallow, full, rebuild

    def run(self):
        scanner.REBUILD_COVERS = self.rebuild
        try:
            res = scanner.scan(root=scanner.BOOKS_ROOT,
                               progress=lambda d, t, m: self.progress.emit(d, t, m),
                               only=self.only, shallow=self.shallow, full=self.full)
        finally:
            scanner.REBUILD_COVERS = False
        self.finished_scan.emit(*res)


class DirLister(QThread):
    """Lists every folder under a path (off the UI thread; the drive is slow)."""
    listed = pyqtSignal(list)

    def __init__(self, roots):
        super().__init__()
        self.roots = roots

    def run(self):
        out = []
        for root in self.roots:
            if os.path.isdir(root):
                out.append(root)
                for dirpath, dirnames, _ in os.walk(root):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    out += [os.path.join(dirpath, d) for d in dirnames]
        self.listed.emit(out)


class LibraryWatcher(QObject):
    """Watches every folder under Books. When something is added, removed or
    renamed it waits for things to settle, then asks for a rescan of just the
    affected folders."""
    changed = pyqtSignal(list, list)          # recursive dirs, shallow dirs

    SETTLE_MS = 5000

    def __init__(self, root):
        super().__init__()
        self.root = root
        self.fs = QFileSystemWatcher(self)
        self.fs.directoryChanged.connect(self._dir_changed)
        self.watched = set()
        self.pending_deep, self.pending_shallow = set(), set()
        self.timer = QTimer(self, singleShot=True, interval=self.SETTLE_MS)
        self.timer.timeout.connect(self._flush)
        self.lister = None
        # the drive can be unplugged and plugged back in
        self.mount_timer = QTimer(self, interval=30000)
        self.mount_timer.timeout.connect(self._check_mount)
        self.mount_timer.start()
        self.mounted = os.path.isdir(root)
        self.on_remount = None

    def start(self):
        self.active = True
        self._watch_tree([self.root])

    def stop(self):
        self.active = False
        self.timer.stop()
        if self.fs.directories():
            self.fs.removePaths(self.fs.directories())
        self.watched.clear()
        self.pending_deep.clear()
        self.pending_shallow.clear()

    def _watch_tree(self, roots):
        lister = DirLister(roots)
        lister.listed.connect(self._add_paths)
        lister.finished.connect(lister.deleteLater)
        self._listers = getattr(self, "_listers", set())
        self._listers.add(lister)
        lister.finished.connect(lambda l=lister: self._listers.discard(l))
        lister.start()

    def _add_paths(self, paths):
        if not getattr(self, "active", False):
            return
        new = [p for p in paths if p not in self.watched]
        if new:
            self.fs.addPaths(new)
            self.watched.update(new)

    def _dir_changed(self, d):
        if not os.path.isdir(d):
            # the folder itself went away; its parent's event handles the rescan
            self.fs.removePath(d)
            self.watched.discard(d)
            parent = os.path.dirname(d)
            if parent.startswith(self.root):
                self.pending_deep.add(d)
                self.pending_shallow.add(parent)
                self.timer.start()
            return
        self.pending_shallow.add(d)
        try:
            subdirs = {os.path.join(d, e.name) for e in os.scandir(d)
                       if e.is_dir() and not e.name.startswith(".")}
        except OSError:
            subdirs = set()
        known = {w for w in self.watched if os.path.dirname(w) == d}
        added, gone = subdirs - known, known - subdirs
        if added:
            # watch new folders straight away so files still being copied in are seen
            self._watch_tree(sorted(added))
            self.pending_deep.update(added)
        for g in gone:
            self.fs.removePath(g)
            self.watched = {w for w in self.watched if w != g and not w.startswith(g + os.sep)}
            self.pending_deep.add(g)
        self.timer.start()

    def _flush(self):
        deep, flat = sorted(self.pending_deep), sorted(self.pending_shallow)
        self.pending_deep, self.pending_shallow = set(), set()
        if deep or flat:
            self.changed.emit(deep, flat)

    def recheck_later(self, deep, flat, ms=30000):
        """Files can still be copying when the first rescan runs; look again."""
        def again():
            self.pending_deep.update(deep)
            self.pending_shallow.update(flat)
            self.timer.start()
        QTimer.singleShot(ms, again)

    def _check_mount(self):
        now = os.path.isdir(self.root)
        if now and not self.mounted and getattr(self, "active", False):
            self.watched.clear()
            if self.fs.directories():
                self.fs.removePaths(self.fs.directories())
            self.start()
            if self.on_remount:
                self.on_remount()
        self.mounted = now


# --------------------------------------------------------------- widgets

class Link(QLabel):
    """A clickable label that emits its text."""
    clicked = pyqtSignal(str)

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setWordWrap(True)
        self.setObjectName("link")

    def mousePressEvent(self, e):
        self.clicked.emit(self.property("value") or self.text())


class DetailsPanel(QScrollArea):
    open_file = pyqtSignal(str, str)          # key, path
    filter_author = pyqtSignal(str)
    filter_collection = pyqtSignal(str)
    fav_changed = pyqtSignal(str, bool)
    status_changed = pyqtSignal(str, str)
    send_to_kobo = pyqtSignal(str)
    kind_changed = pyqtSignal(str, str)
    genre_changed = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.kobo_root = None
        self.setWidgetResizable(True)
        self.setObjectName("details")
        self.setMinimumWidth(320)
        self.key = None
        self.search = ""
        self.main_lib = None
        self.copies_of = lambda b: []
        w = QWidget()
        w.setObjectName("detailsInner")
        self.setWidget(w)
        v = QVBoxLayout(w)
        v.setContentsMargins(26, 26, 26, 26)
        v.setSpacing(10)

        self.cover = QLabel()
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setMinimumHeight(330)
        v.addWidget(self.cover)

        self.title = QLabel()
        self.title.setObjectName("detailTitle")
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.title)
        self.author = Link()
        self.author.setObjectName("detailAuthor")
        self.author.clicked.connect(self.filter_author)
        v.addWidget(self.author)

        row = QHBoxLayout()
        self.fav = QPushButton("☆  Favourite")
        self.fav.setCheckable(True)
        self.fav.setObjectName("favButton")
        self.fav.toggled.connect(self._fav_toggled)
        self.status = QComboBox()
        self.status.setItemDelegate(QStyledItemDelegate(self.status))
        for code, label in STATUSES:
            self.status.addItem(label, code)
        self.status.currentIndexChanged.connect(self._status_changed)
        row.addWidget(self.fav)
        row.addWidget(self.status, 1)
        v.addLayout(row)

        self.buttons = QVBoxLayout()
        self.buttons.setSpacing(6)
        v.addLayout(self.buttons)

        self.meta = QGridLayout()
        self.meta.setHorizontalSpacing(14)
        self.meta.setVerticalSpacing(6)
        v.addSpacing(6)
        v.addLayout(self.meta)

        self.desc = QLabel()
        self.desc.setWordWrap(True)
        self.desc.setObjectName("desc")
        self.desc.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.desc)
        v.addStretch(1)

        self.empty = QLabel("Select a book to see its details.\n\n"
                            "Tips:  /  to search  ·  Enter to open  ·  Ctrl+D favourite")
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        v.insertWidget(0, self.empty)
        self._set_visible(False)

    def _set_visible(self, on):
        for wdg in (self.cover, self.title, self.author, self.fav, self.status, self.desc):
            wdg.setVisible(on)
        self.empty.setVisible(not on)

    @staticmethod
    def _clear(layout):
        while layout.count():
            it = layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def show_book(self, b):
        self.key = None
        if not b:
            self._set_visible(False)
            self._clear(self.buttons)
            self._clear(self.meta)
            return
        self._set_visible(True)

        pm = QPixmap()
        dpr = self.devicePixelRatioF()
        w, h = 240, 360
        if b["cover"] and pm.load(b["cover"]):
            pm = pm.scaled(int(w * dpr), int(h * dpr), Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
            pm.setDevicePixelRatio(dpr)
        else:
            pm = QPixmap(int(w * dpr), int(h * dpr))
            pm.setDevicePixelRatio(dpr)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            paint_placeholder(p, QRect(0, 0, w, h), b["title"], b["author"], 20)
            p.end()
        self.cover.setPixmap(pm)

        self.title.setText(b["title"])
        self.author.setText(b["author"] or "")
        self.author.setProperty("value", b["author"])
        self.author.setVisible(bool(b["author"]))
        self.fav.setChecked(bool(b["fav"]))
        self.fav.setText("★  Favourite" if b["fav"] else "☆  Favourite")
        self.status.setCurrentIndex(max(0, [c for c, _ in STATUSES].index(b["status"] or "")))

        self._clear(self.buttons)
        files = ordered_files(json.loads(b["files"]))
        for i, path in enumerate(files):
            ext = path.rsplit(".", 1)[-1].upper()
            r = reader_for(path)
            btn = QPushButton(f"Open {ext}" + ("   ⏎" if i == 0 else ""))
            btn.setToolTip(f"Opens in {r[0].split(' (')[0]}" if r else "Opens in your default app")
            btn.setObjectName("primary" if i == 0 else "secondary")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, p=path, k=b["key"]: self.open_file.emit(k, p))
            self.buttons.addWidget(btn)
        show = QPushButton("Show in folder")
        show.setObjectName("secondary")
        show.setCursor(Qt.CursorShape.PointingHandCursor)
        show.clicked.connect(lambda _, p=files[0]: show_in_folder(p))
        self.buttons.addWidget(show)
        if self.kobo_root:
            if on_kobo(b, self.kobo_root):
                kb = QPushButton("✓  On your Kobo")
                kb.setEnabled(False)
            elif kobo_plan(b, self.kobo_root):
                kb = QPushButton("Send to Kobo   Ctrl+K")
                kb.clicked.connect(lambda _, k=b["key"]: self.send_to_kobo.emit(k))
            else:
                kb = QPushButton("Kobo can’t read this format")
                kb.setEnabled(False)
            kb.setObjectName("kobo")
            kb.setCursor(Qt.CursorShape.PointingHandCursor)
            self.buttons.addWidget(kb)

        self._clear(self.meta)
        snippet = ""
        if self.search and self.main_lib:
            snippet = self.main_lib.match_snippet(b["key"], self.search)
        rows = [("Matched", snippet), ("Year", str(b["year"] or "")), ("Publisher", b["publisher"] or ""),
                ("Collection", b["collection"] or ""), ("Type", b["kind"] or ""),
                ("Genre", b.get("genre") or "—"),
                ("Formats", (b["formats"] or "").replace(",", " · ")),
                ("Size", human_size(b["size"] or 0)), ("Notes", b["notes"] or ""),
                ("Subjects", b["subjects"] or "")]
        if b["last_opened"]:
            rows.append(("Last opened", time.strftime("%d %b %Y", time.localtime(b["last_opened"]))))
        r = 0
        plain = lambda t: unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().strip().casefold()
        author = plain(b["author"])
        for label, value in rows:
            if not value:
                continue
            if label == "Collection" and plain(value) == author:
                continue          # just the author's name again; it's already under the title
            lab = QLabel(label)
            lab.setObjectName("metaLabel")
            # line the label up with its value (the Type dropdown is taller than plain text)
            lab.setAlignment((Qt.AlignmentFlag.AlignVCenter if label in ("Type", "Genre") else Qt.AlignmentFlag.AlignTop)
                             | Qt.AlignmentFlag.AlignLeft)
            if label == "Collection":
                val = Link(value)
                val.setProperty("value", value)
                val.clicked.connect(self.filter_collection)
            elif label == "Genre":
                val = QComboBox()
                val.setItemDelegate(QStyledItemDelegate(val))
                val.addItem("—", "")
                for g in scanner.GENRES:
                    val.addItem(g, g)
                val.setCurrentIndex(max(0, val.findData(b.get("genre") or "")))
                val.setToolTip("Change this book's genre. Babel remembers your choice. "
                               "Right-click a cover to set it for the whole author.")
                val.currentIndexChanged.connect(
                    lambda _, v=val, key=b["key"]: self.genre_changed.emit(key, v.currentData()))
            elif label == "Type":
                val = QComboBox()
                val.setItemDelegate(QStyledItemDelegate(val))
                for k in KINDS:
                    val.addItem(k, k)
                val.setCurrentIndex(KINDS.index(value) if value in KINDS else 0)
                val.setToolTip("Change what this is. Babel remembers your choice.")
                val.currentIndexChanged.connect(
                    lambda _, v=val, key=b["key"]: self.kind_changed.emit(key, v.currentData()))
            else:
                val = QLabel(value)
                val.setWordWrap(True)
                val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.meta.addWidget(lab, r, 0)
            self.meta.addWidget(val, r, 1)
            r += 1
        for c in self.copies_of(b):
            lab = QLabel("Also in")
            lab.setObjectName("metaLabel")
            lab.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            val = Link(c["collection"])
            val.setProperty("value", c["collection"])
            val.clicked.connect(self.filter_collection)
            val.setToolTip(json.loads(c["files"])[0])
            self.meta.addWidget(lab, r, 0)
            self.meta.addWidget(val, r, 1)
            r += 1
        self.meta.setColumnStretch(1, 1)
        self.desc.setText(b["description"] or "")
        self.desc.setVisible(bool(b["description"]))
        self.key = b["key"]

    def _fav_toggled(self, on):
        self.fav.setText("★  Favourite" if on else "☆  Favourite")
        if self.key:
            self.fav_changed.emit(self.key, on)

    def _status_changed(self, _):
        if self.key:
            self.status_changed.emit(self.key, self.status.currentData())


# Reader apps Babel knows how to launch: (id, name, command)
READER_APPS = [
    ("default", "System default", None),
    ("foliate", "Foliate", "foliate"),
    ("ebook-viewer", "Calibre E-book Viewer", "ebook-viewer"),
    ("papers", "Document Viewer (Papers)", "papers"),
    ("evince", "Document Viewer (Evince)", "evince"),
    ("okular", "Okular", "okular"),
    ("zathura", "Zathura", "zathura"),
    ("xreader", "Xreader", "xreader"),
    ("mcomix", "MComix", "mcomix"),
]
FORMAT_GROUPS = {          # pref suffix -> (label, extensions)
    "epub": ("EPUB", ("epub", "fb2")),
    "pdf": ("PDF", ("pdf", "djvu")),
    "kindle": ("Kindle (MOBI / AZW3)", ("mobi", "azw3", "azw")),
    "comic": ("Comics (CBR / CBZ)", ("cbr", "cbz")),
}


def installed_readers():
    return [a for a in READER_APPS if a[2] is None or shutil.which(a[2])]


def reader_for(path):
    """(app name, command) Babel should use for this file, or None for the system default."""
    ext = path.rsplit(".", 1)[-1].lower()
    for group, (_, exts) in FORMAT_GROUPS.items():
        if ext in exts:
            app_id = prefs().get("reader_" + group)
            for aid, name, cmd in READER_APPS:
                if aid == app_id and cmd and shutil.which(cmd):
                    return name, cmd
    return None


def ordered_files(files):
    """Put the preferred format first."""
    if prefs().get("prefer_format") == "pdf":
        return sorted(files, key=lambda p: p.rsplit(".", 1)[-1].lower() != "pdf")
    return files


def open_with_reader(path):
    r = reader_for(path)
    if r:
        try:
            subprocess.Popen([r[1], path], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    # the default app, straight away (Qt's own route can pop up an app chooser on GNOME)
    if shutil.which("gio"):
        try:
            subprocess.Popen(["gio", "open", path], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def file_uri(path):
    """Fully percent-encoded file:// URI (spaces, commas, accents and quotes all safe)."""
    return bytes(QUrl.fromLocalFile(path).toEncoded()).decode("ascii")


def gvariant_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def show_in_folder(path):
    """Open the file manager with the file highlighted; fall back to opening its folder."""
    if shutil.which("gdbus") and os.path.exists(path):
        try:
            r = subprocess.run(["gdbus", "call", "--session",
                                "--dest", "org.freedesktop.FileManager1",
                                "--object-path", "/org/freedesktop/FileManager1",
                                "--method", "org.freedesktop.FileManager1.ShowItems",
                                "[" + gvariant_str(file_uri(path)) + "]", "''"],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                return
        except Exception:
            pass
    QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))


class FilterList(QWidget):
    """Filterable list used for the Collections and Authors tabs."""
    chosen = pyqtSignal(str)

    def __init__(self, placeholder):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 10, 8, 8)
        v.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.edit.textChanged.connect(self._filter)
        self.list = QListWidget()
        self.list.setObjectName("sideList")
        self.list.setUniformItemSizes(True)
        self.list.setItemDelegate(SmoothItemDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.itemClicked.connect(lambda it: self.chosen.emit(it.data(Qt.ItemDataRole.UserRole)))
        v.addWidget(self.edit)
        v.addWidget(self.list)

    def set_items(self, items):
        self.list.clear()
        for value, label, n in items:
            it = QListWidgetItem(f"{label}   ({n})")
            it.setData(Qt.ItemDataRole.UserRole, value)
            it.setToolTip(label)
            self.list.addItem(it)
        self._filter(self.edit.text())

    def _filter(self, text):
        t = text.lower().strip()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())


# ---------------------------------------------------------- smooth shapes
# Qt stylesheets clip rounded backgrounds without anti-aliasing, which leaves
# stair-stepped corners. These widgets get their rounded shape painted here
# instead, with smoothing on; their stylesheet keeps only padding and text.

def _rgba(r, g, b, a):
    return QColor(r, g, b, int(a * 255))


def round_spec(w):
    """(radius, fill, border) for widgets Babel paints itself, or None."""
    name = w.objectName()
    if isinstance(w, QLineEdit):
        if isinstance(w.parent(), QComboBox):
            return None
        return (10 if name == "search" else 8, QColor(PANEL_2),
                QColor(ACCENT) if w.hasFocus() else QColor(LINE))
    if isinstance(w, QComboBox):
        hot = w.underMouse() or w.view().isVisible()
        return 8, QColor(PANEL_2), QColor(ACCENT) if hot and w.isEnabled() else QColor(LINE)
    if isinstance(w, QToolButton) and name == "menuButton":
        a = 0.15 if (w.isDown() or (w.menu() and w.menu().isVisible())) else 0.08 if w.underMouse() else 0
        return (8, wash(a), None) if a else None
    if isinstance(w, QPushButton):
        hover = w.underMouse() and w.isEnabled()
        if name == "primary":
            fill = ACCENT_DOWN if w.isDown() else ACCENT_HOVER if hover else ACCENT
            return 8, QColor(fill if w.isEnabled() else LINE), None
        if name == "closeButton":
            a = 0.30 if w.isDown() else 0.15 if hover else 0.10
            return 12, wash(a), None
        if name == "destructive":
            return 8, _rgba(224, 27, 36, 0.28 if hover else 0.18), _rgba(224, 27, 36, 0.35)
        if name == "kobo":
            return 8, QColor(PANEL_2), QColor(TEAL) if hover else QColor(LINE)
        if name == "segment":
            if w.isChecked():
                return 13, QColor(PANEL_2), QColor(ACCENT)
            return 13, wash(0.04 if hover else 0), QColor(LINE_STRONG) if hover else QColor(LINE)
        if name in ("secondary", "favButton", "chip") or (not name and isinstance(w.window(), QMessageBox)):
            on = hover or (name == "favButton" and w.isChecked())
            return 12 if name == "chip" else 8, QColor(PANEL_2), QColor(ACCENT) if on else QColor(LINE)
        return None
    if isinstance(w, QFrame) and name == "boxedList":
        return 12, QColor(PANEL_2), QColor(LINE)
    if isinstance(w, QLabel) and name == "keycap":
        return 6, QColor(BG), QColor(LINE)
    if isinstance(w, QMenu):
        return 10, QColor(PANEL_2), QColor(LINE)
    if w.metaObject().className() == "QComboBoxPrivateContainer":     # a dropdown's list pop-up
        return 10, QColor(PANEL_2), QColor(LINE)
    return None


def paint_round(p, rect, radius, fill, border):
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = QRectF(rect)
    if border is not None:
        r = r.adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(border, 1))
        radius -= 0.5
    else:
        p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(fill)
    p.drawRoundedRect(r, radius, radius)


class SmoothShapes(QObject):
    """App-wide filter: paints the rounded background before the widget draws its text."""
    REPAINT_ON = {QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.FocusIn, QEvent.Type.FocusOut,
                  QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.EnabledChange}

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.Paint:
            if isinstance(obj, QWidget):
                spec = round_spec(obj)
                if spec:
                    p = QPainter(obj)
                    paint_round(p, obj.rect(), *spec)
                    p.end()
        elif t in self.REPAINT_ON and isinstance(obj, QWidget) and round_spec(obj) is not None:
            obj.update()
        elif t == QEvent.Type.Show and isinstance(obj, QMenu):
            obj.update()
        elif t == QEvent.Type.Polish and isinstance(obj, QWidget) \
                and obj.metaObject().className() == "QComboBoxPrivateContainer" \
                and not obj.property("babel_smooth"):
            # dropdown lists open in their own little window: make it see-through
            # before it first appears, so its rounded corners aren't filled black
            obj.setProperty("babel_smooth", True)
            obj.setWindowFlags(obj.windowFlags() | Qt.WindowType.FramelessWindowHint
                               | Qt.WindowType.NoDropShadowWindowHint)
            obj.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        return False


def smooth_menu(parent=None):
    """A QMenu whose rounded corners are see-through (painted by SmoothShapes)."""
    m = QMenu(parent)
    m.setWindowFlags(m.windowFlags() | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
    m.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    return m


class SmoothItemDelegate(QStyledItemDelegate):
    """List rows with smooth rounded hover/selection highlights."""
    def __init__(self, parent=None, hover="PANEL_2", selected="SELECT", selected_text="ACCENT_TEXT", inset=(0, 1)):
        super().__init__(parent)
        self.hover, self.selected, self.selected_text, self.inset = hover, selected, selected_text, inset

    def paint(self, p, option, index):
        sel = bool(option.state & QStyle.StateFlag.State_Selected) or bool(index.data(Qt.ItemDataRole.UserRole + 5))
        hov = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if sel or hov:
            p.save()
            dx, dy = self.inset
            paint_round(p, QRectF(option.rect).adjusted(dx, dy, -dx, -dy), 6,
                        QColor(globals()[self.selected if sel else self.hover]), None)
            p.restore()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus)
        if sel and self.selected_text and not index.data(Qt.ItemDataRole.ForegroundRole):
            opt.palette.setColor(opt.palette.ColorRole.Text, QColor(globals()[self.selected_text]))
        super().paint(p, opt, index)


class NavDelegate(SmoothItemDelegate):
    """Draws the nav label with its count right-aligned."""
    def paint(self, p, option, index):
        super().paint(p, option, index)
        n = index.data(Qt.ItemDataRole.UserRole + 1)
        if n:
            p.save()
            p.setPen(QColor(MUTED))
            f = QFont(option.font)
            f.setPixelSize(12)
            p.setFont(f)
            p.drawText(option.rect.adjusted(0, 0, -12, 0),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{n:,}")
            p.restore()


def panel_path(outer, panel, radius):
    """Path for a panel inside a rounded window: only the panel corners that sit on the
    window's corners are rounded, drawn as real curves (path intersections get faceted)."""
    r = panel.intersected(outer)
    near = lambda a, b: abs(a - b) < 1.5
    tl = radius if near(r.left(), outer.left()) and near(r.top(), outer.top()) else 0
    tr = radius if near(r.right(), outer.right()) and near(r.top(), outer.top()) else 0
    br = radius if near(r.right(), outer.right()) and near(r.bottom(), outer.bottom()) else 0
    bl = radius if near(r.left(), outer.left()) and near(r.bottom(), outer.bottom()) else 0
    x0, y0, x1, y1 = r.left(), r.top(), r.right(), r.bottom()
    path = QPainterPath()
    path.moveTo(x0 + tl, y0)
    path.lineTo(x1 - tr, y0)
    if tr:
        path.arcTo(QRectF(x1 - 2 * tr, y0, 2 * tr, 2 * tr), 90, -90)
    path.lineTo(x1, y1 - br)
    if br:
        path.arcTo(QRectF(x1 - 2 * br, y1 - 2 * br, 2 * br, 2 * br), 0, -90)
    path.lineTo(x0 + bl, y1)
    if bl:
        path.arcTo(QRectF(x0, y1 - 2 * bl, 2 * bl, 2 * bl), 270, -90)
    path.lineTo(x0, y0 + tl)
    if tl:
        path.arcTo(QRectF(x0, y0, 2 * tl, 2 * tl), 180, -90)
    path.closeSubpath()
    return path


class WindowFrame(QWidget):
    """The window's background: rounded like a GNOME window, square when maximized."""
    def paintEvent(self, e):
        p = QPainter(self)
        # start from fully transparent, so nothing left over from a maximised frame
        # (which fills every pixel) can survive in the rounded corners
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        win = self.window()
        r = 0 if getattr(win, "fills_screen", lambda: False)() else WINDOW_RADIUS
        # rounded: inset half a pixel so the 1px border is crisp; maximised: fill every pixel
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5) if r else QRectF(self.rect())
        shape = QPainterPath()
        shape.addRoundedRect(rect, r, r)
        p.fillPath(shape, QColor(BG))
        # panels that reach the window's corners are filled here so the corners stay smooth
        for w in getattr(win, "panels", []):
            if w.isVisible():
                box = QRectF(w.mapTo(self, QPoint(0, 0)).toPointF(), QSizeF(w.size()))
                p.fillPath(panel_path(rect, box, r), QColor(PANEL))
        if r:
            p.setPen(QPen(QColor(LINE), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(shape)


class EdgeGrip(QWidget):
    """Invisible strip along a window edge that hands resizing to the compositor."""
    CURSORS = {
        Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
        Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
    }

    def __init__(self, parent, edges):
        super().__init__(parent)
        self.edges = edges
        if edges in self.CURSORS:
            self.setCursor(self.CURSORS[edges])
        elif edges in (Qt.Edge.TopEdge | Qt.Edge.LeftEdge, Qt.Edge.BottomEdge | Qt.Edge.RightEdge):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.window().windowHandle().startSystemResize(self.edges)

# --------------------------------------------------------------- preferences

class Switch(QAbstractButton):
    """A GNOME-style on/off switch."""
    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(46, 26)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        p.setPen(Qt.PenStyle.NoPen)
        track = QColor(ACCENT) if on else QColor(LINE_STRONG)
        if self.underMouse():
            track = track.lighter(110)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, 46, 26), 13, 13)
        p.setBrush(QColor("#ffffff"))
        x = 23 if on else 3
        p.drawEllipse(QRectF(x, 3, 20, 20))

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()


def _label(text, name, wrap=False):
    l = QLabel(text)
    l.setObjectName(name)
    l.setWordWrap(wrap)
    return l


def pref_row(title, subtitle="", control=None):
    row = QWidget()
    row.setObjectName("prefRow")
    h = QHBoxLayout(row)
    h.setContentsMargins(16, 11, 14, 11)
    h.setSpacing(16)
    text = QVBoxLayout()
    text.setSpacing(2)
    t = _label(title, "rowTitle", wrap=True)
    text.addWidget(t)
    if subtitle:
        text.addWidget(_label(subtitle, "rowSub", wrap=True))
    h.addLayout(text, 1)
    if control is not None:
        h.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    row.title_label = t
    return row


def boxed_list(rows):
    box = QFrame()
    box.setObjectName("boxedList")
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(0)
    for i, r in enumerate(rows):
        if i:
            sep = QFrame()
            sep.setObjectName("rowSep")
            sep.setFixedHeight(1)
            v.addWidget(sep)
        v.addWidget(r)
    return box


def pref_combo(options, value, on_change):
    """options: [(value, label)]"""
    c = QComboBox()
    c.setItemDelegate(QStyledItemDelegate(c))
    for v, l in options:
        c.addItem(l, v)
    vals = [v for v, _ in options]
    c.setCurrentIndex(vals.index(value) if value in vals else 0)
    c.currentIndexChanged.connect(lambda _: on_change(c.currentData()))
    c.setMinimumWidth(190)
    return c


def pref_switch(key):
    s = Switch(prefs().get(key))
    s.toggled.connect(lambda on: prefs().set(key, on))
    return s


def pref_button(text, slot, kind="secondary"):
    btn = QPushButton(text)
    btn.setObjectName(kind)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.clicked.connect(slot)
    return btn


HELPER_TOOLS = [   # (command, package hint, what Babel uses it for)
    ("pdftoppm", "poppler", "Covers for PDF books"),
    ("bsdtar", "libarchive", "Covers for comics (CBR / CBZ)"),
    ("ebook-meta", "calibre", "Covers and details for MOBI / AZW3"),
    ("ebook-convert", "calibre", "Converting Kindle books for a Kobo"),
    ("foliate", "foliate", "Reading EPUBs"),
    ("kepubify", "kepubify", "Kobo-format EPUBs"),
    ("udisksctl", "udisks2", "Ejecting a Kobo"),
]


class FramelessDialog(QDialog):
    """Rounded, borderless dialog matching the main window; drag anywhere empty to move."""
    def __init__(self, parent, w, h):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.resize(w, h)
        self._press = None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(LINE), 1))
        p.setBrush(QColor(BG))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), WINDOW_RADIUS, WINDOW_RADIUS)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.globalPosition()

    def mouseMoveEvent(self, e):
        if self._press is not None and (e.globalPosition() - self._press).manhattanLength() > 4:
            self._press = None
            self.windowHandle().startSystemMove()

    def mouseReleaseEvent(self, e):
        self._press = None


class WelcomeDialog(FramelessDialog):
    """First run: ask where the books are."""
    def __init__(self, parent):
        super().__init__(parent, 520, 470)
        v = QVBoxLayout(self)
        v.setContentsMargins(40, 36, 40, 32)
        v.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(QIcon(ICON_PATH).pixmap(96, 96))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(icon)
        v.addSpacing(6)
        t = _label("Welcome to Babel", "aboutName")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(t)
        msg = _label("Choose the folder that holds your books. Babel will find every EPUB, PDF, "
                     "Kindle book and comic inside it, including subfolders, and collect their covers.\n\n"
                     "Your files are never changed or moved.", "aboutText", wrap=True)
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(msg)
        v.addStretch(1)
        choose = pref_button("Choose Folder…", self._choose, "primary")
        later = pref_button("Not Now", self.reject)
        v.addWidget(choose)
        v.addWidget(later)

    def _choose(self):
        start = os.path.expanduser("~/Books") if os.path.isdir(os.path.expanduser("~/Books")) \
            else os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "Choose your books folder", start)
        if d:
            prefs().set("library_root", d)
            self.accept()


class PreferencesDialog(QDialog):
    PAGES = ["General", "Reading", "Appearance", "Library", "Shortcuts", "About"]

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Preferences")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.resize(860, 620)
        self._press = None

        outer = QHBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        # left: page list
        side = QWidget()
        side.setObjectName("prefSide")
        self.side = side
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        side.setFixedWidth(210)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(10, 0, 10, 10)
        sv.setSpacing(6)
        self.side_head = QWidget()
        self.side_head.setFixedHeight(46)
        shl = QHBoxLayout(self.side_head)
        shl.setContentsMargins(8, 0, 0, 0)
        shl.addWidget(_label("Preferences", "prefTitle"))
        sv.addWidget(self.side_head)
        self.nav = QListWidget()
        self.nav.setObjectName("prefNav")
        self.nav.setItemDelegate(SmoothItemDelegate(self.nav, hover="PANEL_2", selected="LINE", selected_text=None))
        self.nav.setMouseTracking(True)
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for p in self.PAGES:
            it = QListWidgetItem(p)
            it.setSizeHint(QSize(0, 36))
            self.nav.addItem(it)
        sv.addWidget(self.nav, 1)
        outer.addWidget(side)

        # right: header + pages
        right = QWidget()
        right.setObjectName("prefMain")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        self.head = QWidget()
        self.head.setFixedHeight(46)
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(24, 11, 11, 11)
        self.page_title = _label("", "pageTitle")
        hl.addWidget(self.page_title)
        hl.addStretch(1)
        close = QPushButton()
        close.setObjectName("closeButton")
        themed_icon(close, CLOSE_ICON)
        close.setIconSize(QSize(16, 16))
        close.setFixedSize(24, 24)
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.clicked.connect(self.close)
        hl.addWidget(close)
        rv.addWidget(self.head)
        self.stack = QStackedWidget()
        rv.addWidget(self.stack, 1)
        outer.addWidget(right, 1)

        for w_ in (self.side_head, self.head):
            w_.installEventFilter(self)

        builders = [self._general, self._reading, self._appearance, self._library,
                    self._shortcuts, self._about]
        for build in builders:
            self.stack.addWidget(self._page(build()))
        self.nav.currentRowChanged.connect(self._switch)
        self.nav.setCurrentRow(0)
        QShortcut(QKeySequence("Escape"), self, activated=self.close)

    # --- chrome
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), WINDOW_RADIUS, WINDOW_RADIUS)
        p.fillPath(shape, QColor(BG))
        outer = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.fillPath(panel_path(outer, QRectF(self.side.geometry()), WINDOW_RADIUS), QColor(PANEL))
        p.setPen(QPen(QColor(LINE), 1))
        p.drawPath(shape)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            self._press = ev.globalPosition()
            return True
        if t == QEvent.Type.MouseMove and self._press is not None:
            if (ev.globalPosition() - self._press).manhattanLength() > 4:
                self._press = None
                self.windowHandle().startSystemMove()
                return True
        if t == QEvent.Type.MouseButtonRelease:
            self._press = None
        return super().eventFilter(obj, ev)

    def _switch(self, i):
        self.stack.setCurrentIndex(i)
        self.page_title.setText(self.PAGES[i])

    def show_page(self, name):
        if name in self.PAGES:
            self.nav.setCurrentRow(self.PAGES.index(name))

    def _page(self, groups):
        inner = QWidget()
        inner.setObjectName("prefPage")
        v = QVBoxLayout(inner)
        v.setContentsMargins(24, 8, 24, 24)
        v.setSpacing(8)
        for title, desc, widget in groups:
            if title:
                v.addSpacing(10)
                v.addWidget(_label(title, "groupTitle"))
            if desc:
                v.addWidget(_label(desc, "rowSub", wrap=True))
            v.addSpacing(2)
            v.addWidget(widget)
        v.addStretch(1)
        sa = QScrollArea()
        sa.setObjectName("prefScroll")
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidget(inner)
        return sa

    # --- pages
    def _general(self):
        p = prefs()
        start = pref_combo([("kind:Book", "Books"), ("shelf:all", "All Items"),
                            ("shelf:fav", "Favourites"), ("shelf:reading", "Reading"),
                            ("shelf:want", "Want to Read"), ("last", "Where I left off")],
                           p.get("start_view"), lambda v: p.set("start_view", v))
        sort = pref_combo([s for s in SORTS], p.get("default_sort"), lambda v: p.set("default_sort", v))
        self.root_row = pref_row(p.get("library_root"),
                                 "Babel lists every book inside this folder and its subfolders.",
                                 pref_button("Change…", self._change_root))
        return [
            ("Startup", "", boxed_list([
                pref_row("Open on", "The shelf Babel shows when it starts.", start),
                pref_row("Default sort", "Search results always use best match.", sort),
            ])),
            ("Library folder", "", boxed_list([self.root_row, self._collections_row()])),
            ("Keeping up to date", "", boxed_list([
                pref_row("Update while open", "Watch the folder and add or remove books as files change.",
                         pref_switch("watch")),
                pref_row("Check for new books at startup",
                         "Picks up anything added or removed while Babel was closed.",
                         pref_switch("scan_on_start")),
            ])),
        ]

    def _collections_row(self):
        edit = QLineEdit(prefs().get("collections_folder"))
        edit.setPlaceholderText("Folder name, e.g. Collections")
        edit.setMinimumWidth(190)
        edit.editingFinished.connect(lambda: prefs().set("collections_folder", edit.text().strip()))
        return pref_row("Collections folder",
                        "A folder inside your library whose subfolders are author collections, "
                        "with files named “Last, First - Title (Publisher, Year)”. Leave empty if you "
                        "don't have one.", edit)

    def _reading(self):
        p = prefs()
        apps = installed_readers()
        rows = []
        for group, (label, exts) in FORMAT_GROUPS.items():
            key = "reader_" + group
            c = pref_combo([(a[0], a[1]) for a in apps], p.get(key),
                           lambda v, k=key: p.set(k, v))
            rows.append(pref_row(label, " · ".join("." + e for e in exts), c))
        first = pref_combo([("epub", "EPUB"), ("pdf", "PDF")], p.get("prefer_format"),
                           lambda v: p.set("prefer_format", v))
        kepub = pref_switch("kobo_kepub")
        has_kepubify = bool(shutil.which("kepubify"))
        kepub.setEnabled(has_kepubify)
        convert = pref_switch("kobo_convert_kindle")
        convert.setEnabled(bool(shutil.which("ebook-convert")))
        return [
            ("Open with", "Only readers installed on this computer are listed.", boxed_list(rows)),
            ("Books with several formats", "", boxed_list([
                pref_row("Open first", "Used by double-click, Enter and the main Open button.", first),
            ])),
            ("Kobo", f"Books are copied into a “{KOBO_FOLDER}” folder on the device.", boxed_list([
                pref_row("Send EPUBs in Kobo format (kepub)",
                         "Faster page turns, better fonts and reading stats on the Kobo."
                         + ("" if has_kepubify else " Needs kepubify, which isn’t installed."), kepub),
                pref_row("Convert Kindle-only books",
                         "AZW3 books are converted to EPUB with Calibre, since Kobo can’t read them.",
                         convert),
            ])),
        ]

    def _appearance(self):
        p = prefs()
        size = pref_combo([("small", "Small"), ("medium", "Medium"), ("large", "Large")],
                          p.get("cover_size"), lambda v: p.set("cover_size", v))
        return [
            ("Style", "", boxed_list([
                pref_row("Appearance", "Follow GNOME's light or dark setting, or always use one.",
                         pref_combo([("system", "Follow system"), ("light", "Light"), ("dark", "Dark")],
                                    p.get("theme"), lambda v: p.set("theme", v))),
            ])),
            ("Covers", "", boxed_list([
                pref_row("Cover size", "", size),
                pref_row("Format badges", "EPUB / PDF labels on each cover.", pref_switch("show_format_badges")),
                pref_row("Type badges", "PAPER / MAGAZINE / COMIC labels.", pref_switch("show_type_badges")),
            ])),
            ("Duplicates", "", boxed_list([
                pref_row("Merge duplicate copies",
                         "Show a book once even when the same file sits in several folders.",
                         pref_switch("merge_duplicates")),
            ])),
        ]

    def _library(self):
        self.stat_files = _label("", "rowValue")
        self.stat_unique = _label("", "rowValue")
        self.stat_covers = _label("", "rowValue")
        self.stat_db = _label("", "rowValue")
        self.genre_btn = pref_button("Look Up", self._lookup_genres)
        self.genre_row = pref_row("", "Genres come from each book's own tags, or from the author's "
                                  "occupation on Wikidata. Only author names are sent.", self.genre_btn)
        self.dup_row = pref_row("", "The same book stored in more than one folder.",
                                pref_button("Show Them", self._show_dups))
        self.exact_btn = pref_button("Find…", self._find_exact)
        self.exact_row = pref_row("Exact duplicate files",
                                  "Byte-for-byte identical copies. Babel keeps one of each and moves the "
                                  "rest to the Trash after you confirm; you can restore them from there.",
                                  self.exact_btn)
        return [
            ("Overview", "", boxed_list([
                pref_row("Files", "", self.stat_files),
                pref_row("Unique books", "Duplicate copies counted once.", self.stat_unique),
                pref_row("Cover cache", "", self.stat_covers),
                pref_row("Database", "", self.stat_db),
            ])),
            ("Duplicates", "", boxed_list([
                self.dup_row,
                self.exact_row,
            ])),
            ("Genres", "", boxed_list([
                self.genre_row,
                pref_row("Look up new authors automatically",
                         "After new books are found, ask Wikidata about authors Babel hasn't seen.",
                         pref_switch("auto_genres")),
            ])),
            ("Maintenance", "", boxed_list([
                pref_row("Rescan library", "Look for new, changed and removed books now.",
                         pref_button("Rescan", lambda: self.main.rescan())),
                pref_row("Rebuild everything",
                         "Re-reads every book and re-extracts every cover. Takes about an hour.",
                         pref_button("Rebuild", self._rebuild)),
                pref_row("Clear reading history",
                         "Empties Recently Opened. Favourites and shelves are kept.",
                         pref_button("Clear", self._clear_history, "destructive")),
                pref_row("Data folder", scanner.DATA_DIR,
                         pref_button("Open", lambda: QDesktopServices.openUrl(
                             QUrl.fromLocalFile(scanner.DATA_DIR)))),
            ])),
            ("Helper tools", "Optional programs Babel uses when they’re installed.", boxed_list([
                pref_row(purpose, f"{cmd}  ·  package: {pkg}",
                         _label("Installed" if shutil.which(cmd) else "Not found",
                                "toolOk" if shutil.which(cmd) else "toolMissing"))
                for cmd, pkg, purpose in HELPER_TOOLS
            ])),
        ]

    def _shortcuts(self):
        keys = [("/   or   Ctrl+F", "Search"), ("Enter", "Open the selected book"),
                ("Esc", "Clear the search, then the filters"), ("Ctrl+D", "Favourite / unfavourite"),
                ("Ctrl+R", "Rescan the library"), ("Ctrl+K", "Send selected books to Kobo"),
                ("Ctrl / Shift + click", "Select several books"), ("Ctrl+,", "Preferences"),
                ("Ctrl+Q   or   Ctrl+W", "Quit"), ("Double-click top bar", "Maximise / restore")]
        rows = []
        for k, action in keys:
            kl = _label(k, "keycap")
            rows.append(pref_row(action, "", kl))
        return [("", "", boxed_list(rows))]

    def _about(self):
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 30, 0, 0)
        v.setSpacing(8)
        icon = QLabel()
        icon.setPixmap(QIcon(ICON_PATH).pixmap(112, 112))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(icon)
        name = _label("Babel", "aboutName")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(name)
        ver = _label(f"Version {VERSION}", "rowSub")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(ver)
        blurb = _label("A library for your Books folder.\n"
                       "Named after Borges’ “The Library of Babel”, the universe made of "
                       "endless hexagonal galleries of books.", "aboutText", wrap=True)
        blurb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addSpacing(8)
        v.addWidget(blurb)
        return [("", "", box)]

    # --- actions
    def refresh_stats(self):
        lib = self.main.lib
        self.stat_files.setText(f"{lib.count():,}")
        self.stat_unique.setText(f"{lib.unique_count():,}")
        try:
            names = os.listdir(scanner.COVERS_DIR)
            size = sum(os.path.getsize(os.path.join(scanner.COVERS_DIR, n)) for n in names)
            self.stat_covers.setText(f"{len(names):,} covers · {human_size(size)}")
        except OSError:
            self.stat_covers.setText("—")
        try:
            self.stat_db.setText(human_size(os.path.getsize(scanner.DB_PATH)))
        except OSError:
            self.stat_db.setText("—")
        n, size = lib.duplicate_stats()
        self.dup_row.title_label.setText(f"{n:,} extra copies using {human_size(size)}")
        self.root_row.title_label.setText(prefs().get("library_root"))
        known, total = lib.genre_stats()
        self.genre_row.title_label.setText(f"{known:,} of {total:,} authors have a genre")

    def _change_root(self):
        d = QFileDialog.getExistingDirectory(self, "Choose your books folder", prefs().get("library_root"))
        if d and d != prefs().get("library_root"):
            prefs().set("library_root", d)
            self.root_row.title_label.setText(d)

    def _lookup_genres(self):
        self.genre_btn.setEnabled(False)
        self.genre_btn.setText("Looking up…")
        self.main.lookup_genres(on_done=self._genres_done,
                                on_progress=lambda d, t: self.genre_btn.setText(f"Looking up… {d}/{t}"))

    def _genres_done(self, n, found):
        self.genre_btn.setEnabled(True)
        self.genre_btn.setText("Look Up")
        self.refresh_stats()

    def _find_exact(self):
        if not scanner.BOOKS_ROOT or not os.path.isdir(scanner.BOOKS_ROOT):
            QMessageBox.information(self, "Library folder not available",
                                    "Connect the drive with your books first.")
            return
        self.exact_btn.setEnabled(False)
        self.exact_btn.setText("Checking…")
        self.finder = DupFinder()
        self.finder.progress.connect(lambda d, t: self.exact_btn.setText(
            f"Checking… {int(100 * d / t) if t else 100}%"))
        self.finder.found.connect(self._exact_found)
        self.finder.start()

    def _exact_found(self, plan):
        self.exact_btn.setEnabled(True)
        self.exact_btn.setText("Find…")
        if not plan:
            QMessageBox.information(self, "No exact duplicates",
                                    "Every book file in your library is unique. Nothing to clean up.")
            return
        size = sum(i["size"] for i in plan)
        root = scanner.BOOKS_ROOT + os.sep
        box = QMessageBox(self)
        box.setWindowTitle("Move duplicates to the Trash?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Found {len(plan):,} exact duplicate cop{'y' if len(plan) == 1 else 'ies'} "
                    f"using {human_size(size)}.")
        box.setInformativeText("Babel keeps one copy of each book (in the fuller collection) and moves "
                               "the others to the Trash, with their covers and any folders left empty. "
                               "Favourites and reading status move to the copy that stays.\n\n"
                               "You can restore anything from the Trash. Click “Show Details” for the list.")
        box.setDetailedText("\n\n".join(
            f"Remove: {i['drop'].replace(root, '')}\nKeep:   {i['keep'].replace(root, '')}" for i in plan))
        move = box.addButton("Move to Trash", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not move:
            return
        self.exact_btn.setEnabled(False)
        self.exact_btn.setText("Moving…")
        self.trasher = DupTrasher(plan)
        self.trasher.done.connect(self._exact_done)
        self.trasher.start()

    def _exact_done(self, files, size, covers, folders):
        self.exact_btn.setEnabled(True)
        self.exact_btn.setText("Find…")
        extra = []
        if covers:
            extra.append(f"{covers:,} cover image{'s' if covers != 1 else ''}")
        if folders:
            extra.append(f"{folders:,} empty folder{'s' if folders != 1 else ''}")
        QMessageBox.information(
            self, "Duplicates moved to the Trash",
            f"Moved {files:,} duplicate file{'s' if files != 1 else ''} ({human_size(size)}) to the Trash"
            + (f", plus {' and '.join(extra)}" if extra else "") + ".\n\n"
            "Empty the Trash to free the space, or restore anything from it.")
        self.main.rescan(quiet=True)
        self.refresh_stats()

    def _show_dups(self):
        self.close()
        self.main.show_only(shelf="dups")

    def _rebuild(self):
        if QMessageBox.question(self, "Rebuild everything?",
                                "Babel will re-read every book and re-extract every cover. "
                                "This takes about an hour; you can keep using Babel meanwhile.") \
                == QMessageBox.StandardButton.Yes:
            self.main.rescan(full=True, rebuild=True)

    def _clear_history(self):
        if QMessageBox.question(self, "Clear reading history?",
                                "Recently Opened will be emptied. Favourites and shelves are kept.") \
                == QMessageBox.StandardButton.Yes:
            self.main.lib.clear_history()
            self.main.reload_sidebar()
            self.main.refresh()


NAV = [
    ("head", "Library"),
    ("shelf:all", "All Items"),
    ("shelf:fav", "Favourites"),
    ("shelf:want", "Want to Read"),
    ("shelf:reading", "Reading"),
    ("shelf:finished", "Finished"),
    ("shelf:opened", "Recently Opened"),
    ("shelf:added", "Recently Added"),
    ("head", "Type"),
    ("kind:Book", "Books"),
    ("kind:Paper", "Papers & Articles"),
    ("kind:Magazine", "Magazines"),
    ("kind:Comic", "Comics"),
    ("head", "Format"),
    ("fmt:EPUB", "EPUB"),
    ("fmt:PDF", "PDF"),
    ("fmt:MOBI,AZW3", "Kindle (MOBI / AZW3)"),
]

SHELF_LABELS = dict(NAV) | {"shelf:dups": "Duplicate Copies"}

# format pills shown when viewing a collection or author: (label, formats or None for all)
FORMAT_FILTERS = [("All", None), ("EPUB", ["EPUB"]), ("PDF", ["PDF"]),
                  ("Kindle", ["MOBI", "AZW3"]), ("Comics", ["CBR", "CBZ"]), ("DjVu", ["DJVU"])]

SORTS = [("relevance", "Best match"), ("title", "Title A–Z"), ("author", "Author A–Z"),
         ("year_desc", "Newest first"), ("year_asc", "Oldest first"),
         ("added", "Recently added"), ("opened", "Recently opened")]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Babel")
        self.setWindowIcon(QIcon(ICON_PATH))
        scanner.BOOKS_ROOT = prefs().get("library_root")
        scanner.COLLECTIONS_FOLDER = prefs().get("collections_folder")
        apply_cover_size(prefs().get("cover_size"))
        self.lib = Library()
        self.settings = QSettings("Babel", "Babel")
        self.state = dict(shelf="all", kind=None, fmt=None, collection=None, author=None, genre=None)
        self.prefs_dialog = None
        self.scan_worker = None

        central = WindowFrame()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        self.frame_layout = outer
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        self.splitter = QSplitter()
        self.splitter.setHandleWidth(1)
        outer.addWidget(self.splitter, 1)
        # our own status bar, inside the rounded frame
        self._status = QStatusBar()
        self._status.setSizeGripEnabled(False)
        outer.addWidget(self._status)

        # --- sidebar
        side = QWidget()
        side.setObjectName("sidebar")
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)
        self.brand_row = QWidget()
        brand = QHBoxLayout(self.brand_row)
        brand.setContentsMargins(18, 18, 18, 10)
        logo = QLabel()
        logo.setPixmap(QIcon(ICON_PATH).pixmap(34, 34))
        name = QLabel("BABEL")
        f = name.font()
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3.5)   # inscription-style capitals
        name.setFont(f)
        name.setObjectName("brand")
        brand.addWidget(logo)
        brand.addWidget(name)
        brand.addStretch(1)
        self.menu_btn = QToolButton()
        self.menu_btn.setObjectName("menuButton")
        themed_icon(self.menu_btn, MENU_ICON)
        self.menu_btn.setIconSize(QSize(16, 16))
        self.menu_btn.setFixedSize(34, 34)
        self.menu_btn.setToolTip("Main menu")
        self.menu_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = smooth_menu(self.menu_btn)
        menu.addAction("Preferences\tCtrl+,", lambda: self.open_preferences())
        menu.addAction("Keyboard Shortcuts", lambda: self.open_preferences("Shortcuts"))
        menu.addSeparator()
        menu.addAction("Rescan Library\tCtrl+R", lambda: self.rescan())
        menu.addAction("Show Duplicate Copies", lambda: self.show_only(shelf="dups"))
        menu.addSeparator()
        menu.addAction("About Babel", lambda: self.open_preferences("About"))
        self.menu_btn.setMenu(menu)
        brand.addWidget(self.menu_btn)
        sv.addWidget(self.brand_row)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("sideTabs")
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setMouseTracking(True)
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setItemDelegate(NavDelegate(self.nav, selected_text=None))
        self.nav.itemClicked.connect(self._nav_clicked)
        self.tabs.addTab(self.nav, "Library")
        self.coll_list = FilterList("Filter collections…")
        self.coll_list.chosen.connect(lambda c: self.show_only(collection=c))
        self.tabs.addTab(self.coll_list, "Collections")
        self.auth_list = FilterList("Filter authors…")
        self.auth_list.chosen.connect(lambda a: self.show_only(author=a))
        self.tabs.addTab(self.auth_list, "Authors")
        sv.addWidget(self.tabs, 1)
        self.splitter.addWidget(side)

        # --- centre
        centre = QWidget()
        centre.setObjectName("centre")
        self.centre = centre
        cv = QVBoxLayout(centre)
        cv.setContentsMargins(22, 16, 10, 0)
        cv.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        self.search.returnPressed.connect(self._search_enter)
        self.search_timer = QTimer(self, singleShot=True, interval=160)
        self.search_timer.timeout.connect(self.refresh)
        self.sort = QComboBox()
        self.sort.setItemDelegate(QStyledItemDelegate(self.sort))
        for code, label in SORTS:
            self.sort.addItem(label, code)
        self.sort.currentIndexChanged.connect(lambda _: self.refresh(keep_sort=True))
        surprise = QPushButton(" Surprise me")
        themed_icon(surprise, DICE_ICON)
        surprise.setIconSize(QSize(16, 16))
        surprise.setObjectName("secondary")
        surprise.setCursor(Qt.CursorShape.PointingHandCursor)
        surprise.clicked.connect(self.surprise)
        self.rescan_btn = QPushButton(" Rescan")
        themed_icon(self.rescan_btn, REFRESH_ICON)
        self.rescan_btn.setIconSize(QSize(16, 16))
        self.rescan_btn.setObjectName("secondary")
        self.rescan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rescan_btn.clicked.connect(lambda: self.rescan())
        bar.addWidget(self.search, 1)
        self.genre_box = QComboBox()
        self.genre_box.setItemDelegate(QStyledItemDelegate(self.genre_box))
        self.genre_box.setToolTip("Show one genre within the current view")
        self.genre_box.addItem("All genres", None)
        self.genre_box.currentIndexChanged.connect(self._genre_picked)
        self.genre_box.setMinimumWidth(150)
        self.genre_box.setMaximumWidth(200)
        self.sort.setMaximumWidth(180)
        self.genre_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.genre_box.view().setMinimumWidth(230)        # the open list may be wider than the box
        self.search.setMinimumWidth(260)
        bar.addWidget(self.genre_box)
        bar.addWidget(self.sort)
        bar.addWidget(surprise)
        bar.addWidget(self.rescan_btn)
        cv.addLayout(bar)

        head = QHBoxLayout()
        self.heading = QLabel("All Items")
        self.heading.setObjectName("heading")
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        head.addWidget(self.heading)
        head.addSpacing(10)
        head.addWidget(self.count_label)
        head.addSpacing(10)
        head.addLayout(self.chips)
        head.addStretch(1)
        self.fmt_box = QWidget()
        self.fmt_pills = QHBoxLayout(self.fmt_box)
        self.fmt_pills.setContentsMargins(0, 0, 8, 0)
        self.fmt_pills.setSpacing(6)
        self.fmt_box.hide()
        head.addWidget(self.fmt_box)
        cv.addLayout(head)

        self.model = BookModel(self.devicePixelRatioF())
        self.view = QListView()
        self.view.setObjectName("grid")
        self.view.setModel(self.model)
        self.view.setItemDelegate(BookDelegate(self.model, self.view))
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(6)
        self.view.setMouseTracking(True)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._grid_menu)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(30)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.selectionModel().currentChanged.connect(self._current_changed)
        self.view.doubleClicked.connect(lambda idx: self.open_book(idx.data(BookModel.KeyRole)))
        cv.addWidget(self.view, 1)

        self.empty_label = QLabel("Nothing here yet.")
        self.empty_label.setObjectName("muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        self.empty_label.setWordWrap(True)
        cv.addWidget(self.empty_label, 1)
        self.empty_btn = QPushButton("Choose Books Folder…")
        self.empty_btn.setObjectName("primary")
        self.empty_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.empty_btn.clicked.connect(self.choose_folder)
        self.empty_btn.hide()
        cv.addWidget(self.empty_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        cv.addSpacing(0)
        self.splitter.addWidget(centre)

        # --- details
        self.details = DetailsPanel()
        self.details.copies_of = self.lib.copies
        self.details.main_lib = self.lib
        self.details.open_file.connect(self.open_path)
        self.details.filter_author.connect(lambda a: self.show_only(author=a))
        self.details.filter_collection.connect(lambda c: self.show_only(collection=c))
        self.details.fav_changed.connect(self.set_fav)
        self.details.status_changed.connect(self.set_status)
        self.details.send_to_kobo.connect(lambda k: self.send_to_kobo([k]))
        self.details.kind_changed.connect(lambda k, kind: self.set_kind([k], kind))
        self.details.genre_changed.connect(lambda k, g: self.set_genre([k], g))
        col = QWidget()
        col.setObjectName("detailsColumn")
        col.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        cl = QVBoxLayout(col)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.details_header = QWidget()
        self.details_header.setObjectName("detailsHeader")
        self.details_header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.details_header.setFixedHeight(46)
        hl = QHBoxLayout(self.details_header)
        # GNOME header bars are ~46px tall with the close button centred in them
        hl.setContentsMargins(12, 11, 11, 11)
        hl.addStretch(1)
        self.close_btn = QPushButton()
        self.close_btn.setObjectName("closeButton")
        themed_icon(self.close_btn, CLOSE_ICON)
        self.close_btn.setIconSize(QSize(16, 16))
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.setToolTip("Close")
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.clicked.connect(self.close)
        hl.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        cl.addWidget(self.details_header)
        cl.addWidget(self.details, 1)
        self.splitter.addWidget(col)
        self.panels = [side, col, self._status]
        self.splitter.splitterMoved.connect(lambda *_: central.update())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([250, 1000, 370])

        # status bar
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.kobo_box = QWidget()
        kb = QHBoxLayout(self.kobo_box)
        kb.setContentsMargins(0, 0, 6, 0)
        kb.setSpacing(8)
        self.kobo_label = QLabel()
        self.kobo_label.setObjectName("koboLabel")
        self.eject_btn = QPushButton("Eject")
        self.eject_btn.setObjectName("chip")
        self.eject_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.eject_btn.clicked.connect(self.eject_kobo)
        kb.addWidget(self.kobo_label)
        kb.addWidget(self.eject_btn)
        self.kobo_box.hide()
        self.statusBar().addPermanentWidget(self.kobo_box)
        self.kobo = None
        self.kobo_sender = None
        self.kobo_timer = QTimer(self, interval=3000)
        self.kobo_timer.timeout.connect(self.check_kobo)
        self.kobo_timer.start()
        QTimer.singleShot(500, self.check_kobo)

        # window chrome: drag to move, double-click to maximize, edges to resize
        self._press_pos = None
        for w_ in (self.brand_row, self.centre, self.details_header):
            w_.installEventFilter(self)
        E = Qt.Edge
        self.grips = [EdgeGrip(central, e) for e in (
            E.LeftEdge, E.RightEdge, E.TopEdge, E.BottomEdge,
            E.TopEdge | E.LeftEdge, E.TopEdge | E.RightEdge,
            E.BottomEdge | E.LeftEdge, E.BottomEdge | E.RightEdge)]
        for seq in ("Ctrl+Q", "Ctrl+W"):
            QShortcut(QKeySequence(seq), self, activated=self.close)

        # shortcuts
        for seq in ("/", "Ctrl+F", "Ctrl+L"):
            QShortcut(QKeySequence(seq), self, activated=self._focus_search)
        QShortcut(QKeySequence("Escape"), self, activated=self._escape)
        QShortcut(QKeySequence("Ctrl+D"), self, activated=self._toggle_fav_current)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=lambda: self.rescan())
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self.open_preferences())
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.send_selected_to_kobo)
        for seq in ("Return", "Enter"):
            QShortcut(QKeySequence(seq), self.view, activated=self._open_current,
                      context=Qt.ShortcutContext.WidgetShortcut)

        # Babel remembers its floating size and whether it was maximised separately.
        # (Qt's saveGeometry() on Wayland records the maximised size as the "normal"
        # one, so dragging a window that opened maximised restored it to ~full screen.)
        size = self.settings.value("normal_size")
        scr = QApplication.primaryScreen()
        if isinstance(size, QSize) and size.isValid():
            w_, h_ = size.width(), size.height()
        else:
            w_, h_ = 1500, 950
        if scr is not None:      # never start out (nearly) as big as the screen when floating
            g = scr.availableGeometry()
            w_, h_ = min(w_, int(g.width() * 0.85)), min(h_, int(g.height() * 0.85))
        self.normal_size = QSize(w_, h_)
        self.resize(self.normal_size)
        self.start_maximized = self.settings.value("maximized", False) in (True, "true", "1", 1)
        sizes = self.settings.value("splitter")
        if sizes:
            self.splitter.restoreState(sizes)

        self.queued_full = False
        self.queued_deep, self.queued_shallow = set(), set()
        self.scan_quiet = False
        self.scan_scope = (None, None)

        self._apply_start_view()
        self._set_sort(prefs().get("default_sort"))
        self.reload_sidebar()
        self.refresh()
        # pick up anything that changed while Babel was closed, then keep watching
        root_ok = bool(scanner.BOOKS_ROOT) and os.path.isdir(scanner.BOOKS_ROOT)
        if root_ok and self.lib.count() == 0:
            QTimer.singleShot(300, lambda: self.rescan())
        elif root_ok and prefs().get("scan_on_start"):
            QTimer.singleShot(1500, lambda: self.rescan(quiet=True))
        self.watcher = LibraryWatcher(scanner.BOOKS_ROOT)
        self.watcher.changed.connect(lambda deep, flat: self.rescan(deep, flat, quiet=True))
        self.watcher.on_remount = lambda: self.rescan(quiet=True)
        if prefs().get("watch") and root_ok:
            QTimer.singleShot(4000, self.watcher.start)
        prefs().changed.connect(self.apply_pref)
        QApplication.instance().styleHints().colorSchemeChanged.connect(lambda *_: self.apply_theme())
        self.apply_theme()
        if not scanner.BOOKS_ROOT:
            QTimer.singleShot(400, self.welcome)
        elif not root_ok:
            self.statusBar().showMessage(
                f"Your library folder isn’t available ({scanner.BOOKS_ROOT}). Is the drive connected?")

    # ----- sidebar
    def reload_sidebar(self):
        c = self.lib.counts(prefs().get("merge_duplicates"))
        self.nav.clear()
        for code, label in NAV:
            if code == "head":
                it = QListWidgetItem(label.upper())
                it.setFlags(Qt.ItemFlag.NoItemFlags)
                f = it.font()
                f.setPixelSize(11)
                f.setWeight(QFont.Weight.Bold)
                f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
                it.setFont(f)
                it.setForeground(QColor(MUTED))
                it.setSizeHint(QSize(0, 38))
            else:
                kind, val = code.split(":", 1)
                if kind == "fmt":
                    n = c.get(code, 0)
                else:
                    n = c.get(code.replace("shelf:", ""), c.get(code, 0))
                if kind in ("kind", "genre") and not n:
                    continue
                it = QListWidgetItem(f"{label}")
                it.setData(Qt.ItemDataRole.UserRole, code)
                it.setData(Qt.ItemDataRole.UserRole + 1, n)
                it.setSizeHint(QSize(0, 32))
            self.nav.addItem(it)
        self._mark_nav()
        cc, ac = self.lib.coll_counts, self.lib.auth_counts
        self.coll_list.set_items([(c_, c_, cc.get(c_, n)) for c_, n in self.lib.collections()])
        self.genre_box.blockSignals(True)
        self.genre_box.clear()
        self.genre_box.addItem("All genres", None)
        for g in scanner.GENRES:
            if c.get("genre:" + g):
                self.genre_box.addItem(g, g)
        self.genre_box.setCurrentIndex(max(0, self.genre_box.findData(self.state.get("genre"))))
        self.genre_box.blockSignals(False)
        self.auth_list.set_items([(a, a, ac.get(a, n)) for a, _, n in self.lib.authors()])
        total = c["all"]
        self.search.setPlaceholderText(f"Search {total:,} items by title, author, collection, subject…   ( / )")

    def _mark_nav(self):
        s = self.state
        active = set()
        if not (s["shelf"] == "all" and (s["kind"] or s["fmt"])):
            active.add(f"shelf:{s['shelf']}")
        if s["kind"]:
            active.add(f"kind:{s['kind']}")
        if s["fmt"]:
            active.add("fmt:" + ",".join(s["fmt"]))
        if s.get("genre"):
            active.add("genre:" + s["genre"])
        for i in range(self.nav.count()):
            it = self.nav.item(i)
            code = it.data(Qt.ItemDataRole.UserRole)
            if not code:
                continue
            on = code in active
            f = it.font()
            f.setWeight(QFont.Weight.DemiBold if on else QFont.Weight.Normal)
            it.setFont(f)
            it.setForeground(QColor(ACCENT_TEXT if on else TEXT))
            it.setData(Qt.ItemDataRole.UserRole + 5, on)

    def _nav_clicked(self, it):
        code = it.data(Qt.ItemDataRole.UserRole)
        if not code:
            return
        kind, val = code.split(":", 1)
        if kind == "shelf":
            self.show_only(shelf=val)
        elif kind == "kind":
            self.show_only(kind=val)
        elif kind == "fmt":
            self.show_only(fmt=val.split(","))
        elif kind == "genre":
            self.show_only(genre=val)

    def show_only(self, **kw):
        """Start fresh: clear the search and every filter, then show just this shelf/author/collection."""
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.search_timer.stop()
        self.state = dict(shelf="all", kind=None, fmt=None, collection=None, author=None, genre=None)
        self.state.update(kw)
        shelf_sort = {"opened": "opened", "added": "added"}.get(self.state["shelf"])
        self._set_sort(shelf_sort or prefs().get("default_sort"))
        self.refresh()

    def _genre_picked(self, _):
        # a filter on top of the current view (Books + Philosophy), keeping the search
        self.state["genre"] = self.genre_box.currentData()
        self.refresh()

    def set_filter(self, **kw):
        self.state.update(kw)
        if "shelf" in kw:
            default = {"opened": "opened", "added": "added"}.get(kw["shelf"])
            if default:
                self.sort.blockSignals(True)
                self.sort.setCurrentIndex([c for c, _ in SORTS].index(default))
                self.sort.blockSignals(False)
        self.refresh()

    # ----- the grid
    def refresh(self, keep_sort=False):
        s = self.state
        text = self.search.text().strip()
        rows = self.lib.query(text, s["shelf"], s["kind"], s["fmt"], s["collection"], s["author"],
                              merge=prefs().get("merge_duplicates"), genre=s.get("genre"))
        mode = self.sort.currentData()
        if mode == "relevance" and not text:
            mode = "title" if not (s["author"] or s["collection"]) else "year_asc"
        if mode == "relevance":
            # bm25 is negative (lower = better); damp papers/magazines so books lead
            weight = {"Book": 1.0, "Comic": 0.8, "Paper": 0.55, "Magazine": 0.5}
            rows.sort(key=lambda r: r["rank"] * weight.get(r["kind"], 1.0))
        elif mode == "title":
            rows.sort(key=lambda r: title_key(r["title"]))
        elif mode == "author":
            rows.sort(key=lambda r: ((r["author_sort"] or "~").lower(), title_key(r["title"])))
        elif mode == "year_desc":
            rows.sort(key=lambda r: (-(r["year"] or 0), title_key(r["title"])))
        elif mode == "year_asc":
            rows.sort(key=lambda r: (r["year"] or 9999, title_key(r["title"])))
        elif mode == "added":
            rows.sort(key=lambda r: (-(r["added"] or 0), title_key(r["title"])))
        elif mode == "opened":
            rows.sort(key=lambda r: -(r["last_opened"] or 0))

        current = self.view.currentIndex().data(BookModel.KeyRole)
        self.model.set_rows(rows)
        if current in self.model.index_of:
            self.view.setCurrentIndex(self.model.index(self.model.index_of[current]))
        else:
            self.view.scrollToTop()

        heading = SHELF_LABELS.get(f"shelf:{s['shelf']}", "All Items")
        if s["shelf"] == "all" and s.get("genre") and not (s["kind"] or s["fmt"]):
            heading = s["genre"]
        elif s["shelf"] == "all" and s["kind"]:
            heading = SHELF_LABELS[f"kind:{s['kind']}"]
        elif s["shelf"] == "all" and s["fmt"]:
            heading = SHELF_LABELS.get("fmt:" + ",".join(s["fmt"]), " / ".join(s["fmt"]))
        if s["author"]:
            heading = s["author"]
        elif s["collection"]:
            heading = s["collection"]
        self.heading.setText(heading)
        self.heading.setToolTip(heading)
        n_desc = sum(1 for r in rows if r.get("via_desc"))
        if text and rows and n_desc == len(rows):
            self.count_label.setText(f'No books by or titled "{text}" · {n_desc:,} mention it in their '
                                     f'description{"s" if n_desc != 1 else ""}')
        elif text and n_desc:
            self.count_label.setText(f'{len(rows):,} items matching "{text}" · '
                                     f'{n_desc:,} only mention it in their description{"s" if n_desc != 1 else ""}')
        else:
            self.count_label.setText(f"{len(rows):,} item{'s' if len(rows) != 1 else ''}"
                                     + (f' matching "{text}"' if text else ""))
        self.search_text = text
        self._render_format_pills(text)
        if hasattr(self, "genre_box"):          # keep the dropdown showing the current genre
            self.genre_box.blockSignals(True)
            self.genre_box.setCurrentIndex(max(0, self.genre_box.findData(self.state.get("genre"))))
            self.genre_box.blockSignals(False)
        self._render_chips()
        self._mark_nav()
        has = bool(rows)
        self.view.setVisible(has)
        self.empty_label.setVisible(not has)
        root = scanner.BOOKS_ROOT
        missing = not root or not os.path.isdir(root)
        self.empty_btn.setVisible(not has and missing)
        if not has:
            if not root:
                self.empty_label.setText("Choose the folder that holds your books to get started.")
            elif missing:
                self.empty_label.setText(f"Your library folder isn’t available:\n{root}\n\n"
                                         "Is the drive connected? Or choose a different folder.")
            elif self.lib.count() == 0:
                self.empty_label.setText("Building your library… covers are being collected.")
            elif text:
                self.empty_label.setText(f'No matches for "{text}".')
            else:
                self.empty_label.setText("New books you add to the Books folder will show up here." if self.state["shelf"] == "added" else "Nothing on this shelf yet.")

    def _render_format_pills(self, text):
        """EPUB / PDF / … pills for narrowing a collection or author to one format."""
        while self.fmt_pills.count():
            w_ = self.fmt_pills.takeAt(0).widget()
            if w_:
                w_.deleteLater()
        s = self.state
        if not (s["author"] or s["collection"] or s.get("genre")):
            self.fmt_box.hide()
            return
        # count formats in the whole collection/author view, ignoring any format already picked
        base = self.lib.query(text, s["shelf"], s["kind"], None, s["collection"], s["author"], genre=s.get("genre"),
                              merge=prefs().get("merge_duplicates"))
        counts = []
        for label, fmts in FORMAT_FILTERS:
            n = len(base) if fmts is None else sum(
                1 for r in base if any(f in (r["formats"] or "").split(",") for f in fmts))
            if n:
                counts.append((label, fmts, n))
        if len(counts) <= 2:          # 'All' plus a single format: nothing to choose between
            self.fmt_box.hide()
            return
        for label, fmts, n in counts:
            btn = QPushButton(f"{label}  {n:,}")
            btn.setObjectName("segment")
            btn.setCheckable(True)
            btn.setChecked(s["fmt"] == fmts)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _, f=fmts: self.set_filter(fmt=f))
            self.fmt_pills.addWidget(btn)
        self.fmt_box.show()

    def _render_chips(self):
        while self.chips.count():
            w = self.chips.takeAt(0).widget()
            if w:
                w.deleteLater()
        s = self.state
        chips = []
        if s["shelf"] != "all" and (s["author"] or s["collection"]):
            chips.append((SHELF_LABELS[f"shelf:{s['shelf']}"], dict(shelf="all")))
        kind_is_heading = s["kind"] and s["shelf"] == "all" and not (s["author"] or s["collection"])
        if s["kind"] and not kind_is_heading:
            chips.append((SHELF_LABELS[f"kind:{s['kind']}"], dict(kind=None)))
        fmt_is_heading = (s["fmt"] and s["shelf"] == "all" and not (s["kind"] or s["author"] or s["collection"])) \
            or (s["fmt"] and (s["author"] or s["collection"]))      # shown by the format pills instead
        if s["fmt"] and not fmt_is_heading:
            chips.append((" / ".join(s["fmt"]), dict(fmt=None)))
        if s["author"] and s["collection"]:
            chips.append((s["collection"][:40], dict(collection=None)))
        elif s["author"]:
            chips.append(("Author", dict(author=None)))
        elif s["collection"]:
            chips.append(("Collection", dict(collection=None)))
        for label, reset in chips:
            b = QPushButton(f"{label}  ✕")
            b.setObjectName("chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, r=reset: self.set_filter(**r))
            self.chips.addWidget(b)

    def _current_changed(self, cur, _prev):
        key = cur.data(BookModel.KeyRole) if cur.isValid() else None
        row = cur.data(BookModel.RowRole) if cur.isValid() else None
        # show the matching sentence only for books that came up through their description
        self.details.search = getattr(self, "search_text", "") if row and row.get("via_desc") else ""
        self.details.show_book(self.lib.book(key) if key else None)

    # ----- actions
    def open_book(self, key):
        b = self.lib.book(key) if key else None
        if b:
            self.open_path(key, ordered_files(json.loads(b["files"]))[0])

    def open_path(self, key, path):
        if not os.path.exists(path):
            self.statusBar().showMessage(f"File not found: {path}", 6000)
            return
        open_with_reader(path)
        self.lib.mark_opened(key)
        self.model.update_row(key, last_opened=time.time())
        self.statusBar().showMessage(f"Opening {os.path.basename(path)}", 4000)
        if self.details.key == key:
            self.details.show_book(self.lib.book(key))

    # ----- Kobo
    def check_kobo(self):
        found = find_kobo()
        was = self.kobo[0] if self.kobo else None
        self.kobo = found
        now = found[0] if found else None
        if found:
            self.kobo_label.setText(f"Kobo connected · {human_size(found[2])} free")
        self.kobo_box.setVisible(bool(found))
        if now != was:
            self.details.kobo_root = now
            if self.details.key:
                self.details.show_book(self.lib.book(self.details.key))
            if found:
                self.statusBar().showMessage("Kobo connected. Select books and press Ctrl+K to send them.", 8000)

    def selected_keys(self):
        keys = [i.data(BookModel.KeyRole) for i in self.view.selectionModel().selectedIndexes()]
        if not keys and self.view.currentIndex().isValid():
            keys = [self.view.currentIndex().data(BookModel.KeyRole)]
        return keys

    def send_selected_to_kobo(self):
        self.send_to_kobo(self.selected_keys())

    def send_to_kobo(self, keys):
        if not keys:
            return
        if not self.kobo:
            self.statusBar().showMessage("Connect your Kobo with a USB cable first.", 6000)
            return
        if self.kobo_sender and self.kobo_sender.isRunning():
            self.statusBar().showMessage("Still sending the last batch to your Kobo…", 4000)
            return
        books = [b for b in (self.lib.book(k) for k in keys) if b]
        self.kobo_sender = KoboSender(books, self.kobo[0])
        self.kobo_sender.progress.connect(self._kobo_progress)
        self.kobo_sender.finished_send.connect(self._kobo_done)
        self.progress.show()
        self.progress.setRange(0, len(books))
        self.eject_btn.setEnabled(False)
        self.kobo_sender.start()

    def _kobo_progress(self, i, n, title):
        self.progress.setValue(i)
        if title:
            self.statusBar().showMessage(f"Sending to Kobo {i + 1}/{n}: {title}")

    def _kobo_done(self, sent, skipped, failed):
        self.progress.hide()
        self.eject_btn.setEnabled(True)
        bits = []
        if sent:
            bits.append(f"{sent} sent to your Kobo")
        if skipped:
            bits.append(f"{skipped} already on it")
        if failed:
            bits.append(f"{len(failed)} failed: " + "; ".join(failed[:3]))
        self.statusBar().showMessage(" · ".join(bits) or "Nothing to send", 12000)
        self.check_kobo()
        if self.details.key:
            self.details.show_book(self.lib.book(self.details.key))

    def eject_kobo(self):
        if not self.kobo:
            return
        root, device, _ = self.kobo
        os.sync()
        ok = True
        if device:
            r = subprocess.run(["udisksctl", "unmount", "-b", device], capture_output=True, text=True)
            ok = r.returncode == 0
            if ok:
                subprocess.run(["udisksctl", "power-off", "-b", device], capture_output=True)
        if ok:
            self.statusBar().showMessage("Your Kobo is safe to unplug.", 10000)
        else:
            self.statusBar().showMessage("Couldn’t eject the Kobo; is a file on it still open?", 8000)
        self.check_kobo()

    def _grid_menu(self, pos):
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        if not self.view.selectionModel().isSelected(idx):
            self.view.setCurrentIndex(idx)
        keys = self.selected_keys()
        n = len(keys)
        m = smooth_menu(self)
        m.addAction("Open", lambda: self.open_book(idx.data(BookModel.KeyRole)))
        a = m.addAction(f"Send {n} book{'s' if n != 1 else ''} to Kobo\tCtrl+K" if self.kobo
                        else "Send to Kobo (not connected)", lambda: self.send_to_kobo(keys))
        a.setEnabled(bool(self.kobo))
        m.addSeparator()
        m.addAction("Add to Favourites", lambda: [self.set_fav(k, True) for k in keys])
        status_menu = smooth_menu(m)
        status_menu.setTitle("Move to Shelf")
        m.addMenu(status_menu)
        kind_menu = smooth_menu(m)
        kind_menu.setTitle("Change Type")
        m.addMenu(kind_menu)
        for kind in KINDS:
            kind_menu.addAction(kind, lambda k=kind: self.set_kind(keys, k))
        genre_menu = smooth_menu(m)
        genre_menu.setTitle("Change Genre")
        m.addMenu(genre_menu)
        for g in scanner.GENRES:
            genre_menu.addAction(g, lambda g=g: self.set_genre(keys, g))
        genre_menu.addSeparator()
        genre_menu.addAction("Automatic", lambda: self.set_genre(keys, ""))
        authors = sorted({(self.lib.book(k) or {}).get("author") or "" for k in keys} - {""})
        if len(authors) == 1:
            who = authors[0]
            auth_menu = smooth_menu(m)
            auth_menu.setTitle(f"Set Genre for Everything by {who}")
            m.addMenu(auth_menu)
            for g in scanner.GENRES:
                auth_menu.addAction(g, lambda g=g: self.set_author_genre(who, g))
            auth_menu.addSeparator()
            auth_menu.addAction("Automatic", lambda: self.set_author_genre(who, ""))
        for code, label in STATUSES:
            status_menu.addAction(label, lambda c=code: [self.set_status(k, c) for k in keys])
        m.addSeparator()
        b = self.lib.book(idx.data(BookModel.KeyRole))
        if b:
            m.addAction("Show in Folder", lambda: show_in_folder(json.loads(b["files"])[0]))
        m.exec(self.view.viewport().mapToGlobal(pos))
        if self.details.key:
            self.details.show_book(self.lib.book(self.details.key))

    def _open_current(self):
        self.open_book(self.view.currentIndex().data(BookModel.KeyRole))

    def set_fav(self, key, on):
        self.lib.set_fav(key, on)
        self.model.update_row(key, fav=int(on))
        self._update_counts()

    def _with_copies(self, keys):
        all_keys = set(keys)
        for k in keys:
            b = self.lib.book(k)
            if b:
                all_keys.update(c["key"] for c in self.lib.copies(b))
        return sorted(all_keys)

    def _after_retag(self, message):
        self.statusBar().showMessage(message, 6000)
        scroll = self.view.verticalScrollBar().value()
        self.reload_sidebar()
        self.refresh()
        self.view.verticalScrollBar().setValue(scroll)
        if self.details.key:
            self.details.show_book(self.lib.book(self.details.key))

    def set_genre(self, keys, genre):
        self.lib.set_genre(self._with_copies(keys), genre)
        n = len(keys)
        what = f"{n} item{'s' if n != 1 else ''}"
        self._after_retag(f"Moved {what} to {genre}" if genre else f"{what} back to the automatic genre")

    def set_author_genre(self, author, genre):
        self.lib.set_author_genre(author, genre)
        self._after_retag(f"Everything by {author} is now {genre}" if genre
                          else f"{author}: back to the automatic genre")

    def set_kind(self, keys, kind):
        # every copy of the same book moves together
        all_keys = set(keys)
        for k in keys:
            b = self.lib.book(k)
            if b:
                all_keys.update(c["key"] for c in self.lib.copies(b))
        self.lib.set_kind(sorted(all_keys), kind)
        label = {"Book": "Books", "Paper": "Papers & Articles", "Magazine": "Magazines", "Comic": "Comics"}[kind]
        n = len(keys)
        self.statusBar().showMessage(f"Moved {n} item{'s' if n != 1 else ''} to {label}", 6000)
        scroll = self.view.verticalScrollBar().value()
        self.reload_sidebar()
        self.refresh()
        self.view.verticalScrollBar().setValue(scroll)
        if self.details.key:
            self.details.show_book(self.lib.book(self.details.key))

    def set_status(self, key, status):
        self.lib.set_status(key, status)
        self.model.update_row(key, status=status)
        self._update_counts()
        if status:
            self.statusBar().showMessage(f"Moved to {STATUS_LABEL[status]}", 3000)

    def _update_counts(self):
        c = self.lib.counts(prefs().get("merge_duplicates"))
        for i in range(self.nav.count()):
            it = self.nav.item(i)
            code = it.data(Qt.ItemDataRole.UserRole)
            if code and code.startswith("shelf:"):
                label = SHELF_LABELS[code]
                it.setData(Qt.ItemDataRole.UserRole + 1, c.get(code[6:], 0))

    def _toggle_fav_current(self):
        key = self.view.currentIndex().data(BookModel.KeyRole)
        if key:
            b = self.lib.book(key)
            self.set_fav(key, not b["fav"])
            self.details.show_book(self.lib.book(key))

    def surprise(self):
        if not self.model.rows:
            return
        i = random.randrange(len(self.model.rows))
        idx = self.model.index(i)
        self.view.setCurrentIndex(idx)
        self.view.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _search_enter(self):
        self.search_timer.stop()
        self.refresh()
        if self.model.rows:
            self.view.setFocus()
            if not self.view.currentIndex().isValid():
                self.view.setCurrentIndex(self.model.index(0))

    def _escape(self):
        if self.search.hasFocus() and self.search.text():
            self.search.clear()
        elif self.search.hasFocus():
            self.view.setFocus()
        else:
            before = dict(self.state)
            if prefs().get("start_view") == "last":
                self.state = dict(shelf="all", kind=None, fmt=None, collection=None, author=None, genre=None)
            else:
                self._apply_start_view()
            if self.state != before:
                self.refresh()

    def rescan(self, only=None, shallow=None, quiet=False, full=False, rebuild=False):
        if self.scan_worker and self.scan_worker.isRunning():
            # one scan at a time; remember what still needs a look
            if only is None and shallow is None:
                self.queued_full = True
            else:
                self.queued_deep.update(only or [])
                self.queued_shallow.update(shallow or [])
            return
        self.scan_quiet = quiet
        self.scan_scope = (only, shallow)
        self.rescan_btn.setEnabled(False)
        if not quiet:
            self.progress.show()
            self.progress.setRange(0, 0)
        self.scan_worker = ScanWorker(only, shallow, full, rebuild)
        self.scan_worker.progress.connect(self._scan_progress)
        self.scan_worker.finished_scan.connect(self._scan_done)
        self.scan_worker.start()

    def _scan_progress(self, done, total, msg):
        if self.scan_quiet and total < 20:
            return
        self.progress.show()
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.statusBar().showMessage(f"Scanning {done:,} / {total:,}  —  {msg}")
        else:
            self.statusBar().showMessage(msg)

    def _scan_done(self, found, processed, removed):
        self.progress.hide()
        self.rescan_btn.setEnabled(True)
        only, shallow = self.scan_scope
        if processed or removed:
            bits = []
            if processed:
                bits.append(f"{processed:,} new or updated")
            if removed:
                bits.append(f"{removed:,} removed")
            self.statusBar().showMessage("Library updated: " + ", ".join(bits), 10000)
            self.model.cache.clear()
            scroll = self.view.verticalScrollBar().value()
            self.reload_sidebar()
            self.refresh()
            self.view.verticalScrollBar().setValue(scroll)
            if only is not None or shallow is not None:
                self.watcher.recheck_later(only or [], shallow or [])
            if processed and prefs().get("auto_genres"):
                self.lookup_genres(quiet=True)      # new books may bring new authors
        elif not self.scan_quiet:
            self.statusBar().showMessage(f"Library up to date: {self.lib.count():,} items", 6000)
        # run anything that came in while this scan was busy
        if self.queued_full:
            self.queued_full = False
            self.queued_deep.clear()
            self.queued_shallow.clear()
            QTimer.singleShot(0, lambda: self.rescan(quiet=True))
        elif self.queued_deep or self.queued_shallow:
            d, s = sorted(self.queued_deep), sorted(self.queued_shallow)
            self.queued_deep.clear()
            self.queued_shallow.clear()
            QTimer.singleShot(0, lambda: self.rescan(d, s, quiet=True))

    def statusBar(self):
        return self._status

    def apply_theme(self):
        """Switch every colour to the light or dark palette."""
        want = wanted_theme()
        if want == THEME and getattr(self, "_theme_applied", False):
            return
        self._theme_applied = True
        set_palette(want)
        global STYLE
        STYLE = build_style()
        QApplication.instance().setStyleSheet(STYLE + chrome_style(WINDOW_RADIUS))
        for widget, path in list(_THEMED):
            try:
                widget.setIcon(QIcon(themed_icon_path(path)))
            except RuntimeError:          # widget already deleted
                _THEMED.remove((widget, path))
        self._mark_nav()
        if self.details.key:
            self.details.show_book(self.lib.book(self.details.key))
        self.view.viewport().update()
        self.centralWidget().update()
        if self.prefs_dialog is not None:
            self.prefs_dialog.update()

    # ----- preferences
    def _apply_start_view(self):
        view = prefs().get("start_view")
        blank = dict(shelf="all", kind=None, fmt=None, collection=None, author=None, genre=None)
        if view == "last":
            try:
                saved = json.loads(self.settings.value("last_state", "") or "{}")
            except ValueError:
                saved = {}
            blank.update({k: v for k, v in saved.items() if k in blank})
        elif view.startswith("kind:"):
            blank["kind"] = view[5:]
        elif view.startswith("shelf:"):
            blank["shelf"] = view[6:]
        self.state = blank

    def _set_sort(self, code):
        codes = [c for c, _ in SORTS]
        if code in codes:
            self.sort.blockSignals(True)
            self.sort.setCurrentIndex(codes.index(code))
            self.sort.blockSignals(False)

    def lookup_genres(self, on_done=None, on_progress=None, quiet=False):
        if getattr(self, "genre_worker", None) and self.genre_worker.isRunning():
            return
        self.genre_worker = GenreWorker()
        if on_progress:
            self.genre_worker.progress.connect(on_progress)

        def finished(n, found):
            if n:
                self.statusBar().showMessage(
                    f"Genres: looked up {n:,} new author{'s' if n != 1 else ''}, found {found:,}", 8000)
                self.reload_sidebar()
                self.refresh()
            elif not quiet:
                self.statusBar().showMessage("Genres: every author has already been looked up", 6000)
            if on_done:
                on_done(n, found)
        self.genre_worker.done.connect(finished)
        self.genre_worker.start()

    def welcome(self):
        WelcomeDialog(self).exec()
        self.refresh()

    def choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose your books folder",
                                             scanner.BOOKS_ROOT or os.path.expanduser("~"))
        if d:
            prefs().set("library_root", d)

    def open_preferences(self, page=None):
        if self.prefs_dialog is None:
            self.prefs_dialog = PreferencesDialog(self)
        self.prefs_dialog.refresh_stats()
        if page:
            self.prefs_dialog.show_page(page)
        self.prefs_dialog.show()
        self.prefs_dialog.raise_()
        self.prefs_dialog.activateWindow()

    def apply_pref(self, key):
        if key == "default_sort":
            if not self.search.text().strip():
                self._set_sort(prefs().get(key))
                self.refresh()
        elif key == "watch":
            if prefs().get("watch"):
                self.watcher.start()
                self.rescan(quiet=True)
            else:
                self.watcher.stop()
        elif key == "cover_size":
            apply_cover_size(prefs().get(key))
            self.model.cache.clear()
            current = self.view.currentIndex().data(BookModel.KeyRole)
            self.model.set_rows(self.model.rows)
            if current in self.model.index_of:
                idx = self.model.index(self.model.index_of[current])
                self.view.setCurrentIndex(idx)
                self.view.scrollTo(idx)
        elif key == "theme":
            self.apply_theme()
        elif key in ("show_format_badges", "show_type_badges"):
            self.view.viewport().update()
        elif key == "merge_duplicates":
            self.reload_sidebar()
            self.refresh()
        elif key.startswith("reader_") or key == "prefer_format":
            if self.details.key:
                self.details.show_book(self.lib.book(self.details.key))
        elif key == "collections_folder":
            scanner.COLLECTIONS_FOLDER = prefs().get(key)
            self.rescan(full=True)                 # re-read names with the new rule
        elif key == "library_root":
            root = prefs().get(key)
            scanner.BOOKS_ROOT = root
            was_watching = self.watcher.fs.directories() or prefs().get("watch")
            self.watcher.stop()
            self.watcher.root = root
            self.watcher.mounted = os.path.isdir(root)
            if was_watching and prefs().get("watch"):
                self.watcher.start()
            self.rescan()

    def _in_drag_area(self, obj, pos):
        if obj is self.centre:
            # only the top bar / heading strip, not the book grid
            return pos.y() < self.view.geometry().top() - 4
        return True

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            if self._in_drag_area(obj, ev.position()):
                self._press_pos = ev.globalPosition()
                return True
        elif t == QEvent.Type.MouseMove and self._press_pos is not None:
            if (ev.globalPosition() - self._press_pos).manhattanLength() > 4:
                self._press_pos = None
                self.windowHandle().startSystemMove()
                return True
        elif t == QEvent.Type.MouseButtonRelease:
            self._press_pos = None
        elif t == QEvent.Type.MouseButtonDblClick and ev.button() == Qt.MouseButton.LeftButton:
            if self._in_drag_area(obj, ev.position()):
                self._press_pos = None
                self.toggle_maximized()
                return True
        return super().eventFilter(obj, ev)

    def toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def fills_screen(self):
        """Square, borderless edges only when the window actually covers the screen.

        Qt's own flags lag behind when you drag a maximised window down (GNOME shrinks it
        before Qt says so), and a frame painted then with solid square corners sticks as
        black corners. So this looks only at the real size: a window covering the screen
        (minus GNOME's top bar) is square, anything smaller is rounded."""
        scr = self.screen()
        if scr is None:
            return self.isMaximized() or self.isFullScreen()
        g = scr.geometry()
        return self.width() >= 0.95 * g.width() and self.height() >= 0.9 * g.height()

    def _apply_chrome(self):
        filling = self.fills_screen()
        if not filling and not self.isMaximized() and not self.isFullScreen() and hasattr(self, "normal_size"):
            self.normal_size = self.size()     # a real floating size: remember it for next time
        m = 0 if filling else 1   # the 1px margin only exists to make room for the border
        if self.frame_layout.contentsMargins().left() != m:
            self.frame_layout.setContentsMargins(m, m, m, m)
        self._place_grips()
        self.centralWidget().update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_chrome()               # repaint the corners at the new size

    def changeEvent(self, e):
        if e.type() == QEvent.Type.WindowStateChange:
            self._apply_chrome()
            # GNOME may still be animating the resize; paint again once it has settled
            for ms in (60, 250):
                QTimer.singleShot(ms, self._apply_chrome)
        super().changeEvent(e)

    def _place_grips(self):
        if not hasattr(self, "grips"):
            return
        c = self.centralWidget()
        w, h, g, k = c.width(), c.height(), 5, 14
        maxed = self.fills_screen()
        E = Qt.Edge
        rects = {
            E.LeftEdge: (0, k, g, h - 2 * k), E.RightEdge: (w - g, k, g, h - 2 * k),
            E.TopEdge: (k, 0, w - 2 * k, g), E.BottomEdge: (k, h - g, w - 2 * k, g),
            E.TopEdge | E.LeftEdge: (0, 0, k, k), E.TopEdge | E.RightEdge: (w - k, 0, k, k),
            E.BottomEdge | E.LeftEdge: (0, h - k, k, k), E.BottomEdge | E.RightEdge: (w - k, h - k, k, k),
        }
        for grip in self.grips:
            grip.setGeometry(*rects[grip.edges])
            grip.setVisible(not maxed)
            grip.raise_()

    def closeEvent(self, e):
        self.settings.setValue("last_state", json.dumps(self.state))
        self.settings.setValue("normal_size", self.normal_size)
        self.settings.setValue("maximized", self.fills_screen())
        self.settings.setValue("splitter", self.splitter.saveState())
        super().closeEvent(e)


def build_style():
    return f"""
* {{ color: {TEXT}; font-size: 13px; }}
QMainWindow {{ background: transparent; }}
#centre, QListView#grid {{ background: {BG}; }}
#sidebar {{ background: transparent; }}
#sideTabs, #sideTabs > QWidget, QListWidget#nav, QListWidget#sideList {{ background: {PANEL}; border: none; }}
#sideTabs::pane {{ border: none; }}
#sideTabs::tab-bar {{ left: 18px; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 1px; margin-right: 16px; border: none;
               border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT_BRIGHT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QLabel#brand {{ font-family: "P052", "Palatino Linotype", "TeX Gyre Pagella", "URW Palladio L", "{SERIF}";
                font-size: 21px; font-weight: 400; color: {TEXT}; padding-left: 8px; }}
QListWidget#nav {{ padding: 4px 8px; outline: none; }}
QListWidget#nav::item {{ padding: 0 10px; background: transparent; }}
QListWidget#sideList {{ outline: none; }}
QListWidget#sideList::item {{ padding: 6px 8px; background: transparent; }}
QLineEdit {{ background: transparent; border: none; padding: 8px 11px;
            selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; }}
QLineEdit#search {{ font-size: 15px; padding: 11px 15px; }}
QComboBox {{ background: transparent; border: none;
             padding: 9px 35px 9px 13px; min-width: 120px; combobox-popup: 0; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
                       width: 28px; border: none; background: transparent; }}
QComboBox::down-arrow {{ image: url("{themed_icon_path(CHEVRON)}"); width: 12px; height: 12px; }}
QComboBox::down-arrow:hover, QComboBox::down-arrow:on {{ image: url("{themed_icon_path(CHEVRON_ACCENT)}"); }}
QComboBoxPrivateContainer {{ background: transparent; border: none; }}
QComboBox QAbstractItemView {{ background: transparent; border: none;
                              padding: 5px; outline: none; selection-background-color: {LINE};
                              selection-color: {ACCENT_TEXT}; }}
QComboBox QAbstractItemView::item {{ min-height: 30px; padding: 0 10px; border-radius: 6px; }}
QPushButton {{ border-radius: 8px; padding: 9px 14px; font-weight: 600; }}
QPushButton#primary {{ background: transparent; color: {ON_ACCENT}; border: none; }}
QPushButton#secondary, QPushButton#favButton {{ background: transparent; border: none; padding: 10px 15px; }}
QPushButton#favButton:checked {{ color: {ACCENT_TEXT}; }}
QPushButton#secondary:disabled {{ color: {MUTED}; }}
QPushButton#segment {{ background: transparent; border: none; padding: 5px 12px; font-size: 12px;
                      font-weight: 600; color: {MUTED}; }}
QPushButton#segment:checked {{ color: {ACCENT_TEXT}; }}
QPushButton#chip {{ background: transparent; border: none; padding: 4px 11px;
                   font-size: 12px; font-weight: 500; color: {ACCENT_TEXT}; }}
QLabel#heading {{ font-family: "{SERIF}"; font-size: 22px; font-weight: 700; }}
QLabel#muted {{ color: {MUTED}; }}
QScrollArea#details, QWidget#detailsInner {{ background: {PANEL}; border: none; }}
#detailsHeader, #detailsColumn {{ background: transparent; }}
QPushButton#closeButton {{ background: transparent; border: none;
                          padding: 0; min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px; }}
QLabel#detailTitle {{ font-family: "{SERIF}"; font-size: 21px; font-weight: 700; }}
QLabel#detailAuthor, QLabel#link {{ color: {ACCENT_TEXT}; }}
QLabel#detailAuthor {{ font-size: 15px; }}
QLabel#metaLabel {{ color: {MUTED}; }}
QLabel#desc {{ color: {DESC}; line-height: 150%; padding-top: 8px; }}
QSplitter::handle {{ background: {LINE}; }}
QStatusBar {{ background: transparent; color: {MUTED}; border-top: 1px solid {LINE}; min-height: 22px; }}
QStatusBar::item {{ border: none; }}
QProgressBar {{ background: {PANEL_2}; border: none; border-radius: 3px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 4px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: {LINE_STRONG}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
QToolButton#menuButton {{ background: transparent; border: none; }}
QToolButton#menuButton::menu-indicator {{ image: none; width: 0; }}
QMenu {{ background: transparent; border: none; padding: 7px; }}
QMenu::item {{ padding: 8px 28px 8px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {LINE}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 5px 6px; }}
#prefSide, #prefMain, #prefPage, QScrollArea#prefScroll, QScrollArea#prefScroll > QWidget,
QScrollArea#prefScroll > QWidget > QWidget {{ background: transparent; border: none; }}
QLabel#prefTitle {{ font-weight: 700; font-size: 15px; }}
QLabel#pageTitle {{ font-weight: 700; font-size: 15px; }}
QListWidget#prefNav {{ background: transparent; border: none; outline: none; }}
QListWidget#prefNav::item {{ padding: 0 10px; color: {TEXT}; background: transparent; }}
QListWidget#prefNav::item:selected {{ color: {TEXT}; font-weight: 600; background: transparent; }}
QLabel#groupTitle {{ font-weight: 700; font-size: 13px; color: {TEXT}; }}
QFrame#boxedList {{ background: transparent; border: none; }}
QFrame#rowSep {{ background: {LINE}; border: none; }}
#prefRow {{ background: transparent; }}
QLabel#rowTitle {{ font-size: 14px; }}
QLabel#rowSub {{ color: {MUTED}; font-size: 12px; }}
QLabel#rowValue {{ color: {MUTED}; }}
QLabel#toolOk {{ color: {TEAL}; }}
QLabel#toolMissing {{ color: {MUTED}; }}
QLabel#keycap {{ background: transparent; border: none; padding: 4px 10px; font-family: monospace; font-size: 12px; }}
QLabel#aboutName {{ font-family: "{SERIF}"; font-size: 26px; font-weight: 700; }}
QLabel#aboutText {{ color: {DESC}; padding: 0 40px; }}
QPushButton#destructive {{ background: transparent; color: {DANGER_TEXT}; border: none; padding: 10px 15px; }}
QMessageBox {{ background: {PANEL}; }}
QMessageBox QPushButton {{ background: transparent; border: none; padding: 10px 15px; min-width: 70px; }}
QPushButton#kobo {{ background: transparent; border: none; padding: 10px 15px; color: {TEAL}; }}
QPushButton#kobo:disabled {{ color: {MUTED}; }}
QLabel#koboLabel {{ color: {TEAL}; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {LINE}; padding: 4px; }}
"""


STYLE = build_style()


def chrome_style(r):
    """Panel corners are painted by WindowFrame now (smoothly); nothing to add here."""
    return ""


# ------------------------------------------------------------- install

BIN_PATH = os.path.expanduser("~/.local/bin/babel")
DESKTOP_PATH = os.path.expanduser("~/.local/share/applications/babel.desktop")
ICON_ROOT = os.path.expanduser("~/.local/share/icons/hicolor")
ICON_SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)


def install():
    """Put Babel in the app menu: copy the binary to ~/.local/bin, add the icon and a launcher."""
    if FROZEN:
        os.makedirs(os.path.dirname(BIN_PATH), exist_ok=True)
        if os.path.abspath(sys.executable) != BIN_PATH:
            shutil.copy2(sys.executable, BIN_PATH)
        os.chmod(BIN_PATH, 0o755)
        exec_line = BIN_PATH
    else:
        exec_line = f"{sys.executable} {os.path.abspath(__file__)}"

    os.makedirs(os.path.join(ICON_ROOT, "scalable", "apps"), exist_ok=True)
    shutil.copyfile(ICON_PATH, os.path.join(ICON_ROOT, "scalable", "apps", "babel.svg"))
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtSvg import QSvgRenderer
    app = QGuiApplication.instance() or QGuiApplication(["babel", "-platform", "offscreen"])
    r = QSvgRenderer(ICON_PATH)
    for s in ICON_SIZES:
        d = os.path.join(ICON_ROOT, f"{s}x{s}", "apps")
        os.makedirs(d, exist_ok=True)
        img = QImage(s, s, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r.render(p, QRectF(0, 0, s, s))
        p.end()
        img.save(os.path.join(d, "babel.png"))

    os.makedirs(os.path.dirname(DESKTOP_PATH), exist_ok=True)
    with open(DESKTOP_PATH, "w") as f:
        f.write(f"""[Desktop Entry]
Type=Application
Name=Babel
GenericName=Library
Comment=Browse and search your book collection
Exec={exec_line}
Icon=babel
Terminal=false
Categories=Office;Viewer;Education;Literature;
Keywords=books;library;ebook;epub;pdf;reading;kobo;
StartupWMClass=babel
""")
    for cmd in (["gtk-update-icon-cache", "-f", "-t", ICON_ROOT],
                ["update-desktop-database", os.path.dirname(DESKTOP_PATH)]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, capture_output=True)
    print("Babel is installed. Find it in your app menu.")
    if FROZEN:
        print(f"  program:  {BIN_PATH}")
    print(f"  launcher: {DESKTOP_PATH}")
    missing = [f"{c} ({pkg})" for c, pkg, _ in HELPER_TOOLS if not shutil.which(c)]
    if missing:
        print("Optional helpers not found (Babel works without them):\n  " + "\n  ".join(missing))


def uninstall():
    """Remove the launcher, icons and installed binary. Your library data is kept."""
    removed = []
    for p in [DESKTOP_PATH, os.path.join(ICON_ROOT, "scalable", "apps", "babel.svg")] + \
             [os.path.join(ICON_ROOT, f"{s}x{s}", "apps", "babel.png") for s in ICON_SIZES]:
        if os.path.exists(p):
            os.remove(p)
            removed.append(p)
    if FROZEN and os.path.exists(BIN_PATH):
        os.remove(BIN_PATH)
        removed.append(BIN_PATH)
    print(f"Removed {len(removed)} files.")
    print(f"Your library and settings are kept in {scanner.DATA_DIR} and ~/.config/Babel;"
          " delete those folders too if you want a clean slate.")


USAGE = """Babel — a desktop library for your books.

usage: babel [--install | --uninstall | --version | --help]

  (no option)   open Babel
  --install     add Babel to your app menu (copies the program to ~/.local/bin)
  --uninstall   remove it from the app menu; your library data is kept
"""


def install_safety_net():
    # PyQt ends the whole program on any uncaught error in a click or timer handler.
    # Log it and carry on instead, so one small bug can't close Babel.
    def keep_running(kind, value, tb):
        import traceback
        log = os.path.join(scanner.DATA_DIR, "errors.log")
        try:
            os.makedirs(scanner.DATA_DIR, exist_ok=True)
            with open(log, "a") as f:
                f.write(time.strftime("\n%Y-%m-%d %H:%M:%S\n") + "".join(traceback.format_exception(kind, value, tb)))
        except OSError:
            pass
        traceback.print_exception(kind, value, tb)
        app_ = QApplication.instance()
        win = app_.activeWindow() if app_ else None
        if win is not None and hasattr(win, "statusBar"):
            win.statusBar().showMessage(f"Something went wrong ({value}). Details saved to {log}", 10000)
    sys.excepthook = keep_running


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE)
        return
    if "--version" in sys.argv:
        print(f"Babel {VERSION}")
        return
    if "--install" in sys.argv:
        install()
        return
    if "--uninstall" in sys.argv:
        uninstall()
        return
    install_safety_net()
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Babel")
    app.setDesktopFileName("babel")
    app.setWindowIcon(QIcon(ICON_PATH))
    app.setStyle("Fusion")
    set_palette(wanted_theme())
    global STYLE
    STYLE = build_style()
    app.setStyleSheet(STYLE + chrome_style(WINDOW_RADIUS))
    app._smooth = SmoothShapes(app)
    app.installEventFilter(app._smooth)
    w = MainWindow()
    w.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    if w.start_maximized:
        w.showMaximized()
    else:
        w.show()
    code = app.exec()
    # Tidy up before Python exits: left to itself, Python destroys Qt objects in an
    # arbitrary order and crashed (segfault) on every close.
    app.removeEventFilter(app._smooth)
    w.model.pool.waitForDone(2000)           # cover loading still in flight
    for top in QApplication.topLevelWidgets():
        top.deleteLater()
    app.processEvents()
    sys.exit(code)


if __name__ == "__main__":
    main()
