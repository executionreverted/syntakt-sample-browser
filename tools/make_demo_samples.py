"""Generate a small synthetic garage kit (kicks, snares, hats, percs, pads, textures, loops) for demos/tests.

Usage: python tools/make_demo_samples.py <out_dir>
Files are 44.1 kHz / 24-bit / stereo on purpose, so the Syntakt conversion has something to do.
"""
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
rng = np.random.default_rng(7)


def t(sec):
    return np.arange(int(sec * SR)) / SR


def env(sec, decay, attack=0.001):
    x = t(sec)
    return np.minimum(1.0, x / attack) * np.exp(-x / decay)


def noise(sec):
    return rng.uniform(-1, 1, int(sec * SR))


def hp(x, n=2):
    for _ in range(n):
        x = np.diff(x, prepend=0.0)
    return x


def lp_fast(x, a):
    """One-pole low-pass as a truncated FIR."""
    from math import ceil
    k = int(ceil(4 / a))
    kern = a * (1 - a) ** np.arange(k)
    return np.convolve(x, kern)[: x.size]


def saw(freq, sec):
    ph = (t(sec) * freq) % 1.0
    return 2 * ph - 1


def note(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def kick(f0, f1, tau, dec, sec=0.9, click=0.3):
    x = t(sec)
    f = f1 + (f0 - f1) * np.exp(-x / tau)
    y = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(sec, dec)
    y[: int(0.003 * SR)] += click * noise(0.003)
    return y


def snare(tone=190, dec=0.12, ndec=0.16, sec=0.5, nmix=0.8):
    return (np.sin(2 * np.pi * tone * t(sec)) * env(sec, dec) * 0.6
            + hp(noise(sec), 1) * env(sec, ndec) * nmix)


def add_at(y, s, x):
    n = min(x.size, y.size - s)
    if n > 0:
        y[s:s + n] += x[:n]


def clap(sec=0.6):
    y = np.zeros(int(sec * SR))
    for i, off in enumerate((0, 0.011, 0.023)):
        add_at(y, int(off * SR), hp(noise(sec - off), 1) * env(sec - off, 0.012 if i < 2 else 0.18))
    return y * 0.7


def hat(dec, sec=0.4):
    return hp(noise(sec), 3) * env(sec, dec) * 0.5


def perc(f, dec=0.06, sec=0.35):
    x = t(sec)
    return (np.sin(2 * np.pi * f * x) + 0.4 * np.sin(2 * np.pi * f * 2.7 * x)) * env(sec, dec)


def pad(notes, sec=7.0, bright=0.02, detune=0.006):
    y = np.zeros(int(sec * SR))
    for n in notes:
        for d in (-detune, 0, detune):
            y += saw(note(n) * (1 + d), sec)
    y = lp_fast(y, bright)
    x = t(sec)
    shape = np.minimum(1, x / 0.8) * np.minimum(1, (sec - x) / 1.5)
    return y * shape


def stab(notes, sec=1.6):
    y = np.zeros(int(sec * SR))
    for n in notes:
        for d in (-0.004, 0.004):
            y += saw(note(n) * (1 + d), sec)
    return lp_fast(y, 0.05) * env(sec, 0.35, attack=0.003)


def crackle(sec=8.0):
    y = hp(noise(sec), 1) * 0.015
    pops = rng.random(y.size) < 0.0009
    y[pops] += rng.uniform(-0.9, 0.9, pops.sum())
    return lp_fast(y, 0.5)


def rain(sec=8.0):
    y = lp_fast(noise(sec), 0.25) * 0.25
    for _ in range(220):
        add_at(y, int(rng.integers(0, y.size)), perc(rng.uniform(1800, 4200), 0.004, 0.05) * rng.uniform(0.05, 0.25))
    return y


def top_loop(bpm=132, bars=2):
    step = 60 / bpm / 4
    sec = step * 16 * bars
    y = np.zeros(int(sec * SR))
    for i in range(16 * bars):
        swing = step * 0.18 if i % 2 else 0.0
        s = int((i * step + swing) * SR)
        add_at(y, s, hat(0.025 if i % 4 != 2 else 0.12, 0.25) * (0.5 + 0.5 * (i % 2 == 0)))
        if i % 16 in (3, 11):
            add_at(y, s, perc(900, 0.03, 0.2) * 0.4)
    return y


def bass_loop(bpm=132, bars=2):
    step = 60 / bpm / 4
    sec = step * 16 * bars
    y = np.zeros(int(sec * SR))
    seq = [41, None, None, 41, None, 44, None, None, 39, None, None, 39, None, None, 46, None] * bars
    for i, n in enumerate(seq):
        if n is None:
            continue
        add_at(y, int(i * step * SR), np.sin(2 * np.pi * note(n) * t(step * 2.5)) * env(step * 2.5, 0.25, 0.005))
    return np.tanh(y * 1.5)


def write(path, y, width=0.15):
    y = y / (np.abs(y).max() + 1e-9) * 0.9
    d = int(width * 0.002 * SR)
    stereo = np.stack([y, np.concatenate([np.zeros(d), y[: y.size - d]])], axis=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), stereo, SR, subtype="PCM_24")


