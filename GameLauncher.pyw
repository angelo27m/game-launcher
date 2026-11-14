"""
Game Launcher - eine eigene Spielebibliothek im Stil von Epic/Steam.

- Spiele hinzufuegen (einzelne .exe, ganzen Ordner scannen, Desktop-Verknuepfungen importieren)
- Ueberwachte Ordner: neue Spiele werden automatisch erkannt und hinzugefuegt
- Co-op-Mods (Seamless Co-op, Mod Engine, ...) werden erkannt und ueber deren Launcher gestartet
- Der Launcher merkt sich den Pfad selbst (in %APPDATA%\\GameLauncher\\),
  Desktop-Verknuepfungen koennen also geloescht werden.
- Spielzeit + "zuletzt gespielt", Suche, Cover-Bilder (PNG/GIF), Pfad neu verknuepfen.
"""
import hashlib
import json
import os
import zipfile
import re
import subprocess
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, simpledialog

try:
    from PIL import Image, ImageTk  # fuer JPG-Cover von Steam (pip install pillow)
except ImportError:
    Image = ImageTk = None

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "GameLauncher")
LIB_FILE = os.path.join(APP_DIR, "library.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
COVER_DIR = os.path.join(APP_DIR, "covers")

BG = "#121212"
PANEL = "#1b1b1f"
CARD = "#202024"
CARD_HOVER = "#2a2a30"
TEXT = "#f2f2f2"
MUTED = "#9a9aa2"
ACCENT = "#0a84ff"
MOD = "#bf5af2"
OK = "#30d158"
DANGER = "#ff453a"
CARD_W, COVER_H = 190, 250
AUTOSCAN_SECONDS = 30
SETTLE_SECONDS = 90  # Ordner muss so lange unveraendert sein (= Download/Entpacken fertig)

# Dateien, die fast nie das eigentliche Spiel sind
EXE_BLACKLIST = ("unins", "setup", "redist", "vcredist", "vc_redist", "dxsetup", "crash",
                 "dotnet", "directx", "physx", "ue4prereq", "uninstall", "helper", "update",
                 "launcherpatcher", "cefprocess", "easyanticheat_setup", "7z", "winrar",
                 "start_protected_game")

# Mod-Starter: wenn so eine Datei im Spielordner liegt, wird DIESE gestartet statt der Spiel-EXE.
# (Regex auf den Dateinamen in Kleinbuchstaben, Name fuer die Anzeige)
MOD_LAUNCHERS = [
    (r"^ersc_launcher\.exe$", "Seamless Co-op"),          # Elden Ring
    (r"^nrsc_launcher\.exe$", "Seamless Co-op"),          # Elden Ring Nightreign
    (r"^ds3sc_launcher\.exe$", "Seamless Co-op"),         # Dark Souls 3
    (r"^ds[12]sc_launcher\.exe$", "Seamless Co-op"),      # Dark Souls 1 / 2
    (r"^launch_?ds3_?sc.*\.exe$", "Seamless Co-op"),
    (r"seamless.*\.exe$", "Seamless Co-op"),
    (r"co-?op.*launcher.*\.exe$|launcher.*co-?op.*\.exe$", "Co-op Mod"),
    (r"^launchmod_.*\.(bat|cmd)$", "Mod Engine"),
    (r"^modengine2?_launcher\.exe$", "Mod Engine"),
    (r"^me3_launcher\.exe$", "Mod Engine 3"),
]
# Mods, die ueber die normale EXE laufen (nur Anzeige, nichts aendern)
MOD_MARKERS = [
    (lambda n, d: n == "ersc.dll" or d == "seamlesscoop", "Seamless Co-op"),
    (lambda n, d: n.startswith("onlinefix") and n.endswith((".dll", ".ini")), "Online-Fix"),
    (lambda n, d: d == "bepinex", "BepInEx Mods"),
    (lambda n, d: d == "melonloader", "MelonLoader Mods"),
]


# ---------------------------------------------------------------- Daten
def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_library():
    return _load(LIB_FILE, [])


def save_library(games):
    _save(LIB_FILE, games)


def norm(p):
    return os.path.normcase(os.path.abspath(p)) if p else ""


def _walk(folder, max_depth):
    base = folder.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(folder):
        depth = root.count(os.sep) - base
        if depth >= max_depth:
            dirs[:] = []
        yield root, dirs, files, depth


def guess_main_exe(folder, max_depth=3):
    """Sucht im Spielordner die wahrscheinlichste Spiel-EXE (groesste, nicht auf Blacklist)."""
    best, best_score = None, -1
    folder_name = os.path.basename(folder).lower().replace(" ", "")
    for root, dirs, files, depth in _walk(folder, max_depth):
        low_root = root.lower()
        if any(s in low_root for s in ("_commonredist", "redist", "directx", "prereq", "thirdparty")):
            continue
        for fn in files:
            low = fn.lower()
            if not low.endswith(".exe") or any(b in low for b in EXE_BLACKLIST):
                continue
            if any(re.search(p, low) for p, _ in MOD_LAUNCHERS):
                continue
            path = os.path.join(root, fn)
            try:
                score = os.path.getsize(path)
            except OSError:
                continue
            stem = low[:-4].replace(" ", "")
            if stem and (stem in folder_name or folder_name in stem):
                score *= 3  # EXE heisst wie der Ordner -> sehr wahrscheinlich das Spiel
            if "shipping" in low:
                score *= 2
            score /= (depth + 1)
            if score > best_score:
                best, best_score = path, score
    return best


def find_mod(folder, max_depth=4):
    """Liefert (mod_launcher_pfad oder None, mod_name oder None)."""
    marker = None
    for root, dirs, files, depth in _walk(folder, max_depth):
        for fn in files:
            low = fn.lower()
            for pat, label in MOD_LAUNCHERS:
                if re.search(pat, low):
                    return os.path.join(root, fn), label
        dname = os.path.basename(root).lower()
        for check, label in MOD_MARKERS:
            if marker is None and (check("", dname) or any(check(f.lower(), "") for f in files)):
                marker = label
    return None, marker


def analyze_game_folder(folder):
    """-> dict mit path (was gestartet wird), original (Spiel-EXE), mod (Name) oder None."""
    exe = guess_main_exe(folder)
    mod_path, mod_name = find_mod(folder)
    if not exe and not mod_path:
        return None
    return {"path": mod_path or exe, "original": exe or "", "mod": mod_name or ""}


def folder_settled(folder):
    """True, wenn seit SETTLE_SECONDS nichts mehr im Ordner geaendert wurde (Download fertig)."""
    newest = 0
    for root, dirs, files, depth in _walk(folder, 6):
        for fn in files:
            try:
                mtime = os.path.getmtime(os.path.join(root, fn))
            except OSError:
                continue
            # Download-Reste nur beachten, wenn sie frisch sind (alte abgebrochene Reste ignorieren)
            if fn.lower().endswith((".part", ".crdownload", ".!qb", ".tmp")) and time.time() - mtime < 600:
                return False
            newest = max(newest, mtime)
    return newest and time.time() - newest > SETTLE_SECONDS


def read_desktop_shortcuts():
    """Liest alle .lnk auf dem Desktop aus (via PowerShell/WScript.Shell)."""
    ps = r"""
$sh = New-Object -ComObject WScript.Shell
$dirs = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'))
$out = @()
foreach ($d in $dirs) { if (Test-Path $d) {
  Get-ChildItem -LiteralPath $d -Filter *.lnk | ForEach-Object {
    $l = $sh.CreateShortcut($_.FullName)
    $out += [pscustomobject]@{ lnk=$_.FullName; name=$_.BaseName; target=$l.TargetPath; args=$l.Arguments; wd=$l.WorkingDirectory }
  } } }
$out | ConvertTo-Json -Compress
"""
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)
        data = json.loads(res.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        return [d for d in data if (d.get("target") or "").lower().endswith(".exe")
                and not os.path.basename(d["target"]).lower().startswith("python")]
    except Exception:
        return []


def find_appid(folder):
    """Steam-App-ID aus den Crack-/Fix-Dateien lesen (OnlineFix.ini, steam_emu.ini, steam_appid.txt)."""
    if not folder or not os.path.isdir(folder):
        return None
    pats = {"onlinefix.ini": r"^\s*RealAppId\s*=\s*(\d+)", "steam_emu.ini": r"^\s*AppId\s*=\s*(\d+)",
            "steam_appid.txt": r"^\s*(\d+)\s*$", "cream_api.ini": r"^\s*appid\s*=\s*(\d+)"}
    for root, dirs, files, depth in _walk(folder, 4):
        for fn in files:
            pat = pats.get(fn.lower())
            if not pat:
                continue
            try:
                with open(os.path.join(root, fn), "r", encoding="utf-8", errors="ignore") as f:
                    m = re.search(pat, f.read(20000), re.I | re.M)
            except OSError:
                continue
            if m and m.group(1) not in ("480", "0"):  # 480 = Spacewar (Platzhalter von Online-Fix)
                return m.group(1)
    return None


def _http(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 GameLauncher"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def search_appid(name):
    try:
        q = urllib.parse.quote(name)
        data = json.loads(_http(f"https://store.steampowered.com/api/storesearch/?term={q}&l=english&cc=US"))
        items = data.get("items") or []
        return str(items[0]["id"]) if items else None
    except Exception:
        return None


def download_cover(appid):
    """Laedt das Hochformat-Cover (600x900) von Steam. -> lokaler Pfad oder None."""
    os.makedirs(COVER_DIR, exist_ok=True)
    dest = os.path.join(COVER_DIR, f"steam_{appid}.jpg")
    if os.path.exists(dest):
        return dest
    for url in (f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/library_600x900_2x.jpg",
                f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg",
                f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"):
        try:
            data = _http(url)
        except Exception:
            continue
        if len(data) > 2000 and data[:3] == b"\xff\xd8\xff":
            with open(dest, "wb") as f:
                f.write(data)
            return dest
    return None


# ---------------------------------------------------------------- Archive (Platz sparen / neu installieren)
ARCHIVE_EXT = (".rar", ".zip", ".7z")


def _tool(*names):
    for base in (r"C:\Program Files\WinRAR", r"C:\Program Files (x86)\WinRAR",
                 r"C:\Program Files\7-Zip", r"C:\Program Files (x86)\7-Zip"):
        for n in names:
            p = os.path.join(base, n)
            if os.path.exists(p):
                return p
    return None


RAR_EXE = _tool("Rar.exe")
UNRAR_EXE = _tool("UnRAR.exe", "Rar.exe")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _key(s):
    s = re.sub(r"\.(rar|zip|7z)$", "", s.lower())
    s = re.sub(r"\s*\(\d+\)$", "", s)
    return re.sub(r"[^a-z0-9]", "", s.replace("steamrip.com", "").replace("steamrip", ""))


def archive_dirs(watched):
    dirs = []
    for w in watched:
        dirs.append(w)
        try:
            dirs += [os.path.join(w, d) for d in os.listdir(w)
                     if os.path.isdir(os.path.join(w, d)) and "archiv" in d.lower()]
        except OSError:
            pass
    return dirs


def find_archive(root, watched):
    """Sucht ein Archiv, das zum Spielordner passt (z. B. 'Nunholy-SteamRIP.com.rar')."""
    if not root:
        return None
    k = _key(os.path.basename(root))
    for d in archive_dirs(watched):
        try:
            for fn in os.listdir(d):
                if fn.lower().endswith(ARCHIVE_EXT) and _key(fn) == k:
                    return os.path.join(d, fn)
        except OSError:
            pass
    return None


def folder_size(folder):
    total = 0
    for root, dirs, files in os.walk(folder):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def disk_size(folder):
    """Tatsaechlich belegter Platz (beruecksichtigt Windows-Komprimierung)."""
    import ctypes
    from ctypes import wintypes
    f = ctypes.windll.kernel32.GetCompressedFileSizeW
    f.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    f.restype = wintypes.DWORD
    total = 0
    for root, dirs, files in os.walk(folder):
        for fn in files:
            hi = wintypes.DWORD(0)
            lo = f(os.path.join(root, fn), ctypes.byref(hi))
            if lo != 0xFFFFFFFF:
                total += (hi.value << 32) + lo
    return total


def gb(n):
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


def run_with_progress(cmd, on_progress):
    """Startet ein Tool und meldet die letzte Prozentangabe aus der Ausgabe."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NOWIN)
    buf = b""
    while True:
        chunk = proc.stdout.read(256)
        if not chunk:
            break
        buf = (buf + chunk)[-400:]
        m = re.findall(rb"(\d{1,3})%", buf)
        if m:
            on_progress(min(100, int(m[-1])))
    return proc.wait()


def archive_top_folder(archive):
    """Name des obersten Ordners im Archiv (falls es genau einen gibt)."""
    try:
        if archive.lower().endswith(".rar") and UNRAR_EXE:
            out = subprocess.run([UNRAR_EXE, "lb", archive], capture_output=True, text=True,
                                 errors="replace", creationflags=NOWIN).stdout
        else:
            out = subprocess.run(["tar", "-tf", archive], capture_output=True, text=True,
                                 errors="replace", creationflags=NOWIN).stdout
    except OSError:
        return None
    tops = {re.split(r"[\\/]", l.strip())[0] for l in out.splitlines() if l.strip()}
    return tops.pop() if len(tops) == 1 else None


# ---------------------------------------------------------------- Mod-Updates (Seamless Co-op von GitHub)
# Prefix der Launcher-Datei -> offizielles Release-Repo des Mod-Autors
MOD_REPOS = {
    "ersc": "yuiamoroll/EldenRingSeamlessCoopRelease",
    "nrsc": "yuiamoroll/NightreignSeamlessCoopRelease",
    "ds1sc": "yuiamoroll/DarkSouls1SeamlessCoopRelease",
    "ds2sc": "yuiamoroll/DarkSouls2SeamlessCoopRelease",
    "ds3sc": "yuiamoroll/DarkSouls3SeamlessCoopRelease",
}
MOD_CACHE = os.path.join(APP_DIR, "mod_updates")


def mod_info(g):
    """-> (prefix, mod_dir) wenn das Spiel einen updatebaren Seamless-Co-op-Launcher hat."""
    m = re.match(r"^([a-z0-9]+)_launcher\.exe$", os.path.basename(g.get("path", "")).lower())
    if m and m.group(1) in MOD_REPOS:
        return m.group(1), os.path.dirname(g["path"])
    return None, None


def latest_release(repo):
    data = json.loads(_http(f"https://api.github.com/repos/{repo}/releases/latest"))
    asset = next((a for a in data.get("assets", []) if a["name"].lower().endswith(".zip")), None)
    if not asset:
        return None
    return {"tag": data["tag_name"], "url": asset["browser_download_url"], "name": asset["name"]}


def release_zip(repo, rel):
    """Laedt das Release-ZIP einmal herunter und cached es."""
    os.makedirs(MOD_CACHE, exist_ok=True)
    path = os.path.join(MOD_CACHE, f"{repo.split('/')[1]}_{rel['tag']}.zip")
    if not os.path.exists(path):
        data = _http(rel["url"], timeout=120)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        zipfile.ZipFile(tmp).testzip()
        os.replace(tmp, path)
    return path


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def mod_up_to_date(prefix, mod_dir, zpath):
    """Vergleicht die installierte <prefix>.dll mit der im Release-ZIP."""
    dll = os.path.join(mod_dir, "SeamlessCoop", f"{prefix}.dll")
    if not os.path.exists(dll):
        return False
    with zipfile.ZipFile(zpath) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(f"/{prefix}.dll")), None)
        if not name:
            return True  # unbekanntes ZIP-Layout -> lieber nichts anfassen
        new = _sha(z.read(name))
    with open(dll, "rb") as f:
        return _sha(f.read()) == new


def apply_mod_update(prefix, mod_dir, zpath):
    """Sichert die alten Mod-Dateien und entpackt das neue Release. Eigene Einstellungen bleiben."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(mod_dir, f"SeamlessCoop_backup_{stamp}.zip")
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as bk:
            for n in names:
                old = os.path.join(mod_dir, *n.split("/"))
                if os.path.exists(old):
                    bk.write(old, n)
        for n in names:
            dest = os.path.join(mod_dir, *n.split("/"))
            if n.lower().endswith("_settings.ini") and os.path.exists(dest):
                # Deine Einstellungen (z. B. Co-op-Passwort) behalten, neue Vorlage daneben legen
                dest += ".neu"
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(n) as src, open(dest, "wb") as dst:
                dst.write(src.read())
    return backup


def clean_name(name):
    """'Elden-Ring-DE-SteamRIP.com' -> 'Elden Ring DE', 'DARK.SOULS.REMASTERED.v1.03.1-OFME' -> 'DARK SOULS REMASTERED'"""
    n = re.sub(r"[-_. ]*steamrip(\.com)?", "", name, flags=re.I)
    n = re.sub(r"[-_. ]v?\d+(\.\d+)+.*$", "", n)          # Versionsnummern
    n = re.sub(r"[-_. ](ofme|online[-_. ]?fix|repack|gog|fitgirl|dodi)$", "", n, flags=re.I)
    n = re.sub(r"\s*\(\d+\)$", "", n)                     # "(1)" vom Doppel-Download
    n = re.sub(r"[._-]+", " ", n).strip()
    return n or name


def fmt_time(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return "noch nicht gespielt" if seconds == 0 else "< 1 Min."
    h, m = divmod(seconds // 60, 60)
    return f"{h} Std. {m} Min." if h else f"{m} Min."


def fmt_date(ts):
    return time.strftime("%d.%m.%Y", time.localtime(ts)) if ts else "nie"


# ---------------------------------------------------------------- UI
class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Game Launcher")
        self.geometry("1180x760")
        self.minsize(640, 480)
        self.configure(bg=BG)
        self.games = load_library()
        self.settings = _load(SETTINGS_FILE, {})
        self.settings.setdefault("watched", [])
        self.settings.setdefault("ignored", [])  # entfernte Spiele nicht automatisch wieder hinzufuegen
        self.running = {}
        self.busy = {}        # spielordner -> Text auf dem Knopf (z. B. "Entpacke 42 %")
        self.busy_btns = {}
        self.images = {}
        self._scanning = False
        self._last_cols = None
        self.sort_mode = tk.StringVar(value="Zuletzt gespielt")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.render())
        self._build()
        self.render()
        self.bind("<Configure>", self._on_resize)
        self.after(1500, self.auto_scan)
        self.after(500, self.fetch_covers)
        self.after(3000, self.check_mod_updates)

    def save_settings(self):
        _save(SETTINGS_FILE, self.settings)

    # ---------- Layout
    def _build(self):
        side = tk.Frame(self, bg=PANEL, width=220)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            self.iconbitmap(default=os.path.join(here, "launcher.ico"))
        except tk.TclError:
            pass
        head = tk.Frame(side, bg=PANEL)
        head.pack(anchor="w", padx=16, pady=(20, 24))
        logo = os.path.join(here, "launcher.png")
        if Image and os.path.exists(logo):
            self._logo = ImageTk.PhotoImage(Image.open(logo).resize((40, 40), Image.LANCZOS))
            tk.Label(head, image=self._logo, bg=PANEL).pack(side="left", padx=(0, 10))
        tk.Label(head, text="LAUNCHER", bg=PANEL, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(side="left")
        for label, cmd in (("＋  Spiel hinzufügen", self.add_exe),
                           ("📁  Spiele-Ordner hinzufügen", self.scan_folder),
                           ("👁  Überwachte Ordner", self.manage_watched),
                           ("🖥  Desktop importieren", self.import_desktop)):
            self._side_button(side, label, cmd)
        tk.Frame(side, bg="#2c2c32", height=1).pack(fill="x", padx=18, pady=18)
        tk.Label(side, text="SORTIEREN", bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=18)
        for opt in ("Zuletzt gespielt", "Name", "Spielzeit", "Neu hinzugefügt"):
            tk.Radiobutton(side, text=opt, value=opt, variable=self.sort_mode, command=self.render,
                           bg=PANEL, fg=TEXT, selectcolor=PANEL, activebackground=PANEL, activeforeground=TEXT,
                           font=("Segoe UI", 10), bd=0, highlightthickness=0).pack(anchor="w", padx=14, pady=1)
        self.stats = tk.Label(side, text="", bg=PANEL, fg=MUTED, font=("Segoe UI", 9), justify="left")
        self.stats.pack(side="bottom", anchor="w", padx=18, pady=18)

        main = tk.Frame(self, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        top = tk.Frame(main, bg=BG)
        top.pack(fill="x", padx=28, pady=(22, 6))
        tk.Label(top, text="Bibliothek", bg=BG, fg=TEXT, font=("Segoe UI", 22, "bold")).pack(side="left")
        sbox = tk.Frame(top, bg=CARD, highlightthickness=1, highlightbackground="#33333a")
        sbox.pack(side="right")
        tk.Label(sbox, text="🔍", bg=CARD, fg=MUTED).pack(side="left", padx=(10, 2))
        tk.Entry(sbox, textvariable=self.search, bg=CARD, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=("Segoe UI", 11), width=26).pack(side="left", ipady=7, padx=(2, 10))
        self.banner = tk.Label(main, text="", bg=BG, fg="white", font=("Segoe UI", 10, "bold"),
                               anchor="w", padx=14, pady=8, justify="left")
        self._banner_job = None

        wrap = tk.Frame(main, bg=BG)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.wrap = wrap
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = tk.Frame(self.canvas, bg=BG)
        self.canvas.create_window((20, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

    def notify(self, text, color=OK, seconds=10):
        self.banner.configure(text=text, bg=color)
        self.banner.pack(fill="x", padx=28, pady=(4, 0), before=self.wrap)
        if self._banner_job:
            self.after_cancel(self._banner_job)
        self._banner_job = self.after(seconds * 1000, self.banner.pack_forget)

    def _side_button(self, parent, text, cmd):
        b = tk.Label(parent, text=text, bg=PANEL, fg=TEXT, font=("Segoe UI", 11), anchor="w", padx=18, pady=9, cursor="hand2")
        b.pack(fill="x")
        b.bind("<Enter>", lambda e: b.configure(bg=CARD_HOVER))
        b.bind("<Leave>", lambda e: b.configure(bg=PANEL))
        b.bind("<Button-1>", lambda e: cmd())

    def _on_resize(self, event):
        if event.widget is self:
            cols = self._columns()
            if cols != self._last_cols:
                self._last_cols = cols
                self.render()

    def _columns(self):
        w = max(self.canvas.winfo_width(), 300)
        return max(1, (w - 30) // (CARD_W + 20))

    # ---------- Rendern
    def render(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.busy_btns = {}
        q = self.search.get().strip().lower()
        games = [g for g in self.games if q in g["name"].lower()]
        mode = self.sort_mode.get()
        if mode == "Name":
            games.sort(key=lambda g: g["name"].lower())
        elif mode == "Spielzeit":
            games.sort(key=lambda g: -g.get("playtime", 0))
        elif mode == "Neu hinzugefügt":
            games.sort(key=lambda g: -g.get("added", 0))
        else:
            games.sort(key=lambda g: (-(g.get("last_played") or 0), g["name"].lower()))

        total = sum(g.get("playtime", 0) for g in self.games)
        self.stats.configure(text=f"{len(self.games)} Spiele\nGesamt: {fmt_time(total)}\n"
                                  f"{len(self.settings['watched'])} überwachte Ordner")

        if not self.games:
            tk.Label(self.grid_frame, text="Noch keine Spiele.\n\nLinks auf „Spiele-Ordner hinzufügen“ klicken und den Ordner wählen,\n"
                     "in den du deine Spiele entpackst (z. B. D:\\Games).\n"
                     "Jeder Unterordner wird ein Spiel – und neue Spiele dort kommen automatisch dazu.",
                     bg=BG, fg=MUTED, font=("Segoe UI", 12), justify="left").grid(row=0, column=0, padx=10, pady=40, sticky="w")
            return
        cols = self._columns()
        for i, g in enumerate(games):
            self._card(g).grid(row=i // cols, column=i % cols, padx=10, pady=10, sticky="n")

    def _cover_image(self, g):
        path = g.get("cover")
        if not path or not os.path.exists(path):
            return None
        key = (path, os.path.getmtime(path))
        if key in self.images:
            return self.images[key]
        if Image:
            try:
                im = Image.open(path).convert("RGB")
                scale = max(CARD_W / im.width, COVER_H / im.height)
                im = im.resize((max(CARD_W, round(im.width * scale)), max(COVER_H, round(im.height * scale))), Image.LANCZOS)
                x0, y0 = (im.width - CARD_W) // 2, (im.height - COVER_H) // 2
                out = ImageTk.PhotoImage(im.crop((x0, y0, x0 + CARD_W, y0 + COVER_H)))
                self.images[key] = out
                return out
            except Exception:
                pass
        try:
            img = tk.PhotoImage(file=path)
        except tk.TclError:
            return None
        w, h = img.width(), img.height()
        scale = max(CARD_W / w, COVER_H / h)
        num, den, best = 1, 1, None
        for n in range(1, 6):
            for d in range(1, 21):
                if n / d >= scale and (best is None or n / d < best):
                    best, num, den = n / d, n, d
        if num > 1:
            img = img.zoom(num)
        if den > 1:
            img = img.subsample(den)
        w, h = img.width(), img.height()
        x0, y0 = max(0, (w - CARD_W) // 2), max(0, (h - COVER_H) // 2)
        out = tk.PhotoImage(width=CARD_W, height=COVER_H)
        out.tk.call(out, "copy", img, "-from", x0, y0, x0 + CARD_W, y0 + COVER_H, "-to", 0, 0)
        self.images[key] = out
        return out

    def _card(self, g):
        exists = os.path.exists(g["path"])
        running = g["path"] in self.running
        card = tk.Frame(self.grid_frame, bg=CARD, width=CARD_W, cursor="hand2",
                        highlightthickness=2, highlightbackground=BG, highlightcolor=BG)
        img = self._cover_image(g)
        if img:
            cover = tk.Label(card, image=img, bg=CARD, bd=0)
        else:
            colors = ["#3b2a7a", "#1f4f7a", "#7a2a4f", "#2a6a4f", "#7a4f1f", "#4f2a7a"]
            col = colors[sum(map(ord, g["name"])) % 6]
            initials = "".join(w[0] for w in g["name"].split()[:2]).upper() or "?"
            cover = tk.Frame(card, bg=col, width=CARD_W, height=COVER_H)
            cover.pack_propagate(False)
            tk.Label(cover, text=initials, bg=col, fg="white", font=("Segoe UI", 44, "bold")).place(relx=.5, rely=.5, anchor="center")
        cover.pack()
        if g.get("mod"):
            tk.Label(card, text=f"🤝 {g['mod']}", bg=MOD, fg="white", font=("Segoe UI", 8, "bold"),
                     padx=6, pady=2).place(x=8, y=8)
        info = tk.Frame(card, bg=CARD, width=CARD_W)
        info.pack(fill="x")
        name = g["name"] if len(g["name"]) <= 24 else g["name"][:22] + "…"
        tk.Label(info, text=name, bg=CARD, fg=TEXT, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        archive = None if exists else self.archive_of(g)
        if exists:
            sub, subcol = f"{fmt_time(g.get('playtime'))} · {fmt_date(g.get('last_played'))}", MUTED
        elif archive:
            sub, subcol = f"Nicht installiert · Archiv {gb(os.path.getsize(archive))}", "#ffd60a"
        else:
            sub, subcol = "⚠ Nicht gefunden", DANGER
        tk.Label(info, text=sub, bg=CARD, fg=subcol, font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=10)
        busy = self.busy.get(self._groot(g))
        if busy:
            btxt, bcol = busy, "#ff9f0a"
        elif running:
            btxt, bcol = "LÄUFT …", OK
        elif g.get("mod_update") and exists:
            btxt, bcol = f"⬆  MOD-UPDATE {g['mod_update']}", "#ff9f0a"
        elif not exists and archive:
            btxt, bcol = "⬇  INSTALLIEREN", OK
        elif not exists:
            btxt, bcol = "PFAD SUCHEN", "#555"
        else:
            btxt, bcol = "▶  SPIELEN", MOD if self._uses_mod(g) else ACCENT
        row = tk.Frame(info, bg=CARD)
        row.pack(fill="x", padx=10, pady=10)
        trash = None
        if exists and not busy and not running:
            # Muelleimer: Spiel "loeschen" = in ein Archiv packen, Ordner entfernen, spaeter wieder installierbar
            trash = tk.Label(row, text="🗑", bg="#3a3a40", fg="white", font=("Segoe UI", 11), width=3,
                             pady=4, cursor="hand2")
            trash.pack(side="right", padx=(6, 0), fill="y")
            trash.bind("<Button-1>", lambda e: self.archive_and_delete(g))
            trash.bind("<Enter>", lambda e: trash.configure(bg=DANGER))
            trash.bind("<Leave>", lambda e: trash.configure(bg="#3a3a40"))
        btn = tk.Label(row, text=btxt, bg=bcol, fg="white", font=("Segoe UI", 10, "bold"), pady=6, cursor="hand2")
        btn.pack(side="left", fill="x", expand=True)
        btn.bind("<Button-1>", lambda e: self.play(g))
        if busy:
            self.busy_btns[self._groot(g)] = btn

        def walk(w):
            yield w
            for c in w.winfo_children():
                yield from walk(c)
        for w in walk(card):
            w.bind("<Button-3>", lambda e: self.context_menu(e, g))
            if w is not btn and w is not trash:
                w.bind("<Double-Button-1>", lambda e: self.play(g))
        # Hover nur an der Karte selbst: ein Rahmen, keine Farbwechsel an Kind-Elementen -> kein Flackern
        card.bind("<Enter>", lambda e: self._hover(card, True))
        card.bind("<Leave>", lambda e: self.after(15, self._hover_check, card))
        return card

    def _uses_mod(self, g):
        return bool(g.get("mod")) and bool(g.get("original")) and norm(g["path"]) != norm(g["original"])

    def _hover(self, card, on):
        col = ACCENT if on else BG
        if card.winfo_exists() and card.cget("highlightbackground") != col:
            card.configure(highlightbackground=col, highlightcolor=col)

    def _hover_check(self, card):
        if not card.winfo_exists():
            return
        x, y = self.winfo_pointerxy()
        inside = card.winfo_rootx() <= x < card.winfo_rootx() + card.winfo_width() and \
            card.winfo_rooty() <= y < card.winfo_rooty() + card.winfo_height()
        self._hover(card, inside)

    # ---------- Cover automatisch laden
    def fetch_covers(self):
        todo = [g for g in self.games if not g.get("cover") and not g.get("cover_tried")]
        if todo and not getattr(self, "_fetching", False):
            self._fetching = True
            threading.Thread(target=self._cover_worker, args=(todo,), daemon=True).start()

    def _cover_worker(self, todo):
        try:
            for g in todo:
                appid = g.get("appid") or find_appid(g.get("root") or os.path.dirname(g.get("original") or g["path"]))
                if not appid:
                    appid = search_appid(g["name"])
                cover = download_cover(appid) if appid else None
                self.after(0, self._cover_done, g, appid, cover)
        finally:
            self._fetching = False

    def _cover_done(self, g, appid, cover):
        g["cover_tried"] = True
        if appid:
            g["appid"] = appid
        if cover:
            g["cover"] = cover
            self.render()
        save_library(self.games)

    # ---------- Starten
    def play(self, g, path=None, skip_update=False):
        if g.get("mod_update") and not path and not skip_update and os.path.exists(g["path"]):
            self.update_mod(g)
            return
        path = path or g["path"]
        if path in self.running or self._groot(g) in self.busy:
            return
        if not os.path.exists(path) and self.archive_of(g):
            self.install(g)
            return
        if not os.path.exists(path):
            if messagebox.askyesno("Pfad fehlt", f"„{g['name']}“ wurde nicht gefunden:\n{path}\n\nNeue Datei auswählen?"):
                self.relink(g)
            return
        args = (g.get("args") or "").split() if path == g["path"] else []
        cmd = ["cmd", "/c", path] + args if path.lower().endswith((".bat", ".cmd")) else [path] + args
        workdir = g.get("workdir") if path == g["path"] and g.get("workdir") else os.path.dirname(path)
        try:
            proc = subprocess.Popen(cmd, cwd=workdir)  # Arbeitsordner = Spielordner (wichtig fuer Mods/Cracks)
        except OSError as ex:
            messagebox.showerror("Start fehlgeschlagen", str(ex))
            return
        self.running[path] = proc
        g["last_played"] = time.time()
        save_library(self.games)
        self.render()
        threading.Thread(target=self._track, args=(g, path, proc), daemon=True).start()

    def _track(self, g, path, proc):
        start = time.time()
        proc.wait()
        g["playtime"] = g.get("playtime", 0) + (time.time() - start)
        self.running.pop(path, None)
        save_library(self.games)
        self.after(0, self.render)

    # ---------- Hinzufuegen
    def _known(self, path):
        p = norm(path)
        return any(p in (norm(g["path"]), norm(g.get("original"))) for g in self.games)

    def _known_root(self, root):
        r = norm(root)
        return any(norm(g.get("root")) == r for g in self.games) or r in self.settings["ignored"]

    def _add(self, name, path, args="", workdir="", root="", original="", mod=""):
        if self._known(path):
            return False
        self.games.append({"name": name, "path": path, "original": original or path, "mod": mod,
                           "root": root, "args": args, "workdir": workdir, "cover": "",
                           "playtime": 0, "last_played": 0, "added": time.time()})
        return True

    def add_exe(self):
        path = filedialog.askopenfilename(title="Spiel-EXE auswählen", filetypes=[("Programme", "*.exe *.bat")])
        if not path:
            return
        path = os.path.normpath(path)
        default = os.path.basename(os.path.dirname(path))
        name = simpledialog.askstring("Name", "Name des Spiels:", initialvalue=default, parent=self)
        if name and self._add(name.strip(), path):
            save_library(self.games)
            self.render()

    def scan_folder(self):
        folder = filedialog.askdirectory(title="Ordner, in dem deine Spiele liegen (jeder Unterordner = ein Spiel)")
        if not folder:
            return
        folder = os.path.normpath(folder)
        if norm(folder) not in map(norm, self.settings["watched"]):
            self.settings["watched"].append(folder)
            self.save_settings()
        self.notify(f"Durchsuche {folder} …", ACCENT, 60)
        self.auto_scan(manual=True)

    def manage_watched(self):
        WatchedDialog(self)

    # ---------- Automatische Erkennung
    def auto_scan(self, manual=False):
        """Laeuft alle AUTOSCAN_SECONDS im Hintergrund: neue Spiele + neu installierte Mods."""
        if not self._scanning:
            self._scanning = True
            known_roots = {norm(g.get("root")) for g in self.games if g.get("root")} | set(self.settings["ignored"])
            known_roots |= {norm(p) for p in self.busy}  # gerade am Entpacken/Packen -> nicht anfassen
            check_mods = [(g["root"], bool(g.get("mod"))) for g in self.games if g.get("root")]
            threading.Thread(target=self._scan_worker, args=(list(self.settings["watched"]), known_roots,
                                                             check_mods, manual), daemon=True).start()
        if not manual:
            self.after(AUTOSCAN_SECONDS * 1000, self.auto_scan)

    def _scan_worker(self, watched, known_roots, check_mods, manual):
        new, mods, pending, empty = [], [], [], []
        try:
            for folder in watched:
                try:
                    subs = [os.path.join(folder, d) for d in sorted(os.listdir(folder))
                            if os.path.isdir(os.path.join(folder, d))]
                except OSError:
                    continue
                for sub in subs:
                    if norm(sub) in known_roots:
                        continue
                    if not folder_settled(sub):
                        pending.append(os.path.basename(sub))
                        continue
                    info = analyze_game_folder(sub)
                    if info:
                        new.append((sub, info))
                    else:
                        empty.append(os.path.basename(sub))
            # Bereits bekannte Spiele: wurde nachtraeglich ein Co-op-Mod reingelegt?
            for root, has_mod in check_mods:
                if not has_mod and os.path.isdir(root):
                    mod_path, mod_name = find_mod(root)
                    if mod_name:
                        mods.append((root, mod_path, mod_name))
        finally:
            self.after(0, self._scan_done, new, mods, pending, empty, manual)

    def _scan_done(self, new, mods, pending, empty, manual):
        self._scanning = False
        added = []
        for sub, info in new:
            if self._add(clean_name(os.path.basename(sub)), info["path"], root=sub,
                         original=info["original"], mod=info["mod"]):
                added.append(clean_name(os.path.basename(sub)) + (f" ({info['mod']})" if info["mod"] else ""))
        modded = []
        for root, mod_path, mod_name in mods:
            for g in self.games:
                if norm(g.get("root")) == norm(root) and not g.get("mod"):
                    g["mod"] = mod_name
                    if mod_path:
                        g.setdefault("original", g["path"])
                        g["path"] = mod_path
                        g["workdir"] = ""
                    modded.append(f"{g['name']} → {mod_name}")
        if added or modded:
            save_library(self.games)
            self.render()
            self.fetch_covers()
        parts = []
        if added:
            parts.append("Neu erkannt: " + ", ".join(added[:5]) + (" …" if len(added) > 5 else ""))
        if modded:
            parts.append("Co-op-Mod erkannt: " + ", ".join(modded[:5]))
        if manual:
            if not parts:
                parts.append("Keine neuen Spiele gefunden.")
            if pending:
                parts.append(f"Wird noch entpackt/geladen (kommt automatisch dazu): {', '.join(pending[:5])}")
            if empty:
                parts.append(f"Keine EXE gefunden in: {', '.join(empty[:5])}")
            self.notify("\n".join(parts), OK if (added or modded) else "#3a3a40", 14)
        elif parts:
            self.notify("\n".join(parts), MOD if modded else OK, 12)
            self.deiconify()
            self.lift()

    def import_desktop(self):
        self.configure(cursor="watch")
        self.update()
        links = read_desktop_shortcuts()
        self.configure(cursor="")
        links = [l for l in links if not self._known(l["target"])]
        if not links:
            messagebox.showinfo("Desktop importieren", "Keine neuen Verknüpfungen zu .exe-Dateien auf dem Desktop gefunden.")
            return
        ImportDialog(self, links)

    def finish_import(self, chosen, delete_lnks):
        deleted, failed = 0, 0
        for l in chosen:
            target = os.path.normpath(l["target"])
            folder = os.path.dirname(target)
            mod_path, mod_name = find_mod(folder, 2)
            self._add(l["name"], mod_path or target, l.get("args") or "", "" if mod_path else (l.get("wd") or ""),
                      original=target, mod=mod_name or "")
        save_library(self.games)  # erst speichern, DANN Verknuepfungen loeschen
        if delete_lnks:
            for l in chosen:
                try:
                    os.remove(l["lnk"])  # loescht nur die Verknuepfung, nicht das Spiel
                    deleted += 1
                except OSError:
                    failed += 1
        self.render()
        msg = f"{len(chosen)} Spiele importiert."
        if delete_lnks:
            msg += f"\n{deleted} Desktop-Verknüpfungen entfernt (die Spiele selbst bleiben unangetastet)."
            if failed:
                msg += f"\n{failed} konnten nicht gelöscht werden (evtl. Admin-Desktop)."
        messagebox.showinfo("Import fertig", msg)

    # ---------- Rechtsklick
    def context_menu(self, event, g):
        m = tk.Menu(self, tearoff=0, bg=PANEL, fg=TEXT, activebackground=ACCENT, activeforeground="white")
        m.add_command(label="▶  Spielen", command=lambda: self.play(g))
        if self._uses_mod(g):
            m.add_command(label="Ohne Mod starten (Original-EXE)", command=lambda: self.play(g, g["original"]))
        if g.get("mod_update"):
            m.add_command(label=f"⬆ Mod-Update {g['mod_update']} installieren", command=lambda: self.update_mod(g))
            m.add_command(label="Ohne Update spielen", command=lambda: self.play(g, skip_update=True))
        if mod_info(g)[0]:
            m.add_command(label="Nach Mod-Updates suchen", command=lambda: self.check_mod_updates(manual=True))
        m.add_separator()
        m.add_command(label="Umbenennen", command=lambda: self.rename(g))
        m.add_command(label="Cover-Bild wählen (PNG)", command=lambda: self.set_cover(g))
        m.add_command(label="Start-Datei ändern / Pfad neu verknüpfen", command=lambda: self.relink(g))
        m.add_command(label="Startparameter", command=lambda: self.set_args(g))
        m.add_command(label="Spielordner öffnen", command=lambda: self.open_folder(g))
        m.add_separator()
        if os.path.isdir(self._groot(g)):
            m.add_command(label="💾 Komprimieren (bleibt spielbar)", command=lambda: self.compress(g))
            m.add_command(label="📦 Archivieren & Ordner löschen (Platz frei machen)", command=lambda: self.archive_and_delete(g))
        elif self.archive_of(g):
            m.add_command(label="⬇ Installieren (aus Archiv)", command=lambda: self.install(g))
        m.add_separator()
        m.add_command(label="Aus Bibliothek entfernen", command=lambda: self.remove(g))
        m.tk_popup(event.x_root, event.y_root)

    # ---------- Mod-Updates
    def check_mod_updates(self, manual=False):
        """Prueft beim Start und alle 6 Std., ob es neue Seamless-Co-op-Versionen gibt."""
        if not manual:
            self.after(6 * 3600 * 1000, self.check_mod_updates)
        todo = [(g, *mod_info(g)) for g in self.games if mod_info(g)[0] and os.path.exists(g["path"])]
        if not todo or getattr(self, "_mod_checking", False):
            if manual and not todo:
                self.notify("Keine Spiele mit updatebarem Co-op-Mod gefunden.", "#3a3a40", 8)
            return
        self._mod_checking = True
        if manual:
            self.notify("Suche nach Mod-Updates …", ACCENT, 30)

        def work():
            results, errors, releases = [], [], {}
            for g, prefix, mod_dir in todo:
                repo = MOD_REPOS[prefix]
                try:
                    if repo not in releases:
                        rel = latest_release(repo)
                        releases[repo] = (rel, release_zip(repo, rel) if rel else None)
                    rel, zpath = releases[repo]
                    if rel:
                        ok = mod_up_to_date(prefix, mod_dir, zpath)
                        results.append((g, rel["tag"], ok))
                except Exception as ex:
                    errors.append(f"{g['name']}: {ex}")
            self.after(0, done, results, errors)

        def done(results, errors):
            self._mod_checking = False
            ups = []
            for g, tag, ok in results:
                if ok:
                    g["mod_version"], g["mod_update"] = tag, ""
                else:
                    g["mod_update"] = tag
                    ups.append(f"{g['name']} → {g.get('mod', 'Mod')} {tag}")
            save_library(self.games)
            self.render()
            if ups:
                self.notify("⬆ Mod-Update verfügbar: " + ", ".join(ups), "#ff9f0a", 20)
            elif manual:
                self.notify("Alle Co-op-Mods sind aktuell." if not errors else
                            "Update-Prüfung fehlgeschlagen (Internet?): " + errors[0], OK if not errors else DANGER, 12)
        threading.Thread(target=work, daemon=True).start()

    def update_mod(self, g):
        prefix, mod_dir = mod_info(g)
        if not prefix:
            return
        if any(norm(os.path.dirname(p)) == norm(mod_dir) for p in self.running) or g["path"] in self.running:
            messagebox.showwarning("Mod-Update", "Bitte zuerst das Spiel schließen.")
            return
        tag = g.get("mod_update")
        if not messagebox.askyesno("Mod-Update", f"{g.get('mod') or 'Mod'} für „{g['name']}“ auf {tag} aktualisieren?\n\n"
                                   "• Quelle: offizielles GitHub-Release des Mod-Autors\n"
                                   "• Deine alten Mod-Dateien werden vorher gesichert\n"
                                   "• Deine Einstellungen (z. B. Co-op-Passwort) bleiben erhalten\n\n"
                                   "Wichtig: Alle Mitspieler brauchen dieselbe Version!"):
            return
        root = self._groot(g)
        self.busy[root] = "MOD WIRD AKTUALISIERT …"
        self.render()

        def work():
            try:
                repo = MOD_REPOS[prefix]
                rel = latest_release(repo)
                zpath = release_zip(repo, rel)
                backup = apply_mod_update(prefix, mod_dir, zpath)
                if not mod_up_to_date(prefix, mod_dir, zpath):
                    raise RuntimeError("Nach dem Update stimmt die Mod-Datei nicht. Sicherung: " + backup)

                def ok():
                    g["mod_version"], g["mod_update"] = rel["tag"], ""
                    save_library(self.games)
                    self.notify(f"✔ {g.get('mod') or 'Mod'} für „{g['name']}“ ist jetzt {rel['tag']}. "
                                f"Sicherung der alten Version: {os.path.basename(backup)}", OK, 15)
                self.after(0, ok)
            except Exception as ex:
                self.after(0, messagebox.showerror, "Mod-Update", f"Update fehlgeschlagen:\n{ex}")
            finally:
                self._set_busy(root, None)
        threading.Thread(target=work, daemon=True).start()

    # ---------- Platz sparen / neu installieren
    def _groot(self, g):
        return g.get("root") or os.path.dirname(g.get("original") or g["path"])

    def archive_of(self, g):
        a = g.get("archive")
        if a and os.path.exists(a):
            return a
        return find_archive(self._groot(g), self.settings["watched"])

    def _set_busy(self, root, text):
        def apply():
            if text is None:
                if self.busy.pop(root, None) is not None:
                    self.render()
                return
            if self.busy.get(root) == text:
                return
            had = root in self.busy
            self.busy[root] = text
            btn = self.busy_btns.get(root)
            if had and btn is not None and btn.winfo_exists():
                btn.configure(text=text)  # nur Knopftext aendern -> kein Neuaufbau, kein Flackern
            else:
                self.render()
        self.after(0, apply)

    def compress(self, g):
        root = self._groot(g)
        if not messagebox.askyesno("Komprimieren", f"„{g['name']}“ mit Windows-Komprimierung (LZX) verkleinern?\n\n"
                                   "• Das Spiel bleibt ganz normal spielbar\n• Spart meist 20–50 % Platz\n"
                                   "• Kann bei großen Spielen einige Minuten dauern"):
            return
        before = folder_size(root)
        self.busy[root] = "KOMPRIMIERE …"
        self.render()

        def work():
            subprocess.run(["compact", "/c", "/s", "/i", "/q", "/exe:lzx", root + "\\*"],
                           cwd=root, capture_output=True, creationflags=NOWIN)
            after = disk_size(root)
            self._set_busy(root, None)
            self.after(0, self.notify, f"„{g['name']}“ komprimiert: {gb(before)} → {gb(after)} "
                                       f"({gb(max(0, before - after))} gespart)", OK, 15)
        threading.Thread(target=work, daemon=True).start()

    def archive_and_delete(self, g):
        root = self._groot(g)
        archive = self.archive_of(g)
        size = folder_size(root)
        if archive:
            plan = f"Es gibt schon ein Archiv:\n{archive}\n\nDer Spielordner ({gb(size)}) wird gelöscht. " \
                   "Spielstände, die IM Spielordner liegen, wären danach weg."
        else:
            if not RAR_EXE:
                messagebox.showerror("Archivieren", "Dafür wird WinRAR benötigt.")
                return
            adir = os.path.join(os.path.dirname(root), "Archive (rar)")
            archive = os.path.join(adir, os.path.basename(root) + ".rar")
            plan = f"Der Spielordner ({gb(size)}) wird gepackt nach:\n{archive}\n\n" \
                   "Erst wenn das Archiv fertig und geprüft ist, wird der Ordner gelöscht."
        if not messagebox.askyesno("Archivieren & löschen",
                                   f"„{g['name']}“\n\n{plan}\n\nDas Spiel bleibt im Launcher (mit Cover und Spielzeit) "
                                   "und kann jederzeit mit „Installieren“ zurückgeholt werden.\n\nFortfahren?",
                                   icon="warning"):
            return
        existed = os.path.exists(archive)
        self.busy[root] = "PACKE …"
        self.render()

        def work():
            try:
                if not existed:
                    os.makedirs(os.path.dirname(archive), exist_ok=True)
                    tmp = archive + ".part.rar"
                    code = run_with_progress([RAR_EXE, "a", "-r", "-ep1", "-m3", "-y", tmp, root],
                                             lambda p: self._set_busy(root, f"PACKE {p} %"))
                    if code != 0:
                        raise RuntimeError(f"WinRAR-Fehler beim Packen (Code {code}).")
                    self._set_busy(root, "PRÜFE ARCHIV …")
                    if subprocess.run([RAR_EXE, "t", "-y", tmp], capture_output=True, creationflags=NOWIN).returncode != 0:
                        raise RuntimeError("Das Archiv ist fehlerhaft. Der Spielordner wurde NICHT gelöscht.")
                    os.replace(tmp, archive)
                self._set_busy(root, "LÖSCHE ORDNER …")
                import shutil
                shutil.rmtree(root)
                g["archive"] = archive
                save_library(self.games)
                self.after(0, self.notify, f"„{g['name']}“ archiviert – {gb(size)} frei. "
                                           "Mit „Installieren“ holst du es zurück.", OK, 15)
            except Exception as ex:
                self.after(0, messagebox.showerror, "Archivieren", str(ex))
            finally:
                self._set_busy(root, None)
        threading.Thread(target=work, daemon=True).start()

    def install(self, g):
        archive = self.archive_of(g)
        if not archive:
            messagebox.showwarning("Installieren", "Kein Archiv für dieses Spiel gefunden.")
            return
        root = self._groot(g)
        parent = os.path.dirname(root) if g.get("root") else os.path.dirname(os.path.dirname(archive))
        is_rar = archive.lower().endswith(".rar")
        if is_rar and not UNRAR_EXE:
            messagebox.showerror("Installieren", "Zum Entpacken wird WinRAR benötigt.")
            return
        top = archive_top_folder(archive)
        dest = parent if top else root
        new_root = os.path.join(parent, top) if top else root
        self.busy[root] = "ENTPACKE …"
        self.busy[new_root] = "ENTPACKE …"
        self.render()

        def work():
            try:
                os.makedirs(dest, exist_ok=True)
                if is_rar:
                    code = run_with_progress([UNRAR_EXE, "x", "-o+", "-y", archive, dest + "\\"],
                                             lambda p: self._set_busy(root, f"ENTPACKE {p} %"))
                else:
                    code = subprocess.run(["tar", "-xf", archive, "-C", dest], creationflags=NOWIN).returncode
                if code != 0:
                    raise RuntimeError(f"Entpacken fehlgeschlagen (Code {code}). Ist genug Speicherplatz frei?")
                info = analyze_game_folder(new_root)
                if not info:
                    raise RuntimeError("Entpackt, aber keine Spiel-EXE gefunden. Rechtsklick → „Start-Datei ändern“.")

                def apply():
                    g.update(root=new_root, path=info["path"], original=info["original"] or info["path"],
                             mod=info["mod"] or "", workdir="", archive=archive)
                    save_library(self.games)
                    self.notify(f"„{g['name']}“ ist installiert – viel Spaß!", OK, 12)
                self.after(0, apply)
            except Exception as ex:
                self.after(0, messagebox.showerror, "Installieren", str(ex))
            finally:
                self._set_busy(new_root, None)
                self._set_busy(root, None)
        threading.Thread(target=work, daemon=True).start()

    def rename(self, g):
        n = simpledialog.askstring("Umbenennen", "Neuer Name:", initialvalue=g["name"], parent=self)
        if n and n.strip():
            g["name"] = n.strip()
            save_library(self.games)
            self.render()

    def set_cover(self, g):
        p = filedialog.askopenfilename(title="Cover-Bild (Hochformat, PNG)", filetypes=[("Bilder", "*.png *.gif")])
        if not p:
            return
        try:
            tk.PhotoImage(file=p)
        except tk.TclError:
            messagebox.showerror("Bild", "Dieses Bild kann nicht gelesen werden. Bitte PNG oder GIF verwenden.")
            return
        os.makedirs(COVER_DIR, exist_ok=True)
        dest = os.path.join(COVER_DIR, f"{int(time.time() * 1000)}{os.path.splitext(p)[1].lower()}")
        with open(p, "rb") as src, open(dest, "wb") as dst:
            dst.write(src.read())
        g["cover"] = dest
        save_library(self.games)
        self.render()

    def relink(self, g):
        start = os.path.dirname(g["path"]) if os.path.isdir(os.path.dirname(g["path"])) else None
        p = filedialog.askopenfilename(title=f"Start-Datei für {g['name']}", initialdir=start,
                                       filetypes=[("Programme", "*.exe *.bat")])
        if p:
            p = os.path.normpath(p)
            g["path"], g["workdir"] = p, ""
            low = os.path.basename(p).lower()
            g["mod"] = next((label for pat, label in MOD_LAUNCHERS if re.search(pat, low)), "")
            if not g["mod"]:
                g["original"] = p
            save_library(self.games)
            self.render()

    def set_args(self, g):
        a = simpledialog.askstring("Startparameter", "Parameter (z. B. -windowed):", initialvalue=g.get("args", ""), parent=self)
        if a is not None:
            g["args"] = a.strip()
            save_library(self.games)

    def open_folder(self, g):
        d = g.get("root") or os.path.dirname(g["path"])
        if os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showwarning("Ordner", "Ordner existiert nicht mehr.")

    def remove(self, g):
        if messagebox.askyesno("Entfernen", f"„{g['name']}“ aus der Bibliothek entfernen?\n(Das Spiel selbst wird NICHT gelöscht.)"):
            self.games.remove(g)
            if g.get("root"):
                self.settings["ignored"].append(norm(g["root"]))  # nicht automatisch wieder hinzufuegen
                self.save_settings()
            save_library(self.games)
            self.render()


class WatchedDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Überwachte Ordner")
        self.configure(bg=PANEL)
        self.geometry("560x360")
        self.transient(app)
        tk.Label(self, text="Neue Spiele in diesen Ordnern werden automatisch hinzugefügt",
                 bg=PANEL, fg=TEXT, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=16, pady=(14, 8))
        self.lb = tk.Listbox(self, bg=CARD, fg=TEXT, selectbackground=ACCENT, relief="flat", font=("Segoe UI", 10))
        self.lb.pack(fill="both", expand=True, padx=16)
        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x", padx=16, pady=12)
        for text, col, cmd in (("Ordner hinzufügen", ACCENT, self.add), ("Entfernen", "#444", self.rem),
                               ("Jetzt scannen", OK, lambda: app.auto_scan(manual=True))):
            b = tk.Label(row, text=text, bg=col, fg="white", font=("Segoe UI", 10, "bold"), padx=12, pady=6, cursor="hand2")
            b.pack(side="left", padx=(0, 8))
            b.bind("<Button-1>", lambda e, c=cmd: c())
        self.refresh()

    def refresh(self):
        self.lb.delete(0, "end")
        for f in self.app.settings["watched"]:
            self.lb.insert("end", f)

    def add(self):
        self.app.scan_folder()
        self.refresh()

    def rem(self):
        sel = self.lb.curselection()
        if sel:
            del self.app.settings["watched"][sel[0]]
            self.app.save_settings()
            self.app.render()
            self.refresh()


class ImportDialog(tk.Toplevel):
    def __init__(self, app, links):
        super().__init__(app)
        self.app, self.links = app, links
        self.title("Desktop-Verknüpfungen importieren")
        self.configure(bg=PANEL)
        self.geometry("560x520")
        self.transient(app)
        self.grab_set()
        tk.Label(self, text="Welche Verknüpfungen sind Spiele?", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        box = tk.Frame(self, bg=PANEL)
        box.pack(fill="both", expand=True, padx=16)
        cv = tk.Canvas(box, bg=PANEL, highlightthickness=0)
        sb = tk.Scrollbar(box, command=cv.yview)
        inner = tk.Frame(cv, bg=PANEL)
        cv.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(fill="both", expand=True)
        self.vars = []
        for l in links:
            v = tk.BooleanVar(value=False)
            self.vars.append(v)
            tk.Checkbutton(inner, text=f"{l['name']}\n   {l['target']}", variable=v, justify="left", anchor="w",
                           bg=PANEL, fg=TEXT, selectcolor=CARD, activebackground=PANEL, activeforeground=TEXT,
                           font=("Segoe UI", 9)).pack(fill="x", anchor="w", pady=2)
        self.delete = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="Danach diese Verknüpfungen vom Desktop löschen (Spiele bleiben im Launcher)",
                       variable=self.delete, bg=PANEL, fg=TEXT, selectcolor=CARD, activebackground=PANEL,
                       activeforeground=TEXT, font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=8)
        b = tk.Label(self, text="Importieren", bg=ACCENT, fg="white", font=("Segoe UI", 10, "bold"), pady=7, cursor="hand2")
        b.pack(fill="x", padx=16, pady=(0, 14))
        b.bind("<Button-1>", lambda e: self.ok())

    def ok(self):
        chosen = [l for l, v in zip(self.links, self.vars) if v.get()]
        delete = self.delete.get()
        self.destroy()
        if chosen:
            self.app.finish_import(chosen, delete)


if __name__ == "__main__":
    try:  # eigenes Taskleisten-Symbol statt Python-Logo
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GameLauncher.L")
    except Exception:
        pass
    Launcher().mainloop()
