# PyInstaller spec. Builds AgentFeed.app on macOS and AgentFeed.exe on
# Windows -- from the same file, on the respective OS (PyInstaller does not
# cross-compile). The bundle carries the dashboard and the domain packs as
# data; the model runtime stays outside it, where it belongs.
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
IS_MAC = sys.platform == "darwin"

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "ui"), "ui"),
        (str(ROOT / "agentfeed" / "domains"), "agentfeed/domains"),
    ],
    hiddenimports=(collect_submodules("agentfeed")
                   + collect_submodules("uvicorn")
                   + ["feedparser", "trafilatura", "ddgs"]),
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="AgentFeed",
    console=False,            # a windowed app: no terminal behind it
    icon=str(ROOT / "packaging" / ("AgentFeed.icns" if IS_MAC else "AgentFeed.ico"))
         if (ROOT / "packaging" / ("AgentFeed.icns" if IS_MAC else "AgentFeed.ico")).exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="AgentFeed")

if IS_MAC:
    app = BUNDLE(
        coll,
        name="AgentFeed.app",
        # The EXE icon is Windows; the bundle needs its own, or PyInstaller
        # ships its stock icon-windowed.icns and the Dock shows a generic app.
        icon=str(ROOT / "packaging" / "AgentFeed.icns"),
        bundle_identifier="dev.agentfeed.app",
        info_plist={
            "CFBundleDisplayName": "AgentFeed",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            # Ask for the native architecture first; a Rosetta launch of an
            # arm64 Python bundle fails with an "incompatible architecture"
            # import error that looks like a broken app.
            "LSArchitecturePriority": ["arm64", "x86_64"],
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
