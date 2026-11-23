import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lib.media import iter_media_files


class MediaDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def create_file(self, relative_path):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately not playable content: discovery must use extensions only.
        path.write_bytes(b"a discovery fixture, not an encoded video")
        return str(path)

    def test_finds_supported_videos_at_multiple_depths_case_insensitively(self):
        extensions = (
            ".mkv", ".mpg", ".mpeg", ".mp4", ".avi", ".mov", ".webm",
            ".m4v", ".wmv", ".flv", ".vob", ".ts", ".m2ts",
        )
        expected = {
            self.create_file("artist/album/disc/song" + extension.upper())
            for extension in extensions
        }
        expected.add(self.create_file("root-video.mp4"))
        expected.add(self.create_file("artist/artist-video.mkv"))

        self.assertEqual(set(iter_media_files(str(self.root))), expected)

    def test_excludes_audio_archives_documents_and_hidden_items(self):
        expected = self.create_file("visible/song.mp4")
        for extension in (
            ".mp3", ".m4a", ".wav", ".flac", ".aac", ".zip", ".rar",
            ".txt", ".html", ".htm", ".srt", ".cdg", ".png", ".jpg",
        ):
            self.create_file("visible/other" + extension)
        self.create_file("visible/.hidden.mp4")
        self.create_file(".hidden/album/song.mkv")
        self.create_file("visible/.metadata/song.mpg")
        self.create_file("visible/no-extension")
        (self.root / "directory.mp4").mkdir()

        self.assertEqual(list(iter_media_files(str(self.root))), [expected])

    def test_does_not_open_files_to_validate_contents(self):
        expected = self.create_file("artist/song.mkv")
        with patch("builtins.open", side_effect=AssertionError("Opened media contents")), \
                patch("io.open", side_effect=AssertionError("Opened media contents")):
            self.assertEqual(list(iter_media_files(str(self.root))), [expected])

    def test_keeps_distinct_files_with_identical_names(self):
        first = self.create_file("artist-one/song.mp4")
        second = self.create_file("artist-two/song.mp4")

        self.assertCountEqual(iter_media_files(str(self.root)), [first, second])

    def test_overlapping_roots_are_scanned_once_in_either_order(self):
        parent_video = self.create_file("parent.mp4")
        nested_video = self.create_file("artist/album/nested.mkv")
        child = self.root / "artist"

        for roots in ((self.root, child), (child, self.root)):
            with self.subTest(roots=roots):
                seen_directories = set()
                found = []
                for root in roots:
                    found.extend(iter_media_files(str(root), seen_directories))
                self.assertCountEqual(found, [parent_video, nested_video])

    def test_directory_symlinks_are_not_followed(self):
        expected = self.create_file("artist/song.mp4")
        outside = self.root / "outside"
        outside.mkdir()
        self.create_file("outside/other.mkv")
        library = self.root / "artist"
        try:
            (library / "loop").symlink_to(library, target_is_directory=True)
            (library / "linked-directory").symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest("Directory symlinks unavailable: " + str(exc))

        self.assertEqual(list(iter_media_files(str(library))), [expected])

    def test_missing_root_is_logged_and_skipped(self):
        with self.assertLogs(level="WARNING"):
            self.assertEqual(list(iter_media_files(str(self.root / "missing"))), [])

    def test_unreadable_subdirectory_is_logged_and_other_videos_are_found(self):
        expected = self.create_file("readable/song.mp4")
        self.create_file("unreadable/hidden-by-permissions.mkv")
        unreadable = self.root / "unreadable"
        original_scandir = os.scandir

        def scandir_with_unreadable_directory(path):
            if os.fspath(path) == str(unreadable):
                raise PermissionError("Permission denied for test directory")
            return original_scandir(path)

        with patch("lib.media.os.scandir", side_effect=scandir_with_unreadable_directory):
            with self.assertLogs(level="WARNING"):
                found = list(iter_media_files(str(self.root)))

        self.assertEqual(found, [expected])


if __name__ == "__main__":
    unittest.main()
