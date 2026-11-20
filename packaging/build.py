"""Build a native CuePorch layout on the target OS, then wrap it in an OS installer."""

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
VERSION = "0.3.0"
WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
APP = DIST / "layout"


def run(*cmd):
    subprocess.run(list(map(str, cmd)), cwd=ROOT, check=True)


def build():
    npm = "npm.cmd" if WINDOWS else "npm"
    run(npm, "--prefix", ROOT / "ui", "ci", "--ignore-scripts", "--no-audit")
    run(npm, "--prefix", ROOT / "ui", "run", "build")
    run(
        "cargo",
        "build",
        "--locked",
        "--release",
        "--manifest-path",
        ROOT / "native/Cargo.toml",
    )
    scratch = ROOT / "build/media-tools"
    scratch.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        imageio_ffmpeg.get_ffmpeg_exe(),
        scratch / ("ffmpeg.exe" if WINDOWS else "ffmpeg"),
    )
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "cueporch-service",
        "--distpath",
        ROOT / "build/frozen",
        "--workpath",
        ROOT / "build/freezer",
        "--specpath",
        ROOT / "build",
        "--copy-metadata",
        "http-ece",
        "--copy-metadata",
        "py-vapid",
        "--collect-all",
        "tzdata",
        "--collect-all",
        "pypdfium2_raw",
        "--collect-data",
        "onelogin.saml2",
        "--copy-metadata",
        "pypdfium2",
        "--copy-metadata",
        "Pillow",
        "--add-data",
        str(ROOT / "deploy/compose.yml") + ":deploy",
        "--add-data",
        str(ROOT / "templates") + ":templates",
        "--add-data",
        str(ROOT / "static") + ":static",
        "--add-data",
        str(ROOT / "live_caption_worker.py") + ":.",
        "--add-binary",
        str(scratch / ("ffmpeg.exe" if WINDOWS else "ffmpeg")) + ":media-tools",
        ROOT / "desktop_service.py",
    )
    APP.mkdir(parents=True, exist_ok=True)
    service = APP / "service"
    if service.exists():
        if service.resolve() != ROOT / "dist" / "layout" / "service":
            raise RuntimeError("Refusing to replace a service layout outside the build directory")
        shutil.rmtree(service)
    shutil.copytree(
        ROOT / "build/frozen/cueporch-service",
        service,
        symlinks=True,
    )
    shutil.copy2(
        ROOT / "native/target/release" / ("cueporch.exe" if WINDOWS else "cueporch"),
        APP / ("CuePorch.exe" if WINDOWS else "CuePorch"),
    )
    for name in ["LICENSE", "NOTICES.md", "README.md"]:
        shutil.copy2(ROOT / name, APP / name)


def package():
    if WINDOWS:
        iscc = (
            os.environ.get("ISCC")
            or shutil.which("iscc")
            or str(Path(os.environ["LOCALAPPDATA"]) / "Programs/Inno Setup 6/ISCC.exe")
        )
        run(iscc, *sys.argv[2:], ROOT / "packaging/windows.iss")
    elif MAC:
        contents = DIST / "macroot/Applications/CuePorch.app/Contents"
        resources = contents / "Resources"
        shutil.copytree(
            APP / "service", resources / "service", dirs_exist_ok=True, symlinks=True
        )
        for name in ("LICENSE", "NOTICES.md", "README.md"):
            shutil.copy2(APP / name, resources / name)
        binaries = contents / "MacOS"
        binaries.mkdir(parents=True, exist_ok=True)
        shutil.copy2(APP / "CuePorch", binaries / "CuePorch")
        (binaries / "service").symlink_to(
            "../Resources/service", target_is_directory=True
        )
        with (contents / "Info.plist").open("wb") as f:
            plistlib.dump(
                dict(
                    CFBundleExecutable="CuePorch",
                    CFBundleIdentifier="org.forkcommons.cueporch",
                    CFBundleName="CuePorch",
                    CFBundlePackageType="APPL",
                    CFBundleVersion=VERSION,
                    CFBundleShortVersionString=VERSION,
                ),
                f,
            )
        run("codesign", "--force", "--sign", "-", contents.parent)
        run("codesign", "--verify", "--strict", contents.parent)
        run(
            "pkgbuild",
            "--root",
            DIST / "macroot",
            "--install-location",
            "/",
            "--identifier",
            "org.forkcommons.cueporch",
            "--version",
            VERSION,
            DIST / f"CuePorch-{VERSION}-macOS-{platform.machine()}.pkg",
        )
    else:
        root = DIST / "debroot"
        shutil.copytree(APP, root / "opt/cueporch", dirs_exist_ok=True, symlinks=True)
        (root / "DEBIAN").mkdir(exist_ok=True)
        arch = subprocess.check_output(
            ["dpkg", "--print-architecture"], text=True
        ).strip()
        (root / "DEBIAN/control").write_text(
            f"Package: cueporch\nVersion: {VERSION}\nArchitecture: {arch}\nMaintainer: ForkCommons\nDescription: Station and exhibition video publishing\n"
        )
        entries = root / "usr/share/applications"
        entries.mkdir(parents=True, exist_ok=True)
        (entries / "cueporch.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=CuePorch\nExec=/opt/cueporch/CuePorch\nCategories=AudioVideo;\nTerminal=false\n"
        )
        run(
            "dpkg-deb",
            "--root-owner-group",
            "--build",
            root,
            DIST / f"cueporch_{VERSION}_{arch}.deb",
        )


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        build()
    elif sys.argv[1:2] == ["package"]:
        package()
    else:
        raise SystemExit("Choose build or package")
