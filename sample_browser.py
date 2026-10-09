"""Sample Browser — Syntakt Twinshot için küçük sample tarayıcı.

- "+ Klasör ekle" ile seçtiğin klasör taranır; sonuçlar önbelleğe (SQLite) yazılır.
  Sonraki açılışlarda tarama yapılmaz, "Yeniden tara" sadece yeni/değişen dosyalara bakar.
- Dosyalar dosya/klasör adına göre kategorilenir; isimden çıkmazsa ses karakterine göre tahmin edilir (~).
- Tıkla / ok tuşlarıyla gez → çalar. Boşluk: çal/durdur. Enter: kite ekle.
- Sürükle-bırak: Transfer'e, Ableton'a, Explorer'a. "Syntakt modu" açıkken sürüklenen dosyalar
  48 kHz / 16-bit / mono / en fazla 5 sn'ye çevrilmiş kopyalardır (önizleme de öyle çalar).

Gereken paketler: PyQt6, numpy, scipy, soundfile, sounddevice
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from PyQt6.QtCore import (QAbstractTableModel, QMimeData, QModelIndex, QSettings, Qt, QThread, QTimer, QUrl,
                          pyqtSignal)
from PyQt6.QtGui import QAction, QColor, QCursor, QDrag, QPainter, QPalette, QPen
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                             QMessageBox, QProgressBar, QPushButton, QSlider, QSplitter, QStackedWidget, QTableView,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

__version__ = "0.1.0"

def _data_dir():
    if os.environ.get("SB_DATA_DIR"):
        return Path(os.environ["SB_DATA_DIR"])
    if getattr(sys, "frozen", False):  # packaged app
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "SyntaktSampleBrowser"
        if os.name == "nt":
            return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SyntaktSampleBrowser"
        return Path.home() / ".local" / "share" / "SyntaktSampleBrowser"
    # Running from source: keep data next to the script, not in AppData. Microsoft Store Python redirects
    # AppData writes to a private folder, and Transfer/Ableton would then not find the files we drag to them.
    return Path(__file__).resolve().parent / "data"


DATA_DIR = _data_dir()
FILE_MANAGER = "Finder" if sys.platform == "darwin" else "Explorer"
DB_PATH = DATA_DIR / "library.db"
CONVERT_DIR = DATA_DIR / "syntakt"
KIT_FILE = DATA_DIR / "current_kit.json"
LOG_FILE = DATA_DIR / "log.txt"

AUDIO_EXT = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}
TWINSHOT_SR = 48000
TWINSHOT_MAX_SEC = 5.0
TWINSHOT_SLOTS = 64
TWINSHOT_MEM = 32 * 1024 * 1024
FEATURES = ("dur", "sr", "ch", "peak_db", "centroid", "flatness", "low", "high", "decay")

MIME_PATHS = "application/x-sample-browser-paths"


# ---------------------------------------------------------------- categorizing

def split_words(s):
    """'RKU_DST_HatOpen-01' -> ' rku dst hat open 01 '"""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    s = re.sub(r"[_\-\.\(\)\[\]&,+]+", " ", s)
    return " " + re.sub(r"\s+", " ", s).strip().lower() + " "


def _rx(words):
    return re.compile(r"(?<![a-z])(?:%s)(?![a-z])" % words)


# Order matters: the first matching rule wins (filename first, then folders from deepest up).
CATEGORY_RULES = [
    ("Cızırtı/Vinyl", _rx(r"vinyl|crackles?|hiss|tape noise")),
    ("Vokal", _rx(r"vocals?|vox|voices?|acapellas?|adlibs?|chants?|spoken")),
    ("Kick", _rx(r"kicks?|kik|kck|bd|bass ?drums?")),
    ("Snare", _rx(r"snares?|snr|sd")),
    ("Clap", _rx(r"claps?|handclaps?|cp")),
    ("Rim", _rx(r"rims?|rimshots?|side ?sticks?|sticks?")),
    ("Hat (açık)", _rx(r"open ?hi ?hats?|open ?hats?|hats? ?open|open ?hh|ohh?")),
    ("Hat (kapalı)", _rx(r"hi ?hats?|hats?|hh|chh?|closed ?hats?")),
    ("Ride/Crash", _rx(r"rides?|crash(es)?|cymbals?|cym|splash|china")),
    ("Shaker/Tamb", _rx(r"shakers?|shkr|shake|tamb(ourine)?s?|maracas?|cabasa")),
    ("Tom", _rx(r"toms?|floor ?tom")),
    ("Perc", _rx(r"percs?|percussions?|congas?|bongos?|cowbells?|claves?|blocks?|wood|knocks?|clacks?|"
                 r"tablas?|djembes?|timbales?|snaps?|snappys?|clicks?|thuds?|clunks?")),
    ("Fill", _rx(r"fills?|rolls?")),
    ("Davul loop", _rx(r"drums?|tops?|breaks?|breakbeats?|beats?|grooves?")),
    ("Bas/Sub", _rx(r"bass(es)?|subs?|808s?|reeses?|wobbles?")),
    ("Pad/Akor", _rx(r"pads?|chords?|stabs?|drones?|strings?|choirs?")),
    ("Keys", _rx(r"keys?|pianos?|rhodes|organs?|wurli|epiano|bells?|mallets?|marimba|guitars?")),
    ("Synth", _rx(r"synths?|leads?|plucks?|arps?|melod(y|ies|ic)|music")),
    ("Atmosfer", _rx(r"atmos|atmospheres?|ambiences?|ambient|textures?|field|rain|nature|wind|room ?tone|"
                     r"soundscapes?")),
    ("Foley", _rx(r"foley|animism|cutlery|silverware|coins?|paper|scraps?|swipes?|splats?|squish(es)?|drips?|"
                  r"scatter|splash(es)?")),
    ("FX", _rx(r"fx|sfx|risers?|rise|uplifters?|downlifters?|sweeps?|impacts?|whoosh|swoosh|noise|"
               r"transitions?|reverse|downers?")),
]
CATEGORY_ORDER = [c for c, _ in CATEGORY_RULES] + ["Diğer"]
TONAL_CATS = {"Bas/Sub", "Pad/Akor", "Keys", "Synth", "Vokal"}
PACK_MARKERS = re.compile(r" - |\bvol\b|vol\.|sample|pack|sounds|\bkit\b|collection|series|presents|edition|"
                          r"\bby\b|library|essentials|suite", re.I)
ONESHOT_RX = _rx(r"one ?shots?|oneshots?|hits?|shots?")
LOOP_RX = _rx(r"loops?|songstarters?|construction|grooves?")
KEY_RX = re.compile(r"^([A-Ga-g])(#|b|sharp|flat)?(m|min|minor|maj|major)?$")
NOTE_RX = re.compile(r"^([A-G])(#|b)?(-?\d)$")


def match_category(scopes):
    for words in scopes:
        for cat, rx in CATEGORY_RULES:
            if rx.search(words):
                return cat
    return None


def guess_category(s):
    if s.low > 0.55 and s.dur < 1.2:
        return "Kick"
    if s.centroid > 6000 and s.dur < 0.5:
        return "Hat (kapalı)"
    if s.centroid > 5000 and s.flatness > 0.2:
        return "Hat (açık)" if s.dur < 1.5 else "Atmosfer"
    if s.flatness < 0.005 and s.dur > 0.8:
        return "Pad/Akor" if s.centroid < 2000 else "Synth"
    if s.dur < 0.5:
        return "Perc"
    return "Diğer"


def parse_bpm(name_words):
    m = re.search(r"(?<!\d)(\d{2,3}) ?bpm", name_words)
    if m:
        return int(m.group(1))
    for t in name_words.split():
        if t.isdigit() and 60 <= int(t) <= 200:
            return int(t)
    return 0


def parse_key(stem, cat):
    for t in reversed(re.split(r"[_\-\s\.\(\)\[\]]+", stem)):
        m = NOTE_RX.match(t)
        if m:
            return t
        m = KEY_RX.match(t)
        if not m:
            continue
        letter, acc, q = m.groups()
        if letter.islower() and not q:
            continue
        if not acc and not q and cat not in TONAL_CATS:
            continue
        acc = {"sharp": "#", "flat": "b"}.get(acc, acc or "")
        minor = "m" if q in ("m", "min", "minor") else ""
        return letter.upper() + acc + minor
    return ""


def character_tags(s):
    tags = []
    if 0 < s.centroid < 700:
        tags.append("Karanlık")
    elif s.centroid > 3500:
        tags.append("Parlak")
    if s.flatness < 0.005 and s.dur > 0.5 and s.low < 0.5:
        tags.append("Tonal")
    elif s.flatness > 0.15:
        tags.append("Gürültülü")
    if s.low > 0.5:
        tags.append("Sub")
    if s.dur < 0.25:
        tags.append("Kısa")
    return tags


class Sample:
    __slots__ = ("path", "root", "name", "stem", "rel", "top", "sub", "dur", "sr", "ch", "peak_db", "centroid",
                 "flatness", "low", "high", "decay", "err", "category", "guessed", "is_loop", "key", "bpm", "tags",
                 "hay", "size", "mtime")


def make_sample(rec):
    s = Sample()
    s.path, s.root = rec["path"], rec["root"]
    s.size, s.mtime = rec.get("size") or 0, rec.get("mtime") or 0
    for k in FEATURES:
        setattr(s, k, rec.get(k) or 0.0)
    s.err = rec.get("err") or ""
    s.name = os.path.basename(s.path)
    s.stem = os.path.splitext(s.name)[0]
    s.rel = os.path.relpath(s.path, s.root)
    parts = Path(s.rel).parts
    dirs = parts[:-1]
    s.top = dirs[0] if dirs else ""
    s.sub = dirs[-1] if len(dirs) > 1 else ""
    name_words = split_words(s.stem)
    dir_scopes = []
    for i, d in reversed(list(enumerate(dirs))):
        w = split_words(d)
        if i == 0 and len(w.split()) >= 3 and PACK_MARKERS.search(d):
            continue  # product / pack name, not a sound type
        dir_scopes.append(w)
    cat = match_category([name_words] + dir_scopes)
    s.guessed = cat is None
    s.category = cat or guess_category(s)
    if s.category == "Diğer":
        s.guessed = False
    path_words = name_words + " ".join(dir_scopes)
    s.bpm = parse_bpm(name_words)
    if ONESHOT_RX.search(path_words):
        s.is_loop = False
    elif LOOP_RX.search(path_words):
        s.is_loop = True
    else:
        s.is_loop = bool(s.bpm) and s.dur > 2.5
    s.key = parse_key(s.stem, s.category)
    s.tags = character_tags(s)
    s.hay = " ".join([s.rel, s.category, s.key, " ".join(s.tags), "loop" if s.is_loop else "oneshot"]).lower()
    return s


def feature_vec(s):
    return np.array([
        math.log10(max(s.centroid, 30.0)) / 4.3 * 2.0,
        math.sqrt(max(s.flatness, 0.0)) * 1.5,
        s.low,
        s.high,
        (math.log10(max(s.dur, 0.01)) + 2) / 4,
        min(s.decay, 3.0) / 3 * 0.8,
    ], dtype=np.float32)


# ---------------------------------------------------------------- audio

def analyze(path):
    info = sf.info(path)
    sr = info.samplerate or 1
    dur = info.frames / sr
    n = min(info.frames, int(sr * 3))
    out = dict(dur=dur, sr=sr, ch=info.channels, peak_db=-120.0, centroid=0.0, flatness=0.0, low=0.0, high=0.0,
               decay=0.0)
    if n <= 0:
        return out
    x, _ = sf.read(path, frames=n, always_2d=True, dtype="float32")
    m = x.mean(axis=1)
    a = np.abs(m)
    peak = float(a.max())
    if peak <= 1e-6:
        return out
    win = max(1, int(sr * 0.01))
    env = np.convolve(a, np.ones(win, dtype=np.float32) / win, mode="same")
    ip = int(env.argmax())
    below = np.flatnonzero(env[ip:] < env[ip] * 0.01)
    decay = below[0] / sr if below.size else (env.size - ip) / sr
    onset = int(np.flatnonzero(a > peak * 0.01)[0])
    seg = m[onset:onset + sr]
    if seg.size < 4096:
        seg = np.pad(seg, (0, 4096 - seg.size))
    p = np.abs(np.fft.rfft(seg * np.hanning(seg.size))) ** 2
    f = np.fft.rfftfreq(seg.size, 1 / sr)
    tot = float(p.sum()) + 1e-12
    band = p[(f > 50) & (f < 16000)] + 1e-12
    out.update(
        peak_db=20 * math.log10(peak),
        centroid=float((f * p).sum() / tot),
        flatness=float(np.exp(np.mean(np.log(band))) / np.mean(band)),
        low=float(p[f < 150].sum() / tot),
        high=float(p[f > 5000].sum() / tot),
        decay=float(decay),
    )
    return out


def analyze_safe(path):
    try:
        return analyze(path)
    except Exception as e:  # unreadable / unsupported file
        return dict(err=str(e)[:200])


def resample(y, sr_in, sr_out):
    """FFT resampling (band-limited). Zero padding at both ends keeps the circular FFT from wrapping."""
    if sr_in == sr_out or y.size == 0:
        return y
    pad = int(0.05 * sr_in)
    z = np.concatenate([np.zeros(pad), y, np.zeros(pad)])
    m = int(round(z.size * sr_out / sr_in))
    out = np.fft.irfft(np.fft.rfft(z), m) * (m / z.size)
    start = int(round(pad * sr_out / sr_in))
    return out[start:start + int(round(y.size * sr_out / sr_in))]


def to_syntakt(path):
    """Mono, 48 kHz, silence-trimmed, max 5 s, peak -1 dBFS float32 array."""
    info = sf.info(path)
    n = min(info.frames, int(info.samplerate * 30))
    x, sr = sf.read(path, frames=n, always_2d=True, dtype="float64")
    y = x.mean(axis=1)
    peak = float(np.abs(y).max()) if y.size else 0.0
    if peak <= 1e-6:
        return np.zeros(64, dtype=np.float32)
    nz = np.flatnonzero(np.abs(y) > peak * 10 ** (-60 / 20))
    y = y[max(0, nz[0] - int(0.001 * sr)):min(y.size, nz[-1] + int(0.01 * sr))]
    y = y[:int((TWINSHOT_MAX_SEC + 0.05) * sr)]  # cut before resampling, it's the slow part
    y = resample(y, int(sr), TWINSHOT_SR)
    cap = int(TWINSHOT_MAX_SEC * TWINSHOT_SR)
    if y.size > cap:
        y = y[:cap]
        fade = int(0.35 * TWINSHOT_SR)
    else:
        fade = min(int(0.005 * TWINSHOT_SR), y.size)
    if fade > 0:
        y[-fade:] *= np.linspace(1.0, 0.0, fade) ** 2
    pk = float(np.abs(y).max())
    if pk > 0:
        y *= 10 ** (-1 / 20) / pk
    return y.astype(np.float32)


def safe_name(stem, limit=40):
    s = re.sub(r"[^A-Za-z0-9 _\-#]+", "_", stem).strip(" _")
    return (s or "sample")[:limit]


def write_syntakt(y, out_path):
    d = (np.random.random(y.size) - np.random.random(y.size)) / 32768.0
    z = np.clip(y.astype(np.float64) + d, -1.0, 1.0 - 1 / 32768)
    sf.write(str(out_path), z, TWINSHOT_SR, subtype="PCM_16")


def syntakt_file(s):
    """Converted copy in the cache; the file keeps the sample's own name (Transfer uses it)."""
    h = hashlib.sha1(f"{s.path}|{s.size}|{s.mtime}".encode("utf-8", "replace")).hexdigest()[:12]
    out = CONVERT_DIR / h / (safe_name(s.stem) + ".wav")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        write_syntakt(to_syntakt(s.path), out)
    return out


