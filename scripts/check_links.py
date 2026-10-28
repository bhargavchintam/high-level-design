"""Check the links between the docs in this repo.

Two checks, on every Markdown file outside code blocks and inline code:

1. Every relative link points to a file that exists.
2. Another doc is linked, not named in plain text: "see concepts/caching.md" should be
   "see [concepts/caching.md](../concepts/caching.md)". Run with --fix to turn such
   mentions into relative links.

Usage:
    python scripts/check_links.py          # report problems, exit 1 if there are any
    python scripts/check_links.py --fix    # also rewrite plain mentions as links

Standard library only.
"""

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLDERS = ("concepts", "case-studies", "ml-system-design")

FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`]*`")
# Inline code, Markdown links and images, and bare URLs: never touched.
PROTECTED = re.compile(r"`[^`]*`|!?\[[^\]]*\]\([^)]*\)|<https?://[^>]*>|https?://\S+")
LINK = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
MENTION = re.compile(r"(?<![\w./:#-])((?:\.\./)?(?:(?:" + "|".join(FOLDERS) + r")/)?[a-z0-9-]+\.md)(?![\w/#-])")


def markdown_files(root=ROOT):
    return sorted(p for p in root.rglob("*.md") if not any(part.startswith(".") for part in p.relative_to(root).parts))


def doc_index(files, root=ROOT):
    """File name -> path from the repo root, for the docs in the three folders."""
    index = {}
    for p in files:
        rel = p.relative_to(root)
        if len(rel.parts) == 2 and rel.parts[0] in FOLDERS:
            index[rel.name] = rel.as_posix()
    return index


def resolve_mention(text, index, root=ROOT):
    """'concepts/caching.md', '../concepts/caching.md' or 'caching.md' -> 'concepts/caching.md', or None."""
    path = text[3:] if text.startswith("../") else text
    if "/" in path:
        return path if (root / path).is_file() else None
    return index.get(path)


def plain_segments(line):
    """(start, end) spans of a line that are not inline code, links or URLs."""
    spans, pos = [], 0
    for m in PROTECTED.finditer(line):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(line)))
    return spans


def check_file(path, index, fix=False, root=ROOT):
    """Return (broken links, plain mentions, new text or None) for one Markdown file."""
    rel_file = path.relative_to(root).as_posix()
    here = path.parent
    broken, mentions, out, changed, in_fence = [], [], [], False, False
    for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        for m in LINK.finditer(INLINE_CODE.sub("", line)):
            target = m.group(1).split("#", 1)[0]
            if target and not re.match(r"^[a-z]+:", target) and not (here / target).resolve().exists():
                broken.append((rel_file, n, m.group(1)))
        new_line, last = [], 0
        for start, end in plain_segments(line):
            new_line.append(line[last:start])
            seg = line[start:end]

            def link_it(m):
                target = resolve_mention(m.group(1), index, root)
                if target is None or target == rel_file:
                    return m.group(0)
                mentions.append((rel_file, n, m.group(1)))
                return f"[{m.group(1)}]({os.path.relpath(root / target, here).replace(os.sep, '/')})"

            new_line.append(MENTION.sub(link_it, seg))
            last = end
        new_line.append(line[last:])
        joined = "".join(new_line)
        changed |= joined != line
        out.append(joined)
    return broken, mentions, ("\n".join(out) if fix and changed else None)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check the links between the docs.")
    ap.add_argument("--fix", action="store_true", help="rewrite plain mentions of other docs as relative links")
    args = ap.parse_args(argv)

    files = markdown_files()
    index = doc_index(files)
    all_broken, all_mentions = [], []
    for path in files:
        broken, mentions, new_text = check_file(path, index, fix=args.fix)
        all_broken += broken
        all_mentions += mentions
        if new_text is not None:
            path.write_text(new_text, encoding="utf-8")
    links = sum(len(LINK.findall(p.read_text(encoding="utf-8"))) for p in files)
    for f, n, target in all_broken:
        print(f"{f}:{n}: broken link: {target}")
    if not args.fix:
        for f, n, text in all_mentions:
            print(f"{f}:{n}: names {text} without a link (run with --fix)")
    print(f"{len(files)} files, {links} links: {len(all_broken)} broken, "
          f"{len(all_mentions)} plain mentions{' fixed' if args.fix else ''}")
    return 1 if all_broken or (all_mentions and not args.fix) else 0


if __name__ == "__main__":
    sys.exit(main())
