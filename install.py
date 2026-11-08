"""Installs Mic Soundboard as a desktop app (Windows).

  Install.bat   -> python install.py             install or repair
  Uninstall.bat -> python install.py --uninstall remove shortcuts (and optionally the app folder)

Puts the app in %LOCALAPPDATA%\\Programs\\Mic Soundboard, creates Start Menu and desktop
shortcuts, and starts it. Safe to run again at any time.
"""
import os
import shutil
import socket
import subprocess
import sys
import time

APP_NAME = "Mic Soundboard"
SRC = os.path.dirname(os.path.abspath(__file__))
PROTECTED = {"data", ".venv", ".git", "__pycache__"}
CONTROL_PORT = 47819


def say(msg=""):
    print(msg, flush=True)


def fail(msg):
    say("\n  Problem: " + msg)
    sys.exit(1)


def target_dir():
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        fail("This installer is for Windows.")
    return os.path.join(root, "Programs", APP_NAME)


def app_running():
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def stop_running_app():
    if not app_running():
        return
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=2) as s:
            s.sendall(b"quit\n")
            s.recv(16)
    except OSError:
        pass
    for _ in range(20):
        if not app_running():
            time.sleep(1)  # let the launcher exit too
            return
        time.sleep(0.5)
    say("  Mic Soundboard is still running. Close its window,")
    input("  then press Enter here to continue...")
    if app_running():
        fail("It's still running. Close it and run Install.bat again.")


def code_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in PROTECTED]
        else:
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        out += [os.path.normpath(os.path.join(rel_dir, f)) for f in filenames]
    return out


def copy_app(dst):
    say(f"  Copying the app to {dst}")
    for rel in code_files(SRC):
        if rel == "soundboard_config.json":
            continue
        d = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(os.path.join(SRC, rel), d)
    # bring settings along: a data folder, or the config file from the first zip version
    os.makedirs(os.path.join(dst, "data"), exist_ok=True)
    dst_cfg = os.path.join(dst, "data", "config.json")
    for src_cfg in (os.path.join(SRC, "data", "config.json"), os.path.join(SRC, "soundboard_config.json")):
        if os.path.exists(src_cfg) and not os.path.exists(dst_cfg):
            shutil.copy2(src_cfg, dst_cfg)
            say("  Brought over your sounds and settings")
            break


def run(args, **kw):
    return subprocess.run(args, **kw)


def make_venv(dst):
    venv = os.path.join(dst, ".venv")
    py = os.path.join(venv, "Scripts", "python.exe")
    if not os.path.exists(py):
        say("  Setting up Python for the app (first time only)...")
        if run([sys.executable, "-m", "venv", venv]).returncode != 0:
            fail("Couldn't create the app's Python environment.")
    say("  Installing what the app needs (a minute the first time)...")
    pip = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    run(pip + ["-U", "pip"])
    if run(pip + ["-r", os.path.join(dst, "requirements.txt")]).returncode != 0:
        fail("Installing packages failed. Check your internet connection and run Install.bat again.")
    return py, os.path.join(venv, "Scripts", "pythonw.exe")


def powershell(script):
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
               capture_output=True, text=True)


def ps_str(s):
    return "'" + s.replace("'", "''") + "'"


def make_shortcuts(dst, pythonw):
    say("  Adding Start Menu and desktop shortcuts")
    script = f"""
$ws = New-Object -ComObject WScript.Shell
$targets = @(
  (Join-Path ([Environment]::GetFolderPath('Programs')) {ps_str(APP_NAME + '.lnk')}),
  (Join-Path ([Environment]::GetFolderPath('Desktop')) {ps_str(APP_NAME + '.lnk')})
)
foreach ($t in $targets) {{
  $s = $ws.CreateShortcut($t)
  $s.TargetPath = {ps_str(pythonw)}
  $s.Arguments = {ps_str('"' + os.path.join(dst, 'launcher.py') + '"')}
  $s.WorkingDirectory = {ps_str(dst)}
  $s.IconLocation = {ps_str(os.path.join(dst, 'static', 'icon.ico'))}
  $s.Description = 'Play sounds through your mic into Discord'
  $s.Save()
}}
"""
    res = powershell(script)
    if res.returncode != 0:
        say("  (Couldn't create shortcuts: " + (res.stderr or "").strip()[:200] + ")")
        say(f"  You can still start it from {dst}\\Mic Soundboard.bat")


def remove_shortcuts():
    powershell(f"""
foreach ($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {{
  $p = Join-Path $d {ps_str(APP_NAME + '.lnk')}
  if (Test-Path $p) {{ Remove-Item $p }}
}}""")


def install():
    dst = target_dir()
    say(f"\n  Installing {APP_NAME}\n")
    stop_running_app()
    moved = os.path.normcase(os.path.abspath(SRC)) != os.path.normcase(os.path.abspath(dst))
    if moved:
        copy_app(dst)
    py, pythonw = make_venv(dst)
    make_shortcuts(dst, pythonw)
    with open(os.path.join(dst, "Mic Soundboard.bat"), "w", encoding="utf-8") as f:
        f.write(f'@echo off\r\nstart "" "{pythonw}" "{os.path.join(dst, "launcher.py")}"\r\n')
    say("\n  Done! Starting Mic Soundboard...")
    subprocess.Popen([pythonw, os.path.join(dst, "launcher.py")], cwd=dst,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    say("\n  From now on, open it from the Start Menu or the desktop icon.")
    say("  It updates itself, so you won't need to download it again.")
    if moved:
        say(f"\n  Everything was copied, so you can delete this folder now:\n  {SRC}")


def uninstall():
    say(f"\n  Uninstalling {APP_NAME}\n")
    stop_running_app()
    remove_shortcuts()
    dst = target_dir()
    say("  Removed the Start Menu and desktop shortcuts.")
    say(f"  Your sounds list and settings are still in:\n  {dst}")
    say("  (Your actual sound files are wherever you keep them. They are never touched.)")
    answer = input("\n  Delete the app folder too? Type YES to delete: ").strip()
    if answer == "YES":
        subprocess.Popen(f'cmd /c timeout /t 3 >nul & rmdir /s /q "{dst}"', shell=True,
                         cwd=os.environ.get("TEMP") or None,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        say("  The folder will be deleted in a moment.")
    else:
        say("  Kept the folder.")


if __name__ == "__main__":
    if os.name != "nt":
        fail("The installer is for Windows.")
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install()
