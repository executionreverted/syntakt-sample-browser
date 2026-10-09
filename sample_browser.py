"""Sample Browser — a small sample browser for building Elektron Syntakt Twinshot kits.

- "+ Add folder" scans only that folder; results are cached in SQLite.
  Later launches don't rescan; "Rescan" only looks at new/changed files.
- Sounds are categorized from file/folder names; if the name says nothing, from the sound itself (~).
- Click / arrow keys to audition. Space: play/stop. Enter: add to kit.
- Drag & drop into Transfer, Ableton, Explorer/Finder. With "Syntakt mode" on, dragged files are copies
  converted to 48 kHz / 16-bit / mono / max 5 s (previews play that version too).
- UI languages: English (default), Turkish.

Requires: PyQt6, numpy, soundfile, sounddevice
"""
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from PyQt6.QtCore import (QAbstractTableModel, QEvent, QMimeData, QModelIndex, QPointF, QRect, QRectF, QSettings, Qt,
                          QThread, QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import (QColor, QCursor, QDrag, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPalette, QPen,
                         QPixmap, QShortcut)
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                             QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                             QMenu, QMessageBox, QProgressBar, QPushButton, QSlider, QSplitter, QStackedWidget,
                             QTableView, QToolButton, QToolTip, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

__version__ = "0.4.1"

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
KITS_DIR = DATA_DIR / "kits"
KIT_DRAG_DIR = DATA_DIR / "kit_drag"  # numbered copies handed to Transfer by "Drag kit into Transfer"
LOG_FILE = DATA_DIR / "log.txt"

AUDIO_EXT = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}
TWINSHOT_SR = 48000
TWINSHOT_MAX_SEC = 5.0
TWINSHOT_SLOTS = 64
TWINSHOT_MEM = 32 * 1024 * 1024
FEATURES = ("dur", "sr", "ch", "peak_db", "centroid", "flatness", "low", "high", "decay")

MIME_PATHS = "application/x-sample-browser-paths"


# ---------------------------------------------------------------- i18n
# English is the main language; Turkish was the original UI copy.

LANGUAGES = {"en": "English", "tr": "Türkçe"}
STRINGS = {
    "en": {
        "window_title": "Sample Browser · Syntakt  v{version}",
        "search_placeholder": "Search name, folder, category, key (e.g. 'kick dark', 'pad fm')…",
        "type_all": "All",
        "type_oneshot": "One-shot",
        "type_loop": "Loop",
        "only5": "≤ 5 s only",
        "syntakt_mode": "Syntakt mode",
        "syntakt_mode_tip": "Previews and drags: 48 kHz · 16-bit · mono · max 5 s, leading/trailing silence "
                            "trimmed, normalized to -1 dB",
        "autoplay": "Autoplay",
        "volume": "Volume",
        "output": "Output",
        "default_output": "Default output",
        "language": "Language",
        "app_caption": "Twinshot kit builder",
        "display_white": "White display",
        "display_red": "Red display",
        "folders": "Folders",
        "add_folder": "+ Add folder",
        "categories": "Categories",
        "character": "Character",
        "empty_state": "To get started, pick a sample folder with <b>+ Add folder</b> on the left.<br>"
                       "Only the folders you add are scanned, and results are saved.",
        "close_similar": "× Close similar",
        "similar_banner": "Sounds closest in character to: <b>{name}</b> (category and character filters are off)",
        "kit_hint": "Drag samples here from the table, double-click, or press Enter. Drag to reorder. "
                    "To load the kit, drag the button at the bottom onto Transfer.",
        "kit_empty": "Your kit is empty.\nDouble-click sounds or drag them here.",
        "kit_remove": "Remove",
        "kit_clear": "Clear",
        "kit_saved_kits": "Saved kits…",
        "kit_save_as": "Save as…",
        "kit_more": "More kit actions",
        "kit_load_folder": "Load kit from folder…",
        "kit_import": "Import kit file…",
        "kit_show_folder": "Show kits folder",
        "kit_delete_saved": "Delete saved kit “{name}”",
        "kit_delete_confirm": "Delete the saved kit “{name}”? (The samples themselves are not touched.)",
        "kit_name_prompt": "Kit name:",
        "kit_overwrite": "A kit called “{name}” already exists. Replace it?",
        "kit_replace_title": "Replace kit?",
        "kit_replace": "Replace the current kit ({n} samples)? It isn't saved under a name yet.",
        "kit_loaded": "Loaded kit “{name}” ({n} samples)",
        "kit_saved": "Saved kit “{name}”",
        "kit_from_folder": "Loaded {n} samples from “{name}” in file order",
        "kit_folder_empty": "No audio files in that folder.",
        "kit_folder_too_many": "The folder has {n} audio files; only the first 64 were used.",
        "kit_export_folder": "Export to folder…",
        "kit_export_zip": "Export .zip…",
        "zip_save_title": "Save kit as .zip",
        "zip_done": "Saved {n} samples to:\n{file}\n\nGood for sharing and backups. Note: Transfer may not import "
                    "zips of Syntakt samples (it can hang on “Converting sample data”). To load the kit, use "
                    "“Drag kit into Transfer”, or unzip and drag the files.",
        "kit_drag": "⠿  Drag kit into Transfer",
        "kit_drag_tip": "Drag this onto Transfer (Drop or Explore page). Every kit sample goes along, numbered in "
                        "slot order and already in Syntakt format. Restart the Syntakt afterwards.",
        "kit_drag_click": "Drag this button onto Transfer — clicking it does nothing.",
        "slots_warning": "Make sure the Syntakt has at least {n} free sample slots (64 in total, shared by every "
                         "project). If it doesn't, free some up in Transfer → Explore → Samples first, otherwise "
                         "the transfer can hang.",
        "menu_play": "Play",
        "menu_loop": "Loop in slot 2 (pads)",
        "loop_tip": "Makes a seamless loop from the sustained part of the sound (max 5 s). Twinshot loops the "
                    "sample in slot 2 when the AMP mode is ADSR, or AHD with HOLD set to NOTE.",
        "loop_marker": "loop",
        "kit_label": "Twinshot kit: {n}/{slots} slots · ≈{mb}/32 MB",
        "kit_over": "Over the Twinshot limit (64 slots / 32 MB)",
        "kit_added": "Added {n} to kit",
        "kit_removed": "Removed {n} from kit",
        "col_kit": "Kit",
        "col_name": "Name",
        "col_category": "Category",
        "col_folder": "Folder",
        "col_length": "Length",
        "col_key": "Key",
        "col_bpm": "BPM",
        "col_character": "Character",
        "seconds": "{s} s",
        "wave_empty": "Select a sample",
        "wave_limit": "5 s (Twinshot limit)",
        "wave_syntakt": "Syntakt preview",
        "wave_tip": "Click to play from here",
        "all_count": "All ({n})",
        "drag_count": "{n} samples",
        "shortcuts": "Space play/stop · Enter add/remove · Ctrl+F search · Esc clear",
        "pick_folder": "Choose sample folder",
        "already_added_title": "Already added",
        "already_added": "This folder is already in your library, inside:\n{root}",
        "remove_folder_title": "Remove folder",
        "remove_folder": "{root}\n\nRemove it from the library? (Files on disk won't be touched.)",
        "status_finding": "{name}: finding files… {n}",
        "status_analyzing": "{name}: analyzing {done}/{total}",
        "status_scan_done": "{name}: scan done ({n} new/changed files)",
        "status_showing": "Showing {shown} samples · {total} in library",
        "err_read": "Couldn't read: {name} — {error}",
        "err_play": "Couldn't play audio: {error}",
        "menu_add_to_kit": "Add to kit",
        "menu_remove_from_kit": "Remove from kit",
        "menu_find_similar": "Find similar",
        "menu_reveal": "Show in {fm}",
        "menu_reveal_converted": "Show Syntakt copy in {fm}",
        "menu_copy_path": "Copy path",
        "menu_rescan": "Rescan (new/changed files)",
        "menu_remove_folder": "Remove from library",
        "menu_open_folder": "Open in {fm}",
        "clear_kit_title": "Clear kit",
        "clear_kit": "Remove all samples from the kit?",
        "save_kit_title": "Save kit",
        "open_kit_title": "Open kit",
        "kit_file_filter": "Kit (*.json)",
        "export_pick": "Export kit to…",
        "export_done_title": "Export complete",
        "export_done": "{n} files exported in Syntakt format:\n{folder}\n\n"
                       "After transferring them, restart the Syntakt so the new samples show up.",
        "export_failed": "Couldn't export:",
        "cat_crackle": "Vinyl/Crackle",
        "cat_vocal": "Vocal",
        "cat_kick": "Kick",
        "cat_snare": "Snare",
        "cat_clap": "Clap",
        "cat_rim": "Rim",
        "cat_hat_open": "Open hat",
        "cat_hat_closed": "Closed hat",
        "cat_cymbal": "Ride/Crash",
        "cat_shaker": "Shaker/Tamb",
        "cat_tom": "Tom",
        "cat_perc": "Perc",
        "cat_fill": "Fill",
        "cat_drum_loop": "Drum loop",
        "cat_bass": "Bass/Sub",
        "cat_pad": "Pad/Chord",
        "cat_keys": "Keys",
        "cat_synth": "Synth",
        "cat_atmos": "Atmosphere",
        "cat_foley": "Foley",
        "cat_fx": "FX",
        "cat_other": "Other",
        "tag_dark": "Dark",
        "tag_bright": "Bright",
        "tag_short": "Short",
        "tag_tonal": "Tonal",
        "tag_noisy": "Noisy",
        "tag_sub": "Sub",
        "tag_loop": "loop",
        "tip_dark": "Mostly low frequencies",
        "tip_bright": "Lots of high end",
        "tip_short": "Shorter than 0.25 s",
        "tip_tonal": "Has a clear pitch",
        "tip_noisy": "Noise-like: hats, shakers, textures",
        "tip_sub": "Heavy below 150 Hz",
    },
    "tr": {
        "window_title": "Sample Browser · Syntakt  v{version}",
        "search_placeholder": "Ara: isim, klasör, kategori, ton (ör. 'kick karanlık', 'pad fm')…",
        "type_all": "Hepsi",
        "type_oneshot": "One-shot",
        "type_loop": "Loop",
        "only5": "Sadece ≤5 sn",
        "syntakt_mode": "Syntakt modu",
        "syntakt_mode_tip": "Önizleme ve sürükleme: 48 kHz · 16-bit · mono · en fazla 5 sn, baş/son sessizlik "
                            "kesilir, -1 dB normalize",
        "autoplay": "Otomatik çal",
        "volume": "Ses",
        "output": "Çıkış",
        "default_output": "Varsayılan çıkış",
        "language": "Dil",
        "app_caption": "Twinshot kit hazırlayıcı",
        "display_white": "Beyaz ekran",
        "display_red": "Kırmızı ekran",
        "folders": "Klasörler",
        "add_folder": "+ Klasör ekle",
        "categories": "Kategori",
        "character": "Karakter",
        "empty_state": "Başlamak için soldan <b>+ Klasör ekle</b> ile bir sample klasörü seç.<br>"
                       "Sadece eklediğin klasör taranır, sonuçlar saklanır.",
        "close_similar": "× Benzerleri kapat",
        "similar_banner": "Ses karakteri en çok şuna benzeyenler: <b>{name}</b> "
                          "(kategori ve karakter filtreleri devre dışı)",
        "kit_hint": "Tablodan buraya sürükle, çift tıkla ya da Enter. Sırayı sürükleyerek değiştir. "
                    "Kiti yüklemek için alttaki tuşu Transfer'e sürükle.",
        "kit_empty": "Kit boş.\nSeslere çift tıkla ya da buraya sürükle.",
        "kit_remove": "Çıkar",
        "kit_clear": "Temizle",
        "kit_saved_kits": "Kayıtlı kitler…",
        "kit_save_as": "Farklı kaydet…",
        "kit_more": "Diğer kit işlemleri",
        "kit_load_folder": "Klasörden kit yükle…",
        "kit_import": "Kit dosyası içe aktar…",
        "kit_show_folder": "Kitler klasörünü göster",
        "kit_delete_saved": "“{name}” kayıtlı kitini sil",
        "kit_delete_confirm": "“{name}” kayıtlı kiti silinsin mi? (Sample'lara dokunulmaz.)",
        "kit_name_prompt": "Kit adı:",
        "kit_overwrite": "“{name}” adında bir kit zaten var. Üzerine yazılsın mı?",
        "kit_replace_title": "Kit değiştirilsin mi?",
        "kit_replace": "Mevcut kit ({n} sample) değiştirilsin mi? Henüz bir adla kaydedilmedi.",
        "kit_loaded": "“{name}” kiti yüklendi ({n} sample)",
        "kit_saved": "“{name}” kiti kaydedildi",
        "kit_from_folder": "“{name}” klasöründen {n} sample dosya sırasıyla yüklendi",
        "kit_folder_empty": "Bu klasörde ses dosyası yok.",
        "kit_folder_too_many": "Klasörde {n} ses dosyası var; sadece ilk 64'ü alındı.",
        "kit_export_folder": "Klasöre aktar…",
        "kit_export_zip": ".zip olarak aktar…",
        "zip_save_title": "Kiti .zip olarak kaydet",
        "zip_done": "{n} sample şu dosyaya kaydedildi:\n{file}\n\nPaylaşmak ve yedeklemek için uygun. Not: Transfer "
                    "Syntakt sample'larını zip'ten almayabiliyor (“Converting sample data”da takılabiliyor). Kiti "
                    "yüklemek için “Kiti Transfer'e sürükle”yi kullan ya da zip'i açıp dosyaları sürükle.",
        "kit_drag": "⠿  Kiti Transfer'e sürükle",
        "kit_drag_tip": "Bunu Transfer'e (Drop ya da Explore sayfasına) sürükle. Kitteki tüm sample'lar slot sırasıyla "
                        "numaralanmış ve Syntakt formatında gider. Sonra Syntakt'ı yeniden başlat.",
        "kit_drag_click": "Bu tuşu Transfer'e sürükle, tıklamak bir şey yapmaz.",
        "slots_warning": "Syntakt'ta en az {n} boş sample slotu olduğundan emin ol (toplam 64, tüm projeler ortak "
                         "kullanır). Yoksa önce Transfer → Explore → Samples'tan yer aç, aksi halde aktarım "
                         "takılabilir.",
        "menu_play": "Çal",
        "menu_loop": "2. slotta loop (pad'ler)",
        "loop_tip": "Sesin sürekli kısmından çıt sesi olmadan dönen bir loop yapar (en fazla 5 sn). Twinshot, AMP "
                    "modu ADSR ya da HOLD'u NOTE olan AHD iken 2. slottaki sample'ı loop'lar.",
        "loop_marker": "loop",
        "kit_label": "Twinshot kiti: {n}/{slots} slot · ≈{mb}/32 MB",
        "kit_over": "Twinshot sınırı aşıldı (64 slot / 32 MB)",
        "kit_added": "Kite {n} ses eklendi",
        "kit_removed": "Kitten {n} ses çıkarıldı",
        "col_kit": "Kit",
        "col_name": "Ad",
        "col_category": "Kategori",
        "col_folder": "Klasör",
        "col_length": "Süre",
        "col_key": "Ton",
        "col_bpm": "BPM",
        "col_character": "Karakter",
        "seconds": "{s} sn",
        "wave_empty": "Bir sample seç",
        "wave_limit": "5 sn (Twinshot sınırı)",
        "wave_syntakt": "Syntakt önizleme",
        "wave_tip": "Tıkla, buradan çalsın",
        "all_count": "Tümü ({n})",
        "drag_count": "{n} sample",
        "shortcuts": "Boşluk çal/durdur · Enter ekle/çıkar · Ctrl+F ara · Esc temizle",
        "pick_folder": "Sample klasörü seç",
        "already_added_title": "Zaten ekli",
        "already_added": "Bu klasör zaten ekli olan şu klasörün içinde:\n{root}",
        "remove_folder_title": "Klasörü kaldır",
        "remove_folder": "{root}\n\nKütüphaneden kaldırılsın mı? (Diskteki dosyalara dokunulmaz.)",
        "status_finding": "{name}: dosyalar bulunuyor… {n}",
        "status_analyzing": "{name}: analiz {done}/{total}",
        "status_scan_done": "{name}: tarama bitti ({n} yeni/değişen dosya)",
        "status_showing": "{shown} sample gösteriliyor · kütüphanede {total}",
        "err_read": "Okunamadı: {name} — {error}",
        "err_play": "Ses çalınamadı: {error}",
        "menu_add_to_kit": "Kite ekle",
        "menu_remove_from_kit": "Kitten çıkar",
        "menu_find_similar": "Benzerlerini bul",
        "menu_reveal": "{fm}'da göster",
        "menu_reveal_converted": "Syntakt kopyasını {fm}'da göster",
        "menu_copy_path": "Yolu kopyala",
        "menu_rescan": "Yeniden tara (yeni/değişen dosyalar)",
        "menu_remove_folder": "Kütüphaneden kaldır",
        "menu_open_folder": "{fm}'da aç",
        "clear_kit_title": "Kiti temizle",
        "clear_kit": "Kitteki tüm sample'lar çıkarılsın mı?",
        "save_kit_title": "Kiti kaydet",
        "open_kit_title": "Kit aç",
        "kit_file_filter": "Kit (*.json)",
        "export_pick": "Kiti nereye aktarayım?",
        "export_done_title": "Dışa aktarıldı",
        "export_done": "{n} dosya Syntakt formatında aktarıldı:\n{folder}\n\n"
                       "Aktardıktan sonra yeni sample'ların görünmesi için Syntakt'ı yeniden başlat.",
        "export_failed": "Aktarılamayanlar:",
        "cat_crackle": "Cızırtı/Vinyl",
        "cat_vocal": "Vokal",
        "cat_kick": "Kick",
        "cat_snare": "Snare",
        "cat_clap": "Clap",
        "cat_rim": "Rim",
        "cat_hat_open": "Hat (açık)",
        "cat_hat_closed": "Hat (kapalı)",
        "cat_cymbal": "Ride/Crash",
        "cat_shaker": "Shaker/Tamb",
        "cat_tom": "Tom",
        "cat_perc": "Perc",
        "cat_fill": "Fill",
        "cat_drum_loop": "Davul loop",
        "cat_bass": "Bas/Sub",
        "cat_pad": "Pad/Akor",
        "cat_keys": "Keys",
        "cat_synth": "Synth",
        "cat_atmos": "Atmosfer",
        "cat_foley": "Foley",
        "cat_fx": "FX",
        "cat_other": "Diğer",
        "tag_dark": "Karanlık",
        "tag_bright": "Parlak",
        "tag_short": "Kısa",
        "tag_tonal": "Tonal",
        "tag_noisy": "Gürültülü",
        "tag_sub": "Sub",
        "tag_loop": "loop",
        "tip_dark": "Çoğunlukla alçak frekanslar",
        "tip_bright": "Tizi bol",
        "tip_short": "0,25 sn'den kısa",
        "tip_tonal": "Belirgin bir perdesi var",
        "tip_noisy": "Gürültü benzeri: hat, shaker, doku",
        "tip_sub": "150 Hz altı ağır",
    },
}
LANG = "en"


