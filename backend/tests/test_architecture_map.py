# -*- coding: utf-8 -*-
"""ARCHITECTURE.md (the code map at the repo root) lists every Python file.

A new file in backend/, memory_sidecar/ or scripts/ without a line in the
map fails here, so the map cannot silently go stale. Backend files are
listed by name (`chat.py`) in section 5; sidecar and script files by their
path (`scripts/launcher.py`) in sections 7 and 8.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAP = os.path.join(ROOT, "ARCHITECTURE.md")

# folder -> (how the map names its files, the section to add a missing one to)
FOLDERS = {
    "backend": (lambda name: "`%s`" % name,
                'section 5 "Backend modules (`backend/`)"'),
    "memory_sidecar": (lambda name: "`memory_sidecar/%s`" % name,
                       'section 7 "Memory sidecar (`memory_sidecar/`)"'),
    "scripts": (lambda name: "`scripts/%s`" % name,
                'section 8 "Launcher and scripts (`scripts/`)"'),
}


def _python_files(folder):
    path = os.path.join(ROOT, folder)
    return sorted(f for f in os.listdir(path)
                  if f.endswith(".py") and os.path.isfile(os.path.join(path, f)))


class ArchitectureMapTest(unittest.TestCase):

    def test_every_python_file_is_on_the_map(self):
        with open(MAP, encoding="utf-8") as fh:
            text = fh.read()
        missing = []
        for folder, (spelled, section) in FOLDERS.items():
            for name in _python_files(folder):
                if spelled(name) not in text:
                    missing.append("  %s/%s is not in ARCHITECTURE.md: add a row "
                                   "%s to the table in %s (one line: what the file owns)."
                                   % (folder, name, spelled(name), section))
        self.assertEqual(missing, [], "\n" + "\n".join(missing))

    def test_listed_files_exist(self):
        # The other way round: a row for a file that was deleted or renamed.
        with open(MAP, encoding="utf-8") as fh:
            text = fh.read()
        stale = []
        for name in sorted(set(_tokens(text))):
            if name.startswith(("scripts/", "memory_sidecar/")):
                path = name
            elif "/" in name:
                continue
            else:
                path = os.path.join("backend", name)
            if not os.path.isfile(os.path.join(ROOT, path)):
                stale.append("  ARCHITECTURE.md lists `%s`, but %s does not exist: "
                             "remove or rename its row." % (name, path))
        self.assertEqual(stale, [], "\n" + "\n".join(stale))


def _tokens(text):
    """Every `name.py` written in backticks (not `chat.generate_greeting`)."""
    parts = text.split("`")
    for i in range(1, len(parts), 2):
        token = parts[i]
        if token.endswith(".py") and " " not in token:
            yield token


if __name__ == "__main__":
    unittest.main()
