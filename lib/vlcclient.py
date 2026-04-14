import os, sys, re, random, shutil
import string, logging, time
import subprocess, zipfile
import xml.etree.ElementTree as ET

import requests

from lib.get_platform import *
from types import SimpleNamespace
from html import unescape

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

		logging.info("VLC command base: " + " ".join(self.cmd_base))

		self.volume_offset = 10
		self.process = None
		self.last_status_text = ""
		self.last_status_time = time.time()
		self.audio_track_cache = {}

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

	def play_file(self, file_path, volume, params = []):
		try:
			file_path = self.process_file(file_path)
			self.is_transposing = True
			if self.process is not None and self.process.poll() is None:
				logging.debug("VLC is currently playing, stopping track...")
				# must wait for VLC to quit or force kill, otherwise VLC http server will be borked
				try:
					self.stop()
					self.process.wait(2)
				except:
					self.process.kill()
			command = self.cmd_base + params + [file_path]
			if self.platform == 'osx' and not os.K.full_screen:
				command.remove('--fullscreen')
				command.remove('--macosx-nativefullscreenmode')
			logging.info("VLC Command: %s" % command)

			self.process = subprocess.Popen(command, stdin = subprocess.PIPE)

			# wait for the process to start
			while self.process.poll() is not None:
				pass

			# wait for VLC HTTP is ready
			while True:
				time.sleep(0.1)
				req = self.command("", False)
				xml = req.text
				if "<info name='Type'>Video</info>" not in xml and "<info name='Type'>Audio</info>" not in xml:
					pass
				elif req.status_code == 200:
					break

			# workaround --volume-save not working in Windows
			okay = False
			while volume and not okay:
				try:
					volume = round(volume)
					xml = self.command(f"volume&val={volume}", False).text
					if int(self.get_val_xml(xml, 'volume')) == volume:
						okay = True
					if not os.K.is_paused and self.get_val_xml(xml, 'state') != 'playing':
						okay = False
				except:
					time.sleep(0.1)

			self.is_transposing = False
			return xml

		except Exception as e:
			logging.error("Playing file failed: " + str(e))
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

	def command(self, command = '', save_status=True):
		self.last_status_time = time.time()
		if not self.is_running():
			return SimpleNamespace(**{'text': self.last_status_text, 'status_code': 500})
		last_exc = None
		for _ in range(10):
			try:
				url = self.http_command_endpoint + command
				request = requests.get(url, auth = ("", self.http_password), timeout = 0.5)
				if self.is_transposing and save_status:
					return SimpleNamespace(**{'text': self.last_status_text, 'status_code': request.status_code})
				if save_status:
					self.last_status_text = request.text
					os.K.has_video = "<info name='Type'>Video</info>" in request.text
				if not os.K.now_playing:
					# by right, here should never be reached
					request.encoding = 'utf-8'
					os.K.now_playing_filename = unescape(unescape(self.get_val_xml(request.text, "info name='filename'")))
					if not os.path.isfile(os.K.now_playing_filename):
						if not os.path.isabs(os.K.now_playing_filename):
							os.K.now_playing_filename = os.path.join(os.K.library_root, os.K.now_playing_filename)
					os.K.now_playing = os.K.filename_from_path(os.K.now_playing_filename)
				return request
			except Exception as exc:
				last_exc = exc
				if len(self.http_hosts) > 1:
					# swap host if loopback choice isn't reachable yet
					self.http_hosts = self.http_hosts[1:] + self.http_hosts[:1]
					self.http_host = self.http_hosts[0]
					self.http_endpoint = "http://%s:%s/requests/status.xml" % (self.http_host, self.port)
					self.http_command_endpoint = self.http_endpoint + "?command="
				time.sleep(0.1)
		logging.error("No active VLC process. Could not run command: %s (%s)", command, last_exc)
		return SimpleNamespace(**{'text': self.last_status_text, 'status_code': 500})

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
		status_xml = xml or self.get_status()

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
				return []
			if shutil.which("ffprobe"):
				cmd = [
					"ffprobe", "-v", "error", "-select_streams", "a",
					"-show_entries", "stream=index", "-of", "csv=p=0", path
				]
				try:
					out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
					return [int(line.strip()) for line in out.splitlines() if line.strip()]
				except Exception:
					return []
			if shutil.which("ffmpeg"):
				cmd = ["ffmpeg", "-hide_banner", "-i", path]
				try:
					proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
					lines = proc.stderr.splitlines()
					indices = []
					for line in lines:
						if "Stream #" in line and "Audio:" in line:
							match = re.search(r"Stream #\\d+:(\\d+)", line)
							if match:
								try:
									indices.append(int(match.group(1)))
								except ValueError:
									pass
					return indices
				except Exception:
					return []
			return []

		cache_key = file_path
		if cache_key and cache_key in self.audio_track_cache:
			cached_ids = self.audio_track_cache[cache_key]
		else:
			cached_ids = None

		current_id, track_ids, stream_count, source = parse_xml_tracks(status_xml)
		if cached_ids:
			track_ids = cached_ids
			source = "cache"

		if not track_ids or current_id is None or stream_count == 0:
			try:
				info_xml = self.command("pl_info", save_status=False).text
				info_current, info_tracks, info_stream_count, info_source = parse_xml_tracks(info_xml)
				if current_id is None:
					current_id = info_current
				if not track_ids and info_tracks:
					track_ids = info_tracks
					source = "pl_info"
				if stream_count == 0 and info_stream_count:
					stream_count = info_stream_count
			except Exception:
				pass

		if not track_ids and stream_count > 0:
			base = 0 if current_id == 0 else 1
			track_ids = list(range(base, base + stream_count))
			source = "stream_count"
			logging.debug("Fallback audio tracks via stream count: %s (current=%s)", track_ids, current_id)

		if not track_ids:
			stream_indices = probe_audio_streams(file_path)
			if stream_indices:
				track_ids = stream_indices
				source = "ffprobe"
				logging.debug("Fallback audio tracks via ffprobe/ffmpeg: %s (current=%s)", track_ids, current_id)

		if cache_key and track_ids:
			self.audio_track_cache[cache_key] = track_ids

		return current_id, track_ids, source or "unknown"

	def seek(self, seek_sec):
		return self.command(f"seek&val={seek_sec}")

	def stop(self):
		try:
			return self.command("pl_stop")
		except:
			e = sys.exc_info()[0]
			logging.warn(f"Track stop: server may have shut down before http return code received: {e}")
			return

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
		try:
			if self.process is not None: self.process.kill()
		except (OSError, AttributeError) as e:
			print(e)
		return

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
		if self.is_transposing:
			return self.last_status_text
		cur_time = time.time()
		if abs(cur_time-self.last_status_time)>1:
			try:
				self.command()
				return self.last_status_text
			except: pass
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
