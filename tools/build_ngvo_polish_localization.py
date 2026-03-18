from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import py7zr
import rarfile

_UNRAR_CANDIDATES = [
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
]
for _unrar in _UNRAR_CANDIDATES:
    if Path(_unrar).exists():
        rarfile.UNRAR_TOOL = _unrar
        break


REPO_ROOT = Path(__file__).resolve().parents[1]

NEXUS_GAME_DOMAIN = "skyrimspecialedition"
NEXUS_API_BASE = "https://api.nexusmods.com/v1"


@dataclass
class SourceSpec:
    id: str
    label: str
    path: Path
    required: bool
    order: int
    source_type: str


@dataclass
class CopyOperation:
    id: str
    src: str
    dst: str
    enabled: bool
    required: bool
    operation_type: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the NGVO - Polish Localization mod from unpacked sources in tmp/."
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "ngvo-polish-build.json"),
        help="Path to the build config JSON file.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete the output directory before copying files.",
    )
    parser.add_argument(
        "--allow-missing-required",
        action="store_true",
        help="Continue even if required translation sources are missing.",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Download and extract mods from ngvo-plus-pl-mods.json to tmp/mods/, then copy to Data/.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("NEXUS_API_KEY"),
        metavar="KEY",
        help="Nexus Mods API key. Defaults to NEXUS_API_KEY env var. Required when --fetch is used.",
    )
    parser.add_argument(
        "--data-dir",
        default=str(REPO_ROOT / "Data"),
        metavar="DIR",
        help="Destination Data/ directory for copied mod files (default: <repo>/Data).",
    )
    parser.add_argument(
        "--unpack-local",
        action="store_true",
        help="Unpack mods with localFile from ngvo-plus-pl-mods.json into tmp/NGVO - Polish Localization/<entry_id>/.",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def load_config(config_path: Path) -> tuple[dict, dict, dict, Path]:
    config = load_json(config_path)
    config_dir = config_path.parent.resolve()
    mods_manifest_path = resolve_path(config_dir, config["modsManifestPath"])
    copy_manifest_path = resolve_path(config_dir, config["copyManifestPath"])
    mods_manifest = load_json(mods_manifest_path)
    copy_manifest = load_json(copy_manifest_path)
    return config, mods_manifest, copy_manifest, config_dir


def get_build_root(config: dict, config_dir: Path) -> Path:
    builds_root = resolve_path(config_dir, config["paths"]["buildsRoot"])
    localization_dir = builds_root / config["localizationOutputName"]
    return localization_dir.resolve()


def parse_copy_operations(copy_manifest: dict) -> list[CopyOperation]:
    operations: list[CopyOperation] = []
    for item in copy_manifest.get("operations", []):
        operations.append(
            CopyOperation(
                id=item["id"],
                src=item["src"],
                dst=item["dst"],
                enabled=item.get("enabled", True),
                required=item.get("required", False),
                operation_type=item.get("type", "copy"),
            )
        )
    return operations


def to_mod_root_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.parts and path.parts[0].lower() == "data":
        return Path(*path.parts[1:]) if len(path.parts) > 1 else Path()
    return path


def build_core_from_manifest(
    config: dict,
    copy_manifest: dict,
    config_dir: Path,
    output_dir: Path,
) -> tuple[list[dict], list[dict]]:
    source_game_root = resolve_path(config_dir, config["paths"]["sourceGameRoot"])
    if not source_game_root.exists():
        raise FileNotFoundError(f"Configured sourceGameRoot does not exist: {source_game_root}")

    copied: list[dict] = []
    missing: list[dict] = []
    for operation in parse_copy_operations(copy_manifest):
        if not operation.enabled:
            continue

        source_path = source_game_root / Path(operation.src)
        destination_path = output_dir / to_mod_root_path(operation.dst)

        if not source_path.exists():
            if operation.required or operation.operation_type == "copy":
                missing.append(
                    {
                        "id": operation.id,
                        "src": str(source_path),
                        "dst": str(destination_path),
                        "required": operation.required,
                        "type": operation.operation_type,
                    }
                )
            continue

        if destination_path == output_dir:
            missing.append(
                {
                    "id": operation.id,
                    "src": str(source_path),
                    "dst": str(destination_path),
                    "required": operation.required,
                    "type": operation.operation_type,
                    "error": "destination-resolves-to-output-root",
                }
            )
            continue

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        copied.append(
            {
                "id": operation.id,
                "src": str(source_path),
                "dst": str(destination_path),
                "type": operation.operation_type,
            }
        )

    return copied, missing


def build_sources(config: dict, mods_manifest: dict, config_dir: Path) -> list[SourceSpec]:
    sources: list[SourceSpec] = []

    mods_root = resolve_path(config_dir, config["modsSourcesRoot"])
    for entry in mods_manifest.get("entries", []):
        sources.append(
            SourceSpec(
                id=entry["id"],
                label=entry["name"],
                path=(mods_root / entry["id"]).resolve(),
                required=entry.get("required", False),
                order=entry.get("order", 0),
                source_type="translation-mod",
            )
        )

    extras_root = resolve_path(config_dir, config["overridesRoot"])
    if extras_root.exists():
        extra_order = config.get("overridesOrder", 9000)
        for index, child in enumerate(sorted(path for path in extras_root.iterdir() if path.is_dir())):
            sources.append(
                SourceSpec(
                    id=f"override-{child.name}",
                    label=f"Override: {child.name}",
                    path=child.resolve(),
                    required=False,
                    order=extra_order + index,
                    source_type="override",
                )
            )

    return sorted(sources, key=lambda item: (item.order, item.label.lower()))


def should_exclude(relative_path: str, patterns: list[str]) -> bool:
    normalized = relative_path.replace("\\", "/")
    return any(fnmatch(normalized, pattern) for pattern in patterns)


_KNOWN_CONTENT_DIRS = frozenset([
    "interface", "data", "textures", "meshes", "scripts", "sound",
    "music", "skse", "translations", "strings", "seq", "lodsettings",
    "grass", "shadersfx", "video", "source",
])


def detect_content_root(source_dir: Path) -> Path:
    """Locate the actual mod content root inside source_dir.

    Handles three layouts found in Nexus archives:
    - flat:          source_dir/ contains mod files directly
    - Data/ wrapper: source_dir/Data/ contains mod files
    - name wrapper:  source_dir/<ModName>/ wraps one of the above

    Recursion stops as soon as the single child folder is a known
    game-content directory (interface/, textures/, scripts/, etc.)
    to avoid stripping meaningful path prefixes.
    """
    data_dir = source_dir / "Data"
    if data_dir.is_dir():
        return data_dir

    children = list(source_dir.iterdir())
    dirs = [p for p in children if p.is_dir()]
    files = [p for p in children if p.is_file()]

    # Strip exactly one wrapper folder, but only if it is not a standard
    # game-content directory (which would indicate we are already inside
    # the mod's file tree).
    if len(dirs) == 1 and not files:
        wrapper = dirs[0]
        if wrapper.name.lower() not in _KNOWN_CONTENT_DIRS:
            return detect_content_root(wrapper)

    return source_dir


def iter_files(root: Path, exclude_patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative_path = path.relative_to(root).as_posix()
        if should_exclude(relative_path, exclude_patterns):
            continue
        files.append(path)
    return files


def copy_tree(
    source: SourceSpec,
    output_dir: Path,
    exclude_patterns: list[str],
    overwrite_log: dict[str, list[str]],
) -> tuple[int, int]:
    content_root = detect_content_root(source.path)
    copied = 0
    replaced = 0

    for file_path in iter_files(content_root, exclude_patterns):
        relative_path = file_path.relative_to(content_root)
        destination = output_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination_key = relative_path.as_posix()

        if destination.exists():
            replaced += 1
            overwrite_log.setdefault(destination_key, []).append(source.id)

        shutil.copy2(file_path, destination)
        copied += 1

    return copied, replaced


def nexus_api_request(path: str, api_key: str) -> list | dict:
    url = f"{NEXUS_API_BASE}/{path}"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": api_key,
            "accept": "application/json",
            "User-Agent": "ngvo-plus-builder/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Nexus API {url} returned HTTP {exc.code}: {exc.reason}") from exc


def get_nexus_download_url(nexus_mod_id: int, nexus_file_id: int, api_key: str) -> str:
    path = f"games/{NEXUS_GAME_DOMAIN}/mods/{nexus_mod_id}/files/{nexus_file_id}/download_link.json"
    links = nexus_api_request(path, api_key)
    if not links:
        raise RuntimeError("Nexus Mods API returned no download links")
    return links[0]["URI"]  # type: ignore[index]


def download_file(url: str, dest_path: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ngvo-plus-builder/1.0"})
    with urllib.request.urlopen(req) as response:
        with dest_path.open("wb") as out_file:
            shutil.copyfileobj(response, out_file)


def extract_archive_safe(archive_path: Path, extract_dir: Path) -> None:
    """Extract archive to extract_dir; guards against zip path traversal."""
    extract_dir.mkdir(parents=True, exist_ok=True)
    suffix = archive_path.suffix.lower()
    resolved_extract = extract_dir.resolve()

    if suffix == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.namelist():
                member_resolved = (extract_dir / member).resolve()
                if not str(member_resolved).startswith(str(resolved_extract)):
                    raise RuntimeError(f"Zip path traversal blocked: {member}")
                zf.extract(member, extract_dir)
    elif suffix == ".7z":
        with py7zr.SevenZipFile(archive_path, mode="r") as zf:
            zf.extractall(path=extract_dir)
    elif suffix == ".rar":
        with rarfile.RarFile(archive_path, "r") as rf:
            rf.extractall(path=extract_dir)
    else:
        shutil.unpack_archive(str(archive_path), str(extract_dir))


def fetch_and_extract_mods(mods_manifest: dict, mods_dir: Path, api_key: str) -> list[dict]:
    """Download and extract each entry from the manifest that has a nexusFileId."""
    mods_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    for entry in mods_manifest.get("entries", []):
        entry_id = entry["id"]
        download_info = entry.get("downloadFile", {})
        nexus_file_id = download_info.get("nexusFileId")
        nexus_mod_id = entry.get("nexusModId")

        if not nexus_file_id:
            print(f"  [SKIP]   {entry_id}: nexusFileId is null — download manually")
            results.append({"id": entry_id, "status": "skipped", "reason": "no-nexus-file-id"})
            continue

        extract_dir = mods_dir / entry_id
        if extract_dir.exists() and any(extract_dir.iterdir()):
            print(f"  [CACHED] {entry_id}")
            results.append({"id": entry_id, "status": "cached", "path": str(extract_dir)})
            continue

        try:
            print(f"  [FETCH]  {entry_id}: requesting download link ...")
            download_url = get_nexus_download_url(nexus_mod_id, nexus_file_id, api_key)

            url_filename = download_url.split("?")[0].rsplit("/", 1)[-1] or f"{entry_id}.zip"
            archive_path = mods_dir / url_filename

            print(f"  [DL]     {url_filename}")
            download_file(download_url, archive_path)

            print(f"  [UNPACK] → {extract_dir}")
            extract_archive_safe(archive_path, extract_dir)

            results.append({"id": entry_id, "status": "downloaded", "archive": str(archive_path), "path": str(extract_dir)})
        except Exception as exc:
            print(f"  [ERROR]  {entry_id}: {exc}", file=sys.stderr)
            results.append({"id": entry_id, "status": "error", "error": str(exc)})

    return results


def _apply_rename_map(target_dir: Path, rename_map: dict[str, str], entry_id: str) -> None:
    """Rename files inside target_dir according to rename_map {old_name: new_name}."""
    for old_name, new_name in rename_map.items():
        old_path = target_dir / old_name
        new_path = target_dir / new_name
        if old_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)
            print(f"  [RENAME] {old_name} → {new_name}")
        else:
            print(f"  [WARN]   {entry_id}: renameFiles: '{old_name}' nie znaleziono w {target_dir.name}")


def unpack_local_mods(
    mods_manifest: dict,
    output_dir: Path,
    exclude_patterns: list[str],
    sources_dir: Path | None = None,
) -> list[dict]:
    """Extract archives referenced by localFile and merge their contents into output_dir.

    Archives are first extracted to a staging dir (tmp/unpack-staging/<entry_id>/)
    for caching.  Processed (flat, renamed) files are also written to
    sources_dir/<entry_id>/ so that the normal build phase can pick them up via
    modsSourcesRoot without re-extracting the archives.
    """
    staging_root = REPO_ROOT / "tmp" / "unpack-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    if sources_dir is not None:
        sources_dir.mkdir(parents=True, exist_ok=True)

    overwrite_log: dict[str, list[str]] = {}
    results: list[dict] = []

    ordered_entries = sorted(mods_manifest.get("entries", []), key=lambda e: e.get("order", 0))

    for entry in ordered_entries:
        entry_id = entry["id"]
        local_file = entry.get("downloadFile", {}).get("localFile")
        if not local_file:
            print(f"  [SKIP]   {entry_id}: brak localFile")
            results.append({"id": entry_id, "status": "skipped", "reason": "no-local-file"})
            continue

        archive_path = (REPO_ROOT / local_file).resolve()
        if not archive_path.exists():
            print(f"  [MISS]   {entry_id}: nie znaleziono {archive_path}", file=sys.stderr)
            results.append({"id": entry_id, "status": "missing", "archive": str(archive_path)})
            continue

        staging_dir = staging_root / entry_id
        cached = staging_dir.exists() and any(staging_dir.iterdir())

        if not cached:
            try:
                print(f"  [UNPACK] {archive_path.name} → staging")
                extract_archive_safe(archive_path, staging_dir)
            except Exception as exc:
                print(f"  [ERROR]  {entry_id}: {exc}", file=sys.stderr)
                results.append({"id": entry_id, "status": "error", "error": str(exc)})
                continue
        else:
            print(f"  [CACHED] {entry_id}")

        download_info = entry.get("downloadFile", {})
        content_root_override = download_info.get("contentRoot")
        rename_map: dict[str, str] = download_info.get("renameFiles", {})

        source_path = staging_dir
        if content_root_override:
            override_path = staging_dir / content_root_override
            if override_path.is_dir():
                source_path = override_path
            else:
                print(f"  [WARN]   {entry_id}: contentRoot '{content_root_override}' nie istnieje, używam staging root")

        source = SourceSpec(
            id=entry_id,
            label=entry.get("name", entry_id),
            path=source_path,
            required=entry.get("required", False),
            order=entry.get("order", 0),
            source_type="local-mod",
        )

        # ── Publish to sources_dir so the build phase can pick it up ────────
        if sources_dir is not None:
            mod_source_dir = sources_dir / entry_id
            if not (mod_source_dir.exists() and any(mod_source_dir.iterdir())):
                copy_tree(source, mod_source_dir, exclude_patterns, {})
                _apply_rename_map(mod_source_dir, rename_map, entry_id)

        # ── Merge into the immediate output directory ────────────────────────
        copied, replaced = copy_tree(source, output_dir, exclude_patterns, overwrite_log)
        _apply_rename_map(output_dir, rename_map, entry_id)

        status = "cached" if cached else "unpacked"
        print(f"  [MERGE]  {entry_id}: {copied} plików ({replaced} nadpisanych)")
        results.append({
            "id": entry_id,
            "status": status,
            "archive": str(archive_path),
            "staging": str(staging_dir),
            "copiedFiles": copied,
            "replacedFiles": replaced,
        })

    return results


def rename_polish_to_english(output_dir: Path) -> list[dict]:
    """Rename every file containing '_polish' in its name to '_english'."""
    renamed: list[dict] = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and "_polish" in path.name.lower():
            new_name = path.name.replace("_polish", "_english").replace("_Polish", "_english")
            new_path = path.with_name(new_name)
            path.rename(new_path)
            renamed.append({"from": path.name, "to": new_name})
            print(f"  [RENAME] {path.name} → {new_name}")
    return renamed


def copy_mods_to_data(
    mods_manifest: dict,
    mods_dir: Path,
    data_dir: Path,
    exclude_patterns: list[str],
) -> list[dict]:
    """Copy extracted mod directories into data_dir, following manifest order."""
    ordered_ids = [
        e["id"]
        for e in sorted(mods_manifest.get("entries", []), key=lambda e: e.get("order", 0))
    ]
    overwrite_log: dict[str, list[str]] = {}
    results: list[dict] = []

    for entry_id in ordered_ids:
        extract_dir = mods_dir / entry_id
        if not extract_dir.is_dir():
            continue
        source = SourceSpec(
            id=entry_id,
            label=entry_id,
            path=extract_dir,
            required=False,
            order=0,
            source_type="downloaded-mod",
        )
        copied, replaced = copy_tree(source, data_dir, exclude_patterns, overwrite_log)
        results.append({"id": entry_id, "copiedFiles": copied, "replacedFiles": replaced})
        if copied:
            print(f"  [DATA]   {entry_id}: {copied} files copied ({replaced} replaced)")

    return results


def pack_mod_for_mo2(mod_dir: Path) -> Path:
    """Create a MO2-installable zip archive from mod_dir contents.

    The archive has a flat layout (files at root, no top-level wrapper folder)
    which MO2 can install directly.
    """
    archive_path = mod_dir.parent / f"{mod_dir.name}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(mod_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(mod_dir))
    return archive_path


def build_report_path(output_dir: Path) -> Path:
    return output_dir.parent / "build-report.json"


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    config, mods_manifest, copy_manifest, config_dir = load_config(config_path)

    output_dir = get_build_root(config, config_dir)
    exclude_patterns = config.get("excludePatterns", [])

    # ── Unpack-local phase ───────────────────────────────────────────────────
    if args.unpack_local:
        pl_mods_path = resolve_path(config_dir, config["modsManifestPath"])
        pl_mods_manifest = load_json(pl_mods_path)
        unpack_output_dir = REPO_ROOT / "tmp" / "NGVO - Polish Localization"
        sources_dir = resolve_path(config_dir, config["modsSourcesRoot"])
        print(f"Rozpakowywanie lokalnych archiwów → {unpack_output_dir}")
        unpack_results = unpack_local_mods(pl_mods_manifest, unpack_output_dir, exclude_patterns, sources_dir)
        unpacked = sum(1 for r in unpack_results if r["status"] == "unpacked")
        cached = sum(1 for r in unpack_results if r["status"] == "cached")
        skipped = sum(1 for r in unpack_results if r["status"] == "skipped")
        missing = sum(1 for r in unpack_results if r["status"] == "missing")
        errors = sum(1 for r in unpack_results if r["status"] == "error")
        print(
            f"Gotowe: {unpacked} rozpakowane, {cached} w cache, "
            f"{skipped} pominięte (brak localFile), {missing} nie znalezione, {errors} błędy"
        )
        report_path = REPO_ROOT / "tmp" / "unpack-local-report.json"
        with report_path.open("w", encoding="utf-8") as handle:
            json.dump({"unpackLocal": unpack_results}, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        print(f"Raport zapisany: {report_path}")

        print(f"\nPrzełączanie nazw _polish → _english w {unpack_output_dir}")
        renamed = rename_polish_to_english(unpack_output_dir)
        print(f"Przemianowano: {len(renamed)} plików")

        return 1 if errors or missing else 0

    # ── Fetch phase ──────────────────────────────────────────────────────────
    if args.fetch:
        api_key: str = args.api_key or ""
        if not api_key:
            print("ERROR: --fetch requires a Nexus Mods API key (--api-key KEY or NEXUS_API_KEY env var).", file=sys.stderr)
            return 1

        mods_dir = REPO_ROOT / "tmp" / "mods"
        data_dir = Path(args.data_dir).resolve()

        print(f"Downloading mods → {mods_dir}")
        fetch_results = fetch_and_extract_mods(mods_manifest, mods_dir, api_key)
        downloaded = sum(1 for r in fetch_results if r["status"] == "downloaded")
        cached = sum(1 for r in fetch_results if r["status"] == "cached")
        skipped = sum(1 for r in fetch_results if r["status"] == "skipped")
        errors = sum(1 for r in fetch_results if r["status"] == "error")
        print(f"Fetch complete: {downloaded} downloaded, {cached} cached, {skipped} skipped (no fileId), {errors} errors")

        print(f"\nCopying mod files → {data_dir}")
        copy_results = copy_mods_to_data(mods_manifest, mods_dir, data_dir, exclude_patterns)
        total_data_files = sum(r["copiedFiles"] for r in copy_results)
        print(f"Data copy complete: {total_data_files} files copied across {len(copy_results)} mods")

        fetch_report_path = REPO_ROOT / "tmp" / "fetch-report.json"
        with fetch_report_path.open("w", encoding="utf-8") as handle:
            json.dump({"fetch": fetch_results, "dataCopy": copy_results}, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        print(f"Fetch report written to: {fetch_report_path}")

        if errors:
            print(f"\nWARNING: {errors} mod(s) failed to download — check fetch-report.json for details.", file=sys.stderr)

        return 0

    # ── Build phase ───────────────────────────────────────────────────────────
    if args.clean and output_dir.exists():
        shutil.rmtree(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        core_copied, core_missing = build_core_from_manifest(config, copy_manifest, config_dir, output_dir)
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 1

    if core_missing and not args.allow_missing_required:
        print("Missing required core copy operations:", file=sys.stderr)
        for item in core_missing:
            print(f"- {item['id']}: {item['src']}", file=sys.stderr)
        return 1

    if core_missing and args.allow_missing_required:
        print("Missing required core copy operations, continuing because --allow-missing-required was used:")
        for item in core_missing:
            print(f"- {item['id']}: {item['src']}")

    sources = build_sources(config, mods_manifest, config_dir)

    missing_required: list[SourceSpec] = []
    missing_optional: list[SourceSpec] = []
    available_sources: list[SourceSpec] = []

    for source in sources:
        if source.path.exists():
            available_sources.append(source)
        elif source.required:
            missing_required.append(source)
        else:
            missing_optional.append(source)

    if missing_required:
        print("Missing required sources, continuing with bootstrap files only:")
        for source in missing_required:
            print(f"- {source.label}: {source.path}")

    overwrite_log: dict[str, list[str]] = {}
    source_summaries: list[dict] = []
    total_copied_files = len(core_copied)

    source_summaries.append(
        {
            "id": "core",
            "label": "NGVO Polish Bootstrap",
            "type": "core",
            "path": str(output_dir),
            "copiedFiles": len(core_copied),
            "replacedFiles": 0,
        }
    )

    for source in available_sources:
        copied, replaced = copy_tree(source, output_dir, exclude_patterns, overwrite_log)
        total_copied_files += copied
        source_summaries.append(
            {
                "id": source.id,
                "label": source.label,
                "type": source.source_type,
                "path": str(source.path),
                "copiedFiles": copied,
                "replacedFiles": replaced,
            }
        )

    if total_copied_files == 0:
        print("Build completed but no files were copied.", file=sys.stderr)
        print("Check whether the configured source folders in tmp/ actually contain unpacked translation files.", file=sys.stderr)

    report = {
        "outputDir": str(output_dir),
        "coreOutputDir": str(output_dir),
        "totalCopiedFiles": total_copied_files,
        "coreBuild": {
            "copiedFiles": len(core_copied),
            "missingOperations": core_missing,
        },
        "sourcesUsed": source_summaries,
        "missingOptionalSources": [
            {
                "id": source.id,
                "label": source.label,
                "path": str(source.path),
            }
            for source in missing_optional
        ],
        "missingRequiredSources": [
            {
                "id": source.id,
                "label": source.label,
                "path": str(source.path),
            }
            for source in missing_required
        ],
        "overwrites": overwrite_log,
    }

    report_path = build_report_path(output_dir)
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print(f"Built mod at: {output_dir}")
    print(f"Report written to: {report_path}")
    print(f"Files copied: {total_copied_files}")
    print(f"Sources used: {len(source_summaries)}")
    print(f"Required sources missing: {len(missing_required)}")
    print(f"Optional sources missing: {len(missing_optional)}")

    print(f"\nPakowanie moda dla MO2 ...")
    archive_path = pack_mod_for_mo2(output_dir)
    archive_size_mb = archive_path.stat().st_size / 1_048_576
    print(f"Archiwum: {archive_path} ({archive_size_mb:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())