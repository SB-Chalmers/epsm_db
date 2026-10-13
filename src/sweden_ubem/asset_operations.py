"""Journaled, identity-checked local asset operations. No implicit deletion targets."""
from __future__ import annotations

import json
import os
import shutil
import ctypes
import errno
import sys
from datetime import datetime, timezone
from pathlib import Path

from .hashing import sha256_file


def identity(path, *, checksum=True):
    path = Path(path)
    stat = path.lstat()
    result = {"device": stat.st_dev, "inode": stat.st_ino, "bytes": stat.st_size,
              "mtime_ns": stat.st_mtime_ns, "mode": stat.st_mode}
    if path.is_symlink():
        result["symlink"] = os.readlink(path)
    elif path.is_file() and checksum:
        result["sha256"] = sha256_file(path)
    return result


def publish_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def safe_relative(value):
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Bundle path must be relative: {value}")
    return path


def rename_exclusive(source, destination):
    """Atomic no-overwrite publication on macOS/Linux; safe fallback elsewhere."""
    library = ctypes.CDLL(None, use_errno=True)
    old, new = os.fsencode(source), os.fsencode(destination)
    if sys.platform == "darwin":
        result = library.renamex_np(ctypes.c_char_p(old), ctypes.c_char_p(new), 4)
    elif hasattr(library, "renameat2"):
        result = library.renameat2(-100, ctypes.c_char_p(old), -100, ctypes.c_char_p(new), 1)
    else:
        os.link(source, destination)
        os.unlink(source)
        return
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def relocation_note(original, destination, root, digest):
    return (f"Original location: {original}\nCurrent location: {destination}\n"
            f"Bundle-relative path: {Path(destination).relative_to(root)}\nSHA-256: {digest}\n"
            f"Relocation date: {datetime.now(timezone.utc).date().isoformat()}\n\n"
            "Consumers must configure the new source root. No compatibility symlink is provided.\n")


def same_filesystem(source, destination_parent):
    return source.stat().st_dev == destination_parent.stat().st_dev


def relocate(entry, root, *, after_publish=None, note=True):
    """Resume prepared/published moves without overwriting a conflicting destination."""
    root = Path(root).resolve()
    source = Path(entry["original"])
    destination = root / safe_relative(entry["destination"])
    if not destination.resolve().is_relative_to(root):
        raise ValueError("Destination escapes bundle root")
    expected = entry["identity"]
    digest = expected["sha256"]
    if source.exists() and identity(source) != expected:
        raise ValueError(f"Source identity changed: {source}")
    if destination.exists():
        if destination.is_symlink() or sha256_file(destination) != digest:
            raise ValueError(f"Conflicting relocation destination: {destination}")
    else:
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Missing relocation source: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if same_filesystem(source, destination.parent):
            rename_exclusive(source, destination)
        else:
            temporary = destination.with_name(destination.name + ".relocating")
            if temporary.exists() and sha256_file(temporary) != digest:
                temporary.unlink()
            if not temporary.exists():
                shutil.copy2(source, temporary)
            if sha256_file(temporary) != digest:
                raise ValueError(f"Copied asset checksum mismatch: {temporary}")
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            rename_exclusive(temporary, destination)
    if after_publish:
        after_publish()
    if sha256_file(destination) != digest:
        raise ValueError(f"Published asset checksum mismatch: {destination}")
    if source.exists() and source != destination:
        if identity(source) != expected:
            raise ValueError(f"Duplicate identity changed: {source}")
        source.unlink()
    if source != destination and note:
        note = source.with_name(source.name + ".RELOCATED.txt")
        text = relocation_note(source, destination, root, digest)
        if note.exists() and note.read_text() != text:
            # An earlier date remains valid during an interrupted/resumed relocation.
            if f"SHA-256: {digest}\n" not in note.read_text() or str(destination) not in note.read_text():
                raise ValueError(f"Conflicting relocation note: {note}")
        else:
            note.write_text(text)
    return destination


def deletion_manifest(targets, *, protected, checksum=True):
    protected = [Path(p).resolve() for p in protected]
    records = []
    for target in targets:
        path = Path(target).absolute()
        resolved = path.resolve()
        if any(resolved == p or resolved.is_relative_to(p) or p.is_relative_to(resolved)
               for p in protected):
            raise ValueError(f"Protected deletion target: {path}")
        if not path.exists() and not path.is_symlink():
            continue
        records.append({"path": str(path), "identity": identity(path, checksum=checksum)})
        if path.is_dir() and not path.is_symlink():
            for child in sorted(path.rglob("*")):
                if child.is_symlink() and any(child.resolve().is_relative_to(p) for p in protected):
                    raise ValueError(f"Deletion symlink reaches protected data: {child}")
                records.append({"path": str(child), "identity": identity(child, checksum=checksum)})
    return {"targets": [str(Path(t).absolute()) for t in targets], "files": records,
            "protected": [str(p) for p in protected], "checksum": checksum}


def delete_listed(manifest, targets):
    requested = {str(Path(t).absolute()) for t in targets}
    if requested != set(manifest["targets"]):
        raise ValueError("Refusing unlisted deletion targets")
    current = deletion_manifest(targets, protected=manifest["protected"],
                                checksum=manifest["checksum"])
    if current["files"] != manifest["files"]:
        raise ValueError("Deletion file identities or directory closure changed")
    for target in targets:
        path = Path(target)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
