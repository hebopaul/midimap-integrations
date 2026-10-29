"""Build a MIDImap integration package with a frozen one-directory worker.

Run with the Python of an isolated builder venv that has
`requirements-build.txt` and this SDK installed (see the author guide):

    <builder-venv>\\Scripts\\python.exe sdk/python/tools/build_package.py ^
        --source sdk/python/examples/simulator --out build/integration-sdk

`--source` is the package source directory: `manifest.json`, `README.md`,
`LICENSE`, and the worker entry script (`worker.py` by default) plus any
modules it imports. The tool:

1. freezes the entry script with PyInstaller (one directory, console
   subsystem, so the worker has standard streams) into `worker/`, named after
   the manifest `entrypoint`;
2. copies the license texts of Python and of every installed runtime
   distribution into `worker/licenses/`;
3. writes `<id>-<version>.midimap-integration` as a ZIP with sorted entries
   and fixed timestamps, and records its SHA-256 and a build report.

It never runs the built executable. The archive digest is reproducible only
if PyInstaller's output is; record the digest of the archive you publish.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

EXPECTED_PYINSTALLER = "6.22.3"
RUNTIME_DISTRIBUTIONS = ("midimap-integration-sdk", "jsonschema", "jsonschema-specifications",
                         "referencing", "rpds-py", "attrs", "typing-extensions")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def fail(message: str) -> None:
    raise SystemExit(f"build_package: {message}")


def check_builder() -> str:
    if sys.prefix == sys.base_prefix:
        fail("run this with the Python of an isolated builder venv, not a system Python")
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        fail("the frozen worker target is windows-x64; build on Windows x64")
    try:
        version = importlib.metadata.version("pyinstaller")
    except importlib.metadata.PackageNotFoundError:
        fail("PyInstaller is not installed; install requirements-build.txt")
    if version != EXPECTED_PYINSTALLER:
        fail(f"PyInstaller {EXPECTED_PYINSTALLER} is required, found {version}")
    return version


def freeze(entry: Path, source: Path, name: str, work: Path) -> Path:
    import PyInstaller.__main__
    dist = work / "dist"
    PyInstaller.__main__.run([
        str(entry), "--onedir", "--console", "--noconfirm", "--clean", "--name", name,
        "--distpath", str(dist), "--workpath", str(work / "pyinstaller"),
        "--specpath", str(work / "spec"), "--paths", str(source),
        "--copy-metadata", "midimap-integration-sdk",
    ])
    return dist / name


def copy_licenses(target: Path) -> list[dict]:
    target.mkdir(parents=True)
    records = []
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copyfile(python_license, target / "python-LICENSE.txt")
        records.append({"name": "Python", "version": platform.python_version(),
                        "files": ["python-LICENSE.txt"]})
    for name in RUNTIME_DISTRIBUTIONS:
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        files = [f for f in dist.files or ()
                 if any(part.lower().startswith(("license", "licence", "copying", "notice"))
                        for part in f.parts)]
        copied = []
        for file in files:
            out = f"{name}-{dist.version}-{PurePosixPath(*file.parts).name}"
            shutil.copyfile(dist.locate_file(file), target / out)
            copied.append(out)
        records.append({"name": name, "version": dist.version,
                        "license": dist.metadata.get("License-Expression")
                        or dist.metadata.get("License"), "files": copied})
    return records


def write_archive(stage: Path, archive: Path) -> list[str]:
    names = sorted(PurePosixPath(p.relative_to(stage).as_posix())
                   for p in stage.rglob("*") if p.is_file())
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for name in names:
            info = zipfile.ZipInfo(str(name), FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 0
            out.writestr(info, (stage / name).read_bytes())
    return [str(n) for n in names]


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def remove_owned_work_dir(out: Path, work: Path) -> None:
    """Delete the previous `<out>/work`, and nothing else.

    Refuses if `out`, `work` or any ancestor of `out` is a symbolic link or
    junction (deletion could then reach outside the build output), or if the
    resolved `work` is not directly inside the resolved `out`. Inside `work`,
    `shutil.rmtree` does not follow links.
    """
    for path in (work, out, *out.parents):
        if _is_link(path):
            fail(f"refusing to clean {work}: {path} is a link")
    if work.resolve().parent != out.resolve():
        fail(f"refusing to clean {work}: it is not inside {out}")
    if work.exists():
        shutil.rmtree(work)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--entry", default="worker.py")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    pyinstaller_version = check_builder()
    source, out = args.source.resolve(), args.out.resolve()
    manifest = json.loads((source / "manifest.json").read_bytes())
    entrypoint = PurePosixPath(manifest["entrypoint"])
    if entrypoint.parts[0] != "worker" or entrypoint.suffix != ".exe" or len(entrypoint.parts) != 2:
        fail("the manifest entrypoint must be worker/<name>.exe")
    work = out / "work"
    remove_owned_work_dir(out, work)
    frozen = freeze(source / args.entry, source, entrypoint.stem, work)
    stage = work / "stage"
    shutil.copytree(frozen, stage / "worker")
    for name in ("manifest.json", "README.md", "LICENSE"):
        shutil.copyfile(source / name, stage / name)
    from midimap_integration_sdk._wire.validation import validate_manifest
    checked = validate_manifest((source / "manifest.json").read_bytes())
    for icon in (checked.ui or {}).get("icons", ()):
        path = source / icon["path"]
        if any(_is_link(p) for p in (path,*path.parents)) or not path.resolve().is_relative_to(source) or not path.is_file():
            fail("declared icon must be an ordinary file inside the package source")
        destination = stage / icon["path"]
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,destination)
    licenses = copy_licenses(stage / "worker" / "licenses")
    archive = out / f"{manifest['id']}-{manifest['version']}.midimap-integration"
    entries = write_archive(stage, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    report = {"archive": archive.name, "sha256": digest, "bytes": archive.stat().st_size,
              "entries": len(entries), "python": platform.python_version(),
              "pyinstaller": pyinstaller_version,
              "hooks_contrib": importlib.metadata.version("pyinstaller-hooks-contrib"),
              "runtime": licenses, "entrypoint": str(entrypoint)}
    (out / "build-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("archive", "sha256", "bytes", "entries")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
