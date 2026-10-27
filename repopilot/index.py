"""Index a local documentation tree without ingesting secrets or huge files."""
import argparse
from pathlib import Path
from .retrieval import ContextIndex


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory.resolve()
    if not root.is_dir():
        parser.error("directory does not exist")
    index = ContextIndex()
    count = 0
    for path in root.rglob("*"):
        if path.is_symlink() or path.suffix.lower() not in (".md", ".txt") or not path.is_file():
            continue
        if path.stat().st_size > 12000:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            index.add(str(path.relative_to(root)), text)
            count += 1
    print(f"Indexed {count} documents")


if __name__ == "__main__":
    main()
