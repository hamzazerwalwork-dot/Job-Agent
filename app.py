"""
Job Agent - desktop app.
Runs the search -> score -> tailor cycle in the background on a schedule.
Applying opens your Chrome with the form pre-filled; you press Submit.
"""
import json, os, queue, sys, threading, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "JobAgent"
APP_DIR.mkdir(parents=True, exist_ok=True)
os.environ["JOB_AGENT_HOME"] = str(APP_DIR)  # must be set before importing job_agent
CONFIG = APP_DIR / "config.json"

import job_agent as ja  # noqa: E402

DEFAULTS = {"api_key": "", "location": "", "min_score": 60, "max_jobs": 10, "interval_h": 6, "autostart": False}


def load_cfg():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    return cfg


class QueueWriter:
    def __init__(self, q): self.q = q
    def write(self, s):
        if s.strip(): self.q.put(("log", s.rstrip()))
    def flush(self): pass


class App(tk.Tk):
    def __init__(self, minimized=False):
        super().__init__()
        self.title("Job Agent")
        self.geometry("980x640")
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker = None
        self.cycle_lock = threading.Lock()
        sys.stdout = QueueWriter(self.q)
        sys.stderr = QueueWriter(self.q)
        self.build_ui()
        self.refresh_jobs()
        self.after(200, self.poll)
        self.apply_key()
        if minimized:
            self.iconify()
            if self.cfg["api_key"] and ja.PROFILE_PATH.exists():
                self.start_agent()

    # ---------- UI ----------
    def build_ui(self):
        top = ttk.LabelFrame(self, text="Settings"); top.pack(fill="x", padx=8, pady=6)
        self.vars = {
            "api_key": tk.StringVar(value=self.cfg["api_key"]),
            "location": tk.StringVar(value=self.cfg["location"]),
            "min_score": tk.IntVar(value=self.cfg["min_score"]),
            "max_jobs": tk.IntVar(value=self.cfg["max_jobs"]),
            "interval_h": tk.IntVar(value=self.cfg["interval_h"]),
            "autostart": tk.BooleanVar(value=self.cfg["autostart"]),
        }
        def row(r, c, label, var, w=12, show=None):
            ttk.Label(top, text=label).grid(row=r, column=c, sticky="e", padx=4, pady=3)
            ttk.Entry(top, textvariable=var, width=w, show=show).grid(row=r, column=c + 1, sticky="w", padx=4)
        row(0, 0, "AI key (free Gemini or Claude)", self.vars["api_key"], 38, "*")
        row(0, 2, "Location (blank = anywhere)", self.vars["location"], 18)
        row(1, 0, "Min match score", self.vars["min_score"], 6)
        row(1, 2, "Max jobs per cycle", self.vars["max_jobs"], 6)
        row(1, 4, "Check every (hours)", self.vars["interval_h"], 6)
        ttk.Checkbutton(top, text="Start with Windows", variable=self.vars["autostart"]).grid(row=0, column=4, columnspan=2, padx=8)
        ttk.Button(top, text="Save settings", command=self.save_cfg).grid(row=1, column=6, padx=8)

        bar = ttk.Frame(self); bar.pack(fill="x", padx=8)
        ttk.Button(bar, text="1. Load CV…", command=self.load_cv).pack(side="left", padx=3)
        self.btn_start = ttk.Button(bar, text="2. Start agent", command=self.start_agent); self.btn_start.pack(side="left", padx=3)
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop_agent, state="disabled"); self.btn_stop.pack(side="left", padx=3)
        ttk.Button(bar, text="Search now", command=self.search_now).pack(side="left", padx=3)
        self.status = ttk.Label(bar, text="Stopped"); self.status.pack(side="right", padx=6)

        mid = ttk.LabelFrame(self, text="Matched jobs (tailored CV ready)"); mid.pack(fill="both", expand=True, padx=8, pady=6)
        cols = ("score", "title", "company", "location", "status")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=10)
        for c, w in zip(cols, (60, 330, 200, 180, 90)):
            self.tree.heading(c, text=c.title()); self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(mid, command=self.tree.yview); sb.pack(side="right", fill="y"); self.tree.config(yscrollcommand=sb.set)

        act = ttk.Frame(self); act.pack(fill="x", padx=8)
        ttk.Button(act, text="Apply (pre-fill form)", command=self.apply_selected).pack(side="left", padx=3)
        ttk.Button(act, text="Open job page", command=lambda: self.open_sel("url")).pack(side="left", padx=3)
        ttk.Button(act, text="Open CV folder", command=lambda: self.open_sel("folder")).pack(side="left", padx=3)
        ttk.Button(act, text="Mark applied", command=lambda: self.mark("applied")).pack(side="left", padx=3)
        ttk.Button(act, text="Skip", command=lambda: self.mark("skipped")).pack(side="left", padx=3)

        self.log = tk.Text(self, height=9, state="disabled", bg="#111", fg="#ddd"); self.log.pack(fill="x", padx=8, pady=6)

    # ---------- config ----------
    def save_cfg(self):
        self.cfg = {k: v.get() for k, v in self.vars.items()}
        CONFIG.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.apply_key()
        self.set_autostart(self.cfg["autostart"])
        self.say("Settings saved.")

    def apply_key(self):
        """Gemini keys (free, from aistudio.google.com) start with AIza; Claude keys start with sk-ant."""
        key = self.cfg["api_key"].strip()
        os.environ.pop("ANTHROPIC_API_KEY", None); os.environ.pop("GEMINI_API_KEY", None)
        if key.startswith("sk-ant"): os.environ["ANTHROPIC_API_KEY"] = key
        elif key: os.environ["GEMINI_API_KEY"] = key

    def set_autostart(self, on):
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            if on:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}" --minimized'
                else:
                    cmd = f'"{sys.executable}" "{Path(__file__).resolve()}" --minimized'
                winreg.SetValueEx(key, "JobAgent", 0, winreg.REG_SZ, cmd)
            else:
                try: winreg.DeleteValue(key, "JobAgent")
                except FileNotFoundError: pass
        except ImportError:
            pass

    # ---------- logging / queue ----------
    def say(self, text): self.q.put(("log", text))

    def poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log.config(state="normal"); self.log.insert("end", payload + "\n"); self.log.see("end"); self.log.config(state="disabled")
                elif kind == "refresh":
                    self.refresh_jobs()
                elif kind == "status":
                    self.status.config(text=payload)
                elif kind == "call":  # run function on main thread
                    fn, box, evt = payload
                    box.append(fn()); evt.set()
        except queue.Empty:
            pass
        self.after(200, self.poll)

    def on_main(self, fn):
        box, evt = [], threading.Event()
        self.q.put(("call", (fn, box, evt))); evt.wait()
        return box[0]

    # ---------- actions ----------
    def load_cv(self):
        path = filedialog.askopenfilename(filetypes=[("CV", "*.pdf *.docx *.txt")])
        if not path: return
        self.save_cfg()
        if not self.cfg["api_key"]:
            messagebox.showwarning("API key", "Enter your Gemini (free) or Claude API key first."); return
        self.say("Reading CV…")
        def work():
            try:
                ja.parse_cv(path)
            except Exception as e:
                self.say(f"! CV parsing failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def start_agent(self):
        if not ja.PROFILE_PATH.exists():
            messagebox.showinfo("CV needed", "Load your CV first."); return
        self.save_cfg()
        self.stop_evt.clear()
        self.btn_start.config(state="disabled"); self.btn_stop.config(state="normal")
        def loop():
            while not self.stop_evt.is_set():
                self.cycle()
                hours = max(1, int(self.cfg["interval_h"]))
                self.q.put(("status", f"Running - next check in {hours}h"))
                if self.stop_evt.wait(hours * 3600): break
            self.q.put(("status", "Stopped"))
        self.worker = threading.Thread(target=loop, daemon=True); self.worker.start()

    def stop_agent(self):
        self.stop_evt.set()
        self.btn_start.config(state="normal"); self.btn_stop.config(state="disabled")

    def search_now(self):
        if not ja.PROFILE_PATH.exists():
            messagebox.showinfo("CV needed", "Load your CV first."); return
        self.save_cfg()
        threading.Thread(target=self.cycle, daemon=True).start()

    def cycle(self):
        if not self.cycle_lock.acquire(blocking=False):
            self.say("A search is already running."); return
        try:
            self.q.put(("status", "Searching…"))
            new = ja.run(int(self.cfg["min_score"]), int(self.cfg["max_jobs"]), self.cfg["location"] or None)
            self.q.put(("refresh", None))
            if new:
                self.say(f"✔ {len(new)} new tailored applications ready for review.")
        except Exception as e:
            self.say(f"! Cycle failed: {e}")
        finally:
            self.cycle_lock.release()
            self.q.put(("status", "Idle"))

    # ---------- jobs table ----------
    def jobs(self): return ja.load_json(ja.JOBS_PATH, [])

    def refresh_jobs(self):
        self.tree.delete(*self.tree.get_children())
        for j in sorted(self.jobs(), key=lambda x: (x.get("status") != "ready", -x.get("score", 0))):
            self.tree.insert("", "end", iid=j["id"], values=(j.get("score"), j["title"], j["company"], j["location"], j.get("status", "ready")))

    def selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select a job", "Select a job in the list first."); return None
        return next(j for j in self.jobs() if j["id"] == sel[0])

    def open_sel(self, what):
        j = self.selected()
        if not j: return
        if what == "url": webbrowser.open(j["url"])
        else: os.startfile(ja.OUT / j["id"]) if hasattr(os, "startfile") else None

    def mark(self, status):
        j = self.selected()
        if not j: return
        self.set_status(j["id"], status)

    def set_status(self, jid, status):
        data = self.jobs()
        for x in data:
            if x["id"] == jid: x["status"] = status
        ja.JOBS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.q.put(("refresh", None))

    def apply_selected(self):
        j = self.selected()
        if not j: return
        def work():
            try:
                info = lambda m: self.on_main(lambda: messagebox.showinfo("Job Agent", m))
                ask = lambda m: self.on_main(lambda: messagebox.askyesno("Job Agent", m))
                ja.apply_to(j["id"], info, info, ask)
                done = self.on_main(lambda: messagebox.askyesno("Job Agent", "Did you submit the application?"))
                self.set_status(j["id"], "applied" if done else "ready")
            except Exception as e:
                self.say(f"! Apply failed: {e}")
        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    App(minimized="--minimized" in sys.argv).mainloop()
