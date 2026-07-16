#!/usr/bin/env python3
"""
install.py, one-shot installer for Sunmi Print Hub.

Pulls the latest source from GitHub, compiles the debug APK, and installs it to the
device connected over adb. Just run it:

    python3 install.py

Options:
    --from-release   Skip compiling; download and install the latest release APK instead
                     (no Android SDK/JDK needed, only adb).
    --keep           Keep the build checkout afterwards (default keeps it too; this is a no-op
                     kept for clarity).

Prereqs to compile: git, a JDK (11 or 17), and the Android SDK (platform 31 + build-tools).
adb (ships in the SDK's platform-tools) is always needed to install.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request

REPO = "PzmuV1517/LocalPosPrinter"
REPO_URL = f"https://github.com/{REPO}.git"
BRANCH = "main"
GRADLE_TAG = "v7.5.0"  # matches android/gradle/wrapper/gradle-wrapper.properties (Gradle 7.5)

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(HERE, ".build", "LocalPosPrinter")
EXE = ".exe" if IS_WIN else ""


# --------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"\033[36m›\033[0m {msg}")


def die(msg: str) -> None:
    print(f"\033[31mERROR:\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd, cwd=None, env=None) -> None:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def have(name: str) -> str | None:
    return shutil.which(name)


def download(url: str, dest: str) -> None:
    log(f"downloading {os.path.basename(dest)}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


# --------------------------------------------------------------------------- adb
def find_adb(sdk: str | None) -> str:
    adb = have("adb")
    if adb:
        return adb
    if sdk:
        cand = os.path.join(sdk, "platform-tools", "adb" + EXE)
        if os.path.isfile(cand):
            return cand
    die("adb not found. Install Android platform-tools (or Android Studio) and re-run.")


def require_device(adb: str) -> None:
    out = subprocess.run([adb, "devices"], capture_output=True, text=True).stdout
    devices = [
        line.split("\t")[0]
        for line in out.splitlines()[1:]
        if line.strip() and line.split("\t")[-1] == "device"
    ]
    if not devices:
        die(
            "No authorized device connected.\n"
            "  • Enable Developer options → USB debugging on the Sunmi device\n"
            "  • Connect it (USB or `adb connect <ip>`) and approve the RSA prompt\n"
            "  • Verify with:  adb devices"
        )
    log(f"device(s): {', '.join(devices)}")


def adb_install(adb: str, apk: str) -> None:
    log("installing APK…")
    run([adb, "install", "-r", apk])


# --------------------------------------------------------------------------- source
def clone_or_update() -> None:
    if not have("git"):
        die("git not found. Install git and re-run.")
    if os.path.isdir(os.path.join(BUILD_DIR, ".git")):
        log("updating existing checkout to latest origin/" + BRANCH)
        run(["git", "-C", BUILD_DIR, "fetch", "--depth", "1", "origin", BRANCH])
        run(["git", "-C", BUILD_DIR, "reset", "--hard", f"origin/{BRANCH}"])
    else:
        log("cloning latest source from GitHub")
        os.makedirs(os.path.dirname(BUILD_DIR), exist_ok=True)
        run(["git", "clone", "--depth", "1", "--branch", BRANCH, REPO_URL, BUILD_DIR])


# --------------------------------------------------------------------------- sdk / gradle
def find_sdk() -> str | None:
    for env in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        p = os.environ.get(env)
        if p and os.path.isdir(p):
            return p
    home = os.path.expanduser("~")
    for c in (
        os.path.join(home, "Library", "Android", "sdk"),   # macOS
        os.path.join(home, "Android", "Sdk"),              # Linux
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"),  # Windows
    ):
        if c and os.path.isdir(c):
            return c
    return None


def write_local_properties(android_dir: str, sdk: str) -> None:
    with open(os.path.join(android_dir, "local.properties"), "w") as f:
        f.write("sdk.dir=" + sdk.replace("\\", "\\\\") + "\n")


def ensure_sdk_packages(sdk: str) -> None:
    """Best-effort: install platform 31 / build-tools if missing and sdkmanager is available."""
    need_platform = not os.path.isdir(os.path.join(sdk, "platforms", "android-31"))
    bt_dir = os.path.join(sdk, "build-tools")
    need_bt = not (os.path.isdir(bt_dir) and os.listdir(bt_dir))
    if not (need_platform or need_bt):
        return
    smgr = have("sdkmanager") or os.path.join(
        sdk, "cmdline-tools", "latest", "bin", "sdkmanager" + (".bat" if IS_WIN else "")
    )
    if not (os.path.isfile(smgr) or have("sdkmanager")):
        log("WARNING: SDK platform 31 / build-tools appear missing and sdkmanager was not found.")
        log("         Install them once via Android Studio (SDK Manager) or the build may fail.")
        return
    log("installing missing SDK packages (platform 31 / build-tools)…")
    pkgs = []
    if need_platform:
        pkgs.append("platforms;android-31")
    if need_bt:
        pkgs.append("build-tools;30.0.3")
    pkgs.append("platform-tools")
    subprocess.run([smgr, f"--sdk_root={sdk}", "--licenses"], input="y\n" * 50, text=True)
    run([smgr, f"--sdk_root={sdk}"] + pkgs)


def ensure_wrapper(android_dir: str) -> str:
    """Make sure the Gradle wrapper (script + jar) exists; download for Gradle 7.5 if not."""
    wdir = os.path.join(android_dir, "gradle", "wrapper")
    jar = os.path.join(wdir, "gradle-wrapper.jar")
    if not os.path.isfile(jar):
        download(
            f"https://raw.githubusercontent.com/gradle/gradle/{GRADLE_TAG}/gradle/wrapper/gradle-wrapper.jar",
            jar,
        )
    props = os.path.join(wdir, "gradle-wrapper.properties")
    if not os.path.isfile(props):
        with open(props, "w") as f:
            f.write(
                "distributionBase=GRADLE_USER_HOME\n"
                "distributionPath=wrapper/dists\n"
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-7.5-bin.zip\n"
                "zipStoreBase=GRADLE_USER_HOME\n"
                "zipStorePath=wrapper/dists\n"
            )
    script = "gradlew.bat" if IS_WIN else "gradlew"
    sp = os.path.join(android_dir, script)
    if not os.path.isfile(sp):
        download(f"https://raw.githubusercontent.com/gradle/gradle/{GRADLE_TAG}/{script}", sp)
    if not IS_WIN:
        os.chmod(sp, 0o755)
    return sp


def java_env() -> dict:
    env = dict(os.environ)
    if IS_MAC and "JAVA_HOME" not in env:
        for ver in ("17", "11"):
            try:
                jh = subprocess.check_output(
                    ["/usr/libexec/java_home", "-v", ver], text=True
                ).strip()
                if jh:
                    env["JAVA_HOME"] = jh
                    break
            except Exception:
                pass
    if not have("java") and "JAVA_HOME" not in env:
        die("No JDK found. Install a JDK 17 (e.g. `brew install --cask temurin@17`) and re-run.")
    return env


def compile_apk() -> str:
    android_dir = os.path.join(BUILD_DIR, "android")
    if not os.path.isdir(android_dir):
        die("android/ not found in the checkout, repo layout changed?")

    sdk = find_sdk()
    if not sdk:
        die(
            "Android SDK not found. Set ANDROID_HOME, or install the SDK "
            "(Android Studio, or command-line tools)."
        )
    log(f"Android SDK: {sdk}")
    write_local_properties(android_dir, sdk)
    ensure_sdk_packages(sdk)
    gradlew = ensure_wrapper(android_dir)

    log("compiling debug APK (first run downloads Gradle + dependencies, be patient)…")
    run([gradlew, ":app:assembleDebug", "--no-daemon"], cwd=android_dir, env=java_env())

    apk = os.path.join(android_dir, "app", "build", "outputs", "apk", "debug", "app-debug.apk")
    if not os.path.isfile(apk):
        die("build finished but the APK was not found where expected.")
    return apk


# --------------------------------------------------------------------------- release
def download_release_apk() -> str:
    log("fetching latest release info from GitHub")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
    )
    data = json.load(urllib.request.urlopen(req))
    tag = data.get("tag_name", "?")
    apk_asset = next((a for a in data.get("assets", []) if a["name"].endswith(".apk")), None)
    if not apk_asset:
        die(f"latest release {tag} has no APK asset attached.")
    log(f"latest release: {tag} ({apk_asset['name']})")
    dest = os.path.join(HERE, ".build", apk_asset["name"])
    download(apk_asset["browser_download_url"], dest)
    return dest


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Install Sunmi Print Hub to a connected device.")
    ap.add_argument("--from-release", action="store_true",
                    help="download+install the latest release APK instead of compiling")
    ap.add_argument("--keep", action="store_true", help="(no-op) keep the build checkout")
    args = ap.parse_args()

    print("\033[1mSunmi Print Hub, installer\033[0m")

    if args.from_release:
        apk = download_release_apk()
    else:
        clone_or_update()
        apk = compile_apk()

    sdk = find_sdk()
    adb = find_adb(sdk)
    require_device(adb)
    adb_install(adb, apk)

    print("\n\033[32m✓ Installed.\033[0m Open \"Sunmi Print Hub\" on the device.")
    print(f"  APK: {apk}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        die(f"a command failed (exit {e.returncode}). See the output above.")
    except KeyboardInterrupt:
        die("cancelled.")
