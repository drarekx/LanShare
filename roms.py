"""Instalación de ROMs en EmuDeck y creación de accesos directos en Steam."""

import datetime as dt
import json
import os
import re
import shutil
import struct
import subprocess
import time
import zlib

HOME = os.path.expanduser("~")
STEAM = os.path.join(HOME, ".local/share/Steam")
SRM_CONFIG = os.path.join(HOME, ".config/steam-rom-manager/userData/userConfigurations.json")
RA_CORES = os.path.join(HOME, ".var/app/org.libretro.RetroArch/config/retroarch/cores")
BACKUPS = os.path.join(HOME, "Backups/LanShare")

# (carpeta dentro de roms, nombre, extensiones). Las extensiones sirven para detectar el sistema
# automáticamente cuando solo encajan con uno.
SYSTEMS = [
    ("nes", "Nintendo NES", ["nes", "fds", "unf", "unif", "zip", "7z"]),
    ("snes", "Super Nintendo", ["sfc", "smc", "fig", "swc", "bs", "zip", "7z"]),
    ("n64", "Nintendo 64", ["n64", "z64", "v64", "zip", "7z"]),
    ("gb", "Game Boy", ["gb", "dmg", "zip", "7z"]),
    ("gbc", "Game Boy Color", ["gbc", "zip", "7z"]),
    ("gba", "Game Boy Advance", ["gba", "zip", "7z"]),
    ("nds", "Nintendo DS", ["nds", "zip", "7z"]),
    ("n3ds", "Nintendo 3DS", ["3ds", "cci", "cia", "cxi", "3dsx"]),
    ("gamecube", "GameCube", ["gcm", "gcz", "rvz", "ciso", "iso"]),
    ("wii", "Wii", ["wbfs", "rvz", "iso", "wad"]),
    ("wiiu/roms", "Wii U", ["wua", "wux", "wud"]),
    ("switch", "Nintendo Switch", ["nsp", "xci"]),
    ("psx", "PlayStation", ["pbp", "m3u", "cue", "chd", "ecm", "bin", "img"]),
    ("ps2", "PlayStation 2", ["iso", "chd", "cso", "bin"]),
    ("psp", "PSP", ["iso", "cso", "pbp"]),
    ("genesis", "Mega Drive / Genesis", ["md", "gen", "smd", "bin", "zip", "7z"]),
    ("mastersystem", "Master System", ["sms", "zip", "7z"]),
    ("gamegear", "Game Gear", ["gg", "zip", "7z"]),
    ("segacd", "Mega CD", ["chd", "cue", "iso", "m3u"]),
    ("dreamcast", "Dreamcast", ["cdi", "gdi", "chd"]),
    ("saturn", "Saturn", ["cue", "chd", "iso", "m3u"]),
    ("fbneo", "Arcade (FBNeo)", ["zip", "7z"]),
    ("mame", "Arcade (MAME 2003 Plus)", ["zip"]),
]
SYSTEM_BY_FOLDER = {s[0]: s for s in SYSTEMS}
# Archivos que forman parte de un juego pero no se lanzan solos (pistas de un .cue)
TRACK_EXTS = {"bin", "img", "sub", "ccd", "raw"}
# Lanzadores de EmuDeck si el parser de SRM no sirve (por ejemplo, un Flatpak que no está instalado)
FALLBACK_LAUNCHERS = {
    "psx": "duckstation.sh", "ps2": "pcsx2-qt.sh", "psp": "ppsspp.sh", "gamecube": "dolphin-emu.sh",
    "wii": "dolphin-emu.sh", "n3ds": "azahar.sh", "switch": "ryujinx.sh", "wiiu/roms": "cemu.sh",
    "nds": "melonds.sh", "dreamcast": "flycast.sh",
}


# ---------------------------------------------------------------- EmuDeck

def emudeck_paths():
    """(romsPath, toolsPath) de ~/emudeck/settings.sh."""
    vals = {}
    try:
        with open(os.path.join(HOME, "emudeck/settings.sh")) as f:
            for line in f:
                m = re.match(r'^(romsPath|toolsPath)=(.*)$', line.strip())
                if m:
                    vals[m.group(1)] = m.group(2).replace('"', "")
    except OSError:
        pass
    return vals.get("romsPath"), vals.get("toolsPath")


