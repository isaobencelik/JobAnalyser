"""
Path resolution that works both in development (running the .py files directly)
and when the app is frozen into a single .exe by PyInstaller.

  APP_DIR              folder the app lives in — next to the .exe when frozen,
                       the project folder in development.
  DATA_DIR             writable folder for jobs.db + config.json. When frozen this
                       is %LOCALAPPDATA%\\JobAnalyser, so a user's favorites, history
                       and Gemini key SURVIVE swapping in a new .exe. In development
                       it stays the project folder (unchanged behaviour).
  resource_path(name)  locate a read-only resource (e.g. job-analyser.html). Prefers
                       a copy sitting next to the .exe — so you can ship a UI tweak by
                       just replacing that one file, no rebuild — then the copy bundled
                       inside the .exe, then the development folder.
"""

import os, sys

FROZEN = getattr(sys, 'frozen', False)

if FROZEN:
    APP_DIR  = os.path.dirname(sys.executable)        # where the .exe sits
    _BUNDLE  = getattr(sys, '_MEIPASS', APP_DIR)      # PyInstaller's temp unpack dir
else:
    APP_DIR  = os.path.dirname(os.path.abspath(__file__))
    _BUNDLE  = APP_DIR


def _resolve_data_dir():
    if FROZEN:
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        d = os.path.join(base, 'JobAnalyser')
    else:
        d = APP_DIR                                   # dev: keep jobs.db in the project
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return APP_DIR


DATA_DIR = _resolve_data_dir()


def resource_path(name):
    """Read-only resource lookup: external copy next to the .exe wins (lets you update
    the UI without rebuilding), then the bundled copy, then the dev folder."""
    external = os.path.join(APP_DIR, name)
    if os.path.exists(external):
        return external
    bundled = os.path.join(_BUNDLE, name)
    if os.path.exists(bundled):
        return bundled
    return external
