"""Fast recursive discovery of local karaoke videos without opening their contents."""

import logging
import os

from constants import media_types


def iter_media_files(media_dir, seen_directories=None):
    """Yield videos, sharing visited directories across roots to avoid overlap.

    Directory entries reuse file-type metadata where the filesystem supplies it,
    avoiding extra stat calls. Resolve only each configured root, rather than
    every video path, to reduce metadata lookups for large network libraries.
    """
    if seen_directories is None:
        seen_directories = set()
    media_dir = os.fspath(media_dir)
    root_key = os.path.normcase(os.path.realpath(media_dir))
    pending = [(media_dir, root_key)]

    while pending:
        directory, directory_key = pending.pop()
        if directory_key in seen_directories:
            continue
        try:
            with os.scandir(directory) as entries:
                seen_directories.add(directory_key)
                for entry in entries:
                    if entry.name.startswith('.'):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child_key = os.path.normcase(os.path.join(directory_key, entry.name))
                            pending.append((entry.path, child_key))
                        elif os.path.splitext(entry.name)[1].lower() in media_types and entry.is_file():
                            yield entry.path
                    except OSError as exc:
                        logging.warning("Unable to read media entry %s: %s", entry.path, exc)
        except OSError as exc:
            logging.warning("Unable to scan media directory %s: %s", directory, exc)