def detect_system(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    matches = [s[0] for s in SYSTEMS if ext in s[2]]
    return matches[0] if len(matches) == 1 else None


def systems_info():
    roms, _ = emudeck_paths()
    return {"available": bool(roms and os.path.isdir(roms)), "romsPath": roms,
            "systems": [{"folder": f, "label": l, "exts": e} for f, l, e in SYSTEMS]}


def rom_path(folder, name):
    """Ruta segura dentro de `folder`. Si ya existe un ROM con ese nombre, se sustituye."""
    name = os.path.basename(name.replace("\\", "/")).strip().lstrip(".") or "rom"
    return os.path.join(folder, name)


def rom_folder(system):
    roms, _ = emudeck_paths()
    if not roms or system not in SYSTEM_BY_FOLDER:
        raise ValueError("Sistema desconocido o EmuDeck no configurado")
    folder = os.path.join(roms, system)
    os.makedirs(folder, exist_ok=True)
    return folder


# ---------------------------------------------------------------- comando de lanzamiento (desde SRM)

def _installed_flatpaks():
    try:
        out = subprocess.run(["flatpak", "list", "--app", "--columns=application"],
                             capture_output=True, text=True, timeout=30).stdout
        return set(out.split())
    except (OSError, subprocess.TimeoutExpired):
        return set()


def _resolve(text, values):
    """Sustituye las variables de Steam ROM Manager. Devuelve None si queda alguna sin resolver."""
    for k, v in values.items():  # primero las variables simples…
        text = text.replace("${" + k + "}", v)
    prev = None
    while prev != text:  # …y luego los bloques ${os:...} anidados, de dentro hacia fuera
        prev = text
        text = re.sub(r"\$\{os:(win|mac|linux)\|([^{}|]*)(?:\|([^{}]*))?\}",
                      lambda m: m.group(2) if m.group(1) == "linux" else (m.group(3) or ""), text)
    return None if "${" in text else text.strip()


def _parser_matches(p, system, ext):
    roms_rel = p.get("romDirectory", "").replace("${romsdirglobal}", "").replace("${/}", "/").strip("/")
    glob = p.get("parserInputs", {}).get("glob", "") or ""
    if roms_rel:
        if roms_rel.rstrip("/") != system:
            return False
    elif not re.search(r"(?<![a-z0-9])" + re.escape(system) + r"(?=[/,|)}])", glob):
        return False
    exts = re.findall(r"\.([A-Za-z0-9]+)", glob.split("${title}")[-1])
    return not exts or ext in {e.lower() for e in exts}


def launch_command(system, rom_path, _flatpaks=None):
    """(exe, start_dir, launch_options) para un ROM, o None si no hay forma conocida de lanzarlo."""
    ext = rom_path.rsplit(".", 1)[-1].lower()
    _, tools = emudeck_paths()
    flatpaks = _flatpaks if _flatpaks is not None else _installed_flatpaks()
    values = {"filePath": rom_path, "fileName": os.path.basename(rom_path),
              "title": os.path.splitext(os.path.basename(rom_path))[0], "/": "/", "racores": RA_CORES,
              "retroarchpath": "/usr/bin/flatpak", "romsdirglobal": emudeck_paths()[0] or "",
              "steamdirglobal": STEAM}
    try:
        with open(SRM_CONFIG) as f:
            parsers = json.load(f)
    except (OSError, ValueError):
        parsers = []
    for p in parsers:
        if p.get("disabled") or not _parser_matches(p, system, ext):
            continue
        exe = _resolve(p.get("executable", {}).get("path", ""), values)
        args = _resolve(p.get("executableArgs", ""), values)
        if not exe or args is None or not os.path.exists(exe):
            continue
        fp = re.match(r"run\s+(\S+)", args) if exe.endswith("/flatpak") else None
        if fp and fp.group(1) not in flatpaks:
            continue  # el Flatpak del emulador no está instalado
        core = re.search(r"-L\s+(\S+_libretro\.so)", args)
        if core and not os.path.exists(core.group(1)):
            continue  # falta el núcleo de RetroArch
        if args.startswith("run ") and not exe.endswith("/flatpak"):
            continue
        return f'"{exe}"', f'"{os.path.dirname(exe)}"', args
    launcher = FALLBACK_LAUNCHERS.get(system)
    if tools and launcher and os.path.exists(os.path.join(tools, "launchers", launcher)):
        exe = os.path.join(tools, "launchers", launcher)
        return f'"{exe}"', f'"{os.path.dirname(exe)}"', f'"{rom_path}"'
    return None


def clean_title(rom_path):
    """Nombre del juego sin extensión, regiones ni etiquetas: «Super Mario Bros. 3 (U) [T+Spa]» → «Super Mario Bros. 3»."""
    t = os.path.splitext(os.path.basename(rom_path))[0]
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t)
    return re.sub(r"\s+", " ", t.replace("_", " ")).strip() or os.path.basename(rom_path)


def needs_shortcut(rom_path):
    ext = rom_path.rsplit(".", 1)[-1].lower()
    if ext in TRACK_EXTS:
        stem = os.path.splitext(rom_path)[0]
        return not any(os.path.exists(stem + c) for c in (".cue", ".CUE", ".m3u", ".gdi"))
    return ext not in {"sub", "ccd"}


