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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soundfile as sf  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
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
    app = QApplication(sys.argv)
    sb.dark_palette(app)
    w = sb.MainWindow()
    w.autoplay.setChecked(False)
    w.resize(1440, 840)
    w.show()
    w.add_root(samples_dir)
    assert wait(app, lambda: not w.scanner.isRunning() and not w.scanner.pending()), "scan did not finish"
    wait(app, lambda: False, 0.5)
    n = len(w.samples)
    cats = {s.name: s.category for s in w.samples.values()}
    print(f"scanned {n} samples")
    for name, cat in sorted(cats.items()):
        print(f"  {cat:14s} {name}")
    assert n > 0
    assert w.model.rowCount() == n

    # filtering
    w.search.setText("kick")
    w.apply_filter()
    assert all("kick" in s.hay for s in w.model.rows) and w.model.rowCount() > 0
    w.search.setText("")
    w.apply_filter()

    # similar sounds
    kick = next(s for s in w.samples.values() if s.category == "Kick")
    w.show_similar(kick)
    print("similar to", kick.name, "->", [s.name for s in w.model.rows[:5]])
    w.clear_similar()

    # kit + conversion
    order = ["Kick", "Snare", "Clap", "Hat (kapalı)", "Hat (açık)", "Perc", "Rim", "Shaker/Tamb", "Pad/Akor",
             "Cızırtı/Vinyl", "Atmosfer"]
    kit = sorted(w.samples.values(), key=lambda s: (order.index(s.category) if s.category in order else 99, s.name))
    w.add_to_kit([s.path for s in kit if s.category in order][:14])
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
    drag_file = sb.syntakt_file(kit[0])
    assert drag_file.exists()

    if shot:
        # pick a pad in the table for the waveform + highlight
        w.syntakt_mode.setChecked(False)
        w.activateWindow()
        w.table.setFocus()
        for i, s in enumerate(w.model.rows):
            if s.category == "Pad/Akor" and s.dur > 5:
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
