"""Detects credentials trying to enter the repo.

One implementation, used by both the pre-commit hook and the test suite, so a
rule can never be tightened in one place and left loose in the other.

Run it directly:

    python -m blink_light.secret_scan --staged   # what git is about to commit
    python -m blink_light.secret_scan --tracked  # everything already tracked
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

# A credential-shaped key assigned a long opaque value. Narrow on purpose: it
# matches an assignment, not a GUID mentioned in prose.
SECRET_ASSIGNMENT = re.compile(
    r"(client_id|client_secret|tenant_id|password|api[_-]?key|access_token|refresh_token)"
    r"[^A-Za-z0-9]{0,4}[:=][^A-Za-z0-9]{0,4}[A-Za-z0-9._-]{16,}",
    re.IGNORECASE,
)

# Files that must never be tracked, whatever they contain. blink-light.json is
# the machine's own config - device serial, calendar provider, a personal
# schedule - so it stays local; blink-light.example.json is the shared copy.
FORBIDDEN_NAMES = {".env", ".envrc", "blink-light.json"}
FORBIDDEN_SUFFIXES = (".token", ".pem", ".pfx", ".local.json")
FORBIDDEN_FRAGMENTS = ("token-cache",)

# The template documents the variable names and is meant to be committed.
ALLOWED_NAMES = {".env.template", ".env.example"}

SCANNED_SUFFIXES = {".py", ".json", ".md", ".bat", ".vbs", ".toml", ".txt", ".cmd", ".yml", ".yaml", ""}


def path_is_forbidden(path: str) -> bool:
    name = Path(path).name
    if name in ALLOWED_NAMES:
        return False
    if name in FORBIDDEN_NAMES or name.startswith(".env."):
        return True
    if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        return True
    return any(fragment in name for fragment in FORBIDDEN_FRAGMENTS)


def should_scan_contents(path: str) -> bool:
    """Test fixtures and the template legitimately contain example values."""
    if path.startswith("tests/") or Path(path).name in ALLOWED_NAMES:
        return False
    return Path(path).suffix in SCANNED_SUFFIXES


def find_secrets(text: str) -> list[str]:
    return [match.group(0) for match in SECRET_ASSIGNMENT.finditer(text)]


def _git(args: list[str], repo: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def scan_staged(repo: Path) -> list[str]:
    """Problems in what is staged right now. Empty list means clean."""
    problems: list[str] = []
    names = [line for line in _git(["diff", "--cached", "--name-only", "--diff-filter=ACM"], repo).splitlines() if line]
    for name in names:
        if path_is_forbidden(name):
            problems.append(f"{name}: this file must never be committed (credentials or local state)")

    for name in names:
        if not should_scan_contents(name):
            continue
        # Only the added lines, so an old value already in history does not
        # block every future commit.
        diff = _git(["diff", "--cached", "-U0", "--", name], repo)
        for line in diff.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            for hit in find_secrets(line[1:]):
                problems.append(f"{name}: credential-shaped value staged: {hit[:70]}")
    return problems


def scan_tracked(repo: Path) -> list[str]:
    problems: list[str] = []
    names = [line for line in _git(["ls-files"], repo).splitlines() if line]
    for name in names:
        if path_is_forbidden(name):
            problems.append(f"{name}: this file must never be tracked")
        if not should_scan_contents(name):
            continue
        full = repo / name
        if not full.exists():
            continue
        for hit in find_secrets(full.read_text(encoding="utf-8", errors="replace")):
            problems.append(f"{name}: credential-shaped value tracked: {hit[:70]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    repo = Path(__file__).resolve().parents[1]
    mode = args[0] if args else "--staged"
    problems = scan_tracked(repo) if mode == "--tracked" else scan_staged(repo)
    if not problems:
        return 0
    print("Blocked: credentials must not enter this repo.", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Put the value in .env (gitignored) and read it via BLINK_LIGHT_*.", file=sys.stderr)
    print("Override once with: git commit --no-verify", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
