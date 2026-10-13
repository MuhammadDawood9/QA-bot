"""Reading and saving client files, safely, for many people at once.

    store = ClientStore(config)
    store.codes()                           # ["E2AB", "E8D", ...]
    store.save(built, action="add")         # validates, blocks a duplicate code, backs up, logs

Layout (inside the data folder, which is shared in a floor install):

    clients/<CODE>.yaml                  the client's rules (src/clients/model.py)
    clients/<CODE>/notes.md              what an import couldn't map, with where it came from
    clients/<CODE>/sources/              copies of the files imported for this client
    color.yaml                           "clients: <CODE>:" holds the client's color overrides
    backups/clients/<CODE>/              the previous version of the client file, on every save
    backups/color/                       the previous color.yaml, when a save changes it
    changelog.csv                        who changed what, when, from which PC
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
import shutil
from pathlib import Path
from typing import Iterable

import yaml

from src.clients.model import BuiltClient, ClientError, clean_code, describe_changes
from src.config import load_yaml, normalize_client_code
from src.utils.safe_io import ChangeEntry, append_changelog, atomic_write_text, backup, current_user

CLIENT_HEADER = """\
# Client rules for {code}. Edited from the app (right-click the mascot > Client > Edit client...),
# which backs up the previous version and logs the change in changelog.csv. Hand edits work too.
#   glossary:   "=" in front = forced spelling, exactly like this
#   ignore:     flags hidden for this client (same fields as ignore.yaml, without client:)
#   overrides:  same structure as "checks:" in config.yaml, e.g. too_short: {{min_words: 3}}
#   ocr:        region: where the burned-in captions are, as fractions of the frame
# Color overrides for this client are in color.yaml under clients: {code}.
"""


def dump_yaml(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


def write_color_clients(path: Path, clients: dict) -> None:
    """Replace the "clients:" block of color.yaml, keeping everything above it (the commented
    defaults) exactly as it is."""
    text = path.read_text(encoding="utf-8") if path.is_file() else "defaults: {}\n"
    m = re.search(r"(?m)^clients:.*$", text)
    head = text[:m.start()] if m else text.rstrip("\n") + "\n\n"
    if clients:
        body = "clients:\n" + "".join("  " + line + "\n" for line in dump_yaml(clients).splitlines())
    else:
        body = "clients: {}\n"
    atomic_write_text(path, head + body)


class ClientStore:
    def __init__(self, config: dict, lock=None, root: Path | None = None):
        """root: where to read and write (the shared folder itself on the floor, so a duplicate
        code saved a minute ago on another PC is seen); default the config's data folder."""
        self.config = config
        paths = config.get("paths") or {}
        root = Path(root) if root is not None else Path(config["_root"])
        self.clients_dir = root / paths.get("clients_dir", "clients")
        self.color_file = root / paths.get("color_file", "color.yaml")
        self.backups_dir = root / paths.get("backups_dir", "backups")
        self.changelog = root / paths.get("changelog_file", "changelog.csv")
        # A context manager held while saving (the shared data lock, src/shared.py); none = no lock.
        self.lock = lock or contextlib.nullcontext

    # --- reading -----------------------------------------------------------------------------

    def _files(self) -> dict[str, Path]:
        if not self.clients_dir.is_dir():
            return {}
        return {normalize_client_code(p.stem): p for p in sorted(self.clients_dir.glob("*.yaml"))}

    def codes(self) -> list[str]:
        return sorted(self._files())

    def path(self, code: str) -> Path:
        code = clean_code(code)
        return self._files().get(code, self.clients_dir / f"{code}.yaml")

    def exists(self, code: str) -> bool:
        """"2CS", "e2cs" and "E2CS" are the same client."""
        return normalize_client_code(code) in self._files()

    def load(self, code: str) -> dict:
        path = self.path(code)
        return load_yaml(path) if path.is_file() else {}

    def names(self) -> dict[str, str]:
        """code -> display name (the code itself when the file has none)."""
        out = {}
        for code, path in self._files().items():
            try:
                data = load_yaml(path)
            except (OSError, yaml.YAMLError):
                data = {}
            out[code] = str(data.get("name") or data.get("project") or code)
        return out

    def color_clients(self) -> dict:
        if not self.color_file.is_file():
            return {}
        data = load_yaml(self.color_file)
        return {normalize_client_code(str(k)): v or {} for k, v in (data.get("clients") or {}).items()}

    def color_block(self, code: str) -> dict:
        return dict(self.color_clients().get(normalize_client_code(code)) or {})

    def notes_file(self, code: str) -> Path:
        return self.clients_dir / clean_code(code) / "notes.md"

    # --- saving -----------------------------------------------------------------------------

    def save(self, built: BuiltClient, action: str, sources: Iterable[Path] = (), user: str | None = None) -> str:
        """Write the client: backup of the previous file, the new file, its color block, notes and
        copies of the imported files, and a changelog row. Returns the change summary.
        action "add" refuses a code that's already used; "edit" needs the client to exist."""
        user = user or current_user()
        code = clean_code(built.code)
        with self.lock():
            exists = self.exists(code)
            if action == "add" and exists:
                raise ClientError(f"There is already a client {code}. Use Edit client... to change it.")
            if action == "edit" and not exists:
                raise ClientError(f"There is no client {code} to edit.")
            path = self.path(code)
            old = self.load(code) if exists else {}
            all_color = self.color_clients()
            old_color = dict(all_color.get(code) or {})

            changes = describe_changes({**old, "color": old_color}, {**built.client, "color": built.color})
            if built.notes:
                changes = (changes + "; " if changes else "") + f"notes.md: +{len(built.notes)} line(s)"
            sources = [Path(s) for s in sources]
            if sources:
                changes = (changes + "; " if changes else "") + "imported " + ", ".join(s.name for s in sources)
            if not changes:
                return ""

            if exists:
                backup(path, self.backups_dir / "clients" / code, user)
            atomic_write_text(path, CLIENT_HEADER.format(code=code) + dump_yaml(built.client))

            if built.color != old_color:
                backup(self.color_file, self.backups_dir / "color", user)
                if built.color:
                    all_color[code] = built.color
                else:
                    all_color.pop(code, None)
                write_color_clients(self.color_file, dict(sorted(all_color.items())))

            if built.notes:
                notes = self.notes_file(code)
                before = notes.read_text(encoding="utf-8") if notes.is_file() else f"# Notes for {code}\n"
                stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
                block = f"\n## {stamp}, {user}\n\n" + "".join(f"- {line}\n" for line in built.notes)
                atomic_write_text(notes, before.rstrip("\n") + "\n" + block)

            for src in sources:
                if src.is_file():
                    folder = self.clients_dir / code / "sources"
                    folder.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, folder / f"{dt.datetime.now():%Y%m%d-%H%M%S}_{src.name}")

            append_changelog(self.changelog, ChangeEntry(file=f"clients/{code}.yaml", action=action, changes=changes,
                                                         client=code, user=user))
        return changes