def tr(key, **kw):
    s = STRINGS.get(LANG, {}).get(key) or STRINGS["en"].get(key, key)
    return s.format(**kw) if kw else s


def cat_name(cat):
    return tr("cat_" + cat)


def secs(x):
    return tr("seconds", s=f"{x:.2f}")


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
# The ids are language-neutral; the UI shows tr("cat_<id>").
CATEGORY_RULES = [
    ("crackle", _rx(r"vinyl|crackles?|hiss|tape noise")),
    ("vocal", _rx(r"vocals?|vox|voices?|acapellas?|adlibs?|chants?|spoken")),
    ("kick", _rx(r"kicks?|kik|kck|bd|bass ?drums?")),
    ("snare", _rx(r"snares?|snr|sd")),
    ("clap", _rx(r"claps?|handclaps?|cp")),
    ("rim", _rx(r"rims?|rimshots?|side ?sticks?|sticks?")),
    ("hat_open", _rx(r"open ?hi ?hats?|open ?hats?|hats? ?open|open ?hh|ohh?")),
    ("hat_closed", _rx(r"hi ?hats?|hats?|hh|chh?|closed ?hats?")),
    ("cymbal", _rx(r"rides?|crash(es)?|cymbals?|cym|splash|china")),
    ("shaker", _rx(r"shakers?|shkr|shake|tamb(ourine)?s?|maracas?|cabasa")),
    ("tom", _rx(r"toms?|floor ?tom")),
    ("perc", _rx(r"percs?|percussions?|congas?|bongos?|cowbells?|claves?|blocks?|wood|knocks?|clacks?|"
                 r"tablas?|djembes?|timbales?|snaps?|snappys?|clicks?|thuds?|clunks?")),
    ("fill", _rx(r"fills?|rolls?")),
    ("drum_loop", _rx(r"drums?|tops?|breaks?|breakbeats?|beats?|grooves?")),
    ("bass", _rx(r"bass(es)?|subs?|808s?|reeses?|wobbles?")),
    ("pad", _rx(r"pads?|chords?|stabs?|drones?|strings?|choirs?")),
    ("keys", _rx(r"keys?|pianos?|rhodes|organs?|wurli|epiano|bells?|mallets?|marimba|guitars?")),
    ("synth", _rx(r"synths?|leads?|plucks?|arps?|melod(y|ies|ic)|music")),
    ("atmos", _rx(r"atmos|atmospheres?|ambiences?|ambient|textures?|field|rain|nature|wind|room ?tone|"
                  r"soundscapes?")),
    ("foley", _rx(r"foley|animism|cutlery|silverware|coins?|paper|scraps?|swipes?|splats?|squish(es)?|drips?|"
                  r"scatter|splash(es)?")),
    ("fx", _rx(r"fx|sfx|risers?|rise|uplifters?|downlifters?|sweeps?|impacts?|whoosh|swoosh|noise|"
               r"transitions?|reverse|downers?")),
]
CATEGORY_ORDER = [c for c, _ in CATEGORY_RULES] + ["other"]
TONAL_CATS = {"bass", "pad", "keys", "synth", "vocal"}
TAG_ROWS = (("dark", "bright", "short"), ("tonal", "noisy", "sub"))
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
        return "kick"
    if s.centroid > 6000 and s.dur < 0.5:
        return "hat_closed"
    if s.centroid > 5000 and s.flatness > 0.2:
        return "hat_open" if s.dur < 1.5 else "atmos"
    if s.flatness < 0.005 and s.dur > 0.8:
        return "pad" if s.centroid < 2000 else "synth"
    if s.dur < 0.5:
        return "perc"
    return "other"


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
        tags.append("dark")
    elif s.centroid > 3500:
        tags.append("bright")
    if s.flatness < 0.005 and s.dur > 0.5 and s.low < 0.5:
        tags.append("tonal")
    elif s.flatness > 0.15:
        tags.append("noisy")
    if s.low > 0.5:
        tags.append("sub")
    if s.dur < 0.25:
        tags.append("short")
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
    if s.category == "other":
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
    # searchable in every UI language: "kick dark" and "kick karanlık" both work
    words = [s.rel, s.key, "loop" if s.is_loop else "oneshot one-shot"]
    for table in STRINGS.values():
        words.append(table.get("cat_" + s.category, ""))
        words.extend(table.get("tag_" + t, "") for t in s.tags)
    s.hay = " ".join(words).lower()
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


