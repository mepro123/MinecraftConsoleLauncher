#!/usr/bin/env python3
"""
Offline Minecraft Launcher (CLI, no GUI)
=========================================

A self-contained launcher that:
  - Downloads and installs vanilla Minecraft versions from Mojang's public
    version manifest
  - Installs Fabric loader (via the official Fabric Meta API)
  - Installs Forge loader (via the official Forge installer, 1.13+ only)
  - Launches the game with an "offline" account (a local username, no
    Microsoft/Mojang login) -- this is the same offline mode Minecraft
    itself has always supported for LAN play / self-hosted servers.

Everything is stored under ./mc_data next to this script, so it never
touches your real .minecraft folder.

USAGE
-----
    python launcher.py list
        List available vanilla RELEASE versions only.

    python launcher.py list snapshots
        List available SNAPSHOT versions only.

    python launcher.py list release snapshots
        List both, merged together in release-time order.

    python launcher.py install 1.20.4
        Download and install vanilla 1.20.4.

    python launcher.py install 1.20.4 --loader fabric
        Install vanilla 1.20.4 + latest Fabric loader.

    python launcher.py install 1.20.4 --loader forge
        Install vanilla 1.20.4 + recommended Forge (1.13+ only).

    python launcher.py install 26.3-snapshot-1 --name Snapshot
        Snapshot ids (old-style like 24w14a, or new-style like
        26.3-snapshot-1) install exactly like any other version id.
        Aliases latest / latest-release / snapshot / latest-snapshot
        also work anywhere a version id is accepted.

    python launcher.py installed
        Show everything you've installed and their launch IDs.

    python launcher.py launch 1.20.4 --username Steve
        Launch a specific installed version/profile.

    python launcher.py launch 1.20.4 --username Steve --ram 4096

REQUIREMENTS
------------
    - Python 3.8+
    - A working Java install matching the Minecraft version's requirement
      (Java is NOT downloaded by this script -- install it yourself and
      make sure `java` is on PATH, or pass --java "C:\\path\\to\\java.exe")
"""

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------
# Auto-install required Python packages on first run
# --------------------------------------------------------------------------

REQUIRED_PACKAGES = ["requests", "colorama"]


def _ensure_dependencies():
    missing = []
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return

    print(f"[launcher] installing missing Python package(s): {', '.join(missing)}")
    base_cmd = [sys.executable, "-m", "pip", "install", *missing]
    try:
        subprocess.check_call(base_cmd)
    except subprocess.CalledProcessError:
        # Some environments (managed Linux distros) refuse global pip installs.
        for extra in (["--user"], ["--break-system-packages"]):
            try:
                subprocess.check_call(base_cmd + extra)
                break
            except subprocess.CalledProcessError:
                continue
        else:
            print(
                "[launcher] ERROR: couldn't auto-install required packages. "
                f"Please run manually: pip install {' '.join(missing)}",
                file=sys.stderr,
            )
            sys.exit(1)

    # re-check import now that install finished
    for pkg in missing:
        importlib.import_module(pkg)


_ensure_dependencies()

import requests  # noqa: E402  (must come after the auto-install step above)
import colorama  # noqa: E402

try:
    # Directly flips Windows' console into ANSI mode via the Win32 API.
    # More reliable across different Windows terminal setups than the
    # older stream-wrapping approach.
    colorama.just_fix_windows_console()
except AttributeError:
    # Older colorama versions don't have this -- fall back to the classic init.
    colorama.init(autoreset=True)


class C:
    """Short color codes for readable/skimmable terminal output."""
    RESET = colorama.Style.RESET_ALL
    BOLD = colorama.Style.BRIGHT
    RED = colorama.Fore.RED
    GREEN = colorama.Fore.GREEN
    YELLOW = colorama.Fore.YELLOW
    CYAN = colorama.Fore.CYAN
    MAGENTA = colorama.Fore.MAGENTA
    GRAY = colorama.Fore.LIGHTBLACK_EX
from requests.adapters import HTTPAdapter  # noqa: E402
from urllib3.util.retry import Retry  # noqa: E402
import time  # noqa: E402

# A shared session with automatic retries for flaky connections (SSL resets,
# timeouts, 5xx from the CDN, etc.) -- large asset downloads (thousands of
# small files) hit these occasionally and shouldn't kill the whole install.
_session = requests.Session()
_retry = Retry(
    total=6,
    connect=6,
    read=6,
    backoff_factor=0.8,
    status_forcelist=[429, 500, 502, 503, 504],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry, pool_maxsize=20))
_session.mount("http://", HTTPAdapter(max_retries=_retry, pool_maxsize=20))

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent / "mc_data"
VERSIONS_DIR = BASE_DIR / "versions"
LIBRARIES_DIR = BASE_DIR / "libraries"
ASSETS_DIR = BASE_DIR / "assets"
NATIVES_ROOT = BASE_DIR / "natives"
GAME_DIR = BASE_DIR / "game"
INSTANCES_DIR = BASE_DIR / "instances"

for _d in (VERSIONS_DIR, LIBRARIES_DIR, ASSETS_DIR, NATIVES_ROOT, GAME_DIR, INSTANCES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

VERSION_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META = "https://meta.fabricmc.net/v2"
FORGE_PROMOTIONS_URL = "https://maven.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_INSTALLER_URL = (
    "https://maven.minecraftforge.net/net/minecraftforge/forge/"
    "{mc}-{forge}/forge-{mc}-{forge}-installer.jar"
)
MODRINTH_API = "https://api.modrinth.com/v2"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def log(msg):
    print(f"{C.CYAN}[launcher]{C.RESET} {msg}")


def warn(msg):
    print(f"{C.YELLOW}[launcher] warning:{C.RESET} {C.YELLOW}{msg}{C.RESET}")


def success(msg):
    print(f"{C.GREEN}{C.BOLD}{msg}{C.RESET}")


def die(msg):
    print(f"{C.RED}{C.BOLD}[launcher] ERROR:{C.RESET} {C.RED}{msg}{C.RESET}", file=sys.stderr)
    sys.exit(1)


