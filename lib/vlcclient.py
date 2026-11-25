import os, sys, re, random, shutil
import string, logging, time, math
import subprocess, zipfile, threading
import xml.etree.ElementTree as ET

import requests

from lib.get_platform import *
from types import SimpleNamespace


class PlaybackError(RuntimeError):
	"""VLC could not start the requested media within its startup deadline."""

def get_default_vlc_path(platform):
	shutil_path = shutil.which('cvlc') or shutil.which('vlc')
	if shutil_path:
		return shutil_path

	if platform == "osx":
		return "/Applications/VLC.app/Contents/MacOS/VLC"
	elif platform == "windows":
		alt_vlc_path = r"C:\\Program Files (x86)\\VideoLAN\VLC\\vlc.exe"
		if os.path.isfile(alt_vlc_path):
			return alt_vlc_path
		else:
			return r"C:\Program Files\VideoLAN\VLC\vlc.exe"
	else:
		return 'vlc'


class VLCClient:
	vol_increment = 10
	startup_timeout = 15.0
	probe_timeout = 5.0
	probe_retry_interval = 30.0
	request_timeout = 0.5
	status_interval = 1.0

	def __init__(self, port = 5002, path = None, qrcode = None, url = None):

		# HTTP remote control server
		self.http_password = "".join([random.choice(string.ascii_letters + string.digits) for n in range(32)])
		self.port = port
		self.http_hosts = ["127.0.0.1", "localhost"]
		self.http_host = self.http_hosts[0]
		self.http_endpoint = "http://%s:%s/requests/status.xml" % (self.http_host, self.port)
		self.http_command_endpoint = self.http_endpoint + "?command="
		self.is_transposing = False

		self.qrcode = qrcode
		self.url = url

		# Handle vlc paths
		self.platform = get_platform()
		if path == None:
			self.path = get_default_vlc_path(self.platform)
		else:
			self.path = path

		# Determine tmp directories (for things like extracted cdg files)
		if self.platform == "windows":
			self.tmp_dir = os.path.expanduser(r"~\\AppData\\Local\\Temp\\pikaraoke\\")
		else:
			self.tmp_dir = "/tmp/pikaraoke/"

		# Set up command line args
		self.cmd_base = [
			self.path,
			"--fullscreen",
			"--play-and-exit",
			"--extraintf", "http",
			"--http-host", self.http_host,
			"--http-port", "%s" % self.port,
			"--http-password", self.http_password,
			"--no-embedded-video",
			"--no-keyboard-events",
			"--no-mouse-events",
			"--video-on-top",
			"--volume-save",
			"--no-video-title",
			"--no-loop",
			"--no-repeat",
			"--mouse-hide-timeout", "10",
		]
		if self.platform == "osx":
			self.cmd_base += [
				"--no-macosx-video-autoresize",
				"--no-macosx-show-playback-buttons",
				"--no-macosx-show-playmode-buttons",
				"--no-macosx-interfacestyle",
				"--macosx-nativefullscreenmode",
				"--macosx-continue-playback=2",
			]
		else:
			self.cmd_base += ["--intf", "dummy"]

		if self.qrcode and self.url:
			self.cmd_base += self.get_marquee_cmd()

		logging.debug("VLC executable: %s", self.path)

		self.volume_offset = 10
		self.process = None
		self.last_status_text = ""
		self.last_status_time = 0.0
		self.current_file_path = None
		self._status_lock = threading.RLock()
		self.playback_generation = 0
		self.audio_track_cache = {}
		self._audio_probe_failed_at = {}

	def get_marquee_cmd(self):
		return ["--sub-source", 'logo{file=%s,position=9,x=2,opacity=200}:marq{marquee="Pikaraoke - connect at: \n%s",position=9,x=38,color=0xFFFFFF,size=11,opacity=200}' % (self.qrcode, self.url)]

	def handle_zipped_cdg(self, file_path):
		extracted_dir = os.path.join(self.tmp_dir, "extracted")
		if (os.path.exists(extracted_dir)):
			shutil.rmtree(extracted_dir)
		with zipfile.ZipFile(file_path, 'r') as zip_ref:
			zip_ref.extractall(extracted_dir)

		mp3_file = None
		cdg_file = None
		files = os.listdir(extracted_dir)
		for file in files:
			ext = os.path.splitext(file)[1]
			if ext.casefold() == ".mp3":
				mp3_file = file
			elif ext.casefold() == ".cdg":
				cdg_file = file

		if (mp3_file is not None) and (cdg_file is not None):
			if (os.path.splitext(mp3_file)[0] == os.path.splitext(cdg_file)[0]):
				return os.path.join(extracted_dir, mp3_file)
			else:
				raise Exception("Zipped .mp3 file did not have a matching .cdg file: " + files)
		else:
			raise Exception("No .mp3 or .cdg was found in the zip file: " + file_path)

	def process_file(self, file_path):
		file_extension = os.path.splitext(file_path)[1]
		if (file_extension.casefold() == ".zip"):
			return self.handle_zipped_cdg(file_path)
		else:
			return file_path

	def _process_running(self):
		return self.process is not None and self.process.poll() is None

	def _playback_token(self):
		with self._status_lock:
			return (getattr(self, 'playback_generation', 0), self.process, getattr(self, 'current_file_path', None))

	def _same_playback(self, token):
		current = self._playback_token()
		return current[0] == token[0] and current[1] is token[1] and current[2] == token[2]

	@staticmethod
	def _media_key(file_path):
		return os.path.normcase(os.path.abspath(os.fspath(file_path))) if file_path else None

	@staticmethod
	def _has_media(xml):
		try:
			root = ET.fromstring(xml)
		except (ET.ParseError, TypeError, ValueError):
			return False
		return any(
			(info.get('name') or '').lower() == 'type'
			and (info.text or '').strip().lower() in ('audio', 'video')
			for info in root.findall('.//info')
		)

	def _discard_process(self):
		"""Bound cleanup as well as startup; reap a failed child when possible."""
		process = self.process
		if process is None:
			return
		try:
			if process.poll() is None:
				process.kill()
			process.wait(timeout=1)
		except (OSError, subprocess.TimeoutExpired):
			logging.debug("VLC process cleanup did not complete", exc_info=True)
		finally:
			if getattr(process, 'stdin', None) is not None:
				try:
					process.stdin.close()
				except OSError:
					pass
			self.process = None

	def play_file(self, file_path, volume, params=None):
		params = list(params or [])
		try:
			numeric_volume = float(volume)
			if not math.isfinite(numeric_volume) or numeric_volume < 0:
				raise ValueError("volume must be a finite nonnegative number")
			# VLC's HTTP volume scale is 0..512; clamping also handles normalization.
			volume = min(512, round(numeric_volume))
		except (TypeError, ValueError, OverflowError) as exc:
			raise PlaybackError("Invalid VLC volume: %r" % (volume,)) from exc

		with self._status_lock:
			self.playback_generation = getattr(self, 'playback_generation', 0) + 1
			generation = self.playback_generation
			self.is_transposing = True
		deadline = time.monotonic() + self.startup_timeout
		try:
			file_path = self.process_file(file_path)
			if self._process_running():
				logging.debug("Stopping previous VLC process before playback")
				self.command("pl_stop", save_status=False, deadline=min(deadline, time.monotonic() + 2), max_attempts=1)
				try:
					self.process.wait(timeout=max(0.01, min(2, deadline - time.monotonic())))
				except subprocess.TimeoutExpired:
					pass
			self._discard_process()
			self.current_file_path = None
			self.last_status_text = ""
			self.last_status_time = 0.0
			command = self.cmd_base + params + [file_path]
			if self.platform == 'osx' and not getattr(getattr(os, 'K', None), 'full_screen', True):
				command = [arg for arg in command if arg not in ('--fullscreen', '--macosx-nativefullscreenmode')]
			logging.debug("Starting VLC for %s", file_path)
			self.process = subprocess.Popen(command, stdin=subprocess.PIPE)
			expected_state = 'paused' if '--start-paused' in params else 'playing'

			def check_startup():
				if self.playback_generation != generation:
					raise PlaybackError("VLC startup was interrupted by another transition")
				if not self._process_running():
					raise PlaybackError("VLC exited before media was ready: %s" % file_path)
				if time.monotonic() >= deadline:
					raise PlaybackError("VLC startup timed out: %s" % file_path)

			while True:
				check_startup()
				response = self.command('', save_status=False, deadline=deadline, max_attempts=1)
				xml = response.text
				if response.status_code == 200 and self._has_media(xml):
					break
				time.sleep(min(0.1, max(0, deadline - time.monotonic())))

			# Set zero explicitly too, so a previously saved volume cannot defeat mute.
			while True:
				check_startup()
				response = self.command(f"volume&val={volume}", save_status=False, deadline=deadline, max_attempts=1)
				xml = response.text
				try:
					volume_ready = int(self.get_val_xml(xml, 'volume')) == volume
				except (TypeError, ValueError):
					volume_ready = False
				if response.status_code == 200 and volume_ready and self.get_val_xml(xml, 'state') == expected_state:
					break
				time.sleep(min(0.1, max(0, deadline - time.monotonic())))

			cache_time = time.monotonic()
			with self._status_lock:
				if self.playback_generation != generation:
					raise PlaybackError("VLC startup was interrupted by another transition")
				self.current_file_path = file_path
				getattr(self, '_audio_probe_failed_at', {}).pop(self._media_key(file_path), None)
				self.last_status_text = xml
				self.last_status_time = cache_time
			return xml
		except Exception as exc:
			if self.playback_generation == generation:
				self._discard_process()
				self.current_file_path = None
				self.last_status_text = ""
				self.last_status_time = 0.0
			if isinstance(exc, PlaybackError):
				raise
			raise PlaybackError("Unable to play %s: %s" % (file_path, exc)) from exc
		finally:
			if self.playback_generation == generation:
				self.is_transposing = False

	def play_file_transpose(self, file_path, semitones, volume, extra_params = []):
		# --speex-resampler-quality=<integer [0 .. 10]>
		#  Resampling quality (0 = worst and fastest, 10 = best and slowest).

		# --src-converter-type={0 (Sinc function (best quality)), 1 (Sinc function (medium quality)),
		#      2 (Sinc function (fast)), 3 (Zero Order Hold (fastest)), 4 (Linear (fastest))}
		#  Sample rate converter type
		#  Different resampling algorithms are supported. The best one is slower, while the fast one exhibits
		#  low quality.

		if self.platform == "raspberry_pi":
			# pi sounds bad on hightest quality setting (CPU not sufficient)
			speex_quality = 10
			src_type = 1
		else:
			speex_quality = 10
			src_type = 0

		params = [
			"--audio-filter",
			"scaletempo_pitch",
			"--pitch-shift",
			"%s" % semitones,
			"--speex-resampler-quality",
			"%s" % speex_quality,
#			"--src-converter-type",
#			"%s" % src_type,
		]

		logging.debug("Transposing file...")
		return self.play_file(file_path, volume, params + extra_params)

	def command(self, command='', save_status=True, deadline=None, max_attempts=3):
		token = self._playback_token()
		is_status_probe = command in ("", "pl_info")
		if not self._process_running():
			return SimpleNamespace(text=self.last_status_text, status_code=500)
		last_exc = None
		for attempt in range(max_attempts):
			if not self._same_playback(token):
				return SimpleNamespace(text='', status_code=409)
			if deadline is not None and time.monotonic() >= deadline:
				break
			try:
				timeout = self.request_timeout
				if deadline is not None:
					timeout = max(0.001, min(timeout, deadline - time.monotonic()))
				response = requests.get(self.http_command_endpoint + command, auth=("", self.http_password), timeout=timeout)
				valid_status = False
				if response.status_code == 200 and save_status and command != "pl_info":
					# Cache only a successful status document, not failed requests or playlist info.
					try:
						root = ET.fromstring(response.text)
					except (ET.ParseError, TypeError, ValueError):
						pass
					else:
						valid_status = root.tag == 'root' and root.find('state') is not None
				cache_time = time.monotonic()
				with self._status_lock:
					if not self._same_playback(token):
						return SimpleNamespace(text='', status_code=409)
					if valid_status and not self.is_transposing:
						self.last_status_text = response.text
						self.last_status_time = cache_time
				return response
			except requests.RequestException as exc:
				if not self._same_playback(token):
					return SimpleNamespace(text='', status_code=409)
				last_exc = exc
				if len(self.http_hosts) > 1:
					self.http_hosts = self.http_hosts[1:] + self.http_hosts[:1]
					self.http_host = self.http_hosts[0]
					self.http_endpoint = "http://%s:%s/requests/status.xml" % (self.http_host, self.port)
					self.http_command_endpoint = self.http_endpoint + "?command="
				if not self._process_running():
					break
				if attempt + 1 < max_attempts:
					remaining = 0.1 if deadline is None else min(0.1, max(0, deadline - time.monotonic()))
					time.sleep(remaining)
		log = logging.debug if is_status_probe else logging.warning
		log("VLC command failed: %s (%s)", command or "status", last_exc)
		with self._status_lock:
			if not self._same_playback(token):
				return SimpleNamespace(text='', status_code=409)
			return SimpleNamespace(text=self.last_status_text, status_code=500)

	def pause(self, save_status=True):
		return self.command("pl_pause", save_status)

	def play(self):
		return self.command("pl_play")

	def get_val_xml(self, xml, key, end_key_str= '<'):
		posi = xml.find(f'<{key}>')
		if posi < 0:
			return None
		s = xml[posi+len(key)+2:]
		posi = s.find(end_key_str)
		if posi < 0:
			return None
		return s[:posi]

	def cast_float(self, num):
		try:
			return float(num)
		except:
			return num

	def get_info_xml(self, xml=None):
		try:
			if xml is None:
				xml = self.get_status()
			return {key: self.cast_float(self.get_val_xml(xml, key)) for key in ['position', 'length', 'volume', 'time', 'audiodelay', 'state', 'subtitledelay', 'rate']}
		except:
			return {}

	def probe_audio_track_ids(self, track_count, current_id=None):
		if not track_count or track_count <= 1:
			return []
		max_id = max(track_count + 2, 4)
		found = []
		original_id = current_id
		for cand in range(0, max_id + 1):
			resp = self.set_audio_track(cand)
			text = resp.text if hasattr(resp, "text") else ""
			new_id = self.get_val_xml(text, 'audiotrack')
			if new_id is None:
				continue
			try:
				new_id = int(new_id)
			except ValueError:
				pass
			if new_id == cand and cand not in found and not (isinstance(cand, int) and cand < 0):
				found.append(cand)
				if len(found) >= track_count:
					break
		if original_id is not None:
			try:
				self.set_audio_track(original_id)
			except Exception:
				pass
		return sorted(found)

	def get_audio_track_info(self, xml=None, file_path=None):
		token = self._playback_token()
		cache_key = self._media_key(file_path or getattr(self, 'current_file_path', None))
		active_key = self._media_key(getattr(self, 'current_file_path', None))
		# A query for the next file must never borrow the previous player's status.
		active_target = file_path is None or (cache_key is not None and cache_key == active_key)
		status_xml = ""
		if active_target:
			if xml is not None:
				status_xml = xml
			elif self._process_running():
				status_xml = self.get_status()

		def safe_text(val):
			if val is None:
				return ""
			if isinstance(val, bytes):
				try:
					return val.decode("utf-8", errors="ignore")
				except Exception:
					return ""
			return str(val)

		def parse_xml_tracks(text):
			current = None
			track_list = []
			stream_count = 0
			track_source = None
			try:
				root = ET.fromstring(safe_text(text))
			except Exception:
				return current, track_list, stream_count, track_source

			audio_track_elem = root.find("audiotrack")
			if audio_track_elem is not None and audio_track_elem.text:
				try:
					current = int(audio_track_elem.text.strip())
				except ValueError:
					current = audio_track_elem.text.strip()

			audio_elem = root.find("audio")
			if audio_elem is not None:
				for track in audio_elem.findall("track"):
					track_id = track.get("id")
					if track_id is None:
						continue
					try:
						track_id = int(track_id)
					except ValueError:
						pass
					if isinstance(track_id, int) and track_id < 0:
						continue
					if track_id not in track_list:
						track_list.append(track_id)
				if track_list:
					track_source = "xml"

			if not track_list:
				for track in root.findall(".//track"):
					type_attr = track.get("type", "")
					if type_attr and type_attr.lower() != "audio":
						continue
					track_id = track.get("id")
					if track_id is None:
						continue
					try:
						track_id = int(track_id)
					except ValueError:
						pass
					if isinstance(track_id, int) and track_id < 0:
						continue
					if track_id not in track_list:
						track_list.append(track_id)
				if track_list:
					track_source = "xml"

			info_elem = root.find("information")
			if info_elem is not None:
				for category in info_elem.findall("category"):
					name = (category.get("name") or "").lower()
					if not name.startswith("stream"):
						continue
					for info in category.findall("info"):
						if (info.get("name") or "").lower() == "type" and (info.text or "").strip().lower() == "audio":
							stream_count += 1
							break

			return current, track_list, stream_count, track_source

		def probe_audio_streams(path):
			if not path:
				return [], None
			if shutil.which("ffprobe"):
				cmd = [
					"ffprobe", "-v", "error", "-select_streams", "a",
					"-show_entries", "stream=index", "-of", "csv=p=0", path
				]
				try:
					out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True, timeout=self.probe_timeout)
					indices = [int(line.strip()) for line in out.splitlines() if line.strip()]
					if indices:
						return indices, "ffprobe"
				except (OSError, ValueError, subprocess.SubprocessError):
					logging.debug("ffprobe audio metadata unavailable for %s", path, exc_info=True)
			if shutil.which("ffmpeg"):
				cmd = ["ffmpeg", "-hide_banner", "-i", path]
				try:
					proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=self.probe_timeout)
					indices = []
					for line in proc.stderr.splitlines():
						if "Stream #" in line and "Audio:" in line:
							match = re.search(r"Stream #\d+:(\d+)", line)
							if match:
								indices.append(int(match.group(1)))
					return indices, "ffmpeg"
				except (OSError, ValueError, subprocess.SubprocessError):
					logging.debug("ffmpeg audio metadata unavailable for %s", path, exc_info=True)
			return [], None

		cached_ids = self.audio_track_cache.get(cache_key) if cache_key else None
		current_id, track_ids, stream_count, source = parse_xml_tracks(status_xml)

		if active_target and self._process_running() and (not track_ids or current_id is None):
			try:
				info_xml = self.command("pl_info", save_status=False, max_attempts=1).text
				info_current, info_tracks, info_stream_count, _ = parse_xml_tracks(info_xml)
				if current_id is None:
					current_id = info_current
				if not track_ids and info_tracks:
					track_ids = info_tracks
					source = "pl_info"
				if stream_count == 0:
					stream_count = info_stream_count
			except (OSError, requests.RequestException):
				logging.debug("VLC audio metadata unavailable", exc_info=True)
		if not self._same_playback(token):
			return None, [], 'stale'

		# Fresh explicit track IDs take precedence over any earlier cache entry.
		if not track_ids and cached_ids and (not stream_count or len(cached_ids) == stream_count):
			track_ids = list(cached_ids)
			source = "cache"

		if not hasattr(self, '_audio_probe_failed_at'):
			self._audio_probe_failed_at = {}
		failed_at = self._audio_probe_failed_at.get(cache_key)
		if not track_ids and (failed_at is None or time.monotonic() - failed_at >= self.probe_retry_interval):
			track_ids, source = probe_audio_streams(file_path or getattr(self, 'current_file_path', None))
			if not self._same_playback(token):
				return None, [], 'stale'
			if cache_key and not track_ids:
				# A missing tool or slow/unreadable file must not trigger probes on every UI poll.
				failed_time = time.monotonic()
				with self._status_lock:
					if not self._same_playback(token):
						return None, [], 'stale'
					self._audio_probe_failed_at[cache_key] = failed_time

		if not track_ids and stream_count > 0:
			base = 0 if current_id == 0 else 1
			track_ids = list(range(base, base + stream_count))
			source = "stream_count"

		with self._status_lock:
			if not self._same_playback(token):
				return None, [], 'stale'
			if cache_key and track_ids and source != "stream_count":
				self.audio_track_cache[cache_key] = list(track_ids)
				self._audio_probe_failed_at.pop(cache_key, None)
		return current_id, track_ids, source or "unknown"

	def seek(self, seek_sec):
		return self.command(f"seek&val={seek_sec}")

	def stop(self):
		response = self.command("pl_stop", max_attempts=1)
		if self.process is not None:
			try:
				self.process.wait(timeout=1)
			except subprocess.TimeoutExpired:
				pass
		self.kill()
		return response

	def restart(self):
		logging.info(self.command("seek&val=0"))
		self.play()
		return self.command("seek&val=0")

	def vol_up(self):
		return self.command(f"volume&val=+{self.vol_increment}")

	def vol_down(self):
		return self.command(f"volume&val=-{self.vol_increment}")

	def vol_set(self, value):
		return self.command(f"volume&val={value}")

	def playspeed_set(self, value):
		return self.command(f"rate&val={value}")

	def set_audio_track(self, track_id):
		return self.command(f"audio_track&val={track_id}")

	def kill(self):
		with self._status_lock:
			self.playback_generation = getattr(self, 'playback_generation', 0) + 1
		self._discard_process()
		with self._status_lock:
			self.current_file_path = None
			self.last_status_text = ""
			self.last_status_time = 0.0
			self.is_transposing = False

	def is_running(self):
		return (self.process != None and self.process.poll() == None) or self.is_transposing

	def is_playing(self):
		if self.is_running():
			status = self.get_status()
			state = self.get_val_xml(status, 'state')
			return state == "playing"
		else:
			return False

	def is_paused(self):
		if self.is_running():
			status = self.get_status()
			state = self.get_val_xml(status, 'state')
			return state == "paused"
		else:
			return False

	def get_status(self):
		token = self._playback_token()
		if self.is_transposing:
			return self.last_status_text
		if not self._process_running():
			return ""
		if time.monotonic() - self.last_status_time >= self.status_interval:
			self.command()
		if not self._same_playback(token):
			return ''
		return self.last_status_text

	def run(self):
		try:
			while True:
				pass
		except KeyboardInterrupt:
			self.kill()

# if __name__ == "__main__":
#     k = VLCClient()
#     k.play_file("/path/to/file.mp4")
#     time.sleep(2)
#     k.pause()
#     k.vol_up()
#     k.vol_up()
#     time.sleep(2)
#     k.vol_down()
#     k.vol_down()
#     time.sleep(2)
#     k.play()
#     time.sleep(2)
#     k.stop()
