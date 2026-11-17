"""Fast dependency-state checks used by the Windows launch scripts.

The marker lives inside .venv so extracting a code-only update over an existing
installation does not trigger pip again unless requirements.txt really changed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
REQUIREMENTS_FILE = PROJECT_DIR / "requirements.txt"
VENV_DIR = PROJECT_DIR / ".venv"
# Bump this marker name (v1 -> v2) only when requirements.txt changes. The
# launcher checks for this exact file and otherwise goes directly to the UI.
MARKER_FILE = VENV_DIR / ".kitkat_dependencies_v1.ready"

PACKAGE_IMPORTS = {
    "customtkinter": "customtkinter",
    "psutil": "psutil",
    "playwright": "playwright",
    "requests": "requests",
    "pyperclip": "pyperclip",
    "pillow": "PIL",
    "easyocr": "easyocr",
}


def requirements_fingerprint() -> str:
    payload = REQUIREMENTS_FILE.read_bytes()
    python_tag = f"python-{sys.version_info.major}.{sys.version_info.minor}\n".encode()
    return hashlib.sha256(python_tag + payload).hexdigest()


def required_imports() -> list[str]:
    modules: list[str] = []
    for raw_line in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        package = line.split(";", 1)[0].strip()
        for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
            package = package.split(separator, 1)[0].strip()
        normalized = package.lower().replace("_", "-")
        modules.append(PACKAGE_IMPORTS.get(normalized, normalized.replace("-", "_")))
    return modules


def missing_imports() -> list[str]:
    return [name for name in required_imports() if importlib.util.find_spec(name) is None]


def write_marker() -> None:
    MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = MARKER_FILE.with_suffix(".tmp")
    temporary.write_text(requirements_fingerprint() + "\n", encoding="ascii")
    temporary.replace(MARKER_FILE)


def dependencies_are_current() -> tuple[bool, str]:
    missing = missing_imports()
    if missing:
        return False, "missing: " + ", ".join(missing)

    expected = requirements_fingerprint()
    if not MARKER_FILE.exists():
        # Adopt a working environment created by an older Kitkat launcher.
        write_marker()
        return True, "existing environment adopted"

    actual = MARKER_FILE.read_text(encoding="ascii").strip()
    if actual != expected:
        return False, "requirements changed"
    return True, "requirements unchanged"


def main() -> int:
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "check"
    if command == "mark":
        write_marker()
        print("Dependency marker updated.")
        return 0
    if command != "check":
        print("Usage: dependency_setup.py [check|mark]")
        return 2

    current, reason = dependencies_are_current()
    print(reason)
    return 0 if current else 1


if __name__ == "__main__":
    raise SystemExit(main())