def build_shortcut(system, rom_path, flatpaks=None):
    cmd = launch_command(system, rom_path, flatpaks)
    if not cmd:
        return None
    exe, start, opts = cmd
    name = clean_title(rom_path)
    appid = zlib.crc32((exe + name).encode()) | 0x80000000
    label = SYSTEM_BY_FOLDER[system][1]
    return {"appid": appid, "appname": name, "exe": exe, "StartDir": start, "icon": "", "ShortcutPath": "",
            "LaunchOptions": opts, "IsHidden": 0, "AllowDesktopConfig": 1, "AllowOverlay": 1, "OpenVR": 0,
            "Devkit": 0, "DevkitGameID": "", "DevkitOverrideAppID": 0, "LastPlayTime": 0, "FlatpakAppID": "",
            "sortas": "", "tags": {"0": label}}


# ---------------------------------------------------------------- shortcuts.vdf (binario)

def vdf_parse(d, i=0):
    out = {}
    while True:
        t = d[i]
        i += 1
        if t == 8:
            return out, i
        j = d.index(b"\0", i)
        k = d[i:j].decode("utf-8", "replace")
        i = j + 1
        if t == 0:
            v, i = vdf_parse(d, i)
        elif t == 1:
            j = d.index(b"\0", i)
            v = d[i:j].decode("utf-8", "replace")
            i = j + 1
        elif t == 2:
            v = struct.unpack("<I", d[i:i + 4])[0]
            i += 4
        else:
            raise ValueError(f"tipo VDF desconocido {t}")
        out[k] = v


def vdf_dump(o):
    b = b""
    for k, v in o.items():
        kb = k.encode() + b"\0"
        if isinstance(v, dict):
            b += b"\0" + kb + vdf_dump(v) + b"\x08"
        elif isinstance(v, int):
            b += b"\x02" + kb + struct.pack("<I", v & 0xFFFFFFFF)
        else:
            b += b"\x01" + kb + v.encode() + b"\0"
    return b


def vdf_file(root):
    return vdf_dump(root) + b"\x08"  # cierre de la raíz: sin él Steam borra el archivo


def shortcut_files():
    base = os.path.join(STEAM, "userdata")
    return [os.path.join(base, u, "config/shortcuts.vdf") for u in os.listdir(base)
            if u.isdigit() and u != "0" and os.path.isdir(os.path.join(base, u, "config"))] if os.path.isdir(base) else []


def game_running():
    return subprocess.run(["pgrep", "-f", "SteamLaunch AppId="], capture_output=True).returncode == 0


def steam_running():
    return subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0


def add_to_steam(entries, log):
    """Cierra Steam, añade los accesos (sin duplicados) y vuelve a abrir Steam. Devuelve cuántos añadió."""
    if not entries:
        return 0
    if game_running():
        raise RuntimeError("Hay un juego abierto en Steam. Ciérralo y vuelve a intentarlo.")
    was_running = steam_running()
    if was_running:
        log("Cerrando Steam…")
        subprocess.Popen(["steam", "-shutdown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            if not steam_running():
                break
            time.sleep(1)
        else:
            raise RuntimeError("Steam no se ha cerrado. Ciérralo a mano y vuelve a intentarlo.")
        time.sleep(2)
    added = 0
    try:
        os.makedirs(BACKUPS, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        for path in shortcut_files():
            if os.path.exists(path):
                with open(path, "rb") as f:
                    data = f.read()
                root, end = vdf_parse(data)
                if vdf_file(root) != data or end != len(data):
                    raise RuntimeError(f"No entiendo el formato de {path}; no lo toco.")
                shutil.copy2(path, os.path.join(BACKUPS, f"shortcuts-{stamp}.vdf"))
            else:
                root = {"shortcuts": {}}
            sc = root.setdefault("shortcuts", {})
            def key(e):
                low = {k.lower(): v for k, v in e.items()}
                return str(low.get("exe", "")), str(low.get("launchoptions", ""))
            existing = {key(e) for e in sc.values()}
            for e in entries:
                if key(e) in existing:
                    log(f"«{e['appname']}» ya estaba en Steam")
                    continue
                sc[str(len(sc))] = e
                existing.add(key(e))
                added += 1
                log(f"✔ «{e['appname']}» añadido a Steam")
            out = vdf_file(root)
            check, end = vdf_parse(out)
            if end != len(out) or len(check["shortcuts"]) != len(sc):
                raise RuntimeError("La verificación del archivo de Steam ha fallado; no se ha guardado nada.")
            tmp = path + ".lanshare.tmp"
            with open(tmp, "wb") as f:
                f.write(out)
            os.replace(tmp, path)
        olds = sorted(os.listdir(BACKUPS))
        for old in olds[:-10]:
            os.remove(os.path.join(BACKUPS, old))
    finally:
        if was_running:
            log("Abriendo Steam de nuevo…")
            subprocess.Popen(["steam"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return added