def loop_ready(path):
    """Seamless loop for Twinshot sample slot 2 (it loops when AMP mode is ADSR, or AHD with HOLD = NOTE).

    Takes the sustained part of the sound (skips the attack swell and the release tail), max 5 s, and
    crossfades its end into its start, so jumping from the last sample back to the first is continuous.
    """
    info = sf.info(path)
    n = min(info.frames, int(info.samplerate * 30))
    x, sr = sf.read(path, frames=n, always_2d=True, dtype="float64")
    y = x.mean(axis=1)
    if y.size < 0.2 * sr or float(np.abs(y).max()) <= 1e-6:
        return to_syntakt(path)
    y = resample(y - y.mean(), int(sr), TWINSHOT_SR)
    sr = TWINSHOT_SR
    win = int(0.05 * sr)
    env = np.sqrt(np.convolve(y ** 2, np.ones(win) / win, mode="same"))
    loud = np.flatnonzero(env >= env.max() * 0.5)
    a, b = int(loud[0]), int(loud[-1])
    cap = int(TWINSHOT_MAX_SEC * sr)
    xf = int(min(0.4 * sr, max((b - a) // 4, 0.02 * sr)))
    seg = y[a:min(b, a + cap + xf)]
    if seg.size < 2 * xf + int(0.1 * sr):  # sustain too short: loop what we have from the loud point on
        seg = y[a:a + cap + xf]
    xf = max(1, min(xf, seg.size // 3))
    body = seg.size - xf
    out = seg[:body].copy()
    t = np.linspace(0.0, 1.0, xf)
    out[:xf] = seg[:xf] * np.sqrt(t) + seg[body:body + xf] * np.sqrt(1.0 - t)
    pk = float(np.abs(out).max())
    if pk > 0:
        out *= 10 ** (-1 / 20) / pk
    return out.astype(np.float32)


def kit_audio(path, loop=False):
    return loop_ready(path) if loop else to_syntakt(path)


def write_syntakt(y, out):
    """16-bit / 48 kHz / mono WAV with TPDF dither; `out` is a path or a binary file object."""
    d = (np.random.random(y.size) - np.random.random(y.size)) / 32768.0
    z = np.clip(y.astype(np.float64) + d, -1.0, 1.0 - 1 / 32768)
    if isinstance(out, (str, Path)):
        sf.write(str(out), z, TWINSHOT_SR, subtype="PCM_16")
    else:
        sf.write(out, z, TWINSHOT_SR, format="WAV", subtype="PCM_16")


def kit_file_name(index, s, loop=False):
    return f"{index:02d}_{safe_name(s.stem, 28)}{'_loop' if loop else ''}.wav"


def syntakt_file(s, loop=False):
    """Converted copy in the cache; the file keeps the sample's own name (Transfer uses it)."""
    h = hashlib.sha1(f"{s.path}|{s.size}|{s.mtime}|{loop}".encode("utf-8", "replace")).hexdigest()[:12]
    out = CONVERT_DIR / h / (safe_name(s.stem) + ("_loop" if loop else "") + ".wav")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        write_syntakt(kit_audio(s.path, loop), out)
    return out


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s.lower())]


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

# ---------------------------------------------------------------- look & feel
# Modelled on the hardware: matte black panel, grey silkscreen labels, red/pink secondary labels and FUNC key,
# OLED menus (selected row = inverted), trig keys with coloured LED numbers.

LED = {"red": "#ff4d4d", "yellow": "#f4d24e", "green": "#a6e15a", "white": "#f2f2f2", "pink": "#ff7d8c",
       "dim": "#8d8d93"}
CATEGORY_LED = {
    "kick": "red", "tom": "red", "bass": "red",
    "snare": "yellow", "clap": "yellow", "rim": "yellow",
    "hat_closed": "white", "hat_open": "white", "cymbal": "white", "shaker": "white",
    "perc": "green", "foley": "green", "fill": "green", "drum_loop": "green",
    "pad": "pink", "keys": "pink", "synth": "pink", "vocal": "pink",
    "atmos": "dim", "crackle": "dim", "fx": "dim", "other": "dim",
}
THEMES = {  # "white" = the current Syntakt OLED, "red" = the red display / amber FUNC look
    "white": {"ink": "#f4f4f4", "oled": "#000000", "func": "#e3525b", "func_text": "#1c0b0c", "accent": "#ff4d4d"},
    "red": {"ink": "#ff5a2e", "oled": "#0c0201", "func": "#e07a3a", "func_text": "#1c0e04", "accent": "#ff7a3d"},
}
THEME = "white"


def theme(key):
    return THEMES[THEME][key]


def accent_color(alpha=255):
    c = QColor(theme("accent"))
    c.setAlpha(alpha)
    return c


def led_color(cat):
    return QColor(LED[CATEGORY_LED.get(cat, "dim")])


def caps(s):
    """Upper-case like the panel labels (Turkish-aware: i -> İ)."""
    return (s.replace("i", "İ") if LANG == "tr" else s).upper()


_icons = {}


def led_icon(cat):
    """Small glowing LED dot in the category's colour."""
    key = CATEGORY_LED.get(cat, "dim")
    if key not in _icons:
        pm = QPixmap(12, 12)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(LED[key])
        glow = QColor(c)
        glow.setAlpha(60)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(0, 0, 12, 12)
        p.setBrush(c)
        p.drawEllipse(3, 3, 6, 6)
        p.end()
        _icons[key] = QIcon(pm)
    return _icons[key]


def build_stylesheet():
    t = THEMES[THEME]
    return f"""
    QWidget {{ background: #141416; color: #e8e8ea; font-size: 12px; }}
    QMainWindow, QStatusBar {{ background: #111113; }}
    QStatusBar {{ color: #8e8e95; }}
    QStatusBar QLabel {{ color: #66666e; background: transparent; }}
    QLabel {{ background: transparent; }}
    QLabel#wordmark {{ color: #fafafa; font-size: 19px; font-weight: 700; }}
    QLabel#caption {{ color: {t['func']}; font-size: 9px; font-weight: 600; }}
    QLabel#section {{ color: #8e8e95; font-size: 10px; font-weight: 700; padding-top: 6px; }}
    QLabel#hint {{ color: #74747c; font-size: 11px; }}
    QLabel#empty {{ color: #8e8e95; font-size: 15px; }}
    QLabel#warn {{ color: {LED['yellow']}; font-size: 11px; }}
    QLineEdit, QComboBox {{ background: #0c0c0e; border: 1px solid #2c2c31; border-radius: 4px; padding: 4px 8px;
        selection-background-color: {t['ink']}; selection-color: {t['oled']}; }}
    QLineEdit:focus, QComboBox:focus {{ border-color: #66666e; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{ background: #0c0c0e; border: 1px solid #2c2c31; outline: 0;
        selection-background-color: {t['ink']}; selection-color: {t['oled']}; }}
    QPushButton, QToolButton {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #323236, stop:1 #252528);
        border: 1px solid #3c3c41; border-bottom-color: #19191c; border-radius: 5px; padding: 6px 10px;
        color: #e8e8ea; font-size: 11px; font-weight: 700; }}
    QPushButton:hover, QToolButton:hover {{ border-color: #5c5c64; }}
    QPushButton:pressed, QToolButton:pressed {{ background: #1c1c1f; }}
    QPushButton:disabled {{ color: #4f4f56; border-color: #2a2a2e; background: #1f1f22; }}
    QPushButton:checked {{ color: {t['accent']}; border: 1px solid {t['accent']}; background: #1c1617; }}
    QPushButton#func {{ background: {t['func']}; color: {t['func_text']}; border: 1px solid {t['func']}; }}
    QPushButton#func:hover {{ border-color: #ffffff; }}
    QPushButton#func:disabled {{ background: #2e2224; color: #5c4c4e; border-color: #2e2224; }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}
    QCheckBox {{ spacing: 6px; color: #d4d4d8; background: transparent; }}
    QCheckBox::indicator {{ width: 9px; height: 9px; border-radius: 2px; background: #2a2a2e; border: 1px solid #4a4a50; }}
    QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}
    QTreeWidget, QListWidget, QTableView {{ background: #0e0e10; alternate-background-color: #131316;
        border: 1px solid #25252a; border-radius: 6px; outline: 0; }}
    QTreeWidget::item, QListWidget::item {{ padding: 3px 4px; border-radius: 2px; }}
    QTreeWidget#oled::item:selected, QListWidget#oled::item:selected {{ background: {t['ink']}; color: {t['oled']}; }}
    QListWidget#kit::item:selected, QTableView::item:selected {{ background: #34353b; color: #ffffff; }}
    QTreeWidget::branch {{ background: transparent; }}
    QHeaderView::section {{ background: #17171a; color: #8e8e95; border: none; border-bottom: 1px solid #25252a;
        padding: 4px 6px; font-size: 10px; font-weight: 700; }}
    QTableCornerButton::section {{ background: #17171a; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle {{ background: #3a3a40; border-radius: 3px; min-height: 24px; min-width: 24px; }}
    QScrollBar::handle:hover {{ background: #50505a; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QSplitter::handle {{ background: #141416; }}
    QSlider {{ background: transparent; }}
    QSlider::groove:horizontal {{ height: 4px; background: #2a2a2e; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: #6d6d75; border-radius: 2px; }}
    QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
        background: qradialgradient(cx:0.5, cy:0.35, radius:0.7, stop:0 #55555c, stop:1 #141416);
        border: 1px solid #5a5a62; }}
    QMenu {{ background: #0c0c0e; border: 1px solid #2c2c31; padding: 4px; }}
    QMenu::item {{ padding: 5px 18px; border-radius: 3px; background: transparent; }}
    QMenu::item:selected {{ background: {t['ink']}; color: {t['oled']}; }}
    QMenu::item:disabled {{ color: #55555b; }}
    QMenu::separator {{ height: 1px; background: #2c2c31; margin: 4px 6px; }}
    QMenu::indicator {{ width: 9px; height: 9px; border-radius: 2px; background: #2a2a2e; border: 1px solid #4a4a50;
        margin-left: 4px; }}
    QMenu::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}
    QToolTip {{ background: #000000; color: {t['ink']}; border: 1px solid #3a3a40; padding: 4px; }}
    QProgressBar {{ background: #0c0c0e; border: 1px solid #2c2c31; border-radius: 3px; max-height: 8px;
        color: transparent; }}
    QProgressBar::chunk {{ background: {t['accent']}; border-radius: 2px; }}
    """


def section_label(text):
    lbl = QLabel(caps(text), objectName="section")
    f = lbl.font()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
    lbl.setFont(f)
    return lbl


def key_button(text, func=False, checkable=False):
    b = QPushButton(caps(text), checkable=checkable)
    if func:
        b.setObjectName("func")
    f = b.font()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
    b.setFont(f)
    return b


# ---------------------------------------------------------------- pixel font (for the OLED display)
# 5x7 dot-matrix glyphs, one string of 7 rows per character.
_FONT_SRC = {
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110", "D": "11100 10010 10001 10001 10001 10010 11100",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110", "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001", "N": "10001 10001 11001 10101 10011 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110", "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010", "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 10001 01010 00100 00100 00100", "Z": "11111 00001 00010 00100 01000 10000 11111",
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
    " ": "00000 00000 00000 00000 00000 00000 00000", ".": "00000 00000 00000 00000 00000 01100 01100",
    ":": "00000 01100 01100 00000 01100 01100 00000", "-": "00000 00000 00000 11111 00000 00000 00000",
    "_": "00000 00000 00000 00000 00000 00000 11111", "/": "00000 00001 00010 00100 01000 10000 00000",
    "#": "01010 01010 11111 01010 11111 01010 01010", "(": "00010 00100 01000 01000 01000 00100 00010",
    ")": "01000 00100 00010 00010 00010 00100 01000", "+": "00000 00100 00100 11111 00100 00100 00000",
    "=": "00000 00000 11111 00000 11111 00000 00000", "<": "00010 00100 01000 10000 01000 00100 00010",
    ">": "01000 00100 00010 00001 00010 00100 01000", "?": "01110 10001 00001 00010 00100 00000 00100",
    "!": "00100 00100 00100 00100 00100 00000 00100", ",": "00000 00000 00000 00000 01100 00100 01000",
    "'": "01100 00100 01000 00000 00000 00000 00000", "&": "01100 10010 10100 01000 10101 10010 01101",
    "%": "11000 11001 00010 00100 01000 10011 00011", "~": "00000 00000 01000 10101 00010 00000 00000",
    "*": "00000 00100 10101 01110 10101 00100 00000", "@": "01110 10001 00001 01101 10101 10101 01110",
}
PIXEL_FONT = {k: tuple(int(r, 2) for r in v.split()) for k, v in _FONT_SRC.items()}
_TO_ASCII = str.maketrans({"Ç": "C", "Ğ": "G", "İ": "I", "Ö": "O", "Ş": "S", "Ü": "U", "ç": "c", "ğ": "g",
                           "ı": "i", "ö": "o", "ş": "s", "ü": "u", "×": "x", "·": "-", "–": "-", "—": "-",
                           "≤": "<", "…": ".", "↻": "@", "[": "(", "]": ")"})


def pixel_text_width(text, scale):
    return max(0, len(text) * 6 * scale - scale)


def draw_pixel_text(p, x, y, text, scale, color):
    """Draw upper-case dot-matrix text; returns the x after the last glyph."""
    text = text.translate(_TO_ASCII).upper()
    for ch in text:
        rows = PIXEL_FONT.get(ch, PIXEL_FONT["?"])
        for r, bits in enumerate(rows):
            if bits:
                for c in range(5):
                    if bits & (16 >> c):
                        p.fillRect(int(x + c * scale), int(y + r * scale), scale, scale, color)
        x += 6 * scale
    return x


def fit_pixel_text(text, scale, width):
    max_chars = max(1, (width + scale) // (6 * scale))
    text = text.translate(_TO_ASCII).upper()
    return text if len(text) <= max_chars else text[:max(1, max_chars - 2)] + ".."
COLUMNS = ["col_kit", "col_name", "col_category", "col_folder", "col_length", "col_key", "col_bpm", "col_character"]


class SampleModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.kit_index = {}  # path -> slot number (1-based)
        self.sort_col, self.sort_order = 1, Qt.SortOrder.AscendingOrder
        self.sorting = True
        self.bold = QFont()
        self.bold.setBold(True)

    def sort_key(self, col):
        return [
            lambda s: (self.kit_index.get(s.path, 10 ** 6), s.name.lower()),
            lambda s: s.name.lower(),
            lambda s: (CATEGORY_ORDER.index(s.category), s.name.lower()),
            lambda s: (s.top.lower(), s.sub.lower(), s.name.lower()),
            lambda s: s.dur,
            lambda s: (s.key == "", s.key),
            lambda s: (not s.is_loop, s.bpm),
            lambda s: s.centroid,
        ][col]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return caps(tr(COLUMNS[section]))
        return None

    def flags(self, index):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        s = self.rows[index.row()]
        c = index.column()
        slot = self.kit_index.get(s.path)
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0:
                return f"{slot:02d}" if slot else ""
            if c == 1:
                return s.name
            if c == 2:
                return cat_name(s.category) + (" ~" if s.guessed else "")
            if c == 3:
                return s.top + (" › " + s.sub if s.sub else "")
            if c == 4:
                return secs(s.dur)
            if c == 5:
                return s.key
            if c == 6:
                return str(s.bpm) if s.is_loop and s.bpm else ""
            if c == 7:
                return " · ".join([tr("tag_" + t) for t in s.tags] + ([tr("tag_loop")] if s.is_loop else []))
        elif role == Qt.ItemDataRole.ToolTipRole:
            return s.path + ("\n" + s.err if s.err else "")
        elif role == Qt.ItemDataRole.DecorationRole:
            if c == 2:
                return led_icon(s.category)
        elif role == Qt.ItemDataRole.BackgroundRole:
            if slot:
                return accent_color(28)
        elif role == Qt.ItemDataRole.FontRole:
            if slot and c in (0, 1):
                return self.bold
        elif role == Qt.ItemDataRole.ForegroundRole:
            if s.err:
                return QColor("#e06c6c")
            if c == 0 and slot:
                return accent_color()
            if c == 4 and s.dur > TWINSHOT_MAX_SEC:
                return QColor("#d9a441")
            if c == 2 and s.guessed:
                return QColor("#9aa4b2")
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if c in (4, 6):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if c == 0:
                return int(Qt.AlignmentFlag.AlignCenter)
        return None

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sort_col, self.sort_order = column, order
        if not self.sorting:
            return
        self.layoutAboutToBeChanged.emit()
        self._sort()
        self.layoutChanged.emit()

    def _sort(self):
        self.rows.sort(key=self.sort_key(self.sort_col), reverse=self.sort_order == Qt.SortOrder.DescendingOrder)

    def set_rows(self, rows, sort=True):
        self.beginResetModel()
        self.rows = rows
        self.sorting = sort
        if sort:
            self._sort()
        self.endResetModel()

    def set_kit(self, kit):
        self.kit_index = {p: i for i, p in enumerate(kit, 1)}
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(COLUMNS) - 1))


class SampleTable(QTableView):
    toggle_requested = pyqtSignal()
    kit_toggle_requested = pyqtSignal()

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
            self.kit_toggle_requested.emit()
            return
        super().keyPressEvent(e)

    def startDrag(self, actions):
        samples = self.win.selected_samples()
        if samples:
            self.win.start_file_drag(self, samples, convert=self.win.syntakt_mode.isChecked())


class KitList(QListWidget):
    dropped = pyqtSignal(object, int)  # new paths, insert row
    reordered = pyqtSignal(object, int)  # moved paths, target row
    remove_requested = pyqtSignal()
    play_requested = pyqtSignal(str)
    move_requested = pyqtSignal(int)  # -1 up, +1 down

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.drop_row = -1

    def paths(self):
        return [it.data(Qt.ItemDataRole.UserRole) for it in self.selectedItems()]

    def startDrag(self, actions):
        samples = [self.win.samples[p] for p in self.paths() if p in self.win.samples]
        if samples:
            self.win.start_file_drag(self, samples, convert=True, loop=self.win.kit_loop)

    def row_at(self, pos):
        idx = self.indexAt(pos)
        if not idx.isValid():
            return self.count()
        rect = self.visualRect(idx)
        return idx.row() + (1 if pos.y() > rect.center().y() else 0)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME_PATHS) or e.mimeData().hasUrls():
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        super().dragMoveEvent(e)  # keeps Qt's auto-scroll while dragging; we decide acceptance below
        if e.mimeData().hasFormat(MIME_PATHS) or e.mimeData().hasUrls():
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()
            self.drop_row = self.row_at(e.position().toPoint())
            self.viewport().update()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self.drop_row = -1
        self.viewport().update()

    def dropEvent(self, e):
        row = self.row_at(e.position().toPoint())
        self.drop_row = -1
        self.viewport().update()
        md = e.mimeData()
        if e.source() is self:
            self.reordered.emit(self.paths(), row)
        else:
            if md.hasFormat(MIME_PATHS):
                paths = bytes(md.data(MIME_PATHS)).decode("utf-8").split("\n")
            else:
                paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            self.dropped.emit([p for p in paths if p], row)
        e.setDropAction(Qt.DropAction.CopyAction)
        e.accept()

    def keyPressEvent(self, e):
        k, mods = e.key(), e.modifiers()
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_requested.emit()
            return
        if k == Qt.Key.Key_Space and self.currentItem():
            self.play_requested.emit(self.currentItem().data(Qt.ItemDataRole.UserRole))
            return
        if mods & Qt.KeyboardModifier.ControlModifier and k in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.move_requested.emit(-1 if k == Qt.Key.Key_Up else 1)
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self.viewport())
        if self.count() == 0:
            p.setPen(QColor("#6b7280"))
            p.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, tr("kit_empty"))
        if self.drop_row >= 0:
            if self.count() == 0:
                y = 2
            elif self.drop_row < self.count():
                y = self.visualItemRect(self.item(self.drop_row)).top()
            else:
                y = self.visualItemRect(self.item(self.count() - 1)).bottom()
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawLine(4, y, self.viewport().width() - 4, y)


