"""End-to-end check of the GUI on a sample folder, optionally saving a screenshot for the README.

Usage: python tools/smoke_test.py <samples_dir> [screenshot.png]
Uses a throwaway data folder, plays no audio.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["SB_DATA_DIR"] = tempfile.mkdtemp(prefix="sb_smoke_")
sys.stdout.reconfigure(encoding="utf-8")  # Windows CI consoles are cp1252
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soundfile as sf  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import sample_browser as sb  # noqa: E402


def wait(app, cond, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def main(samples_dir, shot=None):
    assert set(sb.STRINGS["en"]) == set(sb.STRINGS["tr"]), set(sb.STRINGS["en"]) ^ set(sb.STRINGS["tr"])
    sb.LANG = "tr"
    assert sb.tr("add_folder") == "+ Klasör ekle"
    sb.LANG = "en"
    assert sb.tr("add_folder") == "+ Add folder"

    app = QApplication(sys.argv)
    sb.dark_palette(app)
    w = sb.open_main_window()
    w.autoplay.setChecked(False)
    w.resize(1440, 840)
    w.add_root(samples_dir)
    assert wait(app, lambda: not w.scanner.isRunning() and not w.scanner.pending()), "scan did not finish"
    wait(app, lambda: False, 0.5)
    n = len(w.samples)
    print(f"scanned {n} samples")
    for s in sorted(w.samples.values(), key=lambda s: s.name):
        print(f"  {s.category:11s} {sb.cat_name(s.category):14s} {s.name}")
    assert n > 0 and w.model.rowCount() == n

    # search works in both languages
    for q in ("kick", "dark", "karanlık"):
        w.search.setText(q)
        w.apply_filter()
        assert w.model.rowCount() > 0, q
        assert all(q in s.hay for s in w.model.rows), q
    w.search.setText("")
    w.apply_filter()

    # similar sounds
    kick = next(s for s in w.samples.values() if s.category == "kick")
    w.show_similar(kick)
    print("similar to", kick.name, "->", [s.name for s in w.model.rows[:5]])
    w.clear_similar()

    # kit: add, toggle, reorder, insert at position
    order = ["kick", "snare", "clap", "hat_closed", "hat_open", "perc", "rim", "shaker", "pad", "crackle", "atmos"]
    picks = sorted((s for s in w.samples.values() if s.category in order),
                   key=lambda s: (order.index(s.category), s.name))[:14]
    w.add_to_kit([s.path for s in picks])
    assert w.kit == [s.path for s in picks]
    assert w.model.kit_index[picks[0].path] == 1
    w.toggle_kit([picks[0]])  # all in kit -> removes
    assert picks[0].path not in w.kit
    w.insert_into_kit([picks[0].path], 0)  # back to slot 1
    assert w.kit[0] == picks[0].path
    w.move_in_kit([picks[3].path, picks[5].path], 1)  # drag two items up
    assert w.kit[1:3] == [picks[3].path, picks[5].path], w.kit[:4]
    w.kit_list.setCurrentRow(1)
    w.kit_list.item(1).setSelected(True)
    w.nudge_kit(+1)
    assert w.kit[2] == picks[3].path
    assert w.kit_list.count() == len(w.kit) == 14
    print("kit order ok:", [Path(p).stem for p in w.kit[:4]])

    # export
    out = Path(os.environ["SB_DATA_DIR"]) / "export"
    out.mkdir()
    for i, p in enumerate(w.kit, 1):
        s = w.samples[p]
        sb.write_syntakt(sb.to_syntakt(p), out / f"{i:02d}_{sb.safe_name(s.stem, 28)}.wav")
    for f in sorted(out.iterdir()):
        info = sf.info(str(f))
        assert info.samplerate == 48000 and info.channels == 1 and info.subtype == "PCM_16", f
        assert info.frames <= 5 * 48000, f
    print("exported", len(list(out.iterdir())), "files, kit label:", w.kit_label.text())
    assert sb.syntakt_file(picks[0]).exists()
    assert sb.drag_badge("3 samples").width() > 0

    if shot:
        w.syntakt_mode.setChecked(False)
        w.activateWindow()
        w.table.setFocus()
        for i, s in enumerate(w.model.rows):
            if s.category == "pad" and s.dur > 5 and s.path not in w.kit:
                w.table.selectRow(i)
                w.show_wave(s)
                break
        wait(app, lambda: False, 0.6)
        w.grab().save(shot)
        print("screenshot:", shot)
    w.close()
    print("OK")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
