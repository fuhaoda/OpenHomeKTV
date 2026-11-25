import gzip
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from karaoke import Karaoke
from lib.vlcclient import PlaybackError, VLCClient


class DummyPlayer:
    """A local stateful player double; it never starts subprocesses or sockets."""
    def __init__(self):
        self.running = False
        self.is_transposing = False
        self.volume = 80
        self.paused = False
        self.time = 42
        self.rate = 1.0
        self.track_ids = [1, 2]
        self.current_track = 2
        self.starts = []
        self.kills = 0

    def xml(self):
        return (
            "<root><information><category name='Stream 0'><info name='Type'>Video</info></category></information>"
            f"<volume>{self.volume}</volume><state>{'paused' if self.paused else 'playing'}</state>"
            f"<time>{self.time}</time><rate>{self.rate}</rate></root>"
        )

    def play_file(self, path, volume, params):
        self.starts.append((path, volume, list(params)))
        self.running = True
        self.volume = volume
        self.paused = '--start-paused' in params
        for parameter in params:
            if parameter.startswith('--audio-track-id='):
                self.current_track = int(parameter.split('=', 1)[1])
        return self.xml()

    def play_file_transpose(self, path, semitones, volume, params):
        return self.play_file(path, volume, params)

    def is_running(self):
        return self.running

    def command(self, command='', save_status=True):
        return SimpleNamespace(text=self.xml(), status_code=200)

    def get_info_xml(self, xml=None):
        return {'position': 0.42, 'length': 100, 'time': self.time, 'volume': self.volume,
                'state': 'paused' if self.paused else 'playing', 'rate': self.rate}

    get_val_xml = VLCClient.get_val_xml

    def get_audio_track_info(self, xml=None, file_path=None):
        return self.current_track, list(self.track_ids), 'test'

    def stop(self):
        self.running = False

    def kill(self):
        self.kills += 1
        self.running = False

    def pause(self):
        self.paused = True
        return self.command()

    def play(self):
        self.paused = False
        return self.command()

    def vol_set(self, value):
        self.volume = value
        return self.command()

    def vol_up(self):
        return self.vol_set(self.volume + 10)

    def vol_down(self):
        return self.vol_set(self.volume - 10)

    def playspeed_set(self, value):
        self.rate = value
        return self.command()


class KaraokeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = self.create_video('artist-one/song.mp4')
        self.second = self.create_video('artist-two/song.mp4')
        self.k = Karaoke.__new__(Karaoke)
        self.k.state_lock = threading.RLock()
        self.k.playback_lock = threading.RLock()
        self.k.event_dirty = threading.Event()
        self.k.queue = []
        self.k.queue_json = '[]'
        self.k.available_songs = [self.first, self.second]
        self.k.player_state = {}
        self.k.platform = 'osx'
        self.k.hide_splash_screen = True
        self.k.library_root = str(self.root)
        self.k.save_delays = None
        self.k.dft_delays_file = str(self.root / '.delays')
        self.k.delays = {}
        self.k.delays_dirty = False
        self.k.song2vol = {}
        self.k.volume = 80
        self.k.normalize_vol = False
        self.k.logical_volume = None
        self.k.now_playing = None
        self.k.now_playing_filename = None
        self.k.now_playing_user = None
        self.k.pending_audio_track_index = None
        self.k.pending_audio_track_cli_index = None
        self.k.switchingSong = False
        self.k.audio_delay = self.k.subtitle_delay = 0
        self.k.play_speed = 1
        self.k.now_playing_transpose = 0
        self.k.show_subtitle = True
        self.k.audio_track_index = self.k.audio_track_total = 1
        self.k.vlcclient = DummyPlayer()
        self.k.splash_delay = 0
        self.k.render_splash_screen = Mock()

    def create_video(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return str(path)

    def start(self):
        self.assertTrue(self.k.play_file(self.first))

    def test_successful_start_selects_second_track_and_finishes_transition(self):
        self.start()
        self.assertIn('--audio-track-id=2', self.k.vlcclient.starts[-1][2])
        self.assertEqual(self.k.audio_track_index, 2)
        self.assertEqual(self.k.now_playing_filename, self.first)
        self.assertFalse(self.k.switchingSong)
        self.assertIsNone(self.k.pending_audio_track_index)

    def test_first_song_starts_at_requested_initial_volume_250(self):
        self.k.volume = 250
        self.start()
        self.assertEqual(self.k.vlcclient.starts[0][1], 250)
        self.assertEqual(self.k.volume, 250)

    def test_postlaunch_audio_metadata_applies_preferred_track_not_just_label(self):
        self.k.vlcclient.current_track = 1
        def tracks(xml=None, file_path=None):
            if not self.k.vlcclient.running:
                return None, [], 'unknown'
            return self.k.vlcclient.current_track, [1, 2], 'xml'
        self.k.vlcclient.get_audio_track_info = tracks
        self.start()
        self.assertEqual(len(self.k.vlcclient.starts), 2)
        self.assertNotIn('--audio-track-id=2', self.k.vlcclient.starts[0][2])
        self.assertIn('--audio-track-id=2', self.k.vlcclient.starts[1][2])
        self.assertEqual(self.k.vlcclient.current_track, 2)
        self.assertEqual(self.k.audio_track_index, 2)

    def test_paused_reload_and_transpose_preserve_pause_and_position(self):
        self.start()
        self.k.pause()
        self.assertTrue(self.k.reload_current_track())
        self.assertTrue(self.k.is_paused)
        self.assertIn('--start-paused', self.k.vlcclient.starts[-1][2])
        self.assertIn('--start-time=42.0', self.k.vlcclient.starts[-1][2])
        self.assertTrue(self.k.play_transposed('3'))
        self.assertTrue(self.k.is_paused)
        self.assertIn('--start-paused', self.k.vlcclient.starts[-1][2])

    def test_paused_audio_switch_changes_track_without_resuming(self):
        self.start()
        self.k.pause()
        self.assertTrue(self.k.next_audio_track())
        self.assertEqual(self.k.audio_track_index, 1)
        self.assertIn('--audio-track-id=1', self.k.vlcclient.starts[-1][2])
        self.assertTrue(self.k.is_paused)

    def test_subtitle_toggle_reloads_with_explicit_disable(self):
        self.start()
        self.assertTrue(self.k.toggle_subtitle())
        self.assertIn('--sub-track=-1', self.k.vlcclient.starts[-1][2])
        self.assertFalse(self.k.show_subtitle)

    def test_failed_start_restores_state_and_reraises_explicit_error(self):
        self.k.vlcclient.play_file = Mock(side_effect=PlaybackError('broken input'))
        with self.assertRaises(PlaybackError):
            self.k.play_file(self.first)
        self.assertFalse(self.k.switchingSong)
        self.assertIsNone(self.k.now_playing_filename)
        self.assertIsNone(self.k.pending_audio_track_cli_index)
        self.assertEqual(self.k.vlcclient.kills, 1)
        self.assertTrue(self.k.event_dirty.is_set())

    def test_empty_player_result_is_handled_as_playback_failure(self):
        self.k.vlcclient.play_file = Mock(return_value=None)
        with self.assertRaises(PlaybackError):
            self.k.play_file(self.first)
        self.assertFalse(self.k.switchingSong)
        self.assertIsNone(self.k.now_playing)

    def test_main_loop_skips_failed_song_and_starts_following_song(self):
        self.k.enqueue(self.first, 'one')
        self.k.enqueue(self.second, 'two')
        original = self.k.vlcclient.play_file
        seen = []
        def play(path, volume, params):
            seen.append(path)
            if path == self.first:
                raise PlaybackError('unreadable video')
            return original(path, volume, params)
        self.k.vlcclient.play_file = play
        self.k.handle_run_loop = lambda: setattr(self.k, 'running', False)
        with self.assertLogs(level='ERROR'):
            self.k.run()
        self.assertEqual(seen, [self.first, self.second])
        self.assertEqual(self.k.now_playing_user, 'two')
        self.assertEqual(self.k.queue, [])
        self.assertFalse(self.k.switchingSong)

    def test_start_does_not_hold_global_state_lock_or_block_other_controls(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.k.vlcclient.play_file
        errors = []
        def slow_play(*args):
            entered.set()
            if not release.wait(2):
                raise TimeoutError('test did not release player')
            return original(*args)
        self.k.vlcclient.play_file = slow_play
        def worker():
            try:
                self.k.play_file(self.first)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(1))
        try:
            self.assertTrue(self.k.state_lock.acquire(timeout=0.1))
            self.k.state_lock.release()
            self.assertEqual(self.k.get_state()['volume'], 80)
            self.assertTrue(self.k.enqueue(self.second))
            self.assertFalse(self.k.skip())
            self.assertFalse(self.k.pause())
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_pause_during_reload_reports_busy_without_changing_pause_state(self):
        self.start()
        entered, release = threading.Event(), threading.Event()
        original_command = self.k.vlcclient.command
        errors = []
        def slow_status(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError('test did not release metadata')
            return original_command(*args, **kwargs)
        self.k.vlcclient.command = slow_status
        def worker():
            try:
                self.k.reload_current_track()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertFalse(self.k.pause())
            self.assertFalse(self.k.is_paused)
            self.assertTrue(self.k.state_lock.acquire(timeout=0.1))
            self.k.state_lock.release()
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.k.is_paused)
        self.assertNotIn('--start-paused', self.k.vlcclient.starts[-1][2])

    def test_clear_empty_player_succeeds_but_reports_busy_player_stop(self):
        self.k.enqueue(self.first)
        self.assertTrue(self.k.queue_clear())
        self.assertEqual(self.k.queue, [])
        self.start()
        self.k.enqueue(self.second)
        entered, release = threading.Event(), threading.Event()
        def busy_player():
            with self.k.playback_lock:
                entered.set()
                release.wait(2)
        thread = threading.Thread(target=busy_player)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            self.assertFalse(self.k.queue_clear())
            self.assertEqual(self.k.queue, [])
            self.assertTrue(self.k.vlcclient.running)
        finally:
            release.set()
            thread.join(2)

    def test_enqueue_rejects_paths_outside_catalog_and_duplicate_paths(self):
        self.assertFalse(self.k.enqueue('/outside/other.mp4'))
        self.assertFalse(self.k.enqueue(None))
        self.assertTrue(self.k.enqueue(self.first))
        self.assertFalse(self.k.enqueue(self.first))
        self.assertTrue(self.k.enqueue(self.second))
        self.assertEqual([song['title'] for song in self.k.queue], ['song', 'song'])

    def test_queue_edit_matches_entire_path(self):
        extended = self.create_video('artist-one/song.mp4.old.mp4')
        self.k.available_songs.append(extended)
        self.k.enqueue(extended)
        self.k.enqueue(self.first)
        self.assertTrue(self.k.queue_edit(self.first, 'delete'))
        self.assertEqual([song['file'] for song in self.k.queue], [extended])

    def test_queue_reorder_rejects_stale_size_and_negative_indices(self):
        self.k.enqueue(self.first)
        self.k.enqueue(self.second)
        for request in ({'src': -1, 'tgt': 0, 'size': 2}, {'src': 0, 'tgt': 2, 'size': 2},
                        {'src': 0, 'tgt': 1, 'size': 3}, {'src': 'bad', 'tgt': 1, 'size': 2}):
            self.assertFalse(self.k.queue_edit(None, 'move', **request))
            self.assertEqual([song['file'] for song in self.k.queue], [self.first, self.second])
        self.assertTrue(self.k.queue_edit(None, 'move', src=0, tgt=1, size=2))
        self.assertEqual([song['file'] for song in self.k.queue], [self.second, self.first])

    def test_random_add_handles_exhausted_library_and_invalid_amount(self):
        self.k.enqueue(self.first)
        self.assertFalse(self.k.queue_add_random(3))
        self.assertEqual(len(self.k.queue), 2)
        self.assertFalse(self.k.queue_add_random(1))
        self.assertFalse(self.k.queue_add_random(-1))
        self.assertFalse(self.k.queue_add_random('bad'))

    def test_same_named_files_have_independent_delays(self):
        self.k.set_delays_dict(self.first, 'audio_delay', 0.5)
        self.k.set_delays_dict(self.second, 'audio_delay', -0.2)
        self.assertEqual(self.k.get_saved_delays(self.first)['audio_delay'], 0.5)
        self.assertEqual(self.k.get_saved_delays(self.second)['audio_delay'], -0.2)
        self.assertEqual(len(self.k.delays), 2)

    def test_legacy_delay_settings_load_but_reset_can_override_legacy_value(self):
        self.k.delays = {'song.mp4': {'audio_delay': 0.5, 'show_subtitle': False}}
        self.assertEqual(self.k.get_saved_delays(self.first)['audio_delay'], 0.5)
        self.k.set_delays_dict(self.first, 'audio_delay', 0)
        self.k.set_delays_dict(self.first, 'show_subtitle', True, True)
        self.assertEqual(self.k.get_saved_delays(self.first), {})
        self.assertEqual(self.k.get_saved_delays(self.second)['audio_delay'], 0.5)

    def test_delay_settings_are_saved_as_json_and_legacy_literals_remain_readable(self):
        self.k.save_delays = self.k.dft_delays_file
        Path(self.k.save_delays).write_text("{'song.mp4': {'audio_delay': 0.4, 'show_subtitle': False}}")
        self.k.init_save_delays()
        self.k.set_delays_dict(self.first, 'audio_delay', 0.8)
        self.k.auto_save_delays()
        persisted = json.loads(Path(self.k.save_delays).read_text())
        self.assertEqual(persisted[self.k.delay_key(self.first)]['audio_delay'], 0.8)
        self.assertFalse(self.k.delays_dirty)
        self.k.set_save_delays(False)
        self.assertTrue(Path(self.k.dft_delays_file).exists())

    def test_invalid_delay_settings_are_never_executed(self):
        self.k.save_delays = self.k.dft_delays_file
        marker = self.root / 'should-not-exist'
        Path(self.k.save_delays).write_text(f"__import__('pathlib').Path({str(marker)!r}).touch()")
        with self.assertLogs(level='WARNING'):
            self.k.init_save_delays()
        self.assertFalse(marker.exists())
        self.assertEqual(self.k.delays, {})

    def test_saved_delay_is_applied_only_to_its_target_file(self):
        self.k.save_delays = self.k.dft_delays_file
        self.k.set_delays_dict(self.first, 'audio_delay', 0.5)
        self.start()
        self.assertIn('--audio-desync=500.0', self.k.vlcclient.starts[-1][2])
        self.assertTrue(self.k.play_file(self.second))
        self.assertEqual(self.k.audio_delay, 0)
        self.assertFalse(any(arg.startswith('--audio-desync=') for arg in self.k.vlcclient.starts[-1][2]))

    def test_normalization_is_on_demand_bounded_and_uses_metadata_cache(self):
        self.k.normalize_vol = True
        result = SimpleNamespace(stderr='[Parsed_volumedetect] mean_volume: -12.0 dB')
        with patch('karaoke.subprocess.run', return_value=result) as run:
            first = self.k.get_mp3_volume(self.first)
            second = self.k.get_mp3_volume(self.first)
        self.assertAlmostEqual(first, 10 ** (6 / 20))
        self.assertEqual(first, second)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs['timeout'], 45)
        self.assertIn('volumedetect', run.call_args.args[0])
        self.assertNotIn('s16le', run.call_args.args[0])
        self.assertTrue(self.k.normalize_vol)
        with gzip.open(self.root / '.mp3_volume.json.gz', 'rt') as stream:
            self.assertIn(self.k.delay_key(self.first), json.load(stream))

    def test_loudness_cache_invalidates_after_file_changes_and_separates_same_names(self):
        result = SimpleNamespace(stderr='mean_volume: -18.0 dB')
        with patch('karaoke.subprocess.run', return_value=result) as run:
            self.k.get_mp3_volume(self.first)
            self.k.get_mp3_volume(self.second)
            Path(self.first).write_bytes(b'changed')
            self.k.get_mp3_volume(self.first)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(len(self.k.song2vol), 2)

    def test_loudness_failure_logs_and_preserves_enabled_setting(self):
        self.k.normalize_vol = True
        with patch('karaoke.subprocess.run', side_effect=subprocess.TimeoutExpired('ffmpeg', 45)):
            with self.assertLogs(level='WARNING'):
                self.assertEqual(self.k.get_mp3_volume(self.first), 1.0)
        self.assertTrue(self.k.normalize_vol)
        self.assertEqual(self.k.song2vol, {})

    def test_non_object_loudness_cache_is_ignored(self):
        with gzip.open(self.root / '.mp3_volume.json.gz', 'wt') as stream:
            json.dump([], stream)
        self.assertEqual(self.k.load_volume_cache(), {})

    def test_controls_ignore_invalid_or_nonfinite_values(self):
        self.start()
        for invalid in ('nan', 'inf', 'bad', None):
            self.assertFalse(self.k.vol_set(invalid))
            self.assertFalse(self.k.play_speed_set(invalid))
            self.assertFalse(self.k.set_audio_delay(invalid))
            self.assertFalse(self.k.set_subtitle_delay(invalid))
        self.assertFalse(self.k.vol_set(513))
        self.assertFalse(self.k.play_speed_set(0))
        self.assertEqual(self.k.vol_set('60'), 60)
        self.assertEqual(self.k.play_speed_set('1.2'), 1.2)

    def test_controls_tolerate_incomplete_player_status(self):
        self.start()
        response = SimpleNamespace(text='<root/>', status_code=500)
        self.k.vlcclient.vol_set = Mock(return_value=response)
        self.k.vlcclient.playspeed_set = Mock(return_value=response)
        self.assertFalse(self.k.vol_set(60))
        self.assertFalse(self.k.play_speed_set(1.2))
        self.assertEqual(self.k.volume, 80)

    def test_status_from_before_same_file_reload_cannot_replace_new_state(self):
        self.start()
        self.k.vlcclient.playback_generation = 1
        def old_response():
            self.k.vlcclient.playback_generation = 2
            self.k.volume = 60
            self.k.player_state = {'time': 0, 'volume': 60}
            return {'time': 42, 'volume': 80}
        self.k.vlcclient.get_info_xml = old_response
        with patch.object(self.k, 'update_audio_track_status') as update_audio:
            result = self.k.get_state()
        self.assertEqual(result['time'], 0)
        self.assertEqual(result['volume'], 60)
        update_audio.assert_not_called()

    def test_audio_status_ignores_transitioned_or_explicitly_stale_result(self):
        self.start()
        self.k.vlcclient.playback_generation = 1
        def old_tracks(*args):
            self.k.vlcclient.playback_generation = 2
            return 9, [7, 8, 9], 'xml'
        self.k.vlcclient.get_audio_track_info = old_tracks
        self.k.update_audio_track_status()
        self.assertEqual(self.k.audio_track_total, 2)
        self.k.vlcclient.get_audio_track_info = lambda *args: (None, [], 'stale')
        self.k.update_audio_track_status()
        self.assertEqual(self.k.audio_track_total, 2)

    def test_import_does_not_install_a_process_signal_handler(self):
        script = "import signal; marker=lambda signum,frame:None; signal.signal(signal.SIGTERM, marker); import karaoke; assert signal.getsignal(signal.SIGTERM) is marker"
        import sys
        subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, timeout=10)

    def test_no_current_song_delay_controls_do_not_attempt_path_operations(self):
        self.assertFalse(self.k.set_audio_delay('+'))
        self.assertFalse(self.k.set_subtitle_delay('+'))
        self.assertFalse(self.k.toggle_subtitle())

    def test_natural_song_end_resets_context_instead_of_erasing_detection_first(self):
        self.start()
        self.k.now_playing_user = 'Singer'
        self.k.audio_delay = 0.4
        self.k.vlcclient.running = False
        self.assertFalse(self.k.is_file_playing())
        self.assertIsNotNone(self.k.now_playing_filename)
        self.k.handle_run_loop = lambda: setattr(self.k, 'running', False)
        self.k.run()
        self.assertIsNone(self.k.now_playing_filename)
        self.assertIsNone(self.k.now_playing_user)
        self.assertEqual(self.k.audio_delay, 0)
        self.assertEqual(self.k.get_state()['time'], 0)

    def test_hidden_splash_loop_does_not_use_uninitialized_pygame_display(self):
        with patch('karaoke.pygame.event.get', side_effect=AssertionError('uninitialized display')):
            with patch('karaoke.time.sleep') as sleep:
                self.k.handle_run_loop()
        sleep.assert_called_once_with(0.1)


if __name__ == '__main__':
    unittest.main()