def get_json(url):
    try:
        r = _session.get(url, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        die(f"Failed to fetch {url}: {e}")


def sha1_of(path: Path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest: Path, sha1=None, quiet=False, required=True):
    """Download url -> dest, skipping if the file already exists and matches sha1.
    Retries a few times on connection/SSL hiccups. If required=False, returns
    False on ultimate failure instead of aborting the whole program."""
    if dest.exists():
        if sha1 is None or sha1_of(dest) == sha1:
            return True

    dest.parent.mkdir(parents=True, exist_ok=True)
    if not quiet:
        log(f"downloading {dest.name}")
    tmp = dest.with_suffix(dest.suffix + ".part")

    attempts = 4
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            with _session.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
            if sha1 and sha1_of(tmp) != sha1:
                raise IOError("checksum mismatch after download")
            tmp.replace(dest)
            return True
        except (requests.RequestException, IOError) as e:
            last_err = e
            tmp.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(1.5 * attempt)  # backoff before retrying

    if required:
        die(f"Failed to download {url} after {attempts} attempts: {last_err}")
    else:
        if not quiet:
            warn(f"giving up on {dest.name} after {attempts} attempts: {last_err}")
        return False


def current_os():
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s.startswith("darwin"):
        return "osx"
    return "linux"


def current_arch():
    m = platform.machine().lower()
    return "x64" if m in ("amd64", "x86_64") else ("arm64" if "arm" in m or "aarch64" in m else "x86")


def classpath_sep():
    return ";" if current_os() == "windows" else ":"


def find_java_silent(explicit=None):
    """Like find_java, but returns None instead of dying when nothing is found."""
    if explicit and explicit != "java":
        p = Path(explicit)
        return str(p) if p.exists() else None

    found = shutil.which("java")
    if found:
        return found

    if current_os() == "windows":
        candidates = []
        # Official Minecraft Launcher's bundled runtimes
        for base in (
            Path("C:/Program Files (x86)/Minecraft Launcher/runtime"),
            Path("C:/Program Files/Minecraft Launcher/runtime"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Packages",
        ):
            if base.exists():
                candidates += list(base.glob("**/javaw.exe"))
                candidates += list(base.glob("**/java.exe"))
        # Common JDK/JRE install roots
        for base in (
            Path("C:/Program Files/Java"),
            Path("C:/Program Files/Eclipse Adoptium"),
            Path("C:/Program Files (x86)/Eclipse Adoptium"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Eclipse Adoptium",
        ):
            if base.exists():
                candidates += list(base.glob("*/bin/java.exe"))
        # A JRE this script auto-downloaded previously
        auto_dir = BASE_DIR / "java"
        if auto_dir.exists():
            candidates += list(auto_dir.glob("**/java.exe"))

        if candidates:
            return str(max(candidates, key=lambda p: p.stat().st_mtime))

    return None


def get_java_major_version(java_path):
    """Runs `java -version` and parses the major version number, or None if
    it can't be determined."""
    try:
        result = subprocess.run(
            [java_path, "-version"], capture_output=True, text=True, timeout=10
        )
        output = (result.stderr or "") + (result.stdout or "")
        m = re.search(r'version "(\d+)(?:\.(\d+))?', output)
        if not m:
            return None
        major = int(m.group(1))
        if major == 1 and m.group(2):  # old scheme: "1.8.0_401" == Java 8
            major = int(m.group(2))
        return major
    except Exception:
        return None


ADOPTIUM_URL = (
    "https://api.adoptium.net/v3/binary/latest/{feature}/ga/{os}/{arch}"
    "/jre/hotspot/normal/eclipse"
)


def _adoptium_os():
    return {"windows": "windows", "osx": "mac", "linux": "linux"}[current_os()]


def _adoptium_arch():
    return {"x64": "x64", "arm64": "aarch64", "x86": "x86-32"}.get(current_arch(), "x64")


def download_jre(major_version=21):
    """Downloads and unpacks a portable Eclipse Temurin (Adoptium) JRE of the
    given major version into mc_data/java/, and returns the path to the java
    executable inside it. Cached across runs."""
    exe_name = "java.exe" if current_os() == "windows" else "java"
    extract_dir = BASE_DIR / "java" / f"jre{major_version}"

    if extract_dir.exists():
        existing = list(extract_dir.rglob(exe_name))
        if existing:
            return str(existing[0])

    os_name = _adoptium_os()
    arch = _adoptium_arch()
    url = ADOPTIUM_URL.format(feature=major_version, os=os_name, arch=arch)
    archive_ext = "zip" if os_name == "windows" else "tar.gz"
    archive_path = BASE_DIR / "tmp" / f"jre{major_version}-{os_name}-{arch}.{archive_ext}"

    log(f"no suitable Java found -- downloading Eclipse Temurin JRE {major_version} "
        f"for {os_name}/{arch} (one-time, ~40-180MB)...")
    download(url, archive_path)

    extract_dir.mkdir(parents=True, exist_ok=True)
    log("unpacking Java runtime...")
    if archive_ext == "zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_dir)
    else:
        with tarfile.open(archive_path) as tf:
            tf.extractall(extract_dir)

    candidates = list(extract_dir.rglob(exe_name))
    if not candidates:
        die("Downloaded a Java runtime but couldn't find the java executable inside it.")
    java_path = candidates[0]
    if current_os() != "windows":
        os.chmod(java_path, 0o755)

    log(f"portable Java {major_version} ready at {java_path}")
    return str(java_path)


def resolve_java(explicit=None, required_major=None):
    """Finds a Java executable good enough to run the given required_major
    version, auto-downloading a matching portable JRE if nothing suitable
    is on the system."""
    candidate = find_java_silent(explicit)
    if candidate:
        if required_major:
            major = get_java_major_version(candidate)
            if major is None or major >= required_major:
                return candidate
            log(f"found Java {major} on this system, but this Minecraft version "
                f"needs Java {required_major}+ -- fetching a matching runtime instead.")
        else:
            return candidate
    else:
        log("no Java installation found on this system.")

    return download_jre(required_major or 21)


# --------------------------------------------------------------------------
# Rule evaluation (used for library "rules" and argument "rules")
# --------------------------------------------------------------------------

def rules_allow(rules, features=None):
    """Evaluate a Mojang-style 'rules' list. features is a dict of feature
    flags we support (we support none, so any rule requiring a feature is
    treated as not applicable / disallowed)."""
    if not rules:
        return True
    features = features or {}
    allowed = False
    for rule in rules:
        action = rule.get("action", "allow") == "allow"
        matches = True
        if "os" in rule:
            os_rule = rule["os"]
            if "name" in os_rule and os_rule["name"] != current_os():
                matches = False
            if "arch" in os_rule and os_rule["arch"] != current_arch():
                matches = False
        if "features" in rule:
            for feat_name, feat_val in rule["features"].items():
                if features.get(feat_name) != feat_val:
                    matches = False
        if matches:
            allowed = action
    return allowed


# --------------------------------------------------------------------------
# Vanilla version manifest / version json
# --------------------------------------------------------------------------

def fetch_manifest():
    return get_json(VERSION_MANIFEST_URL)


def find_version_entry(manifest, version_id):
    for v in manifest["versions"]:
        if v["id"] == version_id:
            return v
    return None


_LATEST_ALIASES = {
    "latest": "release",
    "latest-release": "release",
    "release": "release",
    "latest-snapshot": "snapshot",
    "snapshot": "snapshot",
}


def resolve_version_id(version, manifest):
    """Resolves friendly aliases ('latest', 'latest-release', 'snapshot',
    'latest-snapshot') to a real version id using manifest['latest'].
    Any other string is returned unchanged (assumed to already be a real
    version id, e.g. '1.21.11' or a snapshot id like '24w14a')."""
    key = _LATEST_ALIASES.get(version.strip().lower())
    if key is None:
        return version
    resolved = manifest["latest"][key]
    log(f"'{version}' -> resolved to {C.GREEN}{resolved}{C.RESET}")
    return resolved


def fetch_version_json(version_id, manifest=None):
    """Get (and cache) the raw version json for a vanilla version id."""
    dest = VERSIONS_DIR / version_id / f"{version_id}.json"
    if dest.exists():
        return json.loads(dest.read_text())

    manifest = manifest or fetch_manifest()
    entry = find_version_entry(manifest, version_id)
    if entry is None:
        die(f"Unknown version '{version_id}'. Run 'list' to see options.")

    data = get_json(entry["url"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data))
    return data


def resolve_inherits(version_json):
    """If this version json 'inheritsFrom' a parent (Fabric/Forge profiles
    do this), merge libraries/arguments/mainClass with the parent."""
    if "inheritsFrom" not in version_json:
        return version_json

    parent_id = version_json["inheritsFrom"]
    parent = fetch_version_json(parent_id)
    parent = resolve_inherits(parent)  # in case of multi-level inheritance

    merged = dict(parent)
    merged["id"] = version_json.get("id", parent["id"])
    if "mainClass" in version_json:
        merged["mainClass"] = version_json["mainClass"]

    merged["libraries"] = version_json.get("libraries", []) + parent.get("libraries", [])

    # merge "arguments" (new format) or "minecraftArguments" (old format)
    if "arguments" in version_json or "arguments" in parent:
        merged_args = {"game": [], "jvm": []}
        for src in (parent, version_json):
            args = src.get("arguments", {})
            merged_args["game"] += args.get("game", [])
            merged_args["jvm"] += args.get("jvm", [])
        merged["arguments"] = merged_args
        merged.pop("minecraftArguments", None)
    elif "minecraftArguments" in parent:
        merged["minecraftArguments"] = parent["minecraftArguments"]

    # keep parent's asset index / downloads / client jar info
    for key in ("assetIndex", "assets", "downloads", "javaVersion"):
        if key in parent and key not in version_json:
            merged[key] = parent[key]

    return merged


# --------------------------------------------------------------------------
# Downloading the client jar, libraries, natives, assets
# --------------------------------------------------------------------------

def download_client_jar(version_json, version_id):
    downloads = version_json.get("downloads", {})
    client = downloads.get("client")
    dest = VERSIONS_DIR / version_id / f"{version_id}.jar"
    if client:
        download(client["url"], dest, sha1=client.get("sha1"))
    elif not dest.exists():
        die(f"No client jar info for {version_id} and none cached.")
    return dest


def library_target_path(lib):
    """Path a library artifact should be stored at, relative to LIBRARIES_DIR."""
    artifact = lib.get("downloads", {}).get("artifact")
    if artifact and "path" in artifact:
        return LIBRARIES_DIR / artifact["path"]
    # Fall back to Maven-style path built from the "name" (group:artifact:version)
    group, artifact_id, ver = lib["name"].split(":")[:3]
    fname = f"{artifact_id}-{ver}.jar"
    return LIBRARIES_DIR / Path(*group.split(".")) / artifact_id / ver / fname


def download_libraries(version_json, version_id):
    """Download all applicable libraries, and extract any natives.
    Returns (list_of_jar_paths, natives_dir)."""
    natives_dir = NATIVES_ROOT / version_id
    natives_dir.mkdir(parents=True, exist_ok=True)

    jar_paths = []
    for lib in version_json.get("libraries", []):
        if not rules_allow(lib.get("rules")):
            continue

        downloads = lib.get("downloads", {})
        artifact = downloads.get("artifact")

        if artifact and "url" in artifact:
            dest = library_target_path(lib)
            if download(artifact["url"], dest, sha1=artifact.get("sha1"), quiet=True, required=False):
                jar_paths.append(dest)
            else:
                warn(f"skipping library {lib.get('name', dest.name)} -- launch may fail")
        elif "url" in lib:
            # Old-style library entry (pre-modern manifest, e.g. some Forge libs)
            group, artifact_id, ver = lib["name"].split(":")[:3]
            fname = f"{artifact_id}-{ver}.jar"
            rel = Path(*group.split(".")) / artifact_id / ver / fname
            url = lib["url"].rstrip("/") + "/" + str(rel).replace(os.sep, "/")
            dest = LIBRARIES_DIR / rel
            try:
                download(url, dest, quiet=True)
                jar_paths.append(dest)
            except SystemExit:
                log(f"  (skipping unavailable library {lib['name']})")

        # Natives (classifier-based, legacy 'natives' field, LWJGL etc.)
        classifiers = downloads.get("classifiers", {})
        native_key = lib.get("natives", {}).get(current_os())
        if native_key and native_key in classifiers:
            nat = classifiers[native_key]
            nat_dest = LIBRARIES_DIR / nat["path"]
            download(nat["url"], nat_dest, sha1=nat.get("sha1"), quiet=True)
            _extract_natives(nat_dest, natives_dir, lib.get("extract", {}))

    return jar_paths, natives_dir


def _extract_natives(jar_path: Path, out_dir: Path, extract_rules):
    exclude = extract_rules.get("exclude", [])
    try:
        with zipfile.ZipFile(jar_path) as zf:
            for name in zf.namelist():
                if name.startswith("META-INF/"):
                    continue
                if any(name.startswith(ex) for ex in exclude):
                    continue
                if name.endswith("/"):
                    continue
                zf.extract(name, out_dir)
    except zipfile.BadZipFile:
        warn(f"{jar_path.name} is not a valid zip, skipping native extraction")


def download_assets(version_json):
    asset_index_info = version_json.get("assetIndex")
    if not asset_index_info:
        return version_json.get("assets", "legacy")

    index_id = asset_index_info["id"]
    index_path = ASSETS_DIR / "indexes" / f"{index_id}.json"
    download(asset_index_info["url"], index_path, sha1=asset_index_info.get("sha1"))
    index = json.loads(index_path.read_text())

    objects = index.get("objects", {})
    total = len(objects)
    log(f"downloading {total} asset objects (skips ones already cached)...")
    failed = []
    for i, (name, obj) in enumerate(objects.items(), 1):
        h = obj["hash"]
        sub = h[:2]
        dest = ASSETS_DIR / "objects" / sub / h
        url = f"https://resources.download.minecraft.net/{sub}/{h}"
        ok = download(url, dest, sha1=h, quiet=True, required=False)
        if not ok:
            failed.append(name)
        if i % 200 == 0 or i == total:
            fail_color = C.RED if failed else C.GREEN
            print(f"\r  assets: {C.CYAN}{i}/{total}{C.RESET} ({fail_color}{len(failed)} failed{C.RESET})", end="", flush=True)
    print()
    if failed:
        log(f"{len(failed)} asset(s) failed to download after retries -- "
            "just re-run 'install' to fill in the gaps (already-downloaded files are skipped).")

    # Legacy versions also want a "virtual" layout mirroring real file names
    if index.get("virtual") or index.get("map_to_resources"):
        virtual_dir = ASSETS_DIR / "virtual" / index_id
        for name, obj in objects.items():
            h = obj["hash"]
            src = ASSETS_DIR / "objects" / h[:2] / h
            dst = virtual_dir / name
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())

    return index_id


# --------------------------------------------------------------------------
# Offline account (no Microsoft/Mojang auth -- local username only)
# --------------------------------------------------------------------------

def offline_uuid(username: str) -> str:
    """Replicates Java's UUID.nameUUIDFromBytes(('OfflinePlayer:'+name).getBytes())
    which is what vanilla servers use to generate offline-mode UUIDs."""
    data = f"OfflinePlayer:{username}".encode("utf-8")
    md5_bytes = bytearray(hashlib.md5(data).digest())
    md5_bytes[6] = (md5_bytes[6] & 0x0F) | 0x30  # version 3
    md5_bytes[8] = (md5_bytes[8] & 0x3F) | 0x80  # variant
    return str(uuid.UUID(bytes=bytes(md5_bytes)))


# --------------------------------------------------------------------------
# Building and running the launch command
# --------------------------------------------------------------------------

def build_classpath(lib_jars, client_jar):
    parts = [str(p) for p in lib_jars if p.exists()] + [str(client_jar)]
    return classpath_sep().join(parts)


def substitute(value, tokens):
    for k, v in tokens.items():
        value = value.replace("${" + k + "}", str(v))
    return value


def gather_args(version_json, tokens):
    jvm_args, game_args = [], []

    if "arguments" in version_json:
        for bucket, out in (("jvm", jvm_args), ("game", game_args)):
            for item in version_json["arguments"].get(bucket, []):
                if isinstance(item, str):
                    out.append(substitute(item, tokens))
                elif isinstance(item, dict):
                    if not rules_allow(item.get("rules")):
                        continue
                    val = item.get("value", [])
                    if isinstance(val, str):
                        val = [val]
                    out.extend(substitute(v, tokens) for v in val)
        if not jvm_args:  # some profiles omit jvm args entirely
            jvm_args = [
                f"-Djava.library.path={tokens['natives_directory']}",
                "-cp", tokens["classpath"],
            ]
    else:
        # old-style single string
        mc_args = version_json.get("minecraftArguments", "")
        game_args = [substitute(a, tokens) for a in mc_args.split()]
        jvm_args = [
            f"-Djava.library.path={tokens['natives_directory']}",
            "-cp", tokens["classpath"],
        ]

    return jvm_args, game_args


def launch(version_id, username, ram_mb, java_arg, game_dir=None):
    game_dir = game_dir or GAME_DIR
    version_json = resolve_inherits(fetch_version_json(version_id))

    required_major = version_json.get("javaVersion", {}).get("majorVersion")
    java_bin = resolve_java(java_arg, required_major)

    client_jar = download_client_jar(version_json, version_json.get("id", version_id))
    lib_jars, natives_dir = download_libraries(version_json, version_id)
    asset_index_id = download_assets(version_json)

    classpath = build_classpath(lib_jars, client_jar)
    player_uuid = offline_uuid(username)

    tokens = {
        "natives_directory": str(natives_dir),
        "launcher_name": "offline-launcher",
        "launcher_version": "1.0",
        "classpath": classpath,
        "classpath_separator": classpath_sep(),
        "library_directory": str(LIBRARIES_DIR),
        "auth_player_name": username,
        "version_name": version_id,
        "game_directory": str(game_dir),
        "assets_root": str(ASSETS_DIR),
        "game_assets": str(ASSETS_DIR / "virtual" / asset_index_id),
        "assets_index_name": asset_index_id,
        "auth_uuid": player_uuid,
        "auth_access_token": "0",
        "clientid": "0",
        "auth_xuid": "0",
        "user_type": "legacy",
        "version_type": version_json.get("type", "release"),
        "user_properties": "{}",
        "resolution_width": "925",
        "resolution_height": "530",
    }

    jvm_args, game_args = gather_args(version_json, tokens)
    main_class = version_json["mainClass"]

    cmd = [java_bin, f"-Xmx{ram_mb}M", f"-Xms{min(ram_mb, 1024)}M"]
    cmd += jvm_args
    cmd += [main_class]
    cmd += game_args

    success(f"launching {version_id} as '{username}'")
    log(f"game directory: {game_dir}")
    Path(game_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, cwd=str(game_dir))


# --------------------------------------------------------------------------
# Fabric install
# --------------------------------------------------------------------------

def install_fabric(mc_version, loader_version=None):
    if loader_version is None:
        loaders = get_json(f"{FABRIC_META}/versions/loader/{mc_version}")
        if not loaders:
            die(f"No Fabric loader available for Minecraft {mc_version}")
        loader_version = loaders[0]["loader"]["version"]
        log(f"using latest Fabric loader {loader_version}")

    profile = get_json(f"{FABRIC_META}/versions/loader/{mc_version}/{loader_version}/profile/json")
    version_id = profile["id"]  # e.g. "fabric-loader-0.15.7-1.20.4"

    dest = VERSIONS_DIR / version_id / f"{version_id}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(profile))

    # make sure the vanilla parent is present too
    fetch_version_json(mc_version)

    log(f"installed Fabric as version id: {version_id}")
    return version_id


# --------------------------------------------------------------------------
# Forge install (1.13+ only -- uses the official installer jar)
# --------------------------------------------------------------------------

def get_recommended_forge(mc_version):
    promos = get_json(FORGE_PROMOTIONS_URL)["promos"]
    for key in (f"{mc_version}-recommended", f"{mc_version}-latest"):
        if key in promos:
            return promos[key]
    die(f"No Forge build found for Minecraft {mc_version}")


def install_forge(mc_version, forge_version, java_arg):
    if forge_version is None:
        forge_version = get_recommended_forge(mc_version)
        log(f"using recommended Forge {forge_version} for {mc_version}")

    vanilla_json = fetch_version_json(mc_version)
    required_major = vanilla_json.get("javaVersion", {}).get("majorVersion")
    java_bin = resolve_java(java_arg, required_major)

    installer_url = FORGE_INSTALLER_URL.format(mc=mc_version, forge=forge_version)
    installer_path = BASE_DIR / "tmp" / f"forge-{mc_version}-{forge_version}-installer.jar"
    download(installer_url, installer_path)

    log("running Forge installer headlessly (--installClient)...")
    log("(this may take a minute; it downloads its own libraries)")
    result = subprocess.run(
        [java_bin, "-jar", str(installer_path), "--installClient", str(BASE_DIR)],
        cwd=str(BASE_DIR),
    )
    if result.returncode != 0:
        die(
            "Forge installer failed. Note: this script only supports the "
            "modern installer used by Minecraft 1.13+. Older Forge versions "
            "patch the client jar directly and aren't supported here."
        )

    # Find whatever new version folder the installer created
    candidates = [
        p.name for p in VERSIONS_DIR.iterdir()
        if p.is_dir() and mc_version in p.name and "forge" in p.name.lower()
    ]
    if not candidates:
        die("Forge installed but couldn't detect the resulting version id -- "
            f"check {VERSIONS_DIR} manually.")
    version_id = sorted(candidates)[-1]
    log(f"installed Forge as version id: {version_id}")
    return version_id


# --------------------------------------------------------------------------
# Named installations ("instances") -- each gets its own isolated
# saves/mods/config folder, and a friendly name instead of a raw version id.
# --------------------------------------------------------------------------

def instance_dir(name):
    return INSTANCES_DIR / name


def instance_game_dir(name):
    return instance_dir(name) / "game"


def instance_mods_dir(name):
    return instance_game_dir(name) / "mods"


def instance_config_path(name):
    return instance_dir(name) / "instance.json"


def save_instance(name, version_id, mc_version, loader):
    instance_mods_dir(name).mkdir(parents=True, exist_ok=True)
    existing = load_instance(name) or {}
    cfg = {
        "name": name,
        "version_id": version_id,
        "mc_version": mc_version,
        "loader": loader,
    }
    # keep previously saved preferences across a reinstall/upgrade of the same name
    for key in ("username", "ram"):
        if key in existing:
            cfg[key] = existing[key]
    instance_config_path(name).write_text(json.dumps(cfg, indent=2))
    return cfg


def update_instance(name, **kwargs):
    """Merge non-None kwargs into an instance's saved config and write it back."""
    inst = load_instance(name) or {"name": name}
    changed = False
    for k, v in kwargs.items():
        if v is not None and inst.get(k) != v:
            inst[k] = v
            changed = True
    if changed:
        instance_config_path(name).write_text(json.dumps(inst, indent=2))
    return inst


def load_instance(name):
    p = instance_config_path(name)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def list_instances():
    if not INSTANCES_DIR.exists():
        return []
    return sorted(
        p.name for p in INSTANCES_DIR.iterdir()
        if p.is_dir() and (p / "instance.json").exists()
    )


# --------------------------------------------------------------------------
# Mod installation from Modrinth / CurseForge URLs
# --------------------------------------------------------------------------

def install_mod_from_url(url, mc_version, loader, mods_dir):
    mods_dir.mkdir(parents=True, exist_ok=True)
    if "modrinth.com" in url:
        return install_mod_modrinth(url, mc_version, loader, mods_dir)
    elif "curseforge.com" in url:
        return install_mod_curseforge(url, mc_version, loader, mods_dir)
    elif url.lower().endswith(".jar"):
        filename = url.split("/")[-1].split("?")[0]
        dest = mods_dir / filename
        download(url, dest)
        log(f"installed mod: {filename}")
        return dest
    else:
        die(
            "That doesn't look like a Modrinth or CurseForge mod page URL. "
            "Expected something like https://modrinth.com/mod/<slug> or "
            "https://www.curseforge.com/minecraft/mc-mods/<slug>"
        )


def install_mod_modrinth(url, mc_version, loader, mods_dir):
    # Direct CDN file link -- just download it as-is.
    if "cdn.modrinth.com" in url:
        filename = url.split("/")[-1].split("?")[0]
        dest = mods_dir / filename
        download(url, dest)
        log(f"installed mod: {filename}")
        return dest

    m = re.search(r"modrinth\.com/(?:mod|plugin|datapack)/([^/?#]+)(?:/version/([^/?#]+))?", url)
    if not m:
        die(f"Couldn't parse Modrinth URL: {url}")
    slug, version_ref = m.group(1), m.group(2)

    versions = get_json(f"{MODRINTH_API}/project/{slug}/version")
    if not versions:
        die(f"No versions found for Modrinth project '{slug}'")

    chosen = None
    if version_ref:
        for v in versions:
            if version_ref in (v.get("version_number"), v.get("id")):
                chosen = v
                break
        if not chosen:
            die(f"Couldn't find version '{version_ref}' for '{slug}' on Modrinth")
    else:
        # Modrinth returns versions newest-first; narrow to this instance's
        # game version + loader if we know them, else just take the latest.
        filtered = [
            v for v in versions
            if (not mc_version or mc_version in v.get("game_versions", []))
            and (not loader or loader == "vanilla" or loader in v.get("loaders", []))
        ]
        chosen = (filtered or versions)[0]
        if not filtered:
            warn(f"no version explicitly matching mc {mc_version}/{loader} -- "
                f"grabbing the newest available instead: {chosen.get('version_number')}")

    file_info = next((f for f in chosen["files"] if f.get("primary")), chosen["files"][0])
    dest = mods_dir / file_info["filename"]
    download(file_info["url"], dest, sha1=file_info.get("hashes", {}).get("sha1"))
    log(f"installed mod: {file_info['filename']} (Modrinth {chosen.get('version_number', '')})")
    return dest


def install_mod_curseforge(url, mc_version, loader, mods_dir):
    """Uses the unofficial cfwidget.com mirror since CurseForge's real API
    requires a developer API key we don't have. Best-effort: if this mirror
    is down or rate-limited, Modrinth is the more reliable option."""
    m = re.search(
        r"curseforge\.com/minecraft/(mc-mods|modpacks|resourcepacks|shaderpacks|worlds)/([^/?#]+)",
        url,
    )
    if not m:
        die(f"Couldn't parse CurseForge URL: {url}")
    category, slug = m.group(1), m.group(2)

    api_url = f"https://api.cfwidget.com/minecraft/{category}/{slug}"
    try:
        r = _session.get(api_url, timeout=30)
    except requests.RequestException as e:
        die(f"Failed to reach the CurseForge mirror API: {e}")

    if r.status_code == 202:
        die(
            "The unofficial CurseForge mirror is indexing this mod for the first time. "
            "Wait ~15 seconds and run the same command again."
        )
    if r.status_code == 404:
        die(f"CurseForge project not found: {category}/{slug}")
    r.raise_for_status()
    data = r.json()

    files = data.get("files", [])
    matches = [f for f in files if not mc_version or mc_version in f.get("versions", [])]
    chosen = (matches or files or [None])[-1]

    if chosen:
        file_url, filename = chosen["url"], chosen.get("name") or chosen["url"].split("/")[-1]
    else:
        fallback = data.get("download")
        if not fallback:
            die(f"No downloadable files found for CurseForge project '{slug}'")
        file_url = fallback["url"]
        filename = fallback.get("name") or file_url.split("/")[-1]

    dest = mods_dir / filename
    download(file_url, dest)
    log(f"installed mod: {filename} (CurseForge via unofficial mirror)")
    return dest


# --------------------------------------------------------------------------
# Modpack installation (Modrinth .mrpack format)
# --------------------------------------------------------------------------

def _mc_version_key(v):
    """Sortable key for a Minecraft version string, e.g. '1.21.11' -> (1, 21, 11).
    Non-numeric/snapshot-style strings (e.g. '24w14a') sort below real releases."""
    nums = re.findall(r"\d+", v or "")
    return (1, tuple(int(n) for n in nums)) if nums else (0, ())


def _pack_best_game_version(entry):
    """The highest Minecraft version a modpack version entry targets."""
    gvs = entry.get("game_versions") or ["0"]
    return max(gvs, key=_mc_version_key)


def install_modpack_modrinth(url, name, java_arg, version_override=None):
    """Installs a full Modrinth modpack (.mrpack) as a new named installation:
    resolves the pack, installs the correct Minecraft + loader version,
    downloads every mod it references, and copies in its config/overrides."""

    m = re.search(r"modrinth\.com/modpack/([^/?#]+)(?:/version/([^/?#]+))?", url)
    if m:
        slug, url_version_ref = m.group(1), m.group(2)
        version_ref = version_override or url_version_ref
        versions = get_json(f"{MODRINTH_API}/project/{slug}/version")
        if not versions:
            die(f"No versions found for Modrinth modpack '{slug}'")
        if version_ref:
            chosen = next(
                (v for v in versions if version_ref in (v.get("version_number"), v.get("id"))), None
            )
            if not chosen:
                available = ", ".join(v["version_number"] for v in versions[:10])
                die(f"Couldn't find version '{version_ref}' for modpack '{slug}'. "
                    f"Available: {available}{' ...' if len(versions) > 10 else ''} "
                    f"(run 'modpack versions {url}' to see them all)")
        else:
            chosen = max(
                versions,
                key=lambda v: (_mc_version_key(_pack_best_game_version(v)), v.get("date_published", "")),
            )
            log(f"no --version given -- using {chosen['version_number']} "
                f"(targets the newest available game version: {_pack_best_game_version(chosen)})")
        file_info = next((f for f in chosen["files"] if f.get("primary")), chosen["files"][0])
        mrpack_url = file_info["url"]
        mrpack_filename = file_info["filename"]
    elif url.lower().endswith(".mrpack") or "cdn.modrinth.com" in url:
        if version_override:
            warn("--version is ignored for direct .mrpack links (the file itself is already a specific version)")
        mrpack_url = url
        mrpack_filename = url.split("/")[-1].split("?")[0]
    else:
        die(f"Couldn't parse Modrinth modpack URL: {url}")

    tmp_pack = BASE_DIR / "tmp" / mrpack_filename
    log(f"downloading modpack: {mrpack_filename}")
    download(mrpack_url, tmp_pack)

    with zipfile.ZipFile(tmp_pack) as zf:
        index = json.loads(zf.read("modrinth.index.json"))

    deps = index.get("dependencies", {})
    mc_version = deps.get("minecraft")
    if not mc_version:
        die("This modpack's index is missing a Minecraft version -- can't continue.")

    if "fabric-loader" in deps:
        loader, loader_version = "fabric", deps["fabric-loader"]
    elif "forge" in deps:
        loader, loader_version = "forge", deps["forge"]
    elif "quilt-loader" in deps:
        die("This modpack uses Quilt, which isn't supported yet (only Fabric and Forge).")
    else:
        loader, loader_version = "vanilla", None

    log(f"modpack needs Minecraft {mc_version}" + (f" + {loader} {loader_version}" if loader_version else f" ({loader})"))

    version_json = fetch_version_json(mc_version)
    download_client_jar(version_json, mc_version)
    download_libraries(version_json, mc_version)
    download_assets(version_json)

    if loader == "fabric":
        version_id = install_fabric(mc_version, loader_version)
    elif loader == "forge":
        version_id = install_forge(mc_version, loader_version, java_arg)
    else:
        version_id = mc_version

    save_instance(name, version_id, mc_version, loader)
    game_dir = instance_game_dir(name)
    game_dir.mkdir(parents=True, exist_ok=True)

    files = index.get("files", [])
    log(f"downloading {len(files)} modpack files (mods, resourcepacks, etc.)...")
    failed = []
    for i, f in enumerate(files, 1):
        if f.get("env", {}).get("client") == "unsupported":
            continue  # server-only file
        dest = game_dir / f["path"]
        sha1 = f.get("hashes", {}).get("sha1")
        ok = False
        for u in f.get("downloads", []):
            if download(u, dest, sha1=sha1, quiet=True, required=False):
                ok = True
                break
        if not ok:
            failed.append(f["path"])
        if i % 20 == 0 or i == len(files):
            fail_color = C.RED if failed else C.GREEN
            print(f"\r  files: {C.CYAN}{i}/{len(files)}{C.RESET} ({fail_color}{len(failed)} failed{C.RESET})", end="", flush=True)
    print()
    if failed:
        log(f"{len(failed)} file(s) failed to download -- you may need to add them manually.")

    # Overrides (configs, resourcepacks, etc.) get copied straight into the game dir;
    # client-overrides take priority when both exist.
    with zipfile.ZipFile(tmp_pack) as zf:
        for override_folder in ("overrides", "client-overrides"):
            prefix = override_folder + "/"
            for member in zf.namelist():
                if member.startswith(prefix) and not member.endswith("/"):
                    rel = member[len(prefix):]
                    dest = game_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(dest, "wb") as out:
                        out.write(src.read())

    success(f"\nModpack installed as '{name}'! Launch with:")
    print(f"  {C.CYAN}python launcher.py launch {name} --username <yourname>{C.RESET}")


def cmd_modpack_install(args):
    if "modrinth.com" in args.url or args.url.lower().endswith(".mrpack"):
        install_modpack_modrinth(args.url, args.name, args.java, version_override=args.version)
    elif "curseforge.com" in args.url:
        die(
            "CurseForge modpacks aren't supported -- installing one means resolving dozens of "
            "individual mod files, and CurseForge requires a paid developer API key for that "
            "(their public/free access doesn't cover it). Modrinth modpacks are fully supported "
            "and don't have this restriction; check if this pack (or an equivalent) is on Modrinth."
        )
    else:
        die("Expected a Modrinth modpack URL, e.g. https://modrinth.com/modpack/<slug>, "
            "or a direct .mrpack link.")


def cmd_modpack_versions(args):
    m = re.search(r"modrinth\.com/modpack/([^/?#]+)", args.url)
    if not m:
        die("Expected a Modrinth modpack URL, e.g. https://modrinth.com/modpack/<slug>")
    slug = m.group(1)
    versions = get_json(f"{MODRINTH_API}/project/{slug}/version")
    if not versions:
        die(f"No versions found for modpack '{slug}'")

    auto_pick = max(
        versions,
        key=lambda v: (_mc_version_key(_pack_best_game_version(v)), v.get("date_published", "")),
    )

    print(f"{C.BOLD}Versions for '{slug}'{C.RESET} (newest first):")
    for v in versions:
        gv = ", ".join(v.get("game_versions", []))
        loaders = ", ".join(v.get("loaders", []))
        date = v.get("date_published", "")[:10]
        marker = f"  {C.YELLOW}<- default (newest game version){C.RESET}" if v is auto_pick else ""
        print(f"  {C.CYAN}{v['version_number']:<15}{C.RESET} "
              f"mc=[{C.GREEN}{gv}{C.RESET}] loader=[{C.MAGENTA}{loaders}{C.RESET}] {C.GRAY}{date}{C.RESET}{marker}")

    print(f"\nInstall a specific one with:")
    print(f"  {C.CYAN}python launcher.py modpack install {args.url} --version <version> --name <name>{C.RESET}")


# --------------------------------------------------------------------------
# CLI commands
# --------------------------------------------------------------------------

_LIST_KIND_MAP = {
    "release": "release",
    "releases": "release",
    "version": "release",
    "versions": "release",
    "snapshot": "snapshot",
    "snapshots": "snapshot",
}


def cmd_list(args):
    manifest = fetch_manifest()

    kinds = {_LIST_KIND_MAP[k] for k in args.kinds}
    if args.all:
        kinds |= {"release", "snapshot"}
    if not kinds:
        kinds = {"release"}  # default: releases only

    # manifest["versions"] already comes back newest-first, so filtering by
    # type (rather than re-sorting) keeps both kinds merged in release-time
    # order when both are requested.
    versions = [v for v in manifest["versions"] if v["type"] in kinds]

    for v in versions[:args.count]:
        type_color = {"release": C.GREEN, "snapshot": C.YELLOW}.get(v["type"], C.MAGENTA)
        print(f"{C.CYAN}{v['id']:<16}{C.RESET} {type_color}{v['type']:<10}{C.RESET} {C.GRAY}{v['releaseTime'][:10]}{C.RESET}")


def cmd_installed(args):
    instances = list_instances()
    if instances:
        print(f"{C.BOLD}Installations{C.RESET} (use these names with 'launch' and 'mods'):")
        for name in instances:
            inst = load_instance(name)
            loader_color = {"fabric": C.MAGENTA, "forge": C.YELLOW}.get(inst["loader"], C.GRAY)
            print(f"  {C.CYAN}{C.BOLD}{name:<20}{C.RESET} "
                  f"loader={loader_color}{inst['loader']:<8}{C.RESET} "
                  f"mc={C.GREEN}{inst['mc_version']:<10}{C.RESET} -> {C.GRAY}{inst['version_id']}{C.RESET}")
    else:
        print(f"{C.GRAY}No named installations yet. Create one with 'install <version> --name <name>'.{C.RESET}")

    raw = [p.name for p in VERSIONS_DIR.iterdir()] if VERSIONS_DIR.exists() else []
    if raw:
        print(f"\n{C.BOLD}Raw downloaded version files{C.RESET} (usable directly with 'launch' too):")
        for name in sorted(raw):
            print(f"  {C.GRAY}{name}{C.RESET}")


def cmd_install(args):
    manifest = fetch_manifest()
    mc_version = resolve_version_id(args.version, manifest)
    fetch_version_json(mc_version, manifest)
    log(f"vanilla {mc_version} manifest ready")

    version_json = fetch_version_json(mc_version)
    download_client_jar(version_json, mc_version)
    download_libraries(version_json, mc_version)
    if not args.skip_assets:
        download_assets(version_json)
    else:
        log("skipping asset download (--skip-assets)")

    if args.loader == "fabric":
        version_id = install_fabric(mc_version, args.loader_version)
    elif args.loader == "forge":
        version_id = install_forge(mc_version, args.loader_version, args.java)
    else:
        version_id = mc_version

    name = args.name or version_id
    save_instance(name, version_id, mc_version, args.loader)

    success(f"\nInstalled as '{name}'! Launch with:")
    print(f"  {C.CYAN}python launcher.py launch {name} --username <yourname>{C.RESET}")
    if args.loader != "vanilla":
        print("Add mods with:")
        print(f"  {C.CYAN}python launcher.py mods add {name} <modrinth-or-curseforge-url>{C.RESET}")


def cmd_launch(args):
    inst = load_instance(args.name)
    if inst:
        version_id = inst["version_id"]
        game_dir = instance_game_dir(args.name)

        username = args.username or inst.get("username")
        if not username:
            die(f"No username saved for '{args.name}' yet -- run once with --username <name> "
                f"and it'll be remembered automatically after that.")

        if args.ram is not None:
            ram = args.ram
        elif "ram" in inst:
            ram = inst["ram"]
        else:
            ram = 4096
            log(f"no RAM setting saved yet for '{args.name}' -- defaulting to 4096MB (4GB) and remembering it.")

        update_instance(args.name, username=username, ram=ram)

    elif (VERSIONS_DIR / args.name).exists():
        # backwards-compatible: launching a raw version id directly, no saved config
        version_id = args.name
        game_dir = GAME_DIR
        username = args.username
        if not username:
            die("--username is required when launching a raw version id "
                "(no saved settings for these -- use a named installation for that).")
        ram = args.ram if args.ram is not None else 4096
    else:
        die(f"'{args.name}' isn't a known installation. Run 'installed' to see options, "
            f"or 'install <version> --name {args.name}' to create it.")

    launch(version_id, username, ram, args.java, game_dir)


def cmd_mods_add(args):
    inst = load_instance(args.name)
    if not inst:
        die(f"No installation named '{args.name}'. Run 'installed' to see options, "
            f"or create one with 'install <version> --loader fabric --name {args.name}'.")
    if inst["loader"] not in ("fabric", "forge"):
        warn("this is a vanilla installation (no mod loader) -- "
            "most mods won't load without Fabric or Forge. Continuing anyway.")
    install_mod_from_url(args.url, inst["mc_version"], inst["loader"], instance_mods_dir(args.name))


def cmd_mods_list(args):
    if not load_instance(args.name):
        die(f"No installation named '{args.name}'.")
    mods_dir = instance_mods_dir(args.name)
    files = sorted(p.name for p in mods_dir.glob("*.jar")) if mods_dir.exists() else []
    if not files:
        print(f"{C.GRAY}No mods installed.{C.RESET}")
        return
    for f in files:
        print(f"{C.CYAN}{f}{C.RESET}")


def cmd_mods_remove(args):
    if not load_instance(args.name):
        die(f"No installation named '{args.name}'.")
    target = instance_mods_dir(args.name) / args.filename
    if not target.exists():
        die(f"No such mod file: {args.filename} (see 'mods list {args.name}')")
    target.unlink()
    success(f"removed {args.filename}")


def cmd_config(args):
    inst = load_instance(args.name)
    if not inst:
        die(f"No installation named '{args.name}'. Run 'installed' to see options.")

    if args.username is not None or args.ram is not None:
        inst = update_instance(args.name, username=args.username, ram=args.ram)
        success("saved.")

    print(f"{C.BOLD}{C.CYAN}[{args.name}]{C.RESET}")
    username_display = inst.get("username") or f"{C.GRAY}(not set -- pass --username on next launch){C.RESET}"
    print(f"  username: {username_display}")
    ram_display = f"{C.GREEN}{inst['ram']}MB{C.RESET}" if "ram" in inst else f"{C.GRAY}(not set -- defaults to 4096MB on first launch){C.RESET}"
    print(f"  ram:      {ram_display}")
    print(f"  loader:   {C.MAGENTA}{inst['loader']}{C.RESET}")
    print(f"  mc:       {C.GREEN}{inst['mc_version']}{C.RESET}")


def cmd_delete(args):
    inst = load_instance(args.name)
    if not inst:
        die(f"No installation named '{args.name}'. Run 'installed' to see options.")

    if not args.yes:
        answer = input(
            f"{C.YELLOW}Delete installation '{args.name}' (loader={inst['loader']}, mc={inst['mc_version']})? "
            f"This removes its saves, mods, and settings permanently. Type 'yes' to confirm: {C.RESET}"
        )
        if answer.strip().lower() != "yes":
            print(f"{C.GRAY}Cancelled -- nothing was deleted.{C.RESET}")
            return

    shutil.rmtree(instance_dir(args.name))
    success(f"deleted installation '{args.name}'.")
    log("(shared downloaded files in mc_data/versions, libraries, and assets were kept -- "
        "other installations may still be using them)")


_HELP_TEXT_RAW = """\
Offline Minecraft Launcher -- quick reference
==============================================

1) See what versions exist:
     python launcher.py list                        (releases only)
     python launcher.py list snapshots               (snapshots only)
     python launcher.py list release snapshots       (both, merged by release time)

2) Install a version as a named installation:
     python launcher.py install 1.21.11 --name Vanilla
     python launcher.py install 1.21.11 --loader fabric --name MyFabric
     python launcher.py install 1.21.11 --loader forge --name MyForge
     python launcher.py install 26.3-snapshot-1 --name Snapshot
   (--name is optional; if you skip it, the version id itself is used as
   the name. Works for any version id from 'list', release or snapshot.)

3) See what's installed:
     python launcher.py installed

4) Launch it:
     python launcher.py launch MyFabric --username Steve
   The first time, --username is required and gets saved. RAM defaults
   to 4096MB (4GB) automatically on first launch. After that, just:
     python launcher.py launch MyFabric
   and it reuses the saved username/RAM -- no flags needed.

   To change RAM or username later, either pass the flag again on launch
   (it updates the saved value), or use:
     python launcher.py config MyFabric --ram 6144
     python launcher.py config MyFabric --username NewName
     python launcher.py config MyFabric              (just view current settings)

5) Install mods (Fabric/Forge installations only) from a Modrinth or
   CurseForge mod page URL:
     python launcher.py mods add MyFabric https://modrinth.com/mod/sodium
     python launcher.py mods add MyFabric https://www.curseforge.com/minecraft/mc-mods/jei
     python launcher.py mods list MyFabric
     python launcher.py mods remove MyFabric sodium-fabric-0.5.8.jar

6) Delete an installation you don't want anymore (asks for confirmation):
     python launcher.py delete MyFabric
     python launcher.py delete MyFabric --yes     (skip the confirmation prompt)
   This removes that installation's saves/mods/settings, but keeps the
   shared downloaded game files (versions/libraries/assets) since other
   installations may still be using them.

7) Install a full modpack (Minecraft version + loader + all mods +
   configs, all in one go) as a brand new named installation:
     python launcher.py modpack install https://modrinth.com/modpack/fabulously-optimized --name MyPack
   By default it grabs the latest version. To pick a specific one:
     python launcher.py modpack versions https://modrinth.com/modpack/fabulously-optimized
     python launcher.py modpack install https://modrinth.com/modpack/fabulously-optimized --version 6.4.2 --name MyPack
   This works for Modrinth modpacks (.mrpack). CurseForge modpacks aren't
   supported -- resolving their individual mod files needs a paid
   CurseForge developer API key. If a pack (or a close equivalent) is
   also on Modrinth, use that link instead.

Each named installation has its own isolated saves/mods/config folder
under mc_data/instances/<name>/game/, so different modpacks never
collide with each other.

Notes:
  - Java is auto-detected, and auto-downloaded (a portable Eclipse
    Temurin JRE) if nothing suitable is found -- no manual setup needed.
  - CurseForge mod installs use an unofficial mirror API (CurseForge's
    real API needs a developer key we don't have). Modrinth is more
    reliable if a CurseForge link ever fails.
  - Pass --java "C:\\path\\to\\java.exe" to any install/launch command to
    force a specific Java install instead of auto-detecting.
"""


def cmd_help(args=None):
    for i, line in enumerate(_HELP_TEXT_RAW.rstrip("\n").split("\n")):
        stripped = line.strip()
        if i == 0:
            print(f"{C.BOLD}{C.CYAN}{line}{C.RESET}")
        elif set(stripped) == {"="}:
            print(f"{C.BOLD}{C.CYAN}{line}{C.RESET}")
        elif re.match(r"^\d+\)", stripped):
            print(f"{C.BOLD}{C.YELLOW}{line}{C.RESET}")
        elif stripped == "Notes:":
            print(f"{C.BOLD}{line}{C.RESET}")
        elif "python launcher.py" in line:
            print(f"{C.CYAN}{line}{C.RESET}")
        elif stripped.startswith("- "):
            print(f"{C.GRAY}{line}{C.RESET}")
        else:
            print(line)


def build_parser():
    p = argparse.ArgumentParser(description="Offline Minecraft Launcher", add_help=True)
    sub = p.add_subparsers(dest="command")

    p_help = sub.add_parser("help", help="show detailed usage examples")
    p_help.set_defaults(func=cmd_help)

    p_list = sub.add_parser("list", help="list available vanilla versions")
    p_list.add_argument("kinds", nargs="*", choices=list(_LIST_KIND_MAP.keys()),
                         help="what to list: 'release'/'versions' (default), 'snapshots', or both "
                              "(e.g. 'list release snapshots' shows both, merged by release time)")
    p_list.add_argument("--all", action="store_true", help="include both releases and snapshots")
    p_list.add_argument("--count", type=int, default=25)
    p_list.set_defaults(func=cmd_list)

    p_installed = sub.add_parser("installed", help="show installed versions/installations")
    p_installed.set_defaults(func=cmd_installed)

    p_install = sub.add_parser("install", help="install a version as a named installation")
    p_install.add_argument("version", help="Minecraft version id, e.g. 1.21.11 or 24w14a, "
                            "or an alias: latest / latest-release / latest-snapshot")
    p_install.add_argument("--loader", choices=["vanilla", "fabric", "forge"], default="vanilla")
    p_install.add_argument("--loader-version", default=None,
                            help="specific Fabric/Forge version (default: latest/recommended)")
    p_install.add_argument("--name", default=None,
                            help="friendly name for this installation (default: the version id)")
    p_install.add_argument("--skip-assets", action="store_true",
                            help="skip downloading sound/texture assets (faster, but game may complain)")
    p_install.add_argument("--java", default=None,
                            help="path to java executable (for Forge installer); auto-detected/auto-installed if omitted")
    p_install.set_defaults(func=cmd_install)

    p_launch = sub.add_parser("launch", help="launch an installation")
    p_launch.add_argument("name", help="installation name (from 'installed'), or a raw version id")
    p_launch.add_argument("--username", default=None,
                           help="offline account username (only needed the first time; saved after that)")
    p_launch.add_argument("--ram", type=int, default=None,
                           help="max RAM in MB (defaults to 4096 on first launch, then remembered)")
    p_launch.add_argument("--java", default=None,
                           help="path to java executable; auto-detected/auto-installed if omitted")
    p_launch.set_defaults(func=cmd_launch)

    p_mods = sub.add_parser("mods", help="manage mods for an installation")
    mods_sub = p_mods.add_subparsers(dest="mods_command", required=True)

    p_mods_add = mods_sub.add_parser("add", help="install a mod from a Modrinth or CurseForge URL")
    p_mods_add.add_argument("name", help="installation name")
    p_mods_add.add_argument("url", help="Modrinth or CurseForge mod page URL")
    p_mods_add.set_defaults(func=cmd_mods_add)

    p_mods_list = mods_sub.add_parser("list", help="list mods installed for an installation")
    p_mods_list.add_argument("name")
    p_mods_list.set_defaults(func=cmd_mods_list)

    p_mods_remove = mods_sub.add_parser("remove", help="remove an installed mod file")
    p_mods_remove.add_argument("name")
    p_mods_remove.add_argument("filename")
    p_mods_remove.set_defaults(func=cmd_mods_remove)

    p_config = sub.add_parser("config", help="view or change saved username/RAM for an installation")
    p_config.add_argument("name")
    p_config.add_argument("--username", default=None)
    p_config.add_argument("--ram", type=int, default=None, help="RAM in MB")
    p_config.set_defaults(func=cmd_config)

    p_delete = sub.add_parser("delete", aliases=["remove"],
                               help="delete a named installation (saves, mods, settings)")
    p_delete.add_argument("name")
    p_delete.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    p_delete.set_defaults(func=cmd_delete)

    p_modpack = sub.add_parser("modpack", help="install a full modpack as a new named installation")
    modpack_sub = p_modpack.add_subparsers(dest="modpack_command", required=True)

    p_modpack_versions = modpack_sub.add_parser(
        "versions", help="list available versions of a Modrinth modpack"
    )
    p_modpack_versions.add_argument("url", help="Modrinth modpack page URL")
    p_modpack_versions.set_defaults(func=cmd_modpack_versions)

    p_modpack_install = modpack_sub.add_parser(
        "install", help="install a Modrinth modpack (.mrpack) as a new installation"
    )
    p_modpack_install.add_argument("url", help="Modrinth modpack page URL, or a direct .mrpack link")
    p_modpack_install.add_argument("--name", required=True, help="name for the new installation")
    p_modpack_install.add_argument("--version", default=None,
                                    help="specific modpack version (see 'modpack versions <url>'); default: latest")
    p_modpack_install.add_argument("--java", default=None,
                                    help="path to java executable; auto-detected/auto-installed if omitted")
    p_modpack_install.set_defaults(func=cmd_modpack_install)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        cmd_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
