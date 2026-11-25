import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lib.catalog import MediaCatalog
from lib.media import media_title


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def video(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return str(path)

    def test_nonempty_titles_and_numeric_category(self):
        paths = [self.video(name) for name in ('🎵.mp4', '---song.mp4', '123.mpg', '阿里山.mkv')]
        catalog = MediaCatalog([str(self.root)])
        self.assertTrue(all(media_title(path) for path in paths))
        self.assertIn(paths[0], catalog.select(letter='numeric'))
        self.assertIn(paths[1], catalog.select(query='---song'))
        self.assertIn(paths[3], catalog.select(letter='a'))

    def test_same_names_remain_distinct_and_root_order_is_preserved(self):
        first = self.video('first/Zulu.mp4')
        duplicate = self.video('first/album/Zulu.mp4')
        second = self.video('second/Alpha.mp4')
        catalog = MediaCatalog([str(self.root / 'first'), str(self.root / 'second')])
        self.assertCountEqual(catalog.songs[:2], [first, duplicate])
        self.assertEqual(catalog.songs[-1], second)

    def test_search_filters_before_date_metadata_on_sixty_thousand_songs(self):
        paths = ['/synthetic/Song%05d.mp4' % number for number in range(60000)]
        with patch('lib.catalog.iter_media_files', return_value=iter(paths)):
            catalog = MediaCatalog(['/synthetic'])
        with patch('lib.catalog.os.stat', return_value=SimpleNamespace(st_ctime=1)) as stat:
            self.assertEqual(catalog.select(query='Song12345', by_date=True), (paths[12345],))
            self.assertEqual(stat.call_count, 1)
            catalog.select(query='Song12345', by_date=True)
            self.assertEqual(stat.call_count, 1)

    def test_dates_are_cached_until_refresh_and_missing_videos_are_skipped(self):
        first = self.video('first.mp4')
        missing = self.video('missing.mkv')
        catalog = MediaCatalog([str(self.root)])
        os.remove(missing)
        original_stat = os.stat
        with patch('lib.catalog.os.stat', wraps=original_stat) as stat:
            with self.assertLogs(level='WARNING'):
                self.assertEqual(catalog.select(by_date=True), (first,))
            self.assertEqual(stat.call_count, 2)
            self.assertEqual(catalog.select(by_date=True), (first,))
            self.assertEqual(stat.call_count, 2)
        self.video('new.mp4')
        refreshed = MediaCatalog([str(self.root)])
        self.assertEqual(len(refreshed.songs), 2)

    def test_alpha_discovery_never_opens_or_stats_video_contents(self):
        self.video('Artist/Video.MKV')
        with patch('builtins.open', side_effect=AssertionError('Unexpected content read')):
            catalog = MediaCatalog([str(self.root)])
            self.assertEqual(len(catalog.select()), 1)


if __name__ == '__main__':
    unittest.main()
