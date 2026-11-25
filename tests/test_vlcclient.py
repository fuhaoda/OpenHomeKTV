import io
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from lib.vlcclient import PlaybackError, VLCClient


def status(volume=50, state='playing', tracks=(1, 2), current=1):
    audio = ''.join('<track id="%s" />' % track for track in tracks)
    return ('<root><state>%s</state><volume>%s</volume><audiotrack>%s</audiotrack>'
            '<audio>%s</audio><information><category name="Stream 0">'
            '<info name="Type">Video</info></category></information></root>') % (state, volume, current, audio)


def response(xml, code=200):
    return SimpleNamespace(text=xml, status_code=code)


class Process:
    def __init__(self, exited=False):
        self.returncode = 1 if exited else None
        self.stdin = io.BytesIO()
        self.killed = False
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired('fake VLC', timeout)
        return self.returncode


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class VLCClientTests(unittest.TestCase):
    def setUp(self):
        with patch('lib.vlcclient.get_platform', return_value='linux'):
            self.client = VLCClient(path='/fake/vlc')
        self.client.startup_timeout = 0.3
        self.clock = Clock()
        self.monotonic = patch('lib.vlcclient.time.monotonic', side_effect=self.clock.monotonic).start()
        self.sleep = patch('lib.vlcclient.time.sleep', side_effect=self.clock.sleep).start()
        self.addCleanup(patch.stopall)

    def launch(self, process=None, replies=None, volume=50, params=None):
        process = process or Process()
        with patch('lib.vlcclient.subprocess.Popen', return_value=process) as popen:
            with patch('lib.vlcclient.requests.get', side_effect=replies) as get:
                result = self.client.play_file('/media/song.mp4', volume, params)
        return result, process, get, popen

    def test_successful_start_records_current_file_and_actual_status(self):
        xml = status()
        result, process, get, popen = self.launch(replies=[response(xml), response(xml)])
        self.assertEqual(result, xml)
        self.assertEqual(self.client.current_file_path, '/media/song.mp4')
        self.assertEqual(self.client.last_status_text, xml)
        self.assertFalse(self.client.is_transposing)
        self.assertFalse(process.killed)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(popen.call_args.args[0][-1], '/media/song.mp4')

    def test_zero_volume_is_explicitly_restored(self):
        result, _, get, _ = self.launch(volume=0, replies=[response(status(80)), response(status(0))])
        self.assertIn('volume&val=0', get.call_args_list[-1].args[0])
        self.assertEqual(self.client.get_val_xml(result, 'volume'), '0')

    def test_numeric_string_volume_is_accepted(self):
        _, _, get, _ = self.launch(volume='50', replies=[response(status()), response(status())])
        self.assertIn('volume&val=50', get.call_args_list[-1].args[0])

    def test_normalized_volume_is_clamped_to_vlc_range(self):
        _, _, get, _ = self.launch(volume=800, replies=[response(status()), response(status(512))])
        self.assertIn('volume&val=512', get.call_args_list[-1].args[0])

    def test_invalid_volume_fails_before_launching(self):
        for value in ('bad', float('nan'), float('inf'), -1, None):
            with self.subTest(value=value), patch('lib.vlcclient.subprocess.Popen') as popen:
                with self.assertRaisesRegex(PlaybackError, 'Invalid VLC volume'):
                    self.client.play_file('/media/song.mp4', value)
                popen.assert_not_called()
                self.assertFalse(self.client.is_transposing)

    def test_paused_reload_accepts_paused_state_without_global_player(self):
        xml = status(state='paused')
        result, _, _, popen = self.launch(params=['--start-paused'], replies=[response(xml), response(xml)])
        self.assertEqual(result, xml)
        self.assertIn('--start-paused', popen.call_args.args[0])

    def test_immediately_exited_process_fails_without_spinning(self):
        process = Process(exited=True)
        with patch('lib.vlcclient.subprocess.Popen', return_value=process), patch('lib.vlcclient.requests.get') as get:
            with self.assertRaisesRegex(PlaybackError, 'exited'):
                self.client.play_file('/media/song.mp4', 50)
        get.assert_not_called()
        self.assertFalse(self.client.is_transposing)
        self.assertIsNone(self.client.process)
        self.assertTrue(process.stdin.closed)

    def test_readiness_timeout_kills_child_and_clears_old_status(self):
        process = Process()
        self.client.last_status_text = status()
        self.client.current_file_path = '/media/old.mp4'
        with patch('lib.vlcclient.subprocess.Popen', return_value=process), patch('lib.vlcclient.requests.get', return_value=response('<root><state>opening</state></root>')) as get:
            with self.assertRaisesRegex(PlaybackError, 'timed out'):
                self.client.play_file('/media/song.mp4', 50)
        self.assertTrue(process.killed)
        self.assertTrue(process.stdin.closed)
        self.assertFalse(self.client.is_transposing)
        self.assertIsNone(self.client.current_file_path)
        self.assertEqual(self.client.last_status_text, '')
        self.assertLessEqual(get.call_count, 4)

    def test_volume_confirmation_has_same_bounded_deadline(self):
        process = Process()
        with patch('lib.vlcclient.subprocess.Popen', return_value=process), patch('lib.vlcclient.requests.get', return_value=response(status(20))) as get:
            with self.assertRaisesRegex(PlaybackError, 'timed out'):
                self.client.play_file('/media/song.mp4', 50)
        self.assertTrue(process.killed)
        self.assertLessEqual(get.call_count, 5)

    def test_spawn_error_is_a_playback_error_with_transition_restored(self):
        with patch('lib.vlcclient.subprocess.Popen', side_effect=OSError('missing executable')):
            with self.assertRaisesRegex(PlaybackError, 'missing executable'):
                self.client.play_file('/media/song.mp4', 50)
        self.assertFalse(self.client.is_transposing)
        self.assertIsNone(self.client.process)

    def test_previous_process_is_reaped_before_new_launch(self):
        previous = Process()
        self.client.process = previous
        self.client.startup_timeout = 5
        current = Process()
        xml = status()
        _, _, _, _ = self.launch(process=current, replies=[response(xml), response(xml), response(xml)])
        self.assertTrue(previous.killed)
        self.assertTrue(previous.stdin.closed)
        self.assertIs(self.client.process, current)

    def test_macos_windowed_mode_removes_existing_fullscreen_flags(self):
        self.client.platform = 'osx'
        self.client.cmd_base += ['--fullscreen', '--macosx-nativefullscreenmode']
        with patch.object(os, 'K', SimpleNamespace(full_screen=False), create=True):
            _, _, _, popen = self.launch(replies=[response(status()), response(status())])
        self.assertNotIn('--fullscreen', popen.call_args.args[0])
        self.assertNotIn('--macosx-nativefullscreenmode', popen.call_args.args[0])

    def test_failed_http_requests_do_not_refresh_status_cache_timestamp(self):
        self.client.process = Process()
        self.client.last_status_time = 10
        self.client.last_status_text = status()
        with patch('lib.vlcclient.requests.get', side_effect=requests.ConnectionError('offline')) as get:
            result = self.client.command()
        self.assertEqual(result.status_code, 500)
        self.assertEqual(self.client.last_status_time, 10)
        self.assertEqual(get.call_count, 3)
        self.assertLess(self.clock.now, 101)

    def test_successful_status_cache_has_no_global_player_dependency(self):
        self.client.process = Process()
        xml = status()
        with patch('lib.vlcclient.requests.get', return_value=response(xml)):
            result = self.client.command()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.last_status_text, xml)
        self.assertEqual(self.client.last_status_time, self.clock.now)

    def test_http_error_or_non_status_xml_does_not_replace_good_cache(self):
        self.client.process = Process()
        self.client.last_status_text = status()
        self.client.last_status_time = 10
        for reply in (response('unavailable', 500), response('<html>login</html>'), response('broken XML')):
            with self.subTest(reply=reply), patch('lib.vlcclient.requests.get', return_value=reply):
                self.client.command()
                self.assertEqual(self.client.last_status_text, status())
                self.assertEqual(self.client.last_status_time, 10)

    def test_startup_request_timeout_respects_remaining_deadline(self):
        self.client.process = Process()
        with patch('lib.vlcclient.requests.get', return_value=response(status())) as get:
            self.client.command(deadline=self.clock.now + 0.05)
        self.assertLessEqual(get.call_args.kwargs['timeout'], 0.051)

    def test_skip_can_stop_a_live_process_when_its_http_interface_is_unresponsive(self):
        process = Process()
        self.client.process = process
        self.client.current_file_path = '/media/song.mp4'
        self.client.last_status_text = status()
        with patch('lib.vlcclient.requests.get', side_effect=requests.ConnectionError('offline')) as get:
            with self.assertLogs(level='WARNING'):
                self.client.stop()
        self.assertEqual(get.call_count, 1)
        self.assertTrue(process.killed)
        self.assertTrue(process.stdin.closed)
        self.assertIsNone(self.client.process)
        self.assertIsNone(self.client.current_file_path)
        self.assertEqual(self.client.last_status_text, '')

    def test_next_file_never_uses_or_caches_previous_file_status(self):
        self.client.process = Process()
        self.client.current_file_path = '/media/old.mp4'
        self.client.last_status_text = status(tracks=(1, 2), current=2)
        with patch.object(self.client, 'get_status') as get_status, patch.object(self.client, 'command') as command, patch('lib.vlcclient.shutil.which', side_effect=lambda name: '/bin/ffprobe' if name == 'ffprobe' else None), patch('lib.vlcclient.subprocess.check_output', return_value='1\n') as probe:
            info = self.client.get_audio_track_info(file_path='/media/new.mp4')
        self.assertEqual(info, (None, [1], 'ffprobe'))
        self.assertEqual(self.client.audio_track_cache[self.client._media_key('/media/new.mp4')], [1])
        get_status.assert_not_called()
        command.assert_not_called()
        self.assertEqual(probe.call_args.kwargs['timeout'], self.client.probe_timeout)

    def test_fresh_current_xml_replaces_wrong_cached_track_ids(self):
        self.client.process = Process()
        self.client.current_file_path = '/media/new.mp4'
        key = self.client._media_key('/media/new.mp4')
        self.client.audio_track_cache[key] = [1, 2]
        info = self.client.get_audio_track_info(status(tracks=(1,), current=1), '/media/new.mp4')
        self.assertEqual(info, (1, [1], 'xml'))
        self.assertEqual(self.client.audio_track_cache[key], [1])

    def test_explicit_old_xml_cannot_poison_a_different_target(self):
        self.client.current_file_path = '/media/old.mp4'
        with patch('lib.vlcclient.shutil.which', return_value=None):
            info = self.client.get_audio_track_info(status(tracks=(1, 2)), '/media/new.mp4')
        self.assertEqual(info, (None, [], 'unknown'))
        self.assertNotIn(self.client._media_key('/media/new.mp4'), self.client.audio_track_cache)

    def test_ffmpeg_only_fallback_parses_real_stream_lines_and_is_bounded(self):
        stderr = 'Stream #0:1(eng): Audio: aac\nStream #0:2(eng): Audio: ac3\n'
        with patch('lib.vlcclient.shutil.which', side_effect=lambda name: '/bin/ffmpeg' if name == 'ffmpeg' else None), patch('lib.vlcclient.subprocess.run', return_value=SimpleNamespace(stderr=stderr)) as run:
            info = self.client.get_audio_track_info(file_path='/media/song.mkv')
        self.assertEqual(info, (None, [1, 2], 'ffmpeg'))
        self.assertEqual(run.call_args.kwargs['timeout'], self.client.probe_timeout)

    def test_probe_timeout_does_not_raise_or_borrow_old_track_ids(self):
        self.client.current_file_path = '/media/old.mp4'
        self.client.last_status_text = status()
        with patch('lib.vlcclient.shutil.which', return_value='/bin/tool'), patch('lib.vlcclient.subprocess.check_output', side_effect=subprocess.TimeoutExpired('ffprobe', 5)), patch('lib.vlcclient.subprocess.run', side_effect=subprocess.TimeoutExpired('ffmpeg', 5)):
            info = self.client.get_audio_track_info(file_path='/media/new.mp4')
        self.assertEqual(info, (None, [], 'unknown'))

    def test_failed_probe_has_cooldown_instead_of_retrying_on_every_ui_poll(self):
        with patch('lib.vlcclient.shutil.which', side_effect=lambda name: '/bin/ffprobe' if name == 'ffprobe' else None), patch('lib.vlcclient.subprocess.check_output', side_effect=subprocess.TimeoutExpired('ffprobe', 5)) as probe:
            self.client.get_audio_track_info(file_path='/media/new.mp4')
            self.client.get_audio_track_info(file_path='/media/new.mp4')
            self.assertEqual(probe.call_count, 1)
            self.clock.now += self.client.probe_retry_interval
            self.client.get_audio_track_info(file_path='/media/new.mp4')
            self.assertEqual(probe.call_count, 2)

    def test_fresh_audio_metadata_clears_failed_probe_cooldown(self):
        key = self.client._media_key('/media/song.mp4')
        self.client._audio_probe_failed_at[key] = self.clock.now
        self.client.current_file_path = '/media/song.mp4'
        info = self.client.get_audio_track_info(status(tracks=(1,)), '/media/song.mp4')
        self.assertEqual(info, (1, [1], 'xml'))
        self.assertNotIn(key, self.client._audio_probe_failed_at)

    def test_get_status_refreshes_expired_cache_but_not_after_process_exit(self):
        self.client.process = Process()
        self.client.last_status_time = 0
        with patch('lib.vlcclient.requests.get', return_value=response(status())) as get:
            self.assertEqual(self.client.get_status(), status())
            self.assertEqual(self.client.get_status(), status())
            self.assertEqual(get.call_count, 1)
            self.client.process.returncode = 0
            self.assertEqual(self.client.get_status(), '')

    def replace_player_during_read(self, path='/media/new.mp4'):
        with self.client._status_lock:
            self.client.playback_generation += 1
            self.client.process = Process()
            self.client.current_file_path = path
            self.client.last_status_text = status(volume=90, tracks=(7,), current=7)
            self.client.last_status_time = self.clock.now

    def test_inflight_old_http_response_is_not_returned_or_cached_after_song_change(self):
        self.client.process = Process()
        self.client.current_file_path = '/media/old.mp4'
        old = status(volume=10)
        def delayed_reply(*args, **kwargs):
            self.replace_player_during_read()
            return response(old)
        with patch('lib.vlcclient.requests.get', side_effect=delayed_reply):
            result = self.client.command()
        self.assertEqual((result.status_code, result.text), (409, ''))
        self.assertEqual(self.client.get_val_xml(self.client.last_status_text, 'volume'), '90')

    def test_generation_also_rejects_an_old_response_for_reload_of_same_path(self):
        path = '/media/song.mp4'
        self.client.process = Process()
        self.client.current_file_path = path
        def delayed_reply(*args, **kwargs):
            self.replace_player_during_read(path)
            return response(status(volume=10))
        with patch('lib.vlcclient.requests.get', side_effect=delayed_reply):
            self.assertEqual(self.client.get_status(), '')
        self.assertEqual(self.client.get_val_xml(self.client.last_status_text, 'volume'), '90')

    def test_transition_during_xml_parsing_cannot_overwrite_new_status_cache(self):
        self.client.process = Process()
        self.client.current_file_path = '/media/old.mp4'
        from xml.etree.ElementTree import fromstring
        def parsing_reply(xml):
            self.replace_player_during_read()
            return fromstring(xml)
        with patch('lib.vlcclient.requests.get', return_value=response(status(volume=10))), patch('lib.vlcclient.ET.fromstring', side_effect=parsing_reply):
            result = self.client.command()
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.client.get_val_xml(self.client.last_status_text, 'volume'), '90')

    def test_stale_control_retry_never_targets_the_next_player(self):
        self.client.process = Process()
        def delayed_retry_sleep(seconds):
            self.replace_player_during_read()
        with patch('lib.vlcclient.requests.get', side_effect=requests.ConnectionError('old process stopped')) as get, patch('lib.vlcclient.time.sleep', side_effect=delayed_retry_sleep):
            result = self.client.command('pl_pause')
        self.assertEqual(get.call_count, 1)
        self.assertEqual((result.status_code, result.text), (409, ''))

    def test_audio_probe_that_outlives_a_transition_cannot_overwrite_target_cache(self):
        self.client.process = Process()
        self.client.current_file_path = '/media/old.mp4'
        key = self.client._media_key('/media/new.mp4')
        def delayed_probe(*args, **kwargs):
            self.replace_player_during_read()
            self.client.audio_track_cache[key] = [7]
            return '1\n2\n'
        with patch('lib.vlcclient.shutil.which', side_effect=lambda name: '/bin/ffprobe' if name == 'ffprobe' else None), patch('lib.vlcclient.subprocess.check_output', side_effect=delayed_probe):
            result = self.client.get_audio_track_info(file_path='/media/new.mp4')
        self.assertEqual(result, (None, [], 'stale'))
        self.assertEqual(self.client.audio_track_cache[key], [7])

    def test_failed_old_probe_does_not_add_retry_cooldown_to_new_player(self):
        self.client.process = Process()
        self.client.current_file_path = '/media/old.mp4'
        def delayed_probe(*args, **kwargs):
            self.replace_player_during_read()
            raise subprocess.TimeoutExpired('ffprobe', 5)
        with patch('lib.vlcclient.shutil.which', side_effect=lambda name: '/bin/ffprobe' if name == 'ffprobe' else None), patch('lib.vlcclient.subprocess.check_output', side_effect=delayed_probe):
            result = self.client.get_audio_track_info(file_path='/media/new.mp4')
        self.assertEqual(result, (None, [], 'stale'))
        self.assertNotIn(self.client._media_key('/media/new.mp4'), self.client._audio_probe_failed_at)


if __name__ == '__main__':
    unittest.main()
