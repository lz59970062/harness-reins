# PyInstaller spec for a self-contained reins that needs no Python on the target.
#
# onedir rather than onefile: onefile unpacks itself to a temp directory on every
# start, which is slow for the many short-lived workers reins spawns and is the
# layout antivirus products flag most often on Windows.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Platform modules are imported lazily, so static analysis misses them.
hiddenimports = collect_submodules("harness_reins")
# Skills, templates, PowerShell, the Pi extension.
datas = collect_data_files("harness_reins")

a = Analysis(
    [os.path.join(SPECPATH, "reins_main.py")],
    hiddenimports=hiddenimports,
    datas=datas,
    excludes=["tkinter", "unittest", "pydoc"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="reins", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="reins", upx=False)
