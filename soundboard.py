#!/usr/bin/env python3
"""
Mic Soundboard
Play any audio file through your microphone into Discord (or any voice chat).

  real mic ───────┐
                  ├──► mix ──► CABLE Input ══(VB-Audio Virtual Cable)══► CABLE Output ──► Discord's input
  sound buttons ──┘
        └──────────────────► your headphones (optional, so you hear the sounds too)

Setup steps are in README.md.
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from collections import deque

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import numpy as np

try:
    import sounddevice as sd
    import soundfile as sf
except (ImportError, OSError):
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "Missing packages",
        "Some packages aren't installed.\n\nDouble-click start.bat, or run:\n\npip install -r requirements.txt",
    )
    sys.exit(1)

try:
    import keyboard  # optional: global hotkeys that work while Discord/a game is focused
except Exception:
    keyboard = None


SR = 48000                     # Discord runs at 48 kHz
BLOCK = 480                    # 10 ms audio blocks
MIC_PRIME = 3 * BLOCK          # cushion (30 ms) before mic audio is passed on
MIC_MAX = 10 * BLOCK           # never let more than 100 ms of mic audio pile up
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
OLD_CONFIG_PATH = os.path.join(HERE, "soundboard_config.json")   # from the first zip version
ICON_ICO = os.path.join(HERE, "static", "icon.ico")
ICON_PNG = os.path.join(HERE, "static", "icon.png")
CONTROL_PORT = 47819          # lets a second launch bring this window forward, and the installer close it
RESTART_CODE = 3              # tells the launcher to restart (and install an update)
UNDER_LAUNCHER = os.environ.get("MIC_SOUNDBOARD_LAUNCHER") == "1"
UPDATE_EVERY = 3 * 60 * 60    # check GitHub for updates every 3 hours


def app_version():
    try:
        with open(os.path.join(HERE, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "dev"


def changelog_for(version):
    """The CHANGELOG.md section for one version, as plain text."""
    try:
        with open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().lstrip("v").startswith(version)
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()
AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg", ".aiff", ".aif")
AUDIO_TYPES = [("Audio files", " ".join("*" + e for e in AUDIO_EXTS)), ("All files", "*.*")]
COLUMNS = 4
OFF = "(off)"
CABLE_WORDS = ("cable input", "voicemeeter input", "voicemeeter aux input", "vb-audio")


# ───────────────────────────── audio helpers ─────────────────────────────

def load_audio(path):
    """Read a file into a float32 stereo array at 48 kHz."""
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    if sr != SR and len(data) > 1:
        n_out = max(1, int(round(len(data) * SR / sr)))
        x_old = np.arange(len(data), dtype=np.float64)
        x_new = np.linspace(0, len(data) - 1, n_out)
        data = np.column_stack([np.interp(x_new, x_old, data[:, c]) for c in range(2)])
    return np.ascontiguousarray(data, dtype=np.float32)


def stream_extra(device):
    """WASAPI devices need auto_convert so 48 kHz works on any device."""
    try:
        api = sd.query_hostapis(sd.query_devices(device)["hostapi"])["name"]
        if "WASAPI" in api:
            return sd.WasapiSettings(auto_convert=True)
    except Exception:
        pass
    return None


class Voice:
    """One sound that is currently playing."""

    def __init__(self, data, name):
        self.data = data
        self.name = name
        self.pos = {"cable": 0, "monitor": 0}


class Mixer:
    """Mixes the mic with playing sounds and feeds the cable + your headphones."""

    def __init__(self):
        self.lock = threading.Lock()
        self.voices = []
        self.mic_fifo = deque()
        self.mic_frames = 0
        self.mic_primed = False
        self.mic_level = 0.0
        self.mic_on = True
        self.mic_gain = 1.0
        self.sound_gain = 0.7
        self.monitor_gain = 0.5
        self.monitor_active = False
        self.streams = []

    # -- control (called from the GUI) --
    def play(self, data, name):
        with self.lock:
            self.voices.append(Voice(data, name))

    def stop_all(self):
        with self.lock:
            self.voices.clear()

    def playing_names(self):
        with self.lock:
            return [v.name for v in self.voices]

    def healthy(self):
        return all(s.active for s in self.streams)

    def start(self, mic_dev, cable_dev, monitor_dev):
        self.stop()
        streams = []
        common = dict(samplerate=SR, blocksize=BLOCK, dtype="float32", latency="low")
        try:
            if mic_dev is not None:
                ch = max(1, min(2, sd.query_devices(mic_dev)["max_input_channels"]))
                streams.append(sd.InputStream(device=mic_dev, channels=ch, callback=self._mic_cb,
                                              extra_settings=stream_extra(mic_dev), **common))
            ch = max(1, min(2, sd.query_devices(cable_dev)["max_output_channels"]))
            streams.append(sd.OutputStream(device=cable_dev, channels=ch, callback=self._cable_cb,
                                           extra_settings=stream_extra(cable_dev), **common))
            if monitor_dev is not None:
                ch = max(1, min(2, sd.query_devices(monitor_dev)["max_output_channels"]))
                streams.append(sd.OutputStream(device=monitor_dev, channels=ch, callback=self._monitor_cb,
                                               extra_settings=stream_extra(monitor_dev), **common))
            with self.lock:
                self.mic_fifo.clear()
                self.mic_frames = 0
                self.mic_primed = False
                self.monitor_active = monitor_dev is not None
            for s in streams:
                s.start()
        except Exception:
            for s in streams:
                try:
                    s.close()
                except Exception:
                    pass
            self.monitor_active = False
            raise
        self.streams = streams

    def stop(self):
        self.monitor_active = False
        for s in self.streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self.streams = []
        self.mic_level = 0.0

    # -- audio callbacks (run on audio threads) --
    def _mic_cb(self, indata, frames, time_info, status):
        mono = indata.mean(axis=1).astype(np.float32) if indata.shape[1] > 1 else indata[:, 0].copy()
        self.mic_level = float(np.abs(mono).max()) if len(mono) else 0.0
        with self.lock:
            self.mic_fifo.append(mono)
            self.mic_frames += len(mono)
            while self.mic_frames > MIC_MAX and len(self.mic_fifo) > 1:
                self.mic_frames -= len(self.mic_fifo.popleft())

    def _read_mic(self, n):
        out = np.zeros(n, np.float32)
        if not self.mic_primed:
            if self.mic_frames < MIC_PRIME:
                return out
            self.mic_primed = True
        filled = 0
        while filled < n and self.mic_fifo:
            blk = self.mic_fifo[0]
            take = min(n - filled, len(blk))
            out[filled:filled + take] = blk[:take]
            if take == len(blk):
                self.mic_fifo.popleft()
            else:
                self.mic_fifo[0] = blk[take:]
            filled += take
            self.mic_frames -= take
        if filled < n:          # ran dry: wait for a fresh cushion
            self.mic_primed = False
        return out

    def _mix_voices(self, out, key, gain):
        n = len(out)
        for v in self.voices:
            p = v.pos[key]
            if p >= len(v.data):
                continue
            chunk = v.data[p:p + n]
            out[:len(chunk)] += chunk * gain
            v.pos[key] = p + len(chunk)

    def _prune(self):
        keys = ("cable", "monitor") if self.monitor_active else ("cable",)
        self.voices = [v for v in self.voices if any(v.pos[k] < len(v.data) for k in keys)]

    @staticmethod
    def _write(outdata, out):
        np.clip(out, -1.0, 1.0, out=out)
        if outdata.shape[1] == 1:
            outdata[:, 0] = out.mean(axis=1)
        else:
            outdata[:] = out

    def _cable_cb(self, outdata, frames, time_info, status):
        out = np.zeros((frames, 2), np.float32)
        with self.lock:
            mic = self._read_mic(frames)
            if self.mic_on:
                out += (mic * self.mic_gain)[:, None]
            self._mix_voices(out, "cable", self.sound_gain)
            self._prune()
        self._write(outdata, out)

    def _monitor_cb(self, outdata, frames, time_info, status):
        out = np.zeros((frames, 2), np.float32)
        with self.lock:
            self._mix_voices(out, "monitor", self.sound_gain * self.monitor_gain)
        self._write(outdata, out)


# ───────────────────────────────── GUI ─────────────────────────────────

class App:
    def __init__(self, root):
        self.root = root
        self.mixer = Mixer()
        self.cfg = self.load_config()
        self.sounds = [s for s in self.cfg.get("sounds", []) if isinstance(s, dict) and s.get("path")]
        self.stop_hotkey = self.cfg.get("stop_hotkey", "")
        self.cache = {}
        self.events = queue.Queue()
        self.running = False
        self.inputs, self.outputs = {}, {}
        self.hk_win = None
        self._level = 0.0
        self._last_status = None

        self.version = app_version()
        self.update_ready = None
        root.title(f"Mic Soundboard  v{self.version}")
        root.geometry("680x620")
        self.set_icon()
        root.minsize(540, 440)
        self.build_ui()
        self.refresh_devices()
        self.apply_mix()
        self.render_sounds()
        self.register_hotkeys()
        self.preload([s["path"] for s in self.sounds])
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.outputs.get(self.cable_var.get()) is not None:
            self.start(quiet=True)
        self.start_control_server()
        self.build_menu()
        self.root.after(1500, self.show_whats_new)
        self.root.after(10 * 60 * 1000, self.start_update_check)
        self.tick()

    # ---------- layout ----------
    def build_ui(self):
        pad = {"padx": 6, "pady": 3}

        self.status = tk.StringVar(value="Stopped")
        ttk.Label(self.root, textvariable=self.status, anchor="w", relief="sunken",
                  padding=(6, 3)).pack(fill="x", side="bottom")

        dev = ttk.LabelFrame(self.root, text="Audio devices")
        dev.pack(fill="x", padx=8, pady=(8, 4))
        dev.columnconfigure(1, weight=1)

        ttk.Label(dev, text="Audio system").grid(row=0, column=0, sticky="w", **pad)
        self.api_var = tk.StringVar()
        self.api_box = ttk.Combobox(dev, textvariable=self.api_var, state="readonly")
        self.api_box.grid(row=0, column=1, sticky="ew", **pad)
        self.api_box.bind("<<ComboboxSelected>>", lambda e: self.on_api_change())

        self.mic_var, self.cable_var, self.mon_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        rows = [("Your microphone", self.mic_var),
                ("Send to (virtual cable)", self.cable_var),
                ("Hear sounds on", self.mon_var)]
        self.dev_boxes = []
        for r, (label, var) in enumerate(rows, start=1):
            ttk.Label(dev, text=label).grid(row=r, column=0, sticky="w", **pad)
            box = ttk.Combobox(dev, textvariable=var, state="readonly")
            box.grid(row=r, column=1, sticky="ew", **pad)
            box.bind("<<ComboboxSelected>>", lambda e: self.on_device_change())
            self.dev_boxes.append(box)

        btns = ttk.Frame(dev)
        btns.grid(row=0, column=2, rowspan=4, sticky="ns", **pad)
        self.start_btn = ttk.Button(btns, text="Start", command=self.toggle)
        self.start_btn.pack(fill="x", pady=2)
        ttk.Button(btns, text="Refresh", command=self.refresh_devices).pack(fill="x", pady=2)

        mix = ttk.LabelFrame(self.root, text="Volume")
        mix.pack(fill="x", padx=8, pady=4)
        mix.columnconfigure(1, weight=1)
        self.mic_on_var = tk.BooleanVar(value=self.cfg.get("mic_on", True))
        ttk.Checkbutton(mix, text="Your mic", variable=self.mic_on_var,
                        command=self.apply_mix).grid(row=0, column=0, sticky="w", **pad)
        self.mic_vol = self.slider(mix, 0, "mic_vol", 100, 200)
        self.level = ttk.Progressbar(mix, length=90, maximum=1.0)
        self.level.grid(row=0, column=3, **pad)
        ttk.Label(mix, text="Sounds → call").grid(row=1, column=0, sticky="w", **pad)
        self.snd_vol = self.slider(mix, 1, "sound_vol", 70, 150)
        ttk.Label(mix, text="Sounds → your ears").grid(row=2, column=0, sticky="w", **pad)
        self.mon_vol = self.slider(mix, 2, "monitor_vol", 50, 150)

        self.update_bar = tk.Frame(self.root, bg="#2f9e44")
        self.update_label = tk.Label(self.update_bar, bg="#2f9e44", fg="white", font=("Segoe UI", 10, "bold"))
        self.update_label.pack(side="left", padx=8, pady=4)
        tk.Button(self.update_bar, text="Restart & update", command=self.install_update).pack(side="right", padx=6, pady=3)

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8, pady=(6, 0))
        self.sound_bar = bar
        ttk.Button(bar, text="+ Add sounds", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="+ Add folder", command=self.add_folder).pack(side="left", padx=4)
        self.stop_btn = ttk.Button(bar, command=self.mixer.stop_all)
        self.stop_btn.pack(side="right")
        self.stop_btn.bind("<Button-3>", self.stop_menu)
        self.update_stop_label()
        self.exclusive_var = tk.BooleanVar(value=self.cfg.get("one_at_a_time", False))
        ttk.Checkbutton(bar, text="One sound at a time", variable=self.exclusive_var,
                        command=self.save_config).pack(side="right", padx=8)

        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True, padx=8, pady=6)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = ttk.Frame(self.canvas)
        self.grid_win = self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>",
                             lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.grid_win, width=e.width))
        self.canvas.bind_all("<MouseWheel>",
                             lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

    def slider(self, parent, row, key, default, maximum):
        var = tk.IntVar(value=self.cfg.get(key, default))
        ttk.Scale(parent, from_=0, to=maximum, variable=var,
                  command=lambda v: self.apply_mix()).grid(row=row, column=1, sticky="ew", padx=6, pady=3)
        lbl = ttk.Label(parent, width=5)
        lbl.grid(row=row, column=2)
        var.trace_add("write", lambda *a: lbl.config(text=f"{var.get()}%"))
        lbl.config(text=f"{var.get()}%")
        return var

    def render_sounds(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        for c in range(COLUMNS):
            self.grid_frame.columnconfigure(c, weight=1, uniform="snd")
        if not self.sounds:
            ttk.Label(self.grid_frame, justify="center",
                      text="No sounds yet. Click “+ Add sounds”.\n\n"
                           "Click a sound to play it into the call.\n"
                           "Right-click a sound to give it a hotkey, rename it or remove it.",
                      ).grid(row=0, column=0, columnspan=COLUMNS, pady=40)
            return
        for i, s in enumerate(self.sounds):
            text = s["name"] + (f"\n[{s['hotkey']}]" if s.get("hotkey") else "")
            b = tk.Button(self.grid_frame, text=text, wraplength=140, height=3,
                          command=lambda i=i: self.play(i))
            b.grid(row=i // COLUMNS, column=i % COLUMNS, sticky="ew", padx=3, pady=3)
            b.bind("<Button-3>", lambda e, i=i: self.sound_menu(e, i))

    # ---------- devices ----------
    def refresh_devices(self):
        if not self.running:
            try:  # rescan so newly installed devices (like VB-Cable) show up
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        apis = [a["name"] for a in sd.query_hostapis()]
        self.api_box["values"] = apis
        want = self.api_var.get() or self.cfg.get("hostapi") or ("MME" if "MME" in apis else apis[0])
        if want not in apis:
            want = apis[0]
        self.api_var.set(want)
        self.load_devices_for(apis.index(want))

    def load_devices_for(self, api_index):
        self.inputs, self.outputs = {}, {}
        for idx, d in enumerate(sd.query_devices()):
            if d["hostapi"] != api_index:
                continue
            if d["max_input_channels"] > 0:
                self.inputs.setdefault(d["name"], idx)
            if d["max_output_channels"] > 0:
                self.outputs.setdefault(d["name"], idx)
        ins, outs = list(self.inputs), list(self.outputs)
        self.dev_boxes[0]["values"] = [OFF] + ins
        self.dev_boxes[1]["values"] = outs
        self.dev_boxes[2]["values"] = [OFF] + outs

        api = sd.query_hostapis(api_index)

        def name_of(i):
            try:
                return sd.query_devices(i)["name"] if i is not None and i >= 0 else ""
            except Exception:
                return ""

        def is_cable(n):
            return any(w in n.lower() for w in CABLE_WORDS) or "cable output" in n.lower()

        cable_guess = next((n for n in outs if any(w in n.lower() for w in CABLE_WORDS)), "")
        mic_guess = name_of(api.get("default_input_device"))
        if not mic_guess or is_cable(mic_guess):
            mic_guess = next((n for n in ins if not is_cable(n)), OFF)
        mon_guess = name_of(api.get("default_output_device"))
        if not mon_guess or is_cable(mon_guess):
            mon_guess = next((n for n in outs if not is_cable(n)), OFF)

        def pick(var, names, saved, guess):
            if var.get() in names:
                return
            var.set(saved if saved in names else guess)

        pick(self.mic_var, [OFF] + ins, self.cfg.get("mic"), mic_guess)
        pick(self.cable_var, outs, self.cfg.get("cable"), cable_guess)
        pick(self.mon_var, [OFF] + outs, self.cfg.get("monitor"), mon_guess)

        if not cable_guess and not self.cable_var.get():
            self.set_status("No virtual cable found. Install VB-Audio Virtual Cable (see README), then click Refresh.")

    def on_api_change(self):
        for v in (self.mic_var, self.cable_var, self.mon_var):
            v.set("")
        self.cfg.pop("mic", None), self.cfg.pop("cable", None), self.cfg.pop("monitor", None)
        self.refresh_devices()
        self.on_device_change()

    def on_device_change(self):
        self.save_config()
        if self.running:
            self.start()

    def selected(self):
        return (self.inputs.get(self.mic_var.get()),
                self.outputs.get(self.cable_var.get()),
                self.outputs.get(self.mon_var.get()))

    # ---------- start / stop ----------
    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self, quiet=False):
        mic, cable, mon = self.selected()
        if cable is None:
            msg = "Pick your virtual cable under “Send to” (it's called “CABLE Input” after you install VB-Cable)."
            self.set_status(msg) if quiet else messagebox.showwarning("Pick a device", msg)
            return
        if mic is not None and "cable output" in self.mic_var.get().lower():
            messagebox.showwarning("Wrong mic",
                                   "“Your microphone” is set to the cable itself. Pick your real mic, "
                                   "otherwise the audio loops forever.")
            return
        if mon == cable:
            mon = None
        try:
            self.mixer.start(mic, cable, mon)
        except Exception as e:
            self.running = False
            self.start_btn.config(text="Start")
            msg = (f"Couldn't open the audio devices:\n{e}\n\n"
                   "Try a different “Audio system” (MME works on almost every PC).")
            self.set_status("Couldn't start. " + str(e)) if quiet else messagebox.showerror("Audio error", msg)
            return
        self.running = True
        self.start_btn.config(text="Stop")
        self.apply_mix()
        self.save_config()

    def stop(self):
        self.mixer.stop()
        self.running = False
        self.start_btn.config(text="Start")

    def apply_mix(self):
        self.mixer.mic_on = self.mic_on_var.get()
        self.mixer.mic_gain = self.mic_vol.get() / 100
        self.mixer.sound_gain = self.snd_vol.get() / 100
        self.mixer.monitor_gain = self.mon_vol.get() / 100

    # ---------- sounds ----------
    def add_files(self):
        paths = filedialog.askopenfilenames(title="Pick sounds", filetypes=AUDIO_TYPES)
        self.add_paths(paths)

    def add_folder(self):
        d = filedialog.askdirectory(title="Pick a folder of sounds")
        if d:
            self.add_paths(sorted(os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(AUDIO_EXTS)))

    def add_paths(self, paths):
        have = {s["path"] for s in self.sounds}
        new = [p for p in paths if p and p not in have]
        for p in new:
            self.sounds.append({"path": p, "name": os.path.splitext(os.path.basename(p))[0], "hotkey": ""})
        if new:
            self.preload(new)
            self.render_sounds()
            self.save_config()

    def preload(self, paths):
        def work():
            for p in paths:
                if p in self.cache:
                    continue
                try:
                    self.cache[p] = load_audio(p)
                except Exception as e:
                    self.events.put(("load_error", p, str(e)))
        threading.Thread(target=work, daemon=True).start()

    def get_audio(self, path):
        if path in self.cache:
            return self.cache[path]
        try:
            self.cache[path] = load_audio(path)
            return self.cache[path]
        except Exception as e:
            messagebox.showerror("Can't play", f"Couldn't read this file:\n{path}\n\n{e}")
            return None

    def play(self, i):
        if not (0 <= i < len(self.sounds)):
            return
        s = self.sounds[i]
        if not self.running:
            self.set_status("Not running. Pick your devices and press Start.")
            return
        data = self.get_audio(s["path"])
        if data is None:
            return
        if self.exclusive_var.get():
            self.mixer.stop_all()
        self.mixer.play(data, s["name"])

    def sound_menu(self, e, i):
        s = self.sounds[i]
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Play", command=lambda: self.play(i))
        m.add_command(label="Set hotkey…", command=lambda: self.ask_hotkey(i))
        if s.get("hotkey"):
            m.add_command(label="Clear hotkey", command=lambda: self.assign_hotkey(i, ""))
        m.add_command(label="Rename…", command=lambda: self.rename(i))
        m.add_separator()
        if i > 0:
            m.add_command(label="Move left", command=lambda: self.move(i, -1))
        if i < len(self.sounds) - 1:
            m.add_command(label="Move right", command=lambda: self.move(i, 1))
        m.add_command(label="Remove", command=lambda: self.remove(i))
        m.tk_popup(e.x_root, e.y_root)

    def stop_menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Set “Stop all” hotkey…", command=lambda: self.ask_hotkey("stop"))
        if self.stop_hotkey:
            m.add_command(label="Clear hotkey", command=lambda: self.assign_hotkey("stop", ""))
        m.tk_popup(e.x_root, e.y_root)

    def update_stop_label(self):
        self.stop_btn.config(text="■ Stop all" + (f"  [{self.stop_hotkey}]" if self.stop_hotkey else ""))

    def rename(self, i):
        name = simpledialog.askstring("Rename", "New name:", initialvalue=self.sounds[i]["name"], parent=self.root)
        if name:
            self.sounds[i]["name"] = name.strip()
            self.render_sounds()
            self.save_config()

    def move(self, i, d):
        j = i + d
        self.sounds[i], self.sounds[j] = self.sounds[j], self.sounds[i]
        self.render_sounds()
        self.register_hotkeys()
        self.save_config()

    def remove(self, i):
        self.sounds.pop(i)
        self.render_sounds()
        self.register_hotkeys()
        self.save_config()

    # ---------- hotkeys ----------
    def ask_hotkey(self, target):
        if keyboard is None:
            messagebox.showinfo("Hotkeys", "Hotkeys need the “keyboard” package.\n\n"
                                           "Run:  pip install keyboard\nthen restart the soundboard.")
            return
        if self.hk_win:
            return
        self.hk_win = tk.Toplevel(self.root)
        self.hk_win.title("Set hotkey")
        self.hk_win.transient(self.root)
        self.hk_win.resizable(False, False)
        ttk.Label(self.hk_win, padding=20, justify="center",
                  text="Press the key or combo you want\n(for example  ctrl+shift+1  or  f9)\n\nPress Esc to cancel.").pack()

        def work():
            try:
                hk = keyboard.read_hotkey(suppress=False)
            except Exception:
                hk = None
            self.events.put(("hotkey_read", target, hk))
        threading.Thread(target=work, daemon=True).start()

    def assign_hotkey(self, target, hk):
        if hk:  # one key can only do one thing
            for s in self.sounds:
                if s.get("hotkey") == hk:
                    s["hotkey"] = ""
            if self.stop_hotkey == hk:
                self.stop_hotkey = ""
        if target == "stop":
            self.stop_hotkey = hk
        else:
            self.sounds[target]["hotkey"] = hk
        self.update_stop_label()
        self.render_sounds()
        self.register_hotkeys()
        self.save_config()

    def register_hotkeys(self):
        if keyboard is None:
            return
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        bad = []
        for i, s in enumerate(self.sounds):
            if s.get("hotkey"):
                try:
                    keyboard.add_hotkey(s["hotkey"], lambda i=i: self.events.put(("play", i)))
                except Exception:
                    bad.append(s["hotkey"])
        if self.stop_hotkey:
            try:
                keyboard.add_hotkey(self.stop_hotkey, lambda: self.events.put(("stop",)))
            except Exception:
                bad.append(self.stop_hotkey)
        if bad:
            self.set_status("Couldn't register hotkey(s): " + ", ".join(bad))

    # ---------- loop ----------
    def set_status(self, text):
        if text != self._last_status:
            self._last_status = text
            self.status.set(text)

    def tick(self):
        while not getattr(self, "closed", False):
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            kind = ev[0]
            if kind == "play":
                self.play(ev[1])
            elif kind == "stop":
                self.mixer.stop_all()
            elif kind == "show":
                self.bring_to_front()
            elif kind == "quit":
                self.on_close(0)
                return
            elif kind == "update_checked":
                self.on_update_checked(*ev[1:])
            elif kind == "load_error":
                self.set_status(f"Couldn't read {os.path.basename(ev[1])}: {ev[2]}")
            elif kind == "hotkey_read":
                if self.hk_win:
                    self.hk_win.destroy()
                    self.hk_win = None
                hk = ev[2]
                if hk and hk.lower() != "esc":
                    self.assign_hotkey(ev[1], hk)

        if getattr(self, "closed", False):
            return
        lvl = self.mixer.mic_level * self.mixer.mic_gain if (self.running and self.mixer.mic_on) else 0.0
        self._level = max(min(lvl, 1.0), self._level * 0.8)
        self.level["value"] = self._level

        if self.running:
            if not self.mixer.healthy():
                self.stop()
                self.set_status("Audio stopped (a device was unplugged or changed). Click Start.")
            else:
                playing = self.mixer.playing_names()
                if playing:
                    self.set_status("▶ Playing: " + ", ".join(playing))
                else:
                    self.set_status(f"Running: mic + sounds → {self.cable_var.get()}")
        elif self._last_status in (None, "Stopped") or (self._last_status or "").startswith(("Running", "▶")):
            self.set_status("Stopped")
        self.root.after(30, self.tick)

    # ---------- app window, single copy, updates ----------
    def set_icon(self):
        try:
            if os.name == "nt" and os.path.exists(ICON_ICO):
                self.root.iconbitmap(default=ICON_ICO)
            elif os.path.exists(ICON_PNG):
                self._icon_img = tk.PhotoImage(file=ICON_PNG)
                self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def build_menu(self):
        menubar = tk.Menu(self.root)
        helpm = tk.Menu(menubar, tearoff=0)
        helpm.add_command(label="Check for updates now", command=lambda: self.start_update_check(manual=True))
        self.auto_update_var = tk.BooleanVar(value=self.updater_state().get("auto", True))
        helpm.add_checkbutton(label="Update automatically", variable=self.auto_update_var,
                              command=self.toggle_auto_update)
        helpm.add_command(label="What's new", command=lambda: self.show_whats_new(force=True))
        helpm.add_separator()
        helpm.add_command(label="Open app folder", command=lambda: self.open_path(HERE))
        helpm.add_command(label="Open log file", command=lambda: self.open_path(os.path.join(DATA_DIR, "app.log")))
        helpm.add_separator()
        helpm.add_command(label=f"Mic Soundboard v{self.version}", state="disabled")
        menubar.add_cascade(label="Help", menu=helpm)
        self.root.config(menu=menubar)

    @staticmethod
    def open_path(path):
        try:
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("Can't open", str(e))

    @staticmethod
    def updater_state():
        try:
            import updater
            return updater.state()
        except Exception:
            return {}

    def toggle_auto_update(self):
        try:
            import updater
            updater.save_state(auto=self.auto_update_var.get())
        except Exception:
            pass

    def start_control_server(self):
        """Listen on localhost so a second launch can show this window and the installer can close it."""
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", CONTROL_PORT))
            srv.listen(4)
        except OSError:
            return

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    try:
                        conn.settimeout(2)
                        cmd = conn.recv(64).decode("utf-8", "replace").strip()
                        if cmd in ("show", "quit"):
                            self.events.put((cmd,))
                            conn.sendall(b"ok\n")
                    except OSError:
                        pass
        threading.Thread(target=serve, daemon=True).start()

    def bring_to_front(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def start_update_check(self, manual=False):
        def work():
            try:
                import updater
                if not manual and not updater.auto_enabled():
                    return
                latest = updater.check()
                newer = updater.is_newer(latest) and latest != updater.state().get("skip")
                self.events.put(("update_checked", latest if newer else None, manual, None))
            except Exception as e:
                self.events.put(("update_checked", None, manual, str(e)))
        threading.Thread(target=work, daemon=True).start()
        if not manual:
            self.root.after(UPDATE_EVERY * 1000, self.start_update_check)

    def on_update_checked(self, latest, manual, error):
        if latest:
            self.update_ready = latest
            self.update_label.config(text=f"Version {latest} is ready.")
            if not self.update_bar.winfo_ismapped():
                self.update_bar.pack(fill="x", padx=8, pady=(4, 0), before=self.sound_bar)
            if manual:
                self.install_update()
        elif manual:
            if error:
                messagebox.showwarning("Updates", f"Couldn't check for updates.\n\n{error}")
            else:
                messagebox.showinfo("Updates", f"You're on the newest version (v{self.version}).")

    def install_update(self):
        if not self.update_ready:
            return
        msg = (f"Update to version {self.update_ready} now?\n\n"
               "The soundboard closes and reopens in a few seconds. Your mic goes silent in Discord "
               "for that moment, so wait if you're mid-sentence.")
        if not messagebox.askyesno("Update", msg):
            return
        if UNDER_LAUNCHER:
            self.on_close(RESTART_CODE)      # the launcher installs the update and reopens the app
            return
        try:                                 # started some other way (e.g. start.bat): update in place
            import updater
            v = updater.apply(expected=self.update_ready)
            messagebox.showinfo("Updated", f"Updated to {v}. Open the soundboard again to use it.")
            self.on_close(0)
        except Exception as e:
            messagebox.showerror("Update failed", str(e))

    def show_whats_new(self, force=False):
        st = self.updater_state()
        just = st.get("just_updated")
        if not force and just != self.version:
            return
        try:
            import updater
            updater.save_state(just_updated=None)
        except Exception:
            pass
        notes = changelog_for(self.version) or "Small fixes and improvements."
        title = f"Updated to v{self.version}" if not force else f"What's new in v{self.version}"
        messagebox.showinfo(title, notes)

    # ---------- config ----------
    def load_config(self):
        for path in (CONFIG_PATH, OLD_CONFIG_PATH):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
        return {}

    def save_config(self):
        self.cfg = {
            "hostapi": self.api_var.get(),
            "mic": self.mic_var.get(),
            "cable": self.cable_var.get(),
            "monitor": self.mon_var.get(),
            "mic_on": self.mic_on_var.get(),
            "mic_vol": self.mic_vol.get(),
            "sound_vol": self.snd_vol.get(),
            "monitor_vol": self.mon_vol.get(),
            "one_at_a_time": self.exclusive_var.get(),
            "stop_hotkey": self.stop_hotkey,
            "sounds": self.sounds,
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
        except Exception:
            pass

    def on_close(self, code=0):
        if getattr(self, "closed", False):
            return
        self.closed = True
        self.exit_code = code
        self.save_config()
        self.mixer.stop()
        if keyboard is not None:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
        self.root.destroy()


def main():
    try:  # crisp text on high-DPI Windows screens
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    if already_running():
        return 0
    root = tk.Tk()
    app = App(root)
    root.mainloop()
    return getattr(app, "exit_code", 0)


def already_running():
    """If another copy is open, bring it to the front and return True."""
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=1) as s:
            s.sendall(b"show\n")
            return s.recv(16).startswith(b"ok")
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