class Waveform(QWidget):
    """The OLED display: slot box, sample name, waveform and a parameter row, drawn with a dot-matrix font."""
    clicked = pyqtSignal(float)  # position 0..1

    PX = 2  # display pixel size

    def __init__(self):
        super().__init__()
        self.setFixedHeight(158)
        self.setToolTip(tr("wave_tip"))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mins = self.maxs = None
        self.dur = 0.0
        self.pos = -1.0
        self.label = ""
        self.meta = {}
        self._cache = None
        self._wave_rect = None

    def set_audio(self, mono, sr, label="", meta=None):
        self.label = label
        self.meta = meta or {}
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
        self.invalidate()

    def invalidate(self):
        self._cache = None
        self.update()

    def set_pos(self, frac):
        self.pos = frac
        self.update()

    def mousePressEvent(self, e):
        if self.mins is None:
            return
        r = self._wave_rect
        x = e.position().x()
        if r is not None and r.left() <= x <= r.right():
            self.clicked.emit(min(max((x - r.left()) / max(r.width(), 1), 0.0), 0.999))
        else:
            self.clicked.emit(0.0)

    def resizeEvent(self, e):
        self._cache = None
        super().resizeEvent(e)

    def render(self):
        w, h, px = self.width(), self.height(), self.PX
        ink, bg = QColor(theme("ink")), QColor(theme("oled"))
        dim = QColor(ink)
        dim.setAlpha(80)
        pm = QPixmap(w, h)
        pm.fill(QColor("#141416"))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#2c2c31"), 1))
        p.setBrush(QColor("#08080a"))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 8, 8)  # bezel
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        d = QRect(10, 9, w - 20, h - 18)  # glass
        p.fillRect(d, bg)
        x0, y0, x1, y1 = d.left() + 10, d.top() + 8, d.right() - 10, d.bottom() - 7

        if self.mins is None:
            msg = fit_pixel_text(tr("wave_empty"), px, d.width() - 20)
            draw_pixel_text(p, d.center().x() - pixel_text_width(msg, px) // 2, d.center().y() - 7 * px // 2,
                            msg, px, ink)
            self._wave_rect = None
            p.end()
            return pm

        # row 1: inverted slot box, big name, outlined length box
        slot = self.meta.get("slot", "--")
        box_w = pixel_text_width(slot, px) + 4 * px
        p.fillRect(x0, y0, box_w, 11 * px, ink)
        draw_pixel_text(p, x0 + 2 * px, y0 + 2 * px, slot, px, bg)
        length = f"{self.dur:.2f}s"
        lw = pixel_text_width(length, px) + 4 * px
        p.setPen(QPen(ink, px))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(x1 - lw + px // 2, y0 + px // 2, lw - px, 11 * px - px)
        draw_pixel_text(p, x1 - lw + 2 * px, y0 + 2 * px, length, px, ink)
        title_x = x0 + box_w + 4 * px
        title = fit_pixel_text(self.label, 3, x1 - lw - 4 * px - title_x)
        draw_pixel_text(p, title_x, y0 + 1, title, 3, ink)

        # bottom row: parameter tags like the machine page (LABEL value)
        by = y1 - 7 * px
        x = x0
        for k, v in self.meta.get("params", []):
            kw = pixel_text_width(k, px) + 4 * px
            if x + kw + pixel_text_width(v, px) > x1 - 40 * px:
                break
            p.fillRect(x, by - 2 * px, kw, 11 * px, ink)
            draw_pixel_text(p, x + 2 * px, by, k, px, bg)
            x = draw_pixel_text(p, x + kw + 2 * px, by, v, px, ink) + 5 * px
        mode = self.meta.get("mode", "")
        if mode:
            draw_pixel_text(p, x1 - pixel_text_width(mode, px), by, mode, px, ink)

        # waveform, pixel by pixel
        wy0, wy1 = y0 + 11 * px + 5 * px, by - 2 * px - 5 * px
        r = QRect(x0, wy0, x1 - x0, max(wy1 - wy0, 10))
        self._wave_rect = r
        mid = r.top() + r.height() // 2
        half = r.height() // 2
        cols = r.width() // px
        n = self.mins.size
        idx = np.unique((np.arange(cols) * n // max(cols, 1)).clip(0, n - 1))
        lo = np.minimum.reduceat(self.mins, idx)
        hi = np.maximum.reduceat(self.maxs, idx)
        limit = int(cols * TWINSHOT_MAX_SEC / self.dur) if self.dur > TWINSHOT_MAX_SEC else cols + 1
        for i, (a, b) in enumerate(zip(lo.tolist(), hi.tolist())):
            col = i * cols // len(idx)
            top = mid - int(round(max(b, 0.0) * half / px)) * px
            bot = mid + int(round(max(-a, 0.0) * half / px)) * px
            p.fillRect(r.left() + col * px, top, px, max(bot - top, px), ink if col < limit else dim)
        if limit <= cols:  # Twinshot's 5 s cut, as a dotted line
            lx = r.left() + limit * px
            for yy in range(r.top(), r.bottom(), 3 * px):
                p.fillRect(lx, yy, px, px, ink)
            draw_pixel_text(p, lx + 3 * px, r.top(), "5S", px, ink)
        p.end()
        return pm

    def paintEvent(self, e):
        if self._cache is None or self._cache.size() != self.size():
            self._cache = self.render()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        r = self._wave_rect
        if self.pos >= 0 and r is not None:
            px = self.PX
            x = r.left() + int(r.width() * min(self.pos, 1.0)) // px * px
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Difference)
            p.fillRect(x, r.top(), px, r.height(), QColor("#ffffff"))


class KitDragButton(QPushButton):
    """A FUNC-style key you drag (not click) onto Transfer: it carries the whole kit as files."""

    def __init__(self, win):
        super().__init__(caps(tr("kit_drag")))
        self.win = win
        self.setObjectName("func")
        self.setToolTip(tr("kit_drag_tip"))
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        f = self.font()
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
        self.setFont(f)
        self._press = None

    def mousePressEvent(self, e):
        self._press = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None and (e.position().toPoint() - self._press).manhattanLength() >= \
                QApplication.startDragDistance():
            self._press = None
            self.setDown(False)
            self.win.drag_whole_kit(self)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._press is not None:
            self.win.statusBar().showMessage(tr("kit_drag_click"), 4000)
        self._press = None
        super().mouseReleaseEvent(e)


class TrigGrid(QWidget):
    """The 64 Twinshot slots as four pages of 16 trig keys; lit keys show what's in each slot."""
    slot_clicked = pyqtSignal(int)
    GAP = 4

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setMouseTracking(True)
        self.hover = -1
        self.setMinimumWidth(16 * 14 + 15 * self.GAP)

    def key_size(self):
        return max(12.0, min((self.width() - 15 * self.GAP) / 16, 28.0))

    def resizeEvent(self, e):
        s = self.key_size()
        self.setFixedHeight(int(4 * s + 3 * self.GAP) + 2)
        super().resizeEvent(e)

    def rects(self):
        s = self.key_size()
        x0 = (self.width() - (16 * s + 15 * self.GAP)) / 2
        return [QRectF(x0 + (i % 16) * (s + self.GAP), 1 + (i // 16) * (s + self.GAP), s, s) for i in range(64)]

    def index_at(self, pos):
        for i, r in enumerate(self.rects()):
            if r.contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, e):
        i = self.index_at(e.position())
        if i != self.hover:
            self.hover = i
            self.update()

    def leaveEvent(self, e):
        self.hover = -1
        self.update()

    def mousePressEvent(self, e):
        i = self.index_at(e.position())
        if 0 <= i < len(self.win.kit):
            self.slot_clicked.emit(i)

    def event(self, e):
        if e.type() == QEvent.Type.ToolTip:
            i = self.index_at(QPointF(e.pos()))
            if 0 <= i < len(self.win.kit):
                QToolTip.showText(e.globalPos(), f"{i + 1:02d}  {os.path.basename(self.win.kit[i])}", self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        kit, win = self.win.kit, self.win
        selected = set(win.kit_list.paths()) if hasattr(win, "kit_list") else set()
        s = self.key_size()
        f = QFont(self.font())
        f.setPixelSize(max(8, int(s * 0.42)))
        f.setBold(True)
        p.setFont(f)
        for i, r in enumerate(self.rects()):
            filled = i < len(kit)
            path = kit[i] if filled else None
            sample = win.samples.get(path) if filled else None
            p.setPen(QPen(QColor("#3a3a3f" if i != self.hover else "#5c5c64"), 1))
            p.setBrush(QColor("#242427" if filled else "#1b1b1e"))
            p.drawRoundedRect(r, 3, 3)
            if filled:
                c = led_color(sample.category) if sample else QColor(LED["dim"])
                glow = QColor(c)
                glow.setAlpha(55)
                p.setPen(QPen(glow, 3))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r.adjusted(2.5, 2.5, -2.5, -2.5), 2, 2)
                p.setPen(QPen(c, 1))
                p.drawRoundedRect(r.adjusted(3.5, 3.5, -3.5, -3.5), 2, 2)
                p.setPen(c)
                if path in win.kit_loop:
                    p.setBrush(c)
                    p.drawEllipse(QPointF(r.right() - 4, r.top() + 4), 1.6, 1.6)
            else:
                p.setPen(QColor("#4a4a50"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(i + 1))
            if path in selected:
                p.setPen(QPen(QColor("#ffffff"), 1.6))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r.adjusted(0.8, 0.8, -0.8, -0.8), 3, 3)


# ---------------------------------------------------------------- main window

_windows = []


def open_main_window():
    w = MainWindow()
    w.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    w.show()
    _windows.append(w)
    return w


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr("window_title", version=__version__))
        self.resize(1440, 840)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings(str(DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)
        self.samples = {}  # path -> Sample
        self.roots = []
        self.kit = []  # list of paths, in slot order
        self.kit_loop = set()  # kit paths exported as seamless loops (for Twinshot slot 2)
        self.kit_name = ""  # name of the saved kit this one came from, if any
        self.audio_cache = OrderedDict()
        self.current = None
        self.play_start = 0.0
        self.play_len = 0.0
        self.last_play = ("", 0.0)
        self.folder_filter = (None, None)  # (root, top)
        self.similar_to = None
        self._restoring = False
        self._kit_missing = frozenset()

        self.scanner = Scanner(self)
        self.scanner.progress.connect(self.on_scan_progress)
        self.scanner.batch.connect(self.on_scan_batch)
        self.scanner.removed.connect(self.on_scan_removed)
        self.scanner.root_done.connect(self.on_root_done)
        self.scanner.finished.connect(self.on_scanner_finished)

        self.filter_timer = QTimer(self, singleShot=True, interval=150, timeout=self.apply_filter)
        self.refresh_timer = QTimer(self, singleShot=True, interval=1500, timeout=self.refresh_after_scan)
        self.play_timer = QTimer(self, interval=33, timeout=self.tick_playhead)

        self.build_ui()
        self.restore_layout()
        self.load_library()
        self.load_kit()

    # ---------- UI
    def build_ui(self):
        top = QHBoxLayout()
        self.search = QLineEdit(placeholderText=tr("search_placeholder"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda: self.filter_timer.start())
        self.type_combo = QComboBox()
        self.type_combo.addItems([tr("type_all"), tr("type_oneshot"), tr("type_loop")])
        self.type_combo.currentIndexChanged.connect(self.apply_filter)
        self.only5 = QCheckBox(tr("only5"))
        self.only5.toggled.connect(self.apply_filter)
        self.syntakt_mode = QCheckBox(tr("syntakt_mode"))
        self.syntakt_mode.setToolTip(tr("syntakt_mode_tip"))
        self.syntakt_mode.setChecked(self.settings.value("syntakt_mode", True, type=bool))
        self.syntakt_mode.toggled.connect(self.on_syntakt_mode)
        self.autoplay = QCheckBox(tr("autoplay"))
        self.autoplay.setChecked(self.settings.value("autoplay", True, type=bool))
        self.autoplay.toggled.connect(lambda v: self.settings.setValue("autoplay", v))
        self.volume = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=100, maximumWidth=110)
        self.volume.setValue(self.settings.value("volume", 80, type=int))
        self.volume.valueChanged.connect(lambda v: self.settings.setValue("volume", v))
        self.device_combo = QComboBox(minimumWidth=150, maximumWidth=240)
        self.fill_devices()
        self.lang_combo = QComboBox()
        for code, name in LANGUAGES.items():
            self.lang_combo.addItem(name, code)
        self.lang_combo.setCurrentIndex(list(LANGUAGES).index(LANG))
        self.lang_combo.currentIndexChanged.connect(self.on_language)
        self.display_combo = QComboBox()
        for code in THEMES:
            self.display_combo.addItem(tr("display_" + code), code)
        self.display_combo.setCurrentIndex(list(THEMES).index(THEME))
        self.display_combo.currentIndexChanged.connect(self.on_display)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(QLabel("Sample Browser", objectName="wordmark"))
        caption = QLabel(caps(tr("app_caption")), objectName="caption")
        f = caption.font()
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        caption.setFont(f)
        brand.addWidget(caption)
        top.addLayout(brand)
        top.addSpacing(16)
        top.addWidget(self.search, 1)
        for wdg in (self.type_combo, self.only5, self.syntakt_mode, self.autoplay, QLabel(tr("volume")),
                    self.volume, QLabel(tr("output")), self.device_combo, self.display_combo, self.lang_combo):
            top.addWidget(wdg)

        # left: folders + categories + character
        left = QVBoxLayout()
        add_btn = key_button(tr("add_folder"), func=True)
        add_btn.clicked.connect(self.add_folder)
        left.addWidget(add_btn)
        left.addWidget(section_label(tr("folders")))
        self.folder_tree = QTreeWidget(objectName="oled")
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.itemSelectionChanged.connect(self.on_folder_selected)
        self.folder_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.folder_tree.customContextMenuRequested.connect(self.folder_menu)
        left.addWidget(self.folder_tree, 3)
        left.addWidget(section_label(tr("categories")))
        self.cat_list = QListWidget(objectName="oled")
        self.cat_list.itemSelectionChanged.connect(self.apply_filter)
        left.addWidget(self.cat_list, 4)
        left.addWidget(section_label(tr("character")))
        self.tag_buttons = {}
        for row_tags in TAG_ROWS:
            row = QHBoxLayout()
            for t in row_tags:
                b = key_button(tr("tag_" + t), checkable=True)
                b.setToolTip(tr("tip_" + t))
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
        for i, wdt in enumerate((36, 225, 95, 170, 60, 44, 40)):
            self.table.setColumnWidth(i, wdt)
        hh.setSortIndicator(1, Qt.SortOrder.AscendingOrder)
        self.table.selectionModel().currentRowChanged.connect(self.on_current_changed)
        self.table.clicked.connect(lambda idx: self.play_sample(self.model.rows[idx.row()], force=False))
        self.table.doubleClicked.connect(lambda idx: self.toggle_kit([self.model.rows[idx.row()]]))
        self.table.toggle_requested.connect(self.toggle_play)
        self.table.kit_toggle_requested.connect(lambda: self.toggle_kit(self.selected_samples()))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.table_menu)

        self.empty_label = QLabel(tr("empty_state"), objectName="empty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty_label)
        self.stack.addWidget(self.table)

        self.similar_bar = QWidget()
        sb = QHBoxLayout(self.similar_bar)
        sb.setContentsMargins(0, 0, 0, 0)
        self.similar_label = QLabel()
        clear_sim = key_button(tr("close_similar"))
        clear_sim.clicked.connect(self.clear_similar)
        sb.addWidget(self.similar_label, 1)
        sb.addWidget(clear_sim)
        self.similar_bar.hide()

        self.wave = Waveform()
        self.wave.clicked.connect(lambda f: self.current and self.play_sample(self.current, force=True, start=f))
        center = QVBoxLayout()
        center.addWidget(self.similar_bar)
        center.addWidget(self.stack, 1)
        center.addWidget(self.wave)
        center_w = QWidget()
        center_w.setLayout(center)

        # right: kit
        right = QVBoxLayout()
        self.kit_label = QLabel()
        right.addWidget(self.kit_label)
        self.trig = TrigGrid(self)
        self.trig.slot_clicked.connect(self.on_trig_clicked)
        right.addWidget(self.trig)
        hint = QLabel(tr("kit_hint"), objectName="hint")
        hint.setWordWrap(True)
        right.addWidget(hint)
        r0 = QHBoxLayout()
        self.kit_combo = QComboBox()
        self.kit_combo.activated.connect(self.on_kit_combo)
        save_as = key_button(tr("kit_save_as"))
        save_as.clicked.connect(self.save_kit_as)
        self.kit_more = QToolButton(text="•••")
        self.kit_more.setToolTip(tr("kit_more"))
        self.kit_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.kit_more_menu = QMenu(self)
        self.kit_more_menu.aboutToShow.connect(self.fill_kit_more_menu)
        self.kit_more.setMenu(self.kit_more_menu)
        r0.addWidget(self.kit_combo, 1)
        r0.addWidget(save_as)
        r0.addWidget(self.kit_more)
        right.addLayout(r0)
        self.kit_list = KitList(self)
        self.kit_list.setObjectName("kit")
        self.kit_list.itemSelectionChanged.connect(self.trig.update)
        self.kit_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.kit_list.customContextMenuRequested.connect(self.kit_menu)
        self.kit_list.dropped.connect(self.insert_into_kit)
        self.kit_list.reordered.connect(self.move_in_kit)
        self.kit_list.remove_requested.connect(self.remove_from_kit)
        self.kit_list.play_requested.connect(self.play_path)
        self.kit_list.move_requested.connect(self.nudge_kit)
        self.kit_list.itemClicked.connect(lambda it: self.play_path(it.data(Qt.ItemDataRole.UserRole), force=False))
        self.kit_list.currentItemChanged.connect(self.on_kit_current)
        self.kit_list.itemSelectionChanged.connect(self.update_kit_buttons)
        right.addWidget(self.kit_list, 1)
        r1 = QHBoxLayout()
        self.kit_remove_btn = key_button(tr("kit_remove"))
        self.kit_remove_btn.clicked.connect(self.remove_from_kit)
        self.kit_clear_btn = key_button(tr("kit_clear"))
        self.kit_clear_btn.clicked.connect(self.clear_kit)
        r1.addWidget(self.kit_remove_btn)
        r1.addWidget(self.kit_clear_btn)
        right.addLayout(r1)
        r2 = QHBoxLayout()
        self.export_btn = key_button(tr("kit_export_folder"))
        self.export_btn.clicked.connect(self.export_kit)
        self.zip_btn = key_button(tr("kit_export_zip"))
        self.zip_btn.clicked.connect(self.export_zip)
        r2.addWidget(self.export_btn)
        r2.addWidget(self.zip_btn)
        right.addLayout(r2)
        self.drag_btn = KitDragButton(self)
        right.addWidget(self.drag_btn)
        self.slot_warn = QLabel(objectName="warn")
        self.slot_warn.setWordWrap(True)
        right.addWidget(self.slot_warn)
        right_w = QWidget()
        right_w.setLayout(right)

        self.split = QSplitter()
        self.split.addWidget(left_w)
        self.split.addWidget(center_w)
        self.split.addWidget(right_w)
        self.split.setSizes([250, 820, 380])
        self.split.setStretchFactor(1, 1)

        root = QVBoxLayout()
        root.addLayout(top)
        root.addWidget(self.split, 1)
        cw = QWidget()
        cw.setLayout(root)
        self.setCentralWidget(cw)

        hint_label = QLabel(tr("shortcuts"))
        hint_label.setStyleSheet("color:#6b7280; padding-right:8px;")
        self.statusBar().addPermanentWidget(hint_label)
        self.progress = QProgressBar(maximumWidth=260, visible=False)
        self.statusBar().addPermanentWidget(self.progress)

        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: (self.search.setFocus(), self.search.selectAll()))
        QShortcut(QKeySequence("Esc"), self.search, activated=self.search.clear,
                  context=Qt.ShortcutContext.WidgetShortcut)

    def fill_devices(self):
        self.device_combo.addItem(tr("default_output"), None)
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

    def restore_layout(self):
        for key, fn in (("geometry", self.restoreGeometry), ("splitter", self.split.restoreState),
                        ("header_v2", self.table.horizontalHeader().restoreState)):
            v = self.settings.value(key)
            if v is not None:
                try:
                    fn(v)
                except Exception:
                    pass

    def save_layout(self):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.split.saveState())
        self.settings.setValue("header_v2", self.table.horizontalHeader().saveState())
        self.settings.sync()

    def on_syntakt_mode(self, v):
        self.settings.setValue("syntakt_mode", v)
        self.audio_cache.clear()
        if self.current:
            self.show_wave(self.current)

    def on_language(self, _):
        global LANG
        code = self.lang_combo.currentData()
        if code == LANG:
            return
        LANG = code
        self.settings.setValue("language", code)
        self.save_kit(KIT_FILE)
        self.save_layout()
        # rebuild the window in the new language; library and kit reload from the cache
        QTimer.singleShot(0, lambda: (open_main_window(), self.close()))

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
        d = QFileDialog.getExistingDirectory(self, tr("pick_folder"), start)
        if not d:
            return
        self.settings.setValue("last_dir", os.path.dirname(os.path.normpath(d)))
        self.add_root(d)

    def add_root(self, d):
        d = os.path.normpath(d)
        for r in self.roots:
            if is_inside(d, r):
                QMessageBox.information(self, tr("already_added_title"), tr("already_added", root=r))
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
        if QMessageBox.question(self, tr("remove_folder_title"), tr("remove_folder", root=root)
                                ) != QMessageBox.StandardButton.Yes:
            return
        self.stop_scanner()
        self.forget_root(root)
        self.folder_filter = (None, None)
        self.rebuild_sidebar()
        self.apply_filter()
        self.refresh_kit()
        self.resume_scanner()

    def on_scan_progress(self, root, done, total):
        name = os.path.basename(root) or root
        if total < 0:
            self.progress.setRange(0, 0)
            self.statusBar().showMessage(tr("status_finding", name=name, n=-total))
        else:
            self.progress.setRange(0, max(total, 1))
            self.progress.setValue(done)
            self.statusBar().showMessage(tr("status_analyzing", name=name, done=done, total=total))

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
        self.statusBar().showMessage(tr("status_scan_done", name=os.path.basename(root), n=n), 8000)
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
        # only rebuild the kit list when a kit sample appeared/disappeared, so the selection survives scans
        if frozenset(p for p in self.kit if p not in self.samples) != self._kit_missing:
            self.refresh_kit(select=self.kit_list.paths())
        else:
            self.model.set_kit(self.kit)

    # ---------- sidebar
    def rebuild_sidebar(self):
        sel = self.folder_filter
        self.folder_tree.blockSignals(True)
        self.folder_tree.clear()
        counts = Counter((s.root, s.top) for s in self.samples.values())
        root_counts = Counter(s.root for s in self.samples.values())
        all_item = QTreeWidgetItem([tr("all_count", n=len(self.samples))])
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
        items = [(tr("all_count", n=len(base)), None)] + [
            (f"{cat_name(c)}  ({counts[c]})", c) for c in CATEGORY_ORDER if counts.get(c)]
        for label, key in items:
            it = QListWidgetItem(led_icon(key), label) if key else QListWidgetItem(label)
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
        if not self.scanner.isRunning():
            self.statusBar().showMessage(tr("status_showing", shown=len(rows), total=len(self.samples)))

    def show_similar(self, s):
        self.similar_to = s
        self.similar_label.setText(tr("similar_banner", name=s.name))
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

    def on_kit_current(self, cur, prev):
        if cur is not None and self.autoplay.isChecked() and self.kit_list.hasFocus():
            self.play_path(cur.data(Qt.ItemDataRole.UserRole), force=False)

    def load_audio(self, s, loop=False):
        mode = self.syntakt_mode.isChecked()
        key = (s.path, mode, loop)
        if key in self.audio_cache:
            self.audio_cache.move_to_end(key)
            return self.audio_cache[key]
        if loop:  # audition the loop a few times round so the seam is audible
            mono = np.tile(loop_ready(s.path), 3)
            data, sr = mono[:, None], TWINSHOT_SR
        elif mode:
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

    def wave_label(self, s, loop=False):
        return s.stem + (" (loop)" if loop else "")

    def wave_meta(self, s, loop=False):
        """What the OLED shows around the waveform (machine-page style abbreviations)."""
        slot = self.model.kit_index.get(s.path)
        params = [("CAT", cat_name(s.category)), ("KEY", s.key or "-")]
        if s.is_loop and s.bpm:
            params.append(("BPM", str(s.bpm)))
        params.append(("SR", f"{s.sr / 1000:g}K" if s.sr else "-"))
        if loop:
            mode = "LOOP X3"
        elif self.syntakt_mode.isChecked():
            mode = "SYNTAKT 48K MONO"
        else:
            mode = "ORIGINAL"
        return {"slot": f"{slot:02d}" if slot else "--", "params": params, "mode": mode}

    def show_wave(self, s):
        self.current = s
        try:
            _, sr, mono = self.load_audio(s)
            self.wave.set_audio(mono, sr, self.wave_label(s), self.wave_meta(s))
        except Exception as e:
            self.wave.set_audio(None, 1, s.name)
            self.statusBar().showMessage(tr("err_read", name=s.name, error=e), 6000)

    def play_sample(self, s, force=True, start=0.0, loop=False):
        now = time.perf_counter()
        if not force and self.last_play[0] == s.path and now - self.last_play[1] < 0.25:
            return
        self.last_play = (s.path, now)
        self.current = s
        try:
            data, sr, mono = self.load_audio(s, loop)
        except Exception as e:
            self.statusBar().showMessage(tr("err_read", name=s.name, error=e), 6000)
            return
        if self.wave.label != self.wave_label(s, loop) or start == 0.0:
            self.wave.set_audio(mono, sr, self.wave_label(s, loop), self.wave_meta(s, loop))
        offset = int(start * data.shape[0])
        out = data[offset:, :2] if data.shape[1] >= 2 else np.repeat(data[offset:], 2, axis=1)
        out = out * (self.volume.value() / 100.0)
        try:
            sd.stop()
            sd.play(out, sr, device=self.device_combo.currentData())
        except Exception as e:
            self.statusBar().showMessage(tr("err_play", error=e), 6000)
            return
        total = data.shape[0] / sr
        self.play_start = time.perf_counter() - start * total
        self.play_len = total
        self.play_timer.start()

    def play_path(self, path, force=True):
        """Play a kit entry (as a loop if it's marked as one)."""
        s = self.samples.get(path)
        if s:
            self.play_sample(s, force=force, loop=path in self.kit_loop)

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
    def start_file_drag(self, source, samples, convert, loop=frozenset()):
        paths = []
        if convert:
            QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
            try:
                for s in samples:
                    try:
                        paths.append(str(syntakt_file(s, loop=s.path in loop)))
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
        drag.setPixmap(drag_badge(samples[0].name if len(samples) == 1 else tr("drag_count", n=len(samples))))
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.CopyAction)

    # ---------- context menus
    def table_menu(self, pos):
        samples = self.selected_samples()
        if not samples:
            return
        m = QMenu(self)
        in_kit = [s for s in samples if s.path in self.model.kit_index]
        if len(in_kit) < len(samples):
            m.addAction(tr("menu_add_to_kit"), lambda: self.add_to_kit([s.path for s in samples]))
        if in_kit:
            m.addAction(tr("menu_remove_from_kit"), lambda: self.remove_paths([s.path for s in in_kit]))
        m.addAction(tr("menu_find_similar"), lambda: self.show_similar(samples[0]))
        m.addSeparator()
        m.addAction(tr("menu_reveal", fm=FILE_MANAGER), lambda: reveal(samples[0].path))
        m.addAction(tr("menu_reveal_converted", fm=FILE_MANAGER), lambda: reveal(str(syntakt_file(samples[0]))))
        m.addAction(tr("menu_copy_path"), lambda: QApplication.clipboard().setText(
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
            m.addAction(tr("menu_rescan"), lambda: self.scan(root))
            m.addAction(tr("menu_remove_folder"), lambda: self.remove_root(root))
        m.addAction(tr("menu_open_folder", fm=FILE_MANAGER), lambda: open_folder(os.path.join(root, top or "")))
        m.exec(self.folder_tree.viewport().mapToGlobal(pos))

    # ---------- kit
    def kit_changed(self, select=None, message=None):
        self.refresh_kit(select)
        self.save_kit(KIT_FILE)
        if message:
            self.statusBar().showMessage(message, 3000)

    def insert_into_kit(self, paths, row=None):
        new = []
        for p in paths:
            if p in self.samples and p not in self.kit and p not in new:
                new.append(p)
        if not new:
            return
        row = len(self.kit) if row is None else max(0, min(row, len(self.kit)))
        self.kit[row:row] = new
        self.kit_changed(select=new, message=tr("kit_added", n=len(new)))

    def add_to_kit(self, paths):
        self.insert_into_kit(paths)

    def remove_paths(self, paths):
        rm = set(paths)
        n = sum(1 for p in self.kit if p in rm)
        self.kit = [p for p in self.kit if p not in rm]
        self.kit_changed(message=tr("kit_removed", n=n))

    def toggle_kit(self, samples):
        """Enter / double-click: add the selection, or remove it if it's all in the kit already."""
        paths = [s.path for s in samples]
        if paths and all(p in self.model.kit_index for p in paths):
            self.remove_paths(paths)
        else:
            self.add_to_kit(paths)

    def move_in_kit(self, paths, row):
        moving = [p for p in self.kit if p in set(paths)]
        if not moving:
            return
        before = sum(1 for p in self.kit[:row] if p in set(paths))
        rest = [p for p in self.kit if p not in set(paths)]
        row = max(0, min(row - before, len(rest)))
        rest[row:row] = moving
        if rest != self.kit:
            self.kit = rest
            self.kit_changed(select=moving)

    def nudge_kit(self, step):
        rows = sorted(self.kit_list.row(it) for it in self.kit_list.selectedItems())
        if not rows:
            return
        paths = [self.kit[r] for r in rows]
        target = rows[0] - 1 if step < 0 else rows[-1] + 2
        if 0 <= target <= len(self.kit):
            self.move_in_kit(paths, target)

    def remove_from_kit(self):
        self.remove_paths(self.kit_list.paths())

    def clear_kit(self):
        if self.kit and QMessageBox.question(self, tr("clear_kit_title"), tr("clear_kit")
                                             ) == QMessageBox.StandardButton.Yes:
            self.kit = []
            self.kit_loop = set()
            self.kit_changed()

    def set_loop(self, paths, on):
        for p in paths:
            (self.kit_loop.add if on else self.kit_loop.discard)(p)
        self.kit_changed(select=paths)

    def kit_menu(self, pos):
        paths = self.kit_list.paths()
        if not paths:
            return
        m = QMenu(self)
        m.addAction(tr("menu_play"), lambda: self.play_path(paths[0]))
        looped = all(p in self.kit_loop for p in paths)
        act = m.addAction(tr("menu_loop"), lambda: self.set_loop(paths, not looped))
        act.setCheckable(True)
        act.setChecked(looped)
        act.setToolTip(tr("loop_tip"))
        m.setToolTipsVisible(True)
        m.addSeparator()
        m.addAction(tr("menu_remove_from_kit"), lambda: self.remove_paths(paths))
        if paths[0] in self.samples:
            m.addAction(tr("menu_reveal", fm=FILE_MANAGER), lambda: reveal(paths[0]))
        m.exec(self.kit_list.viewport().mapToGlobal(pos))

    def kit_bytes(self):
        return sum(int(min(self.samples[p].dur, TWINSHOT_MAX_SEC) * TWINSHOT_SR * 2) + 44
                   for p in self.kit if p in self.samples)

    def refresh_kit(self, select=None):
        select = set(select or [])
        self.kit_loop &= set(self.kit)
        self.kit_list.blockSignals(True)
        self.kit_list.clear()
        for i, p in enumerate(self.kit, 1):
            s = self.samples.get(p)
            loop = p in self.kit_loop
            if s:
                label = f"{i:02d}   {s.name}   ·   {cat_name(s.category)}   ·   {secs(min(s.dur, TWINSHOT_MAX_SEC))}"
            else:
                label = f"{i:02d}   {os.path.basename(p)}"
            if loop:
                label += f"   ·   ↻ {tr('loop_marker')}"
            it = QListWidgetItem(led_icon(s.category if s else "other"), label)
            it.setData(Qt.ItemDataRole.UserRole, p)
            it.setToolTip(p + ("\n\n" + tr("loop_tip") if loop else ""))
            if not s:
                it.setForeground(QColor("#6b7280"))
            elif loop:
                it.setForeground(accent_color())
            elif s.dur > TWINSHOT_MAX_SEC:
                it.setForeground(QColor("#d9a441"))
            self.kit_list.addItem(it)
            if p in select:
                it.setSelected(True)
                self.kit_list.scrollToItem(it)
        self.kit_list.blockSignals(False)
        self._kit_missing = frozenset(p for p in self.kit if p not in self.samples)
        self.model.set_kit(self.kit)
        size = self.kit_bytes()
        over = len(self.kit) > TWINSHOT_SLOTS or size > TWINSHOT_MEM
        self.kit_label.setText(caps(tr("kit_label", n=len(self.kit), slots=TWINSHOT_SLOTS,
                                       mb=f"{size / 1048576:.1f}")))
        self.kit_label.setStyleSheet(f"font-weight:700; font-size:11px; letter-spacing:1px; "
                                     f"color:{theme('accent') if over else '#d4d4d8'};")
        self.kit_label.setToolTip(tr("kit_over") if over else "")
        self.slot_warn.setText("⚠  " + tr("slots_warning", n=len(self.kit)))
        self.slot_warn.setVisible(bool(self.kit))
        self.drag_btn.setToolTip(tr("kit_drag_tip") + "\n\n" + tr("slots_warning", n=len(self.kit)))
        self.update_kit_buttons()
        self.refresh_kit_combo()
        self.trig.update()

    def on_trig_clicked(self, i):
        self.kit_list.clearSelection()
        it = self.kit_list.item(i)
        if it:
            it.setSelected(True)
            self.kit_list.setCurrentItem(it)
            self.kit_list.scrollToItem(it)
            self.play_path(self.kit[i], force=True)

    def on_display(self, _):
        global THEME
        THEME = self.display_combo.currentData()
        self.settings.setValue("display", THEME)
        QApplication.instance().setStyleSheet(build_stylesheet())
        self.wave.invalidate()
        self.refresh_kit(select=self.kit_list.paths())
        self.model.set_kit(self.kit)

    def update_kit_buttons(self):
        self.kit_remove_btn.setEnabled(bool(self.kit_list.selectedItems()))
        for b in (self.kit_clear_btn, self.export_btn, self.zip_btn, self.drag_btn):
            b.setEnabled(bool(self.kit))

    # ---------- kit files
    def kit_data(self):
        return {"name": self.kit_name, "samples": self.kit, "loop": [p for p in self.kit if p in self.kit_loop]}

    def save_kit(self, path):
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(self.kit_data(), ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log(f"save kit failed: {e}")

    def read_kit_file(self, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        kit = list(dict.fromkeys(data.get("samples", [])))
        return kit, set(data.get("loop", [])) & set(kit), data.get("name", "")

    def load_kit(self, path=KIT_FILE):
        try:
            self.kit, self.kit_loop, self.kit_name = self.read_kit_file(path)
        except Exception:
            self.kit, self.kit_loop, self.kit_name = [], set(), ""
        self.refresh_kit()

    def saved_kits(self):
        return sorted(KITS_DIR.glob("*.json"), key=lambda p: natural_key(p.stem))

    def kit_is_saved(self):
        """True if the working kit is empty or identical to the saved kit it was loaded from."""
        if not self.kit:
            return True
        f = KITS_DIR / f"{self.kit_name}.json"
        if not self.kit_name or not f.exists():
            return False
        try:
            kit, loop, _ = self.read_kit_file(f)
            return kit == self.kit and loop == self.kit_loop
        except Exception:
            return False

    def confirm_replace_kit(self):
        if self.kit_is_saved():
            return True
        return QMessageBox.question(self, tr("kit_replace_title"), tr("kit_replace", n=len(self.kit))
                                    ) == QMessageBox.StandardButton.Yes

    def refresh_kit_combo(self):
        self.kit_combo.blockSignals(True)
        self.kit_combo.clear()
        self.kit_combo.addItem(tr("kit_saved_kits"), None)
        for f in self.saved_kits():
            self.kit_combo.addItem(f.stem, str(f))
        idx = self.kit_combo.findText(self.kit_name) if self.kit_name else -1
        self.kit_combo.setCurrentIndex(max(idx, 0))
        self.kit_combo.blockSignals(False)

    def on_kit_combo(self, index):
        path = self.kit_combo.itemData(index)
        if not path or Path(path).stem == self.kit_name and self.kit_is_saved():
            return
        if not self.confirm_replace_kit():
            self.refresh_kit_combo()
            return
        self.load_kit(path)
        self.kit_name = Path(path).stem
        self.kit_changed(message=tr("kit_loaded", name=self.kit_name, n=len(self.kit)))

    def save_kit_as(self):
        name, ok = QInputDialog.getText(self, tr("save_kit_title"), tr("kit_name_prompt"), text=self.kit_name)
        name = re.sub(r'[\\/:*?"<>|]+', "_", name or "").strip(" .")
        if not ok or not name:
            return
        f = KITS_DIR / f"{name}.json"
        if f.exists() and name != self.kit_name and QMessageBox.question(
                self, tr("save_kit_title"), tr("kit_overwrite", name=name)) != QMessageBox.StandardButton.Yes:
            return
        self.kit_name = name
        self.save_kit(f)
        self.kit_changed(message=tr("kit_saved", name=name))

    def fill_kit_more_menu(self):
        m = self.kit_more_menu
        m.clear()
        m.addAction(tr("kit_load_folder"), self.load_kit_from_folder)
        m.addAction(tr("kit_import"), self.import_kit)
        m.addSeparator()
        m.addAction(tr("kit_show_folder"), lambda: (KITS_DIR.mkdir(parents=True, exist_ok=True),
                                                    open_folder(str(KITS_DIR))))
        if self.kit_name and (KITS_DIR / f"{self.kit_name}.json").exists():
            m.addAction(tr("kit_delete_saved", name=self.kit_name), self.delete_saved_kit)

    def import_kit(self):
        f, _ = QFileDialog.getOpenFileName(self, tr("open_kit_title"), str(KITS_DIR), tr("kit_file_filter"))
        if not f or not self.confirm_replace_kit():
            return
        self.load_kit(f)
        self.kit_name = self.kit_name or Path(f).stem
        self.kit_changed(message=tr("kit_loaded", name=self.kit_name, n=len(self.kit)))

    def delete_saved_kit(self):
        f = KITS_DIR / f"{self.kit_name}.json"
        if f.exists() and QMessageBox.question(self, tr("clear_kit_title"), tr("kit_delete_confirm", name=self.kit_name)
                                               ) == QMessageBox.StandardButton.Yes:
            f.unlink()
            self.kit_name = ""
            self.kit_changed()

    def load_kit_from_folder(self, d=None):
        """Turn a folder of samples (e.g. a Transfer backup of the Syntakt's samples) into the kit, in file order."""
        if not d:
            d = QFileDialog.getExistingDirectory(self, tr("kit_load_folder"),
                                                 self.settings.value("kit_folder_dir", str(Path.home()), type=str))
            if not d:
                return
            self.settings.setValue("kit_folder_dir", os.path.dirname(os.path.normpath(d)))
        d = os.path.normpath(d)
        files = []
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if not x.startswith(".") and x != "__MACOSX"]
            files += [os.path.join(dirpath, f) for f in filenames
                      if not f.startswith("._") and os.path.splitext(f)[1].lower() in AUDIO_EXT]
        files.sort(key=lambda p: natural_key(os.path.relpath(p, d)))
        if not files:
            QMessageBox.information(self, tr("kit_load_folder"), tr("kit_folder_empty"))
            return
        if not self.confirm_replace_kit():
            return
        if len(files) > TWINSHOT_SLOTS:
            QMessageBox.information(self, tr("kit_load_folder"), tr("kit_folder_too_many", n=len(files)))
            files = files[:TWINSHOT_SLOTS]
        # the files have to be in the library to be shown/played: scan the folder (or rescan its library root)
        root = next((r for r in self.roots if is_inside(d, r)), None)
        if root is None:
            self.add_root(d)
        elif any(f not in self.samples for f in files):
            self.scan(root)
        self.kit = files
        self.kit_loop = set()
        self.kit_name = ""
        self.kit_changed(message=tr("kit_from_folder", n=len(files), name=os.path.basename(d)))

    # ---------- export
    def render_kit(self, write):
        """Convert every kit entry and hand (file name, audio) to `write`; returns the failures."""
        errors = []
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            for i, p in enumerate(self.kit, 1):
                s = self.samples.get(p)
                if not s:
                    errors.append(p)
                    continue
                loop = p in self.kit_loop
                try:
                    write(kit_file_name(i, s, loop), kit_audio(p, loop))
                except Exception as e:
                    errors.append(f"{p}: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        return errors

    def export_kit(self):
        if not self.kit:
            return
        d = QFileDialog.getExistingDirectory(self, tr("export_pick"),
                                             self.settings.value("export_dir", str(Path.home()), type=str))
        if not d:
            return
        self.settings.setValue("export_dir", d)
        errors = self.render_kit(lambda name, y: write_syntakt(y, Path(d) / name))
        msg = tr("export_done", n=len(self.kit) - len(errors), folder=d) + "\n\n⚠  " + \
            tr("slots_warning", n=len(self.kit) - len(errors))
        if errors:
            msg += "\n\n" + tr("export_failed") + "\n" + "\n".join(errors[:10])
        QMessageBox.information(self, tr("export_done_title"), msg)
        open_folder(d)

    def prepare_kit_files(self):
        """Numbered Syntakt-format copies of the kit (01_..., 02_...) in a fresh folder; returns their paths."""
        folder = KIT_DRAG_DIR / safe_name(self.kit_name or "Twinshot kit")
        if KIT_DRAG_DIR.exists():
            shutil.rmtree(KIT_DRAG_DIR, ignore_errors=True)  # only our own generated copies live here
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            for i, p in enumerate(self.kit, 1):
                s = self.samples.get(p)
                if not s:
                    continue
                loop = p in self.kit_loop
                try:
                    dst = folder / kit_file_name(i, s, loop)
                    shutil.copyfile(syntakt_file(s, loop), dst)  # converted copies are cached, so this is quick
                    paths.append(dst)
                except Exception as e:
                    log(f"kit drag: {p}: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        return paths

    def drag_whole_kit(self, source):
        paths = self.prepare_kit_files()
        if not paths:
            return
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        drag = QDrag(source)
        drag.setMimeData(md)
        drag.setPixmap(drag_badge(tr("drag_count", n=len(paths))))
        drag.exec(Qt.DropAction.CopyAction)

    def export_zip(self):
        if not self.kit:
            return
        start_dir = self.settings.value("export_dir", str(Path.home()), type=str)
        base = safe_name(self.kit_name or "Twinshot kit")
        f, _ = QFileDialog.getSaveFileName(self, tr("zip_save_title"), os.path.join(start_dir, base + ".zip"),
                                           "Zip (*.zip)")
        if not f:
            return
        self.settings.setValue("export_dir", os.path.dirname(f))
        folder = safe_name(Path(f).stem)  # Transfer recreates this folder on the Syntakt
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            def write(name, y):
                buf = io.BytesIO()
                write_syntakt(y, buf)
                z.writestr(f"{folder}/{name}", buf.getvalue())
            errors = self.render_kit(write)
        msg = tr("zip_done", n=len(self.kit) - len(errors), file=f)
        if errors:
            msg += "\n\n" + tr("export_failed") + "\n" + "\n".join(errors[:10])
        QMessageBox.information(self, tr("export_done_title"), msg)
        reveal(f)

    def closeEvent(self, e):
        self.save_layout()
        self.stop_scanner()
        sd.stop()
        if self in _windows:
            _windows.remove(self)
        super().closeEvent(e)


# ---------------------------------------------------------------- helpers

def drag_badge(text):
    f = QFont()
    f.setPointSize(10)
    w = min(QFontMetrics(f).horizontalAdvance(text) + 20, 420)
    pm = QPixmap(w, 26)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(47, 125, 109, 230))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, w, 26, 6, 6)
    p.setPen(QColor("#ffffff"))
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter,
               QFontMetrics(f).elidedText(text, Qt.TextElideMode.ElideMiddle, w - 16))
    p.end()
    return pm


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
    """Fusion + a matte-black palette (for dialogs and native bits) + the panel stylesheet."""
    app.setStyle("Fusion")
    p = QPalette()
    text = QColor("#e8e8ea")
    p.setColor(QPalette.ColorRole.Window, QColor("#141416"))
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, QColor("#0e0e10"))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor("#131316"))
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor("#2a2a2e"))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor("#34353b"))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor("#000000"))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor("#6a6a72"))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#4f4f56"))
    app.setPalette(p)
    app.setStyleSheet(build_stylesheet())


