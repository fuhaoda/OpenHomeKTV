import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from werkzeug.test import Client
from werkzeug.wrappers import Response

import app as application
from karaoke import Karaoke
from lib.get_platform import set_language
from lib.vlcclient import PlaybackError


def make_library(root):
    library = Karaoke.__new__(Karaoke)
    library.state_lock = threading.RLock()
    library.playback_lock = threading.RLock()
    library.event_dirty = threading.Event()
    library.queue = []
    library.queue_json = '[]'
    library.media_paths = [str(root)]
    library.library_root = str(root)
    library.volume = 128
    library.vlcclient = None
    library.player_state = {}
    library.pending_audio_track_index = None
    library.pending_audio_track_cli_index = None
    library.song2vol = {}
    library.logical_volume = None
    library.normalize_vol = False
    library.save_delays = None
    library.delays = {}
    library.delays_dirty = False
    library.hide_splash_screen = True
    library.platform = 'osx'
    library.url = 'http://127.0.0.1:5232/'
    library.logo_path = library.default_logo_path
    library.get_available_songs()
    return library


class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        set_language('en_US')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('Artist/Don\'t Stop.mp4', 'Artist/🎵.mp4', 'Artist/---song.mp4',
                     'Second/Don\'t Stop.mp4', 'Artist/literal%20name.mp4'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.library = make_library(self.root)
        self.previous = application.K
        application.K = self.library
        self.addCleanup(setattr, application, 'K', self.previous)
        self.client = Client(application.app, Response)

    def test_all_browser_views_render_without_a_running_player(self):
        for route in ('/', '/home', '/f_home', '/queue', '/f_queue', '/browse',
                      '/f_browse', '/info', '/f_info', '/browse?letter=numeric',
                      '/f_browse?letter=numeric', '/browse?page=-1', '/browse?page=99999'):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 200)

    def test_duplicate_title_search_keeps_both_files(self):
        for route in ('/browse', '/f_browse'):
            response = self.client.get(route, query_string={'q': "Don't Stop"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_data(as_text=True).count('class=\'add-song-link'), 2)

    def test_date_browser_caches_metadata_and_tolerates_missing_video(self):
        missing = next(path for path in self.library.available_songs if '🎵' in path)
        Path(missing).unlink()
        with self.assertLogs(level='WARNING'):
            self.assertEqual(self.client.get('/browse?sort=date').status_code, 200)
        with patch('lib.catalog.os.stat', side_effect=AssertionError('Repeated media stat')):
            self.assertEqual(self.library.song_catalog.select(by_date=True), self.library.song_catalog.select(by_date=True))

    def test_literal_percent_filename_is_not_decoded_twice_when_removing(self):
        song = next(path for path in self.library.available_songs if '%20' in path)
        self.assertTrue(self.library.enqueue(song, 'Singer'))
        response = self.client.get('/queue/edit', query_string={'action': 'delete', 'song': song})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.library.queue, [])

    def test_queue_notifications_show_the_title_without_its_directory(self):
        song = self.library.available_songs[0]
        self.library.enqueue(song, 'Singer')
        with patch.object(application, 'flash') as notification:
            self.client.get('/queue/edit', query_string={'action': 'delete', 'song': song})
        message = notification.call_args.args[0]
        self.assertIn(self.library.filename_from_path(song), message)
        self.assertNotIn(str(self.root), message)

    def test_enqueue_only_accepts_catalog_files_and_queue_endpoint_returns_data(self):
        first, second = [path for path in self.library.available_songs if "Don't Stop" in path]
        for song in (first, second):
            response = self.client.get('/enqueue', query_string={'song': song, 'user': '小明/Don\'t'})
            self.assertTrue(json.loads(response.get_data(as_text=True))['success'])
        denied = self.client.get('/enqueue', query_string={'song': '/outside/test.mp4', 'user': 'Singer'})
        self.assertFalse(json.loads(denied.get_data(as_text=True))['success'])
        queue = json.loads(self.client.get('/get_queue').get_data(as_text=True))
        self.assertEqual(len(queue), 2)
        self.assertEqual(queue[0]['title'], queue[1]['title'])

    def test_rename_accepts_slashes_and_quotes_without_path_routing(self):
        self.library.enqueue(self.library.available_songs[0], 'Old/Singer')
        response = self.client.post('/user_rename', data={'old_name': 'Old/Singer', 'new_name': '小明/Don\'t "Singer"'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.library.queue[0]['user'], '小明/Don\'t "Singer"')

    def test_missing_and_invalid_arguments_return_client_errors(self):
        for route in ('/enqueue', '/queue/addrandom?amount=invalid', '/queue/edit?action=move',
                      '/queue/edit?action=unknown', '/queue/addrandom?amount=-1'):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 400)

    def test_playback_errors_are_reported_as_recoverable_service_errors(self):
        with patch.object(self.library, 'next_audio_track', side_effect=PlaybackError('Timed out starting VLC')):
            response = self.client.get('/next_audio_track')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json['success'])

    def test_refresh_publishes_new_catalog_without_reusing_old_date_cache(self):
        old = self.library.song_catalog
        (self.root / 'new.mpg').touch()
        self.assertEqual(self.client.get('/refresh').status_code, 302)
        self.assertIsNot(self.library.song_catalog, old)
        self.assertEqual(len(self.library.available_songs), 6)

    def test_queue_updates_publish_even_during_song_switch(self):
        class StopLoop(BaseException):
            pass
        class Once:
            calls = 0
            def wait(self, timeout):
                self.calls += 1
                if self.calls > 1:
                    raise StopLoop()
            def clear(self):
                pass
        self.library.switchingSong = True
        self.library.event_dirty = Once()
        with patch.object(application, 'broadcast') as broadcast:
            with self.assertRaises(StopLoop):
                application.status_thread()
        broadcast.assert_called_once_with({'type': 'queue', 'data': []})

    def test_snapshot_discards_status_from_before_a_same_file_reload(self):
        self.library.vlcclient = SimpleNamespace(playback_generation=1)
        def old_status():
            self.library.vlcclient.playback_generation = 2
            return {'time': 99, 'length': 100, 'volume': 1}
        with patch.object(self.library, 'get_state', side_effect=old_status):
            self.assertEqual(application.get_nowplaying_snapshot(), {})

    def test_controls_report_busy_without_treating_zero_as_failure(self):
        with patch.object(self.library, 'vol_set', return_value=0):
            response = self.client.get('/vol/0')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), '0')
        with patch.object(self.library, 'set_audio_delay', return_value=0):
            self.assertEqual(self.client.get('/audio_delay/0').status_code, 200)
        with patch.object(self.library, 'pause', return_value=False):
            self.assertEqual(self.client.get('/pause').status_code, 409)
        with patch.object(self.library, 'queue_clear', return_value=False):
            self.assertEqual(self.client.get('/queue/edit?action=clear').status_code, 409)

    def test_drag_reorder_detects_concurrent_reorder_even_when_length_is_unchanged(self):
        songs = self.library.available_songs[:3]
        for song in songs:
            self.library.enqueue(song, 'Singer')
        self.assertTrue(self.library.queue_edit(songs[1], 'up'))
        before = [item['file'] for item in self.library.queue]
        response = self.client.post('/queue/edit', data={
            'action': 'move', 'from': '0', 'to': '2', 'size': '3', 'order': json.dumps(songs),
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual([item['file'] for item in self.library.queue], before)

    def test_drag_reorder_accepts_current_order_in_post_body(self):
        songs = self.library.available_songs[:3]
        for song in songs:
            self.library.enqueue(song, 'Singer')
        response = self.client.post('/queue/edit', data={
            'action': 'move', 'from': '0', 'to': '2', 'size': '3', 'order': json.dumps(songs),
        }, headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['file'] for item in self.library.queue], songs[1:] + songs[:1])


class ConfigurationTests(unittest.TestCase):
    def test_empty_local_configuration_falls_back_to_main(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / 'media_paths.conf').write_text('mv_path_0=Artist/Album\n', encoding='utf-8')
            (directory / 'media_paths.local.conf').write_text('# Empty local override\n', encoding='utf-8')
            self.assertEqual(application.load_media_paths(root), [str(directory / 'Artist/Album')])
            (directory / 'media_paths.local.conf').write_text('mv_path_0=Local/唱歌\n', encoding='utf-8')
            self.assertEqual(application.load_media_paths(root), [str(directory / 'Local/唱歌')])

    def test_missing_configurations_return_no_explicit_roots(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(application.load_media_paths(root), [])

    def test_language_resources_are_independent_of_working_directory(self):
        import os
        previous = os.getcwd()
        languages = getattr(os, 'langs', None)
        try:
            with tempfile.TemporaryDirectory() as root:
                os.chdir(root)
                if hasattr(os, 'langs'):
                    del os.langs
                set_language(None)
                self.assertIn('en_US', os.langs)
        finally:
            os.chdir(previous)
            if languages is not None:
                os.langs = languages
            set_language('en_US')


if __name__ == '__main__':
    unittest.main()
