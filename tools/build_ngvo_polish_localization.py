from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


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


def detect_content_root(source_dir: Path) -> Path:
    data_dir = source_dir / "Data"
    return data_dir if data_dir.is_dir() else source_dir


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


def build_report_path(output_dir: Path) -> Path:
    return output_dir.parent / "build-report.json"


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    config, mods_manifest, copy_manifest, config_dir = load_config(config_path)

    output_dir = get_build_root(config, config_dir)
    exclude_patterns = config.get("excludePatterns", [])

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())