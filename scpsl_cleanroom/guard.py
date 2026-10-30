"""Contamination guard: block anything that looks like original game code from entering a spec.

Run it on every file before it is handed to the porting team (or as a pre-commit
hook on the spec repository). It flags decompiler output, C#/IL fragments and
references to the game's assemblies.
"""

from __future__ import annotations

import re
from pathlib import Path

RULES: list[tuple[str, re.Pattern]] = [
    ("decompiler header", re.compile(r"(?i)decompiled with|ilspy|dnspy|dotpeek|cpp2il|il2cppdumper")),
    ("game assembly reference", re.compile(r"(?i)assembly-csharp|\.dll\b|metadata token|\bRVA\b")),
    ("C# using directive", re.compile(r"^\s*using\s+[A-Z][\w.]*\s*;", re.MULTILINE)),
    ("C# member declaration", re.compile(
        r"\b(public|private|protected|internal)\s+(static\s+|readonly\s+|const\s+|override\s+|virtual\s+)*"
        r"[\w<>\[\],]+\s+\w+\s*(\(|=|;|\{)")),
    ("C# attribute", re.compile(r"\[(SerializeField|Header|Tooltip|Range|CompilerGenerated|Serializable)\b")),
    ("Unity callback", re.compile(r"\bvoid\s+(Update|FixedUpdate|LateUpdate|Awake|Start|OnEnable)\s*\(")),
    ("IL opcode", re.compile(r"\b(ldc\.r4|ldc\.i4|ldfld|stfld|callvirt|ldarg\.\d)\b")),
    ("C# float literal", re.compile(r"\b\d+\.\d+f\b")),
]

TEXT_SUFFIXES = {".json", ".md", ".txt", ".luau", ".lua", ".yaml", ".yml", ".csv", ".jsonl"}


def scan_text(text: str) -> list[tuple[int, str, str]]:
    hits = []
    for name, rx in RULES:
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append((line, name, m.group(0).strip()[:80]))
    return sorted(hits)


def scan_paths(paths: list[str | Path]) -> dict[str, list[tuple[int, str, str]]]:
    report = {}
    for p in map(Path, paths):
        files = [f for f in p.rglob("*") if f.is_file()] if p.is_dir() else [p]
        for f in files:
            if f.suffix in (".cs", ".il", ".dll"):
                report[str(f)] = [(0, "code file", f.suffix)]
                continue
            if f.suffix not in TEXT_SUFFIXES:
                continue
            hits = scan_text(f.read_text(encoding="utf-8", errors="replace"))
            if hits:
                report[str(f)] = hits
    return report
