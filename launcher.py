"""Starts Mic Soundboard like a desktop app (this is what the Start Menu / desktop icon runs).

It checks GitHub for a newer version before starting, keeps the packages installed,
restarts the app when it asks to update (exit code 3), and rolls back an update
that fails to start. Output goes to data/app.log.
"""
import hashlib
import os
import socket
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(DATA_DIR, "app.log")
CONTROL_PORT = 47819          # the running app listens here (show / quit)
RESTART_CODE = 3
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
APP_NAME = "Mic Soundboard"


def log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + f"[launcher] {msg}\n")
    except OSError:
        pass


def message_box(text, error=False):
    if os.name == "nt":
        try:
            import ctypes
            flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags | 0x40000)
            return
        except Exception:
            pass
    print(f"{APP_NAME}: {text}", file=sys.stderr)


def console_python():
    """python.exe next to pythonw.exe, so child processes have working output streams."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        alt = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(alt):
            return alt
    return exe


def send_command(cmd):
    """Tell an already-running copy to do something. Returns True if one answered."""
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=1) as s:
            s.sendall(cmd.encode() + b"\n")
            return s.recv(16).startswith(b"ok")
    except OSError:
        return False


def pip(*args):
    proc = subprocess.run([console_python(), "-m", "pip", "install", "-q", "--disable-pip-version-check", *args],
                          cwd=BASE_DIR, capture_output=True, text=True, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        log("pip failed: " + (proc.stderr or proc.stdout)[-800:])
    return proc.returncode == 0


def deps_importable():
    probe = subprocess.run([console_python(), "-c", "import numpy, sounddevice, soundfile"], cwd=BASE_DIR,
                           capture_output=True, creationflags=NO_WINDOW)
    return probe.returncode == 0


def ensure_deps():
    """Install packages if they're missing or requirements.txt changed (e.g. after an update)."""
    import updater
    try:
        with open(os.path.join(BASE_DIR, "requirements.txt"), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return
    if updater.state().get("req_hash") == digest and deps_importable():
        return
    log("Installing packages (first start or after an update)")
    if pip("-r", "requirements.txt"):
        updater.save_state(req_hash=digest)
        log("Packages installed")
    else:
        log("Installing packages failed; check the internet connection.")


def try_update():
    """Apply a newer version from GitHub if there is one. Returns the new version or None."""
    import updater
    if not updater.auto_enabled():
        return None
    try:
        latest = updater.check()
        if not updater.is_newer(latest) or latest == updater.state().get("skip"):
            return None
        log(f"Updating {updater.local_version()} -> {latest}")
        version = updater.apply(expected=latest)
        log(f"Updated to {version}")
        return version
    except updater.UpdateError as e:
        log(f"Update skipped: {e}")
    except Exception as e:  # an update problem must never stop the app from starting
        log(f"Update error: {type(e).__name__}: {e}")
    return None


def main():
    if send_command("show"):  # already running: bring its window to the front
        return

    crashes = 0
    while True:
        ensure_deps()
        just_updated = try_update()
        if just_updated:
            ensure_deps()  # the new version may need new packages
        started = time.time()
        env = dict(os.environ, MIC_SOUNDBOARD_LAUNCHER="1", PYTHONIOENCODING="utf-8")
        with open(LOG_FILE, "a", encoding="utf-8") as logf:
            code = subprocess.call([console_python(), os.path.join(BASE_DIR, "soundboard.py")], cwd=BASE_DIR,
                                   env=env, stdout=logf, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        log(f"App exited with code {code}")

        if code == 0:
            return            # closed normally
        if code == RESTART_CODE:
            continue          # asked to restart, usually to install an update

        quick = time.time() - started < 20
        if just_updated and quick:
            import updater
            try:
                back = updater.rollback()
                log(f"Version {just_updated} failed to start; rolled back to {back}")
                updater.save_state(req_hash=None)
                message_box(f"The update to version {just_updated} didn't start correctly, so the previous "
                            f"version ({back}) was restored. Your sounds and settings are safe.")
                continue
            except Exception as e:
                log(f"Rollback failed: {e}")
        crashes += 1
        if quick or crashes > 3:
            message_box("Mic Soundboard couldn't start. Details are in data\\app.log inside the app folder "
                        "(Start Menu → right-click Mic Soundboard → Open file location).", error=True)
            return
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Launcher crashed: {type(e).__name__}: {e}")
        message_box(f"Mic Soundboard couldn't start: {e}", error=True)
