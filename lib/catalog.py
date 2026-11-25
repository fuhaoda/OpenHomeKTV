"""An immutable song listing with lazy, per-refresh filesystem metadata caches."""

import logging
import os
import threading

from unidecode import unidecode

from lib.media import iter_media_files, media_title


class MediaCatalog:
    def __init__(self, media_paths):
        self.sort_names = {}
        self.search_names = {}
        songs = []
        seen_directories = set()
        for media_path in media_paths:
            group = []
            for filename in iter_media_files(media_path, seen_directories):
                title = media_title(filename)
                translated = unidecode(title).lower().lstrip()
                while translated and not translated[0].isalnum():
                    translated = translated[1:]
                self.sort_names[filename] = translated
                self.search_names[filename] = title.casefold()
                group.append(filename)
            group.sort(key=lambda filename: (self.sort_names[filename], filename))
            songs.extend(group)
        self.songs = tuple(songs)
        self.song_set = frozenset(songs)
        self._created_times = {}
        self._metadata_lock = threading.RLock()
        self._date_order = None

    def select(self, query="", letter=None, by_date=False):
        candidates = self.songs
        if query:
            folded = query.casefold()
            candidates = [song for song in candidates if folded in self.search_names[song]]
        elif letter == "numeric":
            candidates = [song for song in candidates if not self.sort_names[song][:1].isalpha()]
        elif letter:
            candidates = [song for song in candidates if self.sort_names[song].startswith(letter.lower())]
        return self.by_date(candidates) if by_date else candidates

    def by_date(self, candidates):
        complete_catalog = candidates is self.songs
        with self._metadata_lock:
            if complete_catalog and self._date_order is not None:
                return self._date_order
        for song in candidates:
            with self._metadata_lock:
                if song in self._created_times:
                    continue
            try:
                created = os.stat(song).st_ctime
            except OSError as exc:
                logging.warning("Skipping unavailable video %s: %s", song, exc)
                created = None
            with self._metadata_lock:
                self._created_times.setdefault(song, created)
        with self._metadata_lock:
            result = tuple(sorted(
                (song for song in candidates if self._created_times[song] is not None),
                key=lambda song: (self._created_times[song], song), reverse=True,
            ))
            if complete_catalog:
                self._date_order = result
        return result