# ---------------------------------------------------------------- database / scanning

def db_connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS roots(path TEXT PRIMARY KEY, added REAL, scanned REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS samples(path TEXT PRIMARY KEY, root TEXT, size INTEGER, mtime REAL, "
                "dur REAL, sr INTEGER, ch INTEGER, peak_db REAL, centroid REAL, flatness REAL, low REAL, "
                "high REAL, decay REAL, err TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_root ON samples(root)")
    return con


SAMPLE_COLS = ("path", "root", "size", "mtime") + FEATURES + ("err",)


class Scanner(QThread):
    progress = pyqtSignal(str, int, int)  # root, done, total (total < 0 while counting files)
    batch = pyqtSignal(object)  # list[dict]
    removed = pyqtSignal(object)  # list[str]
    root_done = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.queue = []
        self.lock = threading.Lock()
        self.stop_flag = False
        self.current_root = None

    def enqueue(self, root, front=False):
        with self.lock:
            if root not in self.queue:
                self.queue.insert(0, root) if front else self.queue.append(root)

    def pending(self):
        with self.lock:
            return list(self.queue)

    def run(self):
        self.stop_flag = False
        con = db_connect()
        try:
            while not self.stop_flag:
                with self.lock:
                    root = self.queue.pop(0) if self.queue else None
                if root is None:
                    break
                self.current_root = root
                self.scan_root(con, root)
                if not self.stop_flag:
                    self.current_root = None
        finally:
            con.close()

    def scan_root(self, con, root):
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            if self.stop_flag:
                return
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__MACOSX"]
            for fn in filenames:
                if fn.startswith("._") or os.path.splitext(fn)[1].lower() not in AUDIO_EXT:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((p, st.st_size, st.st_mtime))
            self.progress.emit(root, 0, -len(files))
        known = {p: (sz, mt) for p, sz, mt in con.execute("SELECT path, size, mtime FROM samples WHERE root=?",
                                                           (root,))}
        seen = {p for p, _, _ in files}
        gone = [p for p in known if p not in seen]
        if gone:
            con.executemany("DELETE FROM samples WHERE path=?", [(p,) for p in gone])
            con.commit()
            self.removed.emit(gone)
        todo = [f for f in files if known.get(f[0]) != (f[1], f[2])]
        total, done, buf = len(todo), 0, []
        self.progress.emit(root, 0, total)

        def flush():
            if not buf:
                return
            con.executemany("INSERT OR REPLACE INTO samples(%s) VALUES (%s)"
                            % (",".join(SAMPLE_COLS), ",".join("?" * len(SAMPLE_COLS))),
                            [tuple(r.get(c) for c in SAMPLE_COLS) for r in buf])
            con.commit()
            self.batch.emit(list(buf))
            buf.clear()

        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as ex:
            futs = {ex.submit(analyze_safe, p): (p, sz, mt) for p, sz, mt in todo}
            for fut in as_completed(futs):
                if self.stop_flag:
                    ex.shutdown(cancel_futures=True)
                    break
                p, sz, mt = futs[fut]
                buf.append(dict(path=p, root=root, size=sz, mtime=mt, **fut.result()))
                done += 1
                if len(buf) >= 250:
                    flush()
                    self.progress.emit(root, done, total)
        flush()
        if not self.stop_flag:
            con.execute("UPDATE roots SET scanned=? WHERE path=?", (time.time(), root))
            con.commit()
            self.root_done.emit(root, total)


# ---------------------------------------------------------------- widgets

COLUMNS = ["Ad", "Kategori", "Klasör", "Süre", "Ton", "BPM", "Karakter"]
SORT_KEYS = [
    lambda s: s.name.lower(),
    lambda s: (CATEGORY_ORDER.index(s.category), s.name.lower()),
    lambda s: (s.top.lower(), s.sub.lower(), s.name.lower()),
    lambda s: s.dur,
    lambda s: (s.key == "", s.key),
    lambda s: (not s.is_loop, s.bpm),
    lambda s: s.centroid,
]


class SampleModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.sort_col, self.sort_order = 0, Qt.SortOrder.AscendingOrder
        self.sorting = True

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def flags(self, index):
        return (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        s = self.rows[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0:
                return s.name
            if c == 1:
                return s.category + (" ~" if s.guessed else "")
            if c == 2:
                return s.top + (" › " + s.sub if s.sub else "")
            if c == 3:
                return f"{s.dur:.2f} sn"
            if c == 4:
                return s.key
            if c == 5:
                return str(s.bpm) if s.is_loop and s.bpm else ""
            if c == 6:
                return " · ".join(s.tags + (["loop"] if s.is_loop else []))
        elif role == Qt.ItemDataRole.ToolTipRole:
            return s.path + ("\n" + s.err if s.err else "")
        elif role == Qt.ItemDataRole.ForegroundRole:
            if s.err:
                return QColor("#e06c6c")
            if c == 3 and s.dur > TWINSHOT_MAX_SEC:
                return QColor("#d9a441")
            if c == 1 and s.guessed:
                return QColor("#9aa4b2")
        elif role == Qt.ItemDataRole.TextAlignmentRole and c in (3, 5):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sort_col, self.sort_order = column, order
        if not self.sorting:
            return
        self.layoutAboutToBeChanged.emit()
        self._sort()
        self.layoutChanged.emit()

    def _sort(self):
        self.rows.sort(key=SORT_KEYS[self.sort_col], reverse=self.sort_order == Qt.SortOrder.DescendingOrder)

    def set_rows(self, rows, sort=True):
        self.beginResetModel()
        self.rows = rows
        self.sorting = sort
        if sort:
            self._sort()
        self.endResetModel()


class SampleTable(QTableView):
    play_requested = pyqtSignal()
    toggle_requested = pyqtSignal()
    add_requested = pyqtSignal()

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(22)
        self.setSortingEnabled(True)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Space:
            self.toggle_requested.emit()
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.add_requested.emit()
            return
        super().keyPressEvent(e)

    def startDrag(self, actions):
        samples = self.win.selected_samples()
        if samples:
            self.win.start_file_drag(self, samples, convert=self.win.syntakt_mode.isChecked())


class KitList(QListWidget):
    dropped = pyqtSignal(object)  # list[str]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def startDrag(self, actions):
        paths = [it.data(Qt.ItemDataRole.UserRole) for it in self.selectedItems()]
        samples = [self.win.samples[p] for p in paths if p in self.win.samples]
        if samples:
            self.win.start_file_drag(self, samples, convert=True)

    def dragEnterEvent(self, e):
        if e.source() is not self and (e.mimeData().hasFormat(MIME_PATHS) or e.mimeData().hasUrls()):
            e.acceptProposedAction()
        else:
            e.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(MIME_PATHS):
            paths = bytes(md.data(MIME_PATHS)).decode("utf-8").split("\n")
        else:
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        self.dropped.emit([p for p in paths if p])
        e.acceptProposedAction()


class Waveform(QWidget):
    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(72)
        self.mins = self.maxs = None
        self.dur = 0.0
        self.pos = -1.0
        self.label = ""
        self._cols = None  # cached ((w, h), [(x, y1, y2), ...])

    def set_audio(self, mono, sr, label=""):
        self.label = label
        self._cols = None
        if mono is None or mono.size == 0:
            self.mins = self.maxs = None
            self.dur = 0.0
        else:
            n = 1600
            pad = (-mono.size) % n
            y = np.pad(mono, (0, pad)).reshape(n, -1)
            self.mins, self.maxs = y.min(axis=1), y.max(axis=1)
            self.dur = mono.size / sr
        self.pos = -1.0
        self.update()

    def set_pos(self, frac):
        self.pos = frac
        self.update()

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#16181d"))
        if self.mins is None:
            p.setPen(QColor("#6b7280"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Bir sample seç")
            return
        mid = h / 2
        if self._cols is None or self._cols[0] != (w, h):
            n = self.mins.size
            idx = np.unique(np.arange(w) * n // w)
            lo = np.minimum.reduceat(self.mins, idx)
            hi = np.maximum.reduceat(self.maxs, idx)
            xs = (idx * w // n).tolist()
            self._cols = ((w, h), list(zip(xs, (mid - hi * mid * 0.95).astype(int).tolist(),
                                           (mid - lo * mid * 0.95).astype(int).tolist())))
        p.setPen(QPen(QColor("#5fb3a1")))
        for x, y1, y2 in self._cols[1]:
            p.drawLine(x, y1, x, y2)
        if self.dur > TWINSHOT_MAX_SEC:
            x5 = int(w * TWINSHOT_MAX_SEC / self.dur)
            p.fillRect(x5, 0, w - x5, h, QColor(0, 0, 0, 110))
            p.setPen(QPen(QColor("#d9a441"), 1, Qt.PenStyle.DashLine))
            p.drawLine(x5, 0, x5, h)
            p.drawText(x5 + 4, 14, "5 sn (Twinshot sınırı)")
        if self.pos >= 0:
            p.setPen(QPen(QColor("#f5f5f5"), 1))
            x = int(w * min(self.pos, 1.0))
            p.drawLine(x, 0, x, h)
        p.setPen(QColor("#9aa4b2"))
        p.drawText(6, h - 6, f"{self.label}   {self.dur:.2f} sn")


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Sample Browser · Syntakt  v{__version__}")
        self.resize(1400, 820)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings(str(DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)
        self.samples = {}  # path -> Sample
        self.roots = []
        self.kit = []  # list of paths
        self.audio_cache = OrderedDict()
        self.current = None
        self.play_start = 0.0
        self.play_len = 0.0
        self.last_play = ("", 0.0)
        self.folder_filter = (None, None)  # (root, top)
        self.similar_to = None
        self._restoring = False

        self.scanner = Scanner(self)
        self.scanner.progress.connect(self.on_scan_progress)
        self.scanner.batch.connect(self.on_scan_batch)
        self.scanner.removed.connect(self.on_scan_removed)
        self.scanner.root_done.connect(self.on_root_done)
        self.scanner.finished.connect(self.on_scanner_finished)

        self.build_ui()
        self.load_library()
        self.load_kit()

        self.filter_timer = QTimer(self, singleShot=True, interval=150, timeout=self.apply_filter)
        self.refresh_timer = QTimer(self, singleShot=True, interval=1500, timeout=self.refresh_after_scan)
        self.play_timer = QTimer(self, interval=33, timeout=self.tick_playhead)

    # ---------- UI
    def build_ui(self):
        top = QHBoxLayout()
        self.search = QLineEdit(placeholderText="Ara: isim, klasör, kategori, ton (ör. 'kick karanlık', 'pad fm')…")
        self.search.textChanged.connect(lambda: self.filter_timer.start())
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Hepsi", "One-shot", "Loop"])
        self.type_combo.currentIndexChanged.connect(self.apply_filter)
        self.only5 = QCheckBox("Sadece ≤5 sn")
        self.only5.toggled.connect(self.apply_filter)
        self.syntakt_mode = QCheckBox("Syntakt modu")
        self.syntakt_mode.setToolTip("Önizleme ve sürükleme: 48 kHz · 16-bit · mono · en fazla 5 sn, "
                                     "baş/son sessizlik kesilir, -1 dB normalize")
        self.syntakt_mode.setChecked(self.settings.value("syntakt_mode", True, type=bool))
        self.syntakt_mode.toggled.connect(lambda v: (self.settings.setValue("syntakt_mode", v),
                                                     self.audio_cache.clear()))
        self.autoplay = QCheckBox("Otomatik çal")
        self.autoplay.setChecked(self.settings.value("autoplay", True, type=bool))
        self.autoplay.toggled.connect(lambda v: self.settings.setValue("autoplay", v))
        self.volume = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=100, maximumWidth=110)
        self.volume.setValue(self.settings.value("volume", 80, type=int))
        self.volume.valueChanged.connect(lambda v: self.settings.setValue("volume", v))
        self.device_combo = QComboBox(minimumWidth=220)
        self.fill_devices()
        for wdg in (self.search,):
            top.addWidget(wdg, 1)
        for wdg in (self.type_combo, self.only5, self.syntakt_mode, self.autoplay, QLabel("Ses"), self.volume,
                    QLabel("Çıkış"), self.device_combo):
            top.addWidget(wdg)

        # left: folders + categories + character
        left = QVBoxLayout()
        add_btn = QPushButton("+ Klasör ekle")
        add_btn.clicked.connect(self.add_folder)
        left.addWidget(add_btn)
        self.folder_tree = QTreeWidget()
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.itemSelectionChanged.connect(self.on_folder_selected)
        self.folder_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.folder_tree.customContextMenuRequested.connect(self.folder_menu)
        left.addWidget(self.folder_tree, 3)
        left.addWidget(QLabel("Kategori"))
        self.cat_list = QListWidget()
        self.cat_list.itemSelectionChanged.connect(self.apply_filter)
        left.addWidget(self.cat_list, 4)
        left.addWidget(QLabel("Karakter"))
        self.tag_buttons = {}
        for row_tags in (("Karanlık", "Parlak", "Kısa"), ("Tonal", "Gürültülü", "Sub")):
            row = QHBoxLayout()
            for t in row_tags:
                b = QPushButton(t, checkable=True)
                b.toggled.connect(self.apply_filter)
                self.tag_buttons[t] = b
                row.addWidget(b)
            left.addLayout(row)
        left_w = QWidget()
        left_w.setLayout(left)

        # center: table + waveform
        self.model = SampleModel()
        self.table = SampleTable(self)
        self.table.setModel(self.model)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        for i, wdt in enumerate((250, 100, 210, 62, 48, 42)):
            self.table.setColumnWidth(i, wdt)
        hh.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.table.selectionModel().currentRowChanged.connect(self.on_current_changed)
        self.table.clicked.connect(lambda idx: self.play_sample(self.model.rows[idx.row()], force=False))
        self.table.doubleClicked.connect(lambda idx: self.add_to_kit([self.model.rows[idx.row()].path]))
        self.table.toggle_requested.connect(self.toggle_play)
        self.table.add_requested.connect(lambda: self.add_to_kit([s.path for s in self.selected_samples()]))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.table_menu)

        self.empty_label = QLabel("Başlamak için soldan <b>+ Klasör ekle</b> ile bir sample klasörü seç.<br>"
                                  "Sadece eklediğin klasör taranır, sonuçlar saklanır.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color:#9aa4b2; font-size:15px;")
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_label)
        self.stack.addWidget(self.table)

        self.similar_bar = QWidget()
        sb = QHBoxLayout(self.similar_bar)
        sb.setContentsMargins(0, 0, 0, 0)
        self.similar_label = QLabel()
        clear_sim = QPushButton("× Benzerleri kapat")
        clear_sim.clicked.connect(self.clear_similar)
        sb.addWidget(self.similar_label, 1)
        sb.addWidget(clear_sim)
        self.similar_bar.hide()

        self.wave = Waveform()
        self.wave.clicked.connect(lambda: self.current and self.play_sample(self.current, force=True))
        center = QVBoxLayout()
        center.addWidget(self.similar_bar)
        center.addWidget(self.stack, 1)
        center.addWidget(self.wave)
        center_w = QWidget()
        center_w.setLayout(center)

        # right: kit
        right = QVBoxLayout()
        self.kit_label = QLabel()
        self.kit_label.setStyleSheet("font-weight:600;")
        right.addWidget(self.kit_label)
        hint = QLabel("Tablodan buraya sürükle, çift tıkla ya da Enter. Buradan Transfer'e sürüklediklerin "
                      "her zaman Syntakt formatındadır.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa4b2;")
        right.addWidget(hint)
        self.kit_list = KitList(self)
        self.kit_list.dropped.connect(self.add_to_kit)
        self.kit_list.itemDoubleClicked.connect(
            lambda it: self.play_path(it.data(Qt.ItemDataRole.UserRole)))
        right.addWidget(self.kit_list, 1)
        r1 = QHBoxLayout()
        for text, fn in (("Çıkar", self.remove_from_kit), ("Temizle", self.clear_kit)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            r1.addWidget(b)
        right.addLayout(r1)
        r2 = QHBoxLayout()
        for text, fn in (("Kaydet…", self.save_kit_as), ("Aç…", self.open_kit)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            r2.addWidget(b)
        right.addLayout(r2)
        export = QPushButton("Dışa aktar (Syntakt formatı)…")
        export.clicked.connect(self.export_kit)
        right.addWidget(export)
        right_w = QWidget()
        right_w.setLayout(right)

        split = QSplitter()
        split.addWidget(left_w)
        split.addWidget(center_w)
        split.addWidget(right_w)
        split.setSizes([250, 860, 340])
        split.setStretchFactor(1, 1)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addWidget(split, 1)
        cw = QWidget()
        cw.setLayout(root)
        self.setCentralWidget(cw)

        self.progress = QProgressBar(maximumWidth=260, visible=False)
        self.statusBar().addPermanentWidget(self.progress)

    def fill_devices(self):
        self.device_combo.addItem("Varsayılan çıkış", None)
        try:
            apis = sd.query_hostapis()
            ds = next((i for i, a in enumerate(apis) if "DirectSound" in a["name"]), None)
            for i, d in enumerate(sd.query_devices()):
                if d["max_output_channels"] > 0 and (ds is None or d["hostapi"] == ds):
                    self.device_combo.addItem(d["name"], i)
        except Exception as e:
            log(f"device list failed: {e}")
        saved = self.settings.value("device_name", "", type=str)
        idx = self.device_combo.findText(saved) if saved else -1
        if idx >= 0:
            self.device_combo.setCurrentIndex(idx)
        self.device_combo.currentIndexChanged.connect(
            lambda _: self.settings.setValue("device_name", self.device_combo.currentText()))

    # ---------- library
    def load_library(self):
        con = db_connect()
        con.row_factory = sqlite3.Row
        roots = list(con.execute("SELECT path, scanned FROM roots ORDER BY added"))
        self.roots = [r["path"] for r in roots]
        for r in con.execute("SELECT * FROM samples"):
            rec = dict(r)
            if rec["root"] in self.roots:
                self.samples[rec["path"]] = make_sample(rec)
        con.close()
        self.rebuild_sidebar()
        self.apply_filter()
        # finish scans that were interrupted last time (the app was closed mid-scan)
        unfinished = [r["path"] for r in roots if r["scanned"] is None]
        if unfinished:
            QTimer.singleShot(500, lambda: [self.scan(r) for r in unfinished])

    def add_folder(self):
        start = self.settings.value("last_dir", str(Path.home()), type=str)
        d = QFileDialog.getExistingDirectory(self, "Sample klasörü seç", start)
        if not d:
            return
        self.settings.setValue("last_dir", os.path.dirname(os.path.normpath(d)))
        self.add_root(d)

    def add_root(self, d):
        d = os.path.normpath(d)
        for r in self.roots:
            if is_inside(d, r):
                QMessageBox.information(self, "Zaten ekli", f"Bu klasör zaten ekli olan şu klasörün içinde:\n{r}")
                return
        inner = [r for r in self.roots if is_inside(r, d)]
        if inner:
            self.stop_scanner()
            for r in inner:
                self.forget_root(r)
        con = db_connect()
        con.execute("INSERT OR IGNORE INTO roots(path, added, scanned) VALUES (?, ?, NULL)", (d, time.time()))
        con.commit()
        con.close()
        self.roots.append(d)
        self.rebuild_sidebar()
        self.scan(d)

    def scan(self, root):
        self.scanner.enqueue(root)
        if not self.scanner.isRunning():
            self.scanner.start()
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)

    def stop_scanner(self):
        """Stop the scan; an unfinished folder goes back to the front of the queue."""
        if self.scanner.isRunning():
            self.scanner.stop_flag = True
            self.scanner.wait()
            if self.scanner.current_root:
                self.scanner.enqueue(self.scanner.current_root, front=True)
                self.scanner.current_root = None

    def resume_scanner(self):
        if self.scanner.pending() and not self.scanner.isRunning():
            self.scanner.start()
            self.progress.setVisible(True)
            self.progress.setRange(0, 0)

    def forget_root(self, root):
        con = db_connect()
        con.execute("DELETE FROM samples WHERE root=?", (root,))
        con.execute("DELETE FROM roots WHERE path=?", (root,))
        con.commit()
        con.close()
        with self.scanner.lock:
            self.scanner.queue = [q for q in self.scanner.queue if q != root]
        self.samples = {p: s for p, s in self.samples.items() if s.root != root}
        if root in self.roots:
            self.roots.remove(root)

    def remove_root(self, root):
        if QMessageBox.question(self, "Klasörü kaldır",
                                f"{root}\n\nKütüphaneden kaldırılsın mı? (Diskteki dosyalara dokunulmaz.)"
                                ) != QMessageBox.StandardButton.Yes:
            return
        self.stop_scanner()
        self.forget_root(root)
        self.folder_filter = (None, None)
        self.rebuild_sidebar()
        self.apply_filter()
        self.resume_scanner()

    def on_scan_progress(self, root, done, total):
        name = os.path.basename(root) or root
        if total < 0:
            self.progress.setRange(0, 0)
            self.statusBar().showMessage(f"{name}: dosyalar bulunuyor… {-total}")
        else:
            self.progress.setRange(0, max(total, 1))
            self.progress.setValue(done)
            self.statusBar().showMessage(f"{name}: analiz {done}/{total}")

    def on_scan_batch(self, recs):
        for rec in recs:
            if rec["root"] in self.roots:
                self.samples[rec["path"]] = make_sample(rec)
        if not self.refresh_timer.isActive():
            self.refresh_timer.start()

    def on_scan_removed(self, paths):
        for p in paths:
            self.samples.pop(p, None)
        self.refresh_timer.start()

    def on_root_done(self, root, n):
        self.statusBar().showMessage(f"{os.path.basename(root)}: tarama bitti ({n} yeni/değişen dosya)", 8000)
        self.refresh_after_scan()

    def on_scanner_finished(self):
        if self.scanner.isRunning():
            return
        if self.scanner.pending() and not self.scanner.stop_flag:
            self.scanner.start()
            return
        self.progress.setVisible(False)
        self.refresh_after_scan()

    def refresh_after_scan(self):
        self.rebuild_sidebar()
        self.apply_filter(keep_selection=True)

    # ---------- sidebar
    def rebuild_sidebar(self):
        sel = self.folder_filter
        self.folder_tree.blockSignals(True)
        self.folder_tree.clear()
        counts = Counter((s.root, s.top) for s in self.samples.values())
        root_counts = Counter(s.root for s in self.samples.values())
        all_item = QTreeWidgetItem([f"Tümü ({len(self.samples)})"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, (None, None))
        self.folder_tree.addTopLevelItem(all_item)
        selected_item = all_item
        for r in self.roots:
            it = QTreeWidgetItem([f"{os.path.basename(r) or r} ({root_counts.get(r, 0)})"])
            it.setToolTip(0, r)
            it.setData(0, Qt.ItemDataRole.UserRole, (r, None))
            self.folder_tree.addTopLevelItem(it)
            if sel == (r, None):
                selected_item = it
            for (rr, top), n in sorted(counts.items(), key=lambda kv: kv[0][1].lower()):
                if rr != r or not top:
                    continue
                ch = QTreeWidgetItem([f"{top} ({n})"])
                ch.setData(0, Qt.ItemDataRole.UserRole, (r, top))
                it.addChild(ch)
                if sel == (r, top):
                    selected_item = ch
            it.setExpanded(len(self.roots) <= 3)
        selected_item.setSelected(True)
        self.folder_tree.blockSignals(False)
        self.stack.setCurrentIndex(1 if self.roots else 0)
        self.update_categories()

    def update_categories(self):
        cur = [it.data(Qt.ItemDataRole.UserRole) for it in self.cat_list.selectedItems()]
        self.cat_list.blockSignals(True)
        self.cat_list.clear()
        base = [s for s in self.samples.values() if self.in_folder(s)]
        counts = Counter(s.category for s in base)
        items = [("Tümü", None, len(base))] + [(c, c, counts[c]) for c in CATEGORY_ORDER if counts.get(c)]
        for label, key, n in items:
            it = QListWidgetItem(f"{label}  ({n})")
            it.setData(Qt.ItemDataRole.UserRole, key)
            self.cat_list.addItem(it)
            if key in cur or (not cur and key is None):
                it.setSelected(True)
        self.cat_list.blockSignals(False)

    def on_folder_selected(self):
        items = self.folder_tree.selectedItems()
        self.folder_filter = items[0].data(0, Qt.ItemDataRole.UserRole) if items else (None, None)
        self.update_categories()
        self.apply_filter()

    def in_folder(self, s):
        root, top = self.folder_filter
        return (root is None or s.root == root) and (top is None or s.top == top)

    # ---------- filtering
    def apply_filter(self, keep_selection=False):
        if not isinstance(keep_selection, bool):
            keep_selection = False
        cats = {it.data(Qt.ItemDataRole.UserRole) for it in self.cat_list.selectedItems()} - {None}
        tags = [t for t, b in self.tag_buttons.items() if b.isChecked()]
        terms = self.search.text().lower().split()
        kind = self.type_combo.currentIndex()
        only5 = self.only5.isChecked()
        rows = []
        for s in self.samples.values():
            if not self.in_folder(s):
                continue
            if terms and not all(t in s.hay for t in terms):
                continue
            if self.similar_to is None:
                if cats and s.category not in cats:
                    continue
                if tags and not all(t in s.tags for t in tags):
                    continue
            if kind == 1 and s.is_loop or kind == 2 and not s.is_loop:
                continue
            if only5 and s.dur > TWINSHOT_MAX_SEC:
                continue
            rows.append(s)
        if self.similar_to is not None:
            ref = feature_vec(self.similar_to)
            mat = np.stack([feature_vec(s) for s in rows]) if rows else np.zeros((0, 6))
            order = np.argsort(np.linalg.norm(mat - ref, axis=1))[:400] if rows else []
            rows = [rows[i] for i in order]
        keep = self.current.path if (keep_selection and self.current) else None
        scroll = self.table.verticalScrollBar().value()
        self._restoring = True
        self.model.set_rows(rows, sort=self.similar_to is None)
        if keep:
            for i, s in enumerate(self.model.rows):
                if s.path == keep:
                    self.table.selectRow(i)
                    break
            self.table.verticalScrollBar().setValue(scroll)
        self._restoring = False
        shown = len(rows)
        if not self.scanner.isRunning():
            self.statusBar().showMessage(f"{shown} sample gösteriliyor · kütüphanede {len(self.samples)}")

    def show_similar(self, s):
        self.similar_to = s
        self.similar_label.setText(f"Ses karakteri en çok şuna benzeyenler: <b>{s.name}</b> "
                                   f"(kategori ve karakter filtreleri devre dışı)")
        self.similar_bar.show()
        self.table.setSortingEnabled(False)
        self.apply_filter()
        self.table.selectRow(0)

    def clear_similar(self):
        self.similar_to = None
        self.similar_bar.hide()
        self.table.setSortingEnabled(True)
        self.apply_filter(keep_selection=True)

    # ---------- playback
    def selected_samples(self):
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.model.rows[r] for r in rows if r < len(self.model.rows)]

    def on_current_changed(self, cur, prev):
        if self._restoring or not cur.isValid() or cur.row() >= len(self.model.rows):
            return
        s = self.model.rows[cur.row()]
        if self.autoplay.isChecked():
            self.play_sample(s, force=False)
        else:
            self.show_wave(s)

    def load_audio(self, s):
        mode = self.syntakt_mode.isChecked()
        key = (s.path, mode)
        if key in self.audio_cache:
            self.audio_cache.move_to_end(key)
            return self.audio_cache[key]
        if mode:
            mono = to_syntakt(s.path)
            data, sr = mono[:, None], TWINSHOT_SR
        else:
            info = sf.info(s.path)
            data, sr = sf.read(s.path, frames=min(info.frames, int(info.samplerate * 90)), always_2d=True,
                               dtype="float32")
            mono = data.mean(axis=1)
        item = (data, sr, mono)
        self.audio_cache[key] = item
        while len(self.audio_cache) > 40:
            self.audio_cache.popitem(last=False)
        return item

    def show_wave(self, s):
        self.current = s
        try:
            _, sr, mono = self.load_audio(s)
            self.wave.set_audio(mono, sr, s.name)
        except Exception as e:
            self.wave.set_audio(None, 1, s.name)
            self.statusBar().showMessage(f"Okunamadı: {s.name} — {e}", 6000)

    def play_sample(self, s, force=True):
        now = time.perf_counter()
        if not force and self.last_play[0] == s.path and now - self.last_play[1] < 0.25:
            return
        self.last_play = (s.path, now)
        self.current = s
        try:
            data, sr, mono = self.load_audio(s)
        except Exception as e:
            self.statusBar().showMessage(f"Okunamadı: {s.name} — {e}", 6000)
            return
        self.wave.set_audio(mono, sr, s.name)
        out = data[:, :2] if data.shape[1] >= 2 else np.repeat(data, 2, axis=1)
        out = out * (self.volume.value() / 100.0)
        try:
            sd.stop()
            sd.play(out, sr, device=self.device_combo.currentData())
        except Exception as e:
            self.statusBar().showMessage(f"Ses çalınamadı: {e}", 6000)
            return
        self.play_start, self.play_len = time.perf_counter(), out.shape[0] / sr
        self.play_timer.start()

    def play_path(self, path):
        s = self.samples.get(path)
        if s:
            self.play_sample(s)

    def toggle_play(self):
        if self.play_timer.isActive():
            self.stop_play()
        else:
            sel = self.selected_samples()
            if sel:
                self.play_sample(sel[0])

    def stop_play(self):
        sd.stop()
        self.play_timer.stop()
        self.wave.set_pos(-1)

    def tick_playhead(self):
        if self.play_len <= 0:
            return
        frac = (time.perf_counter() - self.play_start) / self.play_len
        if frac >= 1:
            self.play_timer.stop()
            self.wave.set_pos(-1)
        else:
            self.wave.set_pos(frac)

    # ---------- drag & drop
    def start_file_drag(self, source, samples, convert):
        paths = []
        if convert:
            QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
            try:
                for s in samples:
                    try:
                        paths.append(str(syntakt_file(s)))
                    except Exception as e:
                        log(f"convert failed {s.path}: {e}")
            finally:
                QApplication.restoreOverrideCursor()
        else:
            paths = [s.path for s in samples]
        if not paths:
            return
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(p) for p in paths])
        md.setData(MIME_PATHS, "\n".join(s.path for s in samples).encode("utf-8"))
        drag = QDrag(source)
        drag.setMimeData(md)
        drag.exec(Qt.DropAction.CopyAction)

    # ---------- context menus
    def table_menu(self, pos):
        samples = self.selected_samples()
        if not samples:
            return
        m = QMenu(self)
        m.addAction("Kite ekle", lambda: self.add_to_kit([s.path for s in samples]))
        m.addAction("Benzerlerini bul", lambda: self.show_similar(samples[0]))
        m.addSeparator()
        m.addAction(f"{FILE_MANAGER}'da göster", lambda: reveal(samples[0].path))
        m.addAction(f"Syntakt kopyasını {FILE_MANAGER}'da göster", lambda: reveal(str(syntakt_file(samples[0]))))
        m.addAction("Yolu kopyala", lambda: QApplication.clipboard().setText(
            "\n".join(s.path for s in samples)))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def folder_menu(self, pos):
        it = self.folder_tree.itemAt(pos)
        if not it:
            return
        root, top = it.data(0, Qt.ItemDataRole.UserRole)
        if root is None:
            return
        m = QMenu(self)
        if top is None:
            m.addAction("Yeniden tara (yeni/değişen dosyalar)", lambda: self.scan(root))
            m.addAction("Kütüphaneden kaldır", lambda: self.remove_root(root))
        m.addAction(f"{FILE_MANAGER}'da aç", lambda: open_folder(os.path.join(root, top or "")))
        m.exec(self.folder_tree.viewport().mapToGlobal(pos))

    # ---------- kit
    def add_to_kit(self, paths):
        added = 0
        for p in paths:
            if p in self.samples and p not in self.kit:
                self.kit.append(p)
                added += 1
        if added:
            self.refresh_kit()
            self.save_kit(KIT_FILE)

    def remove_from_kit(self):
        rm = {it.data(Qt.ItemDataRole.UserRole) for it in self.kit_list.selectedItems()}
        self.kit = [p for p in self.kit if p not in rm]
        self.refresh_kit()
        self.save_kit(KIT_FILE)

    def clear_kit(self):
        if self.kit and QMessageBox.question(self, "Kiti temizle", "Kitteki tüm sample'lar çıkarılsın mı?"
                                             ) == QMessageBox.StandardButton.Yes:
            self.kit = []
            self.refresh_kit()
            self.save_kit(KIT_FILE)

    def kit_bytes(self):
        return sum(int(min(self.samples[p].dur, TWINSHOT_MAX_SEC) * TWINSHOT_SR * 2) + 44
                   for p in self.kit if p in self.samples)

    def refresh_kit(self):
        self.kit_list.clear()
        for i, p in enumerate(self.kit, 1):
            s = self.samples.get(p)
            label = f"{i:02d}  {s.name}  ·  {s.category}  ·  {min(s.dur, 5):.2f} sn" if s else f"{i:02d}  {p}"
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, p)
            it.setToolTip(p)
            if s and s.dur > TWINSHOT_MAX_SEC:
                it.setForeground(QColor("#d9a441"))
            self.kit_list.addItem(it)
        mb = self.kit_bytes() / 1024 / 1024
        over = len(self.kit) > TWINSHOT_SLOTS or self.kit_bytes() > TWINSHOT_MEM
        self.kit_label.setText(f"Twinshot kiti: {len(self.kit)}/{TWINSHOT_SLOTS} slot · ≈{mb:.1f}/32 MB")
        self.kit_label.setStyleSheet(f"font-weight:600; color:{'#e06c6c' if over else '#e5e7eb'};")

    def save_kit(self, path):
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps({"samples": self.kit}, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log(f"save kit failed: {e}")

    def load_kit(self, path=KIT_FILE):
        try:
            self.kit = [p for p in json.loads(Path(path).read_text(encoding="utf-8"))["samples"]]
        except Exception:
            self.kit = []
        self.refresh_kit()

    def save_kit_as(self):
        f, _ = QFileDialog.getSaveFileName(self, "Kiti kaydet", str(DATA_DIR / "kit.json"), "Kit (*.json)")
        if f:
            self.save_kit(f)

    def open_kit(self):
        f, _ = QFileDialog.getOpenFileName(self, "Kit aç", str(DATA_DIR), "Kit (*.json)")
        if f:
            self.load_kit(f)
            self.save_kit(KIT_FILE)

    def export_kit(self):
        if not self.kit:
            return
        d = QFileDialog.getExistingDirectory(self, "Kiti nereye aktarayım?",
                                             self.settings.value("export_dir", str(Path.home()), type=str))
        if not d:
            return
        self.settings.setValue("export_dir", d)
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        errors = []
        try:
            for i, p in enumerate(self.kit, 1):
                s = self.samples.get(p)
                if not s:
                    errors.append(p)
                    continue
                try:
                    write_syntakt(to_syntakt(p), Path(d) / f"{i:02d}_{safe_name(s.stem, 28)}.wav")
                except Exception as e:
                    errors.append(f"{p}: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        msg = f"{len(self.kit) - len(errors)} dosya Syntakt formatında aktarıldı:\n{d}"
        if errors:
            msg += "\n\nAktarılamayanlar:\n" + "\n".join(errors[:10])
        QMessageBox.information(self, "Dışa aktarıldı", msg)
        open_folder(d)

    def closeEvent(self, e):
        self.stop_scanner()
        sd.stop()
        super().closeEvent(e)


# ---------------------------------------------------------------- helpers

def is_inside(path, root):
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:  # different drives
        return False


def reveal(path):
    path = os.path.normpath(path)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def open_folder(path):
    path = os.path.normpath(path)
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def log(msg):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def dark_palette(app):
    app.setStyle("Fusion")
    p = QPalette()
    base, alt, text = QColor("#1f2228"), QColor("#252932"), QColor("#e5e7eb")
    p.setColor(QPalette.ColorRole.Window, QColor("#1a1d22"))
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, alt)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor("#2a2e37"))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor("#2f7d6d"))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor("#2a2e37"))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor("#7b8494"))
    app.setPalette(p)
    app.setStyleSheet("QPushButton:checked { background:#2f7d6d; color:white; }")


def selftest():
    """Checks the audio stack inside a packaged build (libsndfile, PortAudio). Exit code 0 = OK."""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="sb_selftest_"))
    x = np.sin(2 * np.pi * 220 * np.arange(44100 * 6) / 44100).astype(np.float32)
    src = d / "Test_Pad_Am.wav"
    sf.write(str(src), np.stack([x, x], axis=1), 44100, subtype="PCM_24")
    f = analyze(str(src))
    assert abs(f["dur"] - 6) < 0.01, f
    s = make_sample(dict(path=str(src), root=str(d), **f))
    assert (s.category, s.key) == ("Pad/Akor", "Am"), (s.category, s.key)
    y = to_syntakt(str(src))
    assert y.size == TWINSHOT_MAX_SEC * TWINSHOT_SR, y.size
    out = d / "out.wav"
    write_syntakt(y, out)
    info = sf.info(str(out))
    assert (info.samplerate, info.channels, info.subtype) == (48000, 1, "PCM_16"), info
    sd.query_hostapis()
    print("selftest OK", file=sys.stderr)
    return 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.excepthook = lambda t, v, tb: log("".join(traceback.format_exception(t, v, tb)))
    app = QApplication(sys.argv)
    dark_palette(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