def load_language():
    """Language and display colour, read before any window is built."""
    global LANG, THEME
    s = QSettings(str(DATA_DIR / "settings.ini"), QSettings.Format.IniFormat)
    code = s.value("language", "en", type=str)
    LANG = code if code in LANGUAGES else "en"
    disp = s.value("display", "white", type=str)
    THEME = disp if disp in THEMES else "white"


def selftest():
    """Checks the audio stack inside a packaged build (libsndfile, PortAudio). Exit code 0 = OK."""
    import tempfile
    assert set(STRINGS["en"]) == set(STRINGS["tr"]), set(STRINGS["en"]) ^ set(STRINGS["tr"])
    d = Path(tempfile.mkdtemp(prefix="sb_selftest_"))
    x = np.sin(2 * np.pi * 220 * np.arange(44100 * 6) / 44100).astype(np.float32)
    src = d / "Test_Pad_Am.wav"
    sf.write(str(src), np.stack([x, x], axis=1), 44100, subtype="PCM_24")
    f = analyze(str(src))
    assert abs(f["dur"] - 6) < 0.01, f
    s = make_sample(dict(path=str(src), root=str(d), **f))
    assert (s.category, s.key) == ("pad", "Am"), (s.category, s.key)
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
    load_language()
    dark_palette(app)
    open_main_window()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