def main(out):
    k = Path(out) / "Night Bus Garage Kit"
    write(k / "Kicks" / "NB_Kick_Deep_01.wav", kick(140, 46, 0.03, 0.35))
    write(k / "Kicks" / "NB_Kick_Soft_02.wav", kick(110, 52, 0.02, 0.22, click=0.1))
    write(k / "Kicks" / "NB_Kick_Long_Sub_03.wav", kick(120, 41, 0.04, 0.6, sec=1.4))
    write(k / "Kicks" / "NB_Kick_Tight_04.wav", kick(180, 60, 0.015, 0.12, sec=0.5, click=0.5))
    write(k / "Snares" / "NB_Snare_Rimmy_01.wav", snare(230, 0.05, 0.09, nmix=0.6))
    write(k / "Snares" / "NB_Snare_Dusty_02.wav", snare(180, 0.1, 0.2))
    write(k / "Snares" / "NB_Snare_Ghost_03.wav", snare(260, 0.03, 0.05, sec=0.25, nmix=0.4))
    write(k / "Snares" / "NB_Clap_Wide_01.wav", clap())
    write(k / "Hats" / "NB_Hat_Closed_Tick_01.wav", hat(0.018, 0.15))
    write(k / "Hats" / "NB_Hat_Closed_Soft_02.wav", hat(0.03, 0.2))
    write(k / "Hats" / "NB_Hat_Open_Short_01.wav", hat(0.16, 0.7))
    write(k / "Percs" / "NB_Perc_Wood_01.wav", perc(820))
    write(k / "Percs" / "NB_Perc_Wood_High_02.wav", perc(1350, 0.04))
    write(k / "Percs" / "NB_Rim_Click_01.wav", perc(1700, 0.015, 0.12))
    write(k / "Percs" / "NB_Shaker_Soft_01.wav", hp(noise(0.25), 2) * np.sin(np.pi * t(0.25) / 0.25) ** 2 * 0.4)
    write(k / "Pads" / "NB_Pad_Dark_Fm.wav", pad([53, 56, 60, 63], bright=0.012))
    write(k / "Pads" / "NB_Pad_Lush_C#m.wav", pad([49, 52, 56, 61], bright=0.03))
    write(k / "Pads" / "NB_Pad_Choir_Gm.wav", pad([55, 58, 62, 67], sec=8.0, bright=0.02, detune=0.01))
    write(k / "Pads" / "NB_Chord_Stab_Ebm.wav", stab([51, 54, 58, 61]))
    write(k / "Pads" / "NB_Chord_Stab_Bbm7.wav", stab([46, 49, 53, 56]))
    write(k / "Textures" / "NB_Vinyl_Crackle_01.wav", crackle())
    write(k / "Textures" / "NB_Rain_Window_01.wav", rain())
    write(k / "Loops" / "NB_132_Top_Loop_Shuffle.wav", top_loop())
    write(k / "Loops" / "NB_132_Bass_Loop_Fm.wav", bass_loop())
    print("written to", k)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo_samples")
