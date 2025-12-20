from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


def scan_path(path: Path, nonprintable_threshold: float) -> List[str]:
    """Return a list of textual hygiene issues for a given path."""

    issues: List[str] = []
    try:
        content: bytes = path.read_bytes()
    except FileNotFoundError:
        # Skip deleted files gracefully for pre-commit hooks.
        return issues

    if b"\x00" in content:
        issues.append("contains NUL bytes")

    try:
        text: str = content.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - defensive
        issues.append(f"not valid UTF-8: {exc}")
        return issues

    if text:
        nonprintable_count: int = sum(
            1 for char in text if not (char.isprintable() or char in "\n\r\t")
        )
        ratio: float = nonprintable_count / len(text)
        if ratio > nonprintable_threshold:
            issues.append(
                f"non-printable character ratio {ratio:.4f} exceeds {nonprintable_threshold:.4f}"
            )

    return issues


def detect_offenders(
    paths: Iterable[Path], nonprintable_threshold: float
) -> Dict[Path, List[str]]:
    offenders: Dict[Path, List[str]] = {}
    for path in paths:
        issues = scan_path(path, nonprintable_threshold)
        if issues:
            offenders[path] = issues
    return offenders


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate documentation files for UTF-8 text, NUL-byte absence, "
            "and non-printable character ratios."
        )
    )
    parser.add_argument(
        "files", nargs="*", help="Files to scan for text hygiene issues."
    )
    parser.add_argument(
        "--nonprintable-threshold",
        type=float,
        default=0.02,
        help="Maximum allowed ratio of non-printable characters (default: 0.02).",
    )
    args = parser.parse_args(argv)

    file_paths: List[Path] = [Path(name) for name in args.files]
    offenders = detect_offenders(file_paths, args.nonprintable_threshold)
    if offenders:
        details = "; ".join(
            f"{path}: {', '.join(reasons)}" for path, reasons in offenders.items()
        )
        parser.error(
            "Documentation hygiene check failed for the following files: " + details
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
