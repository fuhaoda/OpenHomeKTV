import os, sys, random, time, json
import logging, socket, subprocess, threading
import shutil
import gzip
import warnings
import ast
import math
import re
import tempfile
from subprocess import check_output
from collections import *
from functools import wraps

from lib.media import media_title
from lib.catalog import MediaCatalog

# pygame 2.5.x imports pkg_resources during startup, which emits a deprecation
# warning under modern setuptools. Narrowly suppress that third-party warning
# so real app warnings remain visible.
warnings.filterwarnings(
	"ignore",
	message="pkg_resources is deprecated as an API.*",
	category=UserWarning,
	module="pygame.pkgdata",
)
import pygame
import qrcode
import arabic_reshaper
from bidi.algorithm import get_display
from unidecode import unidecode
from lib import vlcclient
from lib.get_platform import *

if get_platform() != "windows":
	from signal import SIGALRM, alarm, signal

from lib.events import ip2websock, ip2pane, ws_send, flash


def synchronized_state(method):
	@wraps(method)
	def wrapper(self, *args, **kwargs):
		with self.state_lock:
			return method(self, *args, **kwargs)
	return wrapper


def serialized_playback(method):
	"""Serialize player transitions without making other requests wait on VLC."""
	@wraps(method)
	def wrapper(self, *args, **kwargs):
		if not self.playback_lock.acquire(blocking=False):
			return False
		try:
			return method(self, *args, **kwargs)
		finally:
			self.playback_lock.release()
	return wrapper


def cleanse_modules(name):
	try:
		for module_name in sorted(sys.modules.keys()):
			if module_name.startswith(name):
				del sys.modules[module_name]
		del globals()[name]
	except:
		pass


class Karaoke:
	ref_W, ref_H = 1920, 1080      # reference screen size, control drawing scale

	queue = []
	queue_json = ''
	available_songs = []
	songname_trans = {} # transliteration is used for sorting and initial letter search
	now_playing = None
	now_playing_filename = None
	now_playing_user = None
	now_playing_transpose = 0
	audio_delay = 0
	has_video = True
	has_subtitle = False
	subtitle_delay = 0
	play_speed = 1.0
	show_subtitle = True
	audio_track_index = 1
	audio_track_total = 1
	audio_track_source = "unknown"
	is_paused = True
	firstSongStarted = False
	switchingSong = False
	qr_code_path = None
	base_path = os.path.dirname(__file__)
	volume_offset = 0
	default_logo_path = os.path.join(base_path, "logo.jpg")
	logical_volume = None   # for normalized volume
	status_dirty = True
	event_dirty = threading.Event()

	def __init__(self, args):

		# override with supplied constructor args if provided
		self.__dict__.update(args.__dict__)
		self.media_paths = list(getattr(args, 'media_paths', []) or [])
		if not self.media_paths:
			self.media_paths = [args.library_root]
		self.library_root = getattr(args, 'library_root', self.media_paths[0])
		self.volume_offset = self.volume = args.volume
		self.logo_path = self.default_logo_path if args.logo_path == None else args.logo_path

		# other initializations
		self.platform = get_platform()
		self.vlcclient = None
		self.screen = None
		self.player_state = {}
		self.log_level = int(args.log_level)
		self.pending_audio_track_index = None
		self.pending_audio_track_cli_index = None
		self.state_lock = threading.RLock()
		self.playback_lock = threading.RLock()
		self.queue = []
		self.queue_json = '[]'
		self.available_songs = []
		self.songname_trans = {}
		self.event_dirty = threading.Event()
		self.full_screen = not args.windowed

		logging.basicConfig(
			format = "[%(asctime)s] %(levelname)s: %(message)s",
			datefmt = "%Y-%m-%d %H:%M:%S",
			level = self.log_level,
		)

		logging.debug(vars(args))

		if self.save_delays:
			self.init_save_delays()

		# Generate connection URL and QR code, retry in case pi is still starting up
		# and doesn't have an IP yet (occurs when launched from /etc/rc.local)
		end_time = int(time.time()) + 30

		if self.platform == "raspberry_pi":
			while int(time.time()) < end_time:
				addresses_str = check_output(["hostname", "-I"]).strip().decode("utf-8")
				addresses = addresses_str.split(" ")
				self.ip = addresses[0]
				if not self.is_network_connected():
					logging.debug("Couldn't get IP, retrying....")
				else:
					break
		else:
			self.ip = self.get_ip()

		logging.debug("IP address (for QR code and splash screen): " + self.ip)

		self.url = "%s://%s:%s" % (('https' if self.ssl else 'http'), self.ip, self.port)

		# get songs from configured media paths
		self.get_available_songs()
		self.song2vol = self.load_volume_cache()

		# clean up old sessions
		self.kill_player()

		self.generate_qr_code()
		self.vlcclient = vlcclient.VLCClient(port = self.vlc_port, path = self.vlc_path,
		                                     qrcode = (self.qr_code_path if self.show_overlay else None), url = self.url)

		if not self.hide_splash_screen:
			self.initialize_screen(not args.windowed)
			self.render_splash_screen()


	# Other ip-getting methods are unreliable and sometimes return 127.0.0.1
	# https://stackoverflow.com/a/28950776
	def get_ip(self):
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		try:
			# doesn't even have to be reachable
			s.connect(("8.8.8.8", 1))
			IP = s.getsockname()[0]
		except Exception:
			IP = "127.0.0.1"
		finally:
			s.close()
		return IP

	def is_network_connected(self):
		return not len(self.ip) < 7

	def generate_qr_code(self):
		logging.debug("Generating URL QR code")
		qr = qrcode.QRCode(version = 1, box_size = 1, border = 4, error_correction = qrcode.constants.ERROR_CORRECT_H)
		qr.add_data(self.url)
		qr.make()
		img = qr.make_image()
		self.qr_code_path = os.path.join(self.base_path, "qrcode.png")
		img.save(self.qr_code_path)

	def get_default_display_mode(self):
		if self.platform == "raspberry_pi":
			# HACK apparently if display mode is fullscreen the vlc window will be at the bottom of pygame
			os.environ["SDL_VIDEO_CENTERED"] = "1"
			return pygame.NOFRAME
		return pygame.FULLSCREEN

	def initialize_screen(self, fullscreen=True):
		if not self.hide_splash_screen:
			logging.debug("Initializing pygame")
			pygame.init()
			pygame.display.set_caption("pikaraoke")
			pygame.mouse.set_visible(0)
			self.fonts = {}
			self.WIDTH = pygame.display.Info().current_w
			self.HEIGHT = pygame.display.Info().current_h
			logging.debug("Initializing screen mode")

			if self.platform != "raspberry_pi":
				self.toggle_full_screen(fullscreen)
			else:
				# this section is an unbelievable nasty hack - for some reason Pygame
				# needs a keyboardinterrupt to initialise in some limited circumstances
				# source: https://stackoverflow.com/questions/17035699/pygame-requires-keyboard-interrupt-to-init-display
				class Alarm(Exception):
					pass

				def alarm_handler(signum, frame):
					raise Alarm

				signal(SIGALRM, alarm_handler)
				alarm(3)
				try:
					self.toggle_full_screen(fullscreen)
					alarm(0)
				except Alarm:
					raise KeyboardInterrupt
			logging.debug("Done initializing splash screen")

	def toggle_full_screen(self, fullscreen=None):
		if not self.hide_splash_screen:
			logging.debug("Toggling fullscreen...")
			self.full_screen = not self.full_screen if fullscreen is None else fullscreen
			if self.full_screen:
				self.screen = pygame.display.set_mode([self.WIDTH, self.HEIGHT], self.get_default_display_mode())
			else:
				self.screen = pygame.display.set_mode([self.WIDTH*3//4, self.HEIGHT*3//4], pygame.RESIZABLE)
			if self.is_file_playing():
				self.play_transposed(self.now_playing_transpose)
			else:
				self.render_splash_screen()

	def normalize(self, v):
		r = self.screen.get_width()/self.ref_W
		if type(v) is list:
			return [i*r for i in v]
		elif type(v) is tuple:
			return tuple(i * r for i in v)
		return v*r

	def render_splash_screen(self):
		if self.hide_splash_screen:
			return

		# Clear the screen and start
		logging.debug("Rendering splash screen")
		self.screen.fill((0, 0, 0))
		blitY = self.ref_W*self.screen.get_height()//self.screen.get_width() - 40
		sysfont_size = 30

		# Draw logo and name
		text = self.render_font(sysfont_size * 2, getString(136), (255, 255, 255))
		if not hasattr(self, 'logo'):
			self.logo = pygame.image.load(self.logo_path)
		_, _, W, H = self.normalize(list(self.logo.get_rect()))
		W, H = W/2, H/2
		center = self.screen.get_rect().center
		self.logo1 = pygame.transform.scale(self.logo, (W, H))
		self.screen.blit(self.logo1, (center[0]-W/2, center[1]-H/2-text[1].height/2))
		self.screen.blit(text[0], (center[0]-text[1].width/2, center[1]+H/2))

		if not self.hide_ip:
			qr_size = 150
			if not hasattr(self, 'p_image'):
				self.p_image = pygame.image.load(self.qr_code_path)
			self.p_image1 = pygame.transform.scale(self.p_image, self.normalize((qr_size, qr_size)))
			self.screen.blit(self.p_image1, self.normalize((20, blitY - 125)))
			if not self.is_network_connected():
				text = self.render_font(sysfont_size, getString(48), (255, 255, 255))
				self.screen.blit(text[0], self.normalize((qr_size + 35, blitY)))
				time.sleep(10)
				logging.info("No IP found. Network/Wifi configuration required. For wifi config, try: sudo raspi-config or the desktop GUI: startx")
				self.stop()
			else:
				text = self.render_font(sysfont_size, getString(49) + self.url, (255, 255, 255))
				self.screen.blit(text[0], self.normalize((qr_size + 35, blitY)))
				if not self.firstSongStarted:
					text = self.render_font(sysfont_size, getString(51), (255, 255, 255))
					self.screen.blit(text[0], self.normalize((qr_size + 35, blitY - 120)))
					text = self.render_font(sysfont_size, getString(52), (255, 255, 255))
					self.screen.blit(text[0], self.normalize((qr_size + 35, blitY - 80)))

		blitY = 10
		if not self.has_video:
			logging.debug("Rendering current song to splash screen")
			render_next_song = self.render_font([60, 50, 40], getString(58) + (self.now_playing or ''), (255, 255, 0))
			render_next_user = self.render_font([50, 40, 30], getString(57) + (self.now_playing_user or ''), (0, 240, 0))
			self.screen.blit(render_next_song[0], (self.screen.get_width() - render_next_song[1].width - 10, self.normalize(10)))
			self.screen.blit(render_next_user[0], (self.screen.get_width() - render_next_user[1].width - 10, self.normalize(80)))
			blitY += 140

		if len(self.queue) >= 1:
			logging.debug("Rendering next song to splash screen")
			next_song = self.queue[0]["title"]
			next_user = self.queue[0]["user"]
			render_next_song = self.render_font([60, 50, 40], getString(56) + next_song, (255, 255, 0))
			render_next_user = self.render_font([50, 40, 30], getString(57) + next_user, (0, 240, 0))
			self.screen.blit(render_next_song[0], (self.screen.get_width() - render_next_song[1].width - 10, self.normalize(blitY)))
			self.screen.blit(render_next_user[0], (self.screen.get_width() - render_next_user[1].width - 10, self.normalize(blitY+70)))
		elif not self.firstSongStarted:
			header = self.render_font(sysfont_size, getString(196) + ':', (255, 255, 0))
			self.screen.blit(header[0], self.normalize((20, 20)))
			path_y = 20 + sysfont_size + 5
			for path in self.media_paths:
				path_text = self.render_font(sysfont_size, path, (255, 255, 0))
				self.screen.blit(path_text[0], self.normalize((20, path_y)))
				path_y += sysfont_size + 5
			text2 = self.render_font(sysfont_size, getString(197) + ': %d'%len(self.available_songs), (255, 255, 0))
			self.screen.blit(text2[0], self.normalize((20, path_y + 5)))

	def render_font(self, sizes, text, *kargs):
		if type(sizes) != list:
			sizes = [sizes]

		# normalize font size
		sizes = [s*self.screen.get_width()/self.ref_W for s in sizes]

		# initialize fonts if not found
		for size in sizes:
			if size not in self.fonts:
				self.fonts[size] = [pygame.freetype.SysFont(pygame.freetype.get_default_font(), size)] \
						+ [pygame.freetype.Font(os.path.join(self.base_path, 'font', name), size) for name in ['arial-unicode-ms.ttf', 'unifont.ttf']]

		# find a font that contains all characters of the song title, if cannot find, then display transliteration instead
		found = None
		for ii, font in enumerate(self.fonts[size]):
			if None not in font.get_metrics(text):
				found = ii
				break
		if found is None:
			text = unidecode(text)
			found = 0

		# reshape Arabic text
		text = get_display(arabic_reshaper.reshape(text))

		# draw the font, if too wide, half the string
		width = self.screen.get_width()
		for size in sorted(sizes, reverse = True):
			font = self.fonts[size][found]
			render = font.render(text, *kargs)
			# reduce font size if text too long
			if render[1].width > width and size != min(sizes):
				continue
			while render[1].width >= width:
				text = text[:int(len(text) * min(width / render[1].width, 0.618))] + '…'
				del render
				render = font.render(text, *kargs)
			break
		return render

	def get_available_songs(self):
		logging.info("Fetching available songs in: " + ", ".join(self.media_paths))
		catalog = MediaCatalog(self.media_paths)
		with getattr(self, 'state_lock', threading.RLock()):
			self.song_catalog = catalog
			self.songname_trans = catalog.sort_names
			self.available_songs = list(catalog.songs)
			self.available_song_set = catalog.song_set

	def get_all_assoc_files(self, song_path):
		song_dir = os.path.dirname(song_path)
		basename = os.path.basename(song_path)
		basestem = os.path.splitext(basename)
		return [
			os.path.join(song_dir, basename),
			os.path.join(song_dir, basestem[0] + '.cdg'),
		]

	def delete_if_exist(self, filename):
		if os.path.isfile(filename):
			try:
				os.remove(filename)
			except:
				pass

	def delete(self, song_path):
		logging.info("Deleting song: " + song_path)

		# delete all associated cdg files if exist
		for fn in self.get_all_assoc_files(song_path):
			self.delete_if_exist(fn)

		self.get_available_songs()

	def rename_if_exist(self, old_path, new_path):
		if os.path.isfile(old_path):
			try:
				shutil.move(old_path, new_path)
			except:
				pass

	def rename(self, song_path, new_basestem):
		logging.info("Renaming song: '" + song_path + "' to: " + new_basestem)
		song_dir = os.path.dirname(song_path)
		ext = os.path.splitext(song_path)
		if len(ext) < 2:
			ext += ['']
		new_basename = new_basestem + ext[1]
		new_path = os.path.join(song_dir, new_basename)

		# rename all associated cdg files if exist
		for src, tgt in zip(self.get_all_assoc_files(song_path), self.get_all_assoc_files(new_path)):
			self.rename_if_exist(src, tgt)

		# rename queue entry if inside queue
		for item in self.queue:
			if item['file'] == song_path:
				item['file'] = new_path
				item['title'] = self.filename_from_path(item['file'])
				break

		self.get_available_songs()

	def filename_from_path(self, file_path):
		return media_title(file_path)

	def kill_player(self):
		logging.debug("Killing old VLC processes")
		if self.vlcclient is not None:
			self.vlcclient.kill()

	@serialized_playback
	def play_file(self, file_path, extra_params=None):
		params = list(extra_params or [])
		with self.state_lock:
			self.switchingSong = True
			is_new_media = file_path != self.now_playing_filename
		try:
			if is_new_media:
				with self.state_lock:
					self.auto_save_delays()
					saved = self.get_saved_delays(file_path) if self.save_delays else {}
					self.audio_delay = saved.get('audio_delay', 0)
					self.subtitle_delay = saved.get('subtitle_delay', 0)
					self.show_subtitle = saved.get('show_subtitle', True)
				if self.pending_audio_track_index is None and self.pending_audio_track_cli_index is None:
					self.prepare_default_audio_track(file_path)
			with self.state_lock:
				if self.platform != 'osx' and not self.hide_splash_screen:
					window = pygame.display.get_wm_info().get('window')
					if window:
						params += ['--drawable-hwnd' if self.platform == 'windows' else '--drawable-xid', hex(window)]
				if self.audio_delay:
					params.append(f'--audio-desync={self.audio_delay * 1000}')
				if self.subtitle_delay:
					params.append(f'--sub-delay={self.subtitle_delay * 10}')
				params.append('--sub-track=0' if self.show_subtitle else '--sub-track=-1')
				if self.pending_audio_track_cli_index is not None:
					params.append(f'--audio-track-id={self.pending_audio_track_cli_index}')
				if self.play_speed != 1:
					params.append(f'--rate={self.play_speed}')
				self.now_playing = self.filename_from_path(file_path)
				self.now_playing_filename = file_path
				self.is_paused = '--start-paused' in params
				volume = self.volume
				transpose = self.now_playing_transpose
			if self.normalize_vol:
				media_vol = self.get_mp3_volume(file_path)
				if self.logical_volume is not None:
					volume = self.logical_volume / media_vol
			else:
				media_vol = 1.0
			volume = min(max(round(float(volume)), 0), 512)
			logging.info("Playing video in VLC: %s", file_path)
			if transpose == 0:
				xml = self.vlcclient.play_file(file_path, volume, params)
			else:
				xml = self.vlcclient.play_file_transpose(file_path, transpose, volume, params)
			if not isinstance(xml, str) or not xml:
				raise vlcclient.PlaybackError("VLC did not return playback status")
			current_track, desired_track = self.set_default_audio_track(xml)
			submitted_track = next((parameter.split('=', 1)[1] for parameter in params if parameter.startswith('--audio-track-id=')), None)
			if desired_track is not None and str(submitted_track) != str(desired_track) and current_track != desired_track:
				# When no prelaunch metadata was available, the first valid status may
				# reveal the accompaniment track. Apply it once, rather than only
				# updating the UI label while the original audio keeps playing.
				params = [parameter for parameter in params if not parameter.startswith('--audio-track-id=')]
				params.append(f'--audio-track-id={desired_track}')
				if transpose == 0:
					xml = self.vlcclient.play_file(file_path, volume, params)
				else:
					xml = self.vlcclient.play_file_transpose(file_path, transpose, volume, params)
				if not isinstance(xml, str) or not xml:
					raise vlcclient.PlaybackError("VLC did not return playback status")
				self.set_default_audio_track(xml)
			with self.state_lock:
				self.has_subtitle = "<info name='Type'>Subtitle</info>" in xml
				self.has_video = "<info name='Type'>Video</info>" in xml
				actual_volume = self.vlcclient.get_val_xml(xml, 'volume')
				try:
					self.volume = round(float(actual_volume))
				except (ValueError, TypeError):
					self.volume = volume
				self.media_vol = media_vol
				if self.normalize_vol:
					self.logical_volume = self.volume * media_vol
			return True
		except Exception:
			with self.state_lock:
				self.reset_now_playing()
			self.vlcclient.kill()
			raise
		finally:
			with self.state_lock:
				self.pending_audio_track_index = None
				self.pending_audio_track_cli_index = None
				self.switchingSong = False
				self.status_dirty = True
				self.event_dirty.set()


	@serialized_playback
	def play_transposed(self, semitones):
		try:
			semitones = int(semitones)
			if not -24 <= semitones <= 24:
				return False
		except (TypeError, ValueError):
			return False
		with self.state_lock:
			if self.switchingSong or not self.is_file_playing():
				return False
			self.now_playing_transpose = semitones
		return self.reload_current_track()

	def is_file_playing(self):
		return self.vlcclient is not None and self.vlcclient.is_running()

	def is_song_in_queue(self, song_path):
		return song_path in map(lambda t: t['file'], self.queue)

	@synchronized_state
	def enqueue(self, song_path, user="Pikaraoke"):
		catalog = getattr(self, 'available_song_set', self.available_songs)
		if not isinstance(song_path, str) or song_path not in catalog:
			logging.warning("Ignoring song outside the current catalog: %r", song_path)
			return False
		if self.is_song_in_queue(song_path):
			logging.info("Song is already in queue: %s", song_path)
			return False
		self.queue.append({"user": str(user), "file": song_path, "title": self.filename_from_path(song_path)})
		self.update_queue()
		return True


	@synchronized_state
	def queue_add_random(self, amount):
		try:
			amount = int(amount)
		except (TypeError, ValueError):
			return False
		if amount <= 0:
			return False
		queued = {song['file'] for song in self.queue}
		candidates = [song for song in self.available_songs if song not in queued]
		selected = random.sample(candidates, min(amount, len(candidates)))
		for song in selected:
			self.queue.append({"user": "Random", "file": song, "title": self.filename_from_path(song)})
		if selected:
			self.update_queue()
		return len(selected) == amount

	@synchronized_state
	def update_queue(self):
		self.queue_json = json.dumps(self.queue)
		self.status_dirty = True
		self.event_dirty.set()


	def queue_clear(self):
		with self.state_lock:
			playing_or_starting = self.switchingSong or self.is_file_playing()
			self.queue = []
			self.update_queue()
		return self.skip() if playing_or_starting else True


	@synchronized_state
	def queue_edit(self, song_file, action, **kwargs):
		if action == "move":
			try:
				src, tgt, size = (int(kwargs[name]) for name in ('src', 'tgt', 'size'))
			except (ValueError, TypeError, KeyError):
				return False
			if size != len(self.queue) or not (0 <= src < size and 0 <= tgt < size):
				logging.info("Ignoring stale or invalid queue move: %s", kwargs)
				return False
			self.queue.insert(tgt, self.queue.pop(src))
		else:
			index = next((i for i, item in enumerate(self.queue) if item['file'] == song_file), None)
			if index is None:
				return False
			if action == 'delete':
				del self.queue[index]
			elif action == 'up' and index > 0:
				self.queue.insert(index - 1, self.queue.pop(index))
			elif action == 'down' and index + 1 < len(self.queue):
				self.queue.insert(index + 1, self.queue.pop(index))
			else:
				return False
		self.update_queue()
		return True


	@serialized_playback
	def skip(self):
		with self.state_lock:
			if self.switchingSong or not self.is_file_playing():
				return False
		self.vlcclient.stop()
		with self.state_lock:
			self.reset_now_playing()
			self.status_dirty = True
			self.event_dirty.set()
		return True


	@serialized_playback
	@synchronized_state
	def seek(self, seek_sec):
		if self.switchingSong:
			return False
		if self.is_file_playing():
			self.vlcclient.seek(seek_sec)
			self.enforce_audio_track()
			return True
		logging.warning("Tried to seek, but no file is playing!")
		return False

	def delay_key(self, filename):
		return os.path.normcase(os.path.realpath(filename))

	def get_saved_delays(self, filename):
		key = self.delay_key(filename)
		if key in self.delays:
			return dict(self.delays[key])
		return dict(self.delays.get(os.path.basename(filename), {}))

	def set_delays_dict(self, filename, key, val, dft_val=0):
		if not filename:
			return
		path_key = self.delay_key(filename)
		delays = self.get_saved_delays(filename)
		if val == dft_val:
			delays.pop(key, None)
		else:
			delays[key] = val
		# An empty entry explicitly overrides an inherited legacy basename entry.
		self.delays[path_key] = delays
		self.delays_dirty = True

	@serialized_playback
	@synchronized_state
	def set_audio_delay(self, delay):
		if self.switchingSong or not self.is_file_playing():
			return False
		try:
			value = self.audio_delay + 0.1 if delay == '+' else self.audio_delay - 0.1 if delay == '-' else 0 if delay == '' else float(delay)
			if not math.isfinite(value):
				return False
		except (TypeError, ValueError):
			return False
		self.audio_delay = value
		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'audio_delay', value)
		self.vlcclient.command(f'audiodelay&val={value}')
		self.status_dirty = True
		return value

	@serialized_playback
	@synchronized_state
	def set_subtitle_delay(self, delay):
		if self.switchingSong or not self.is_file_playing():
			return False
		try:
			value = self.subtitle_delay + 0.1 if delay == '+' else self.subtitle_delay - 0.1 if delay == '-' else 0 if delay == '' else float(delay)
			if not math.isfinite(value):
				return False
		except (TypeError, ValueError):
			return False
		self.subtitle_delay = value
		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'subtitle_delay', value)
		self.vlcclient.command(f'subdelay&val={value}')
		self.status_dirty = True
		return value

	@serialized_playback
	def toggle_subtitle(self):
		with self.state_lock:
			if self.switchingSong or not self.is_file_playing():
				return False
			self.show_subtitle = not self.show_subtitle
			if self.save_delays:
				self.set_delays_dict(self.now_playing_filename, 'show_subtitle', self.show_subtitle, True)
		return self.reload_current_track()

	@serialized_playback
	@synchronized_state
	def pause(self):
		if self.switchingSong or not self.is_file_playing():
			return False
		response = self.vlcclient.play() if self.is_paused else self.vlcclient.pause()
		if getattr(response, 'status_code', 200) != 200:
			return False
		self.is_paused = not self.is_paused
		self.status_dirty = True
		return True

	def read_player_number(self, xml, name):
		try:
			value = float(self.vlcclient.get_val_xml(xml, name))
			return value if math.isfinite(value) else None
		except (ValueError, TypeError):
			return None

	@serialized_playback
	@synchronized_state
	def vol_up(self):
		if self.switchingSong or not self.is_file_playing():
			return False
		response = self.vlcclient.vol_up()
		value = self.read_player_number(response.text, 'volume')
		if getattr(response, 'status_code', 200) != 200 or value is None:
			return False
		self.volume = round(value)
		self.update_logical_vol()
		self.status_dirty = True
		return self.volume

	@serialized_playback
	@synchronized_state
	def vol_down(self):
		if self.switchingSong or not self.is_file_playing():
			return False
		response = self.vlcclient.vol_down()
		value = self.read_player_number(response.text, 'volume')
		if getattr(response, 'status_code', 200) != 200 or value is None:
			return False
		self.volume = round(value)
		self.update_logical_vol()
		self.status_dirty = True
		return self.volume

	@serialized_playback
	@synchronized_state
	def vol_set(self, volume):
		if self.switchingSong or not self.is_file_playing():
			return False
		try:
			volume = float(volume)
			if not math.isfinite(volume) or not 0 <= volume <= 512:
				return False
		except (TypeError, ValueError):
			return False
		response = self.vlcclient.vol_set(round(volume))
		value = self.read_player_number(response.text, 'volume')
		if getattr(response, 'status_code', 200) != 200 or value is None:
			return False
		self.volume = round(value)
		self.update_logical_vol()
		self.status_dirty = True
		return self.volume

	@serialized_playback
	@synchronized_state
	def play_speed_set(self, speed):
		if self.switchingSong or not self.is_file_playing():
			return False
		try:
			speed = float(speed)
			if not math.isfinite(speed) or not 0.25 <= speed <= 4:
				return False
		except (TypeError, ValueError):
			return False
		response = self.vlcclient.playspeed_set(speed)
		value = self.read_player_number(response.text, 'rate')
		if getattr(response, 'status_code', 200) != 200 or value is None:
			return False
		self.play_speed = value
		self.status_dirty = True
		return self.play_speed

	def _infer_audio_track_cli_index(self, desired_index):
		if not self.is_file_playing():
			return desired_index
		current_id, track_ids, _ = self.vlcclient.get_audio_track_info(file_path=self.now_playing_filename)
		return self._get_audio_track_id(track_ids, desired_index)

	def _get_audio_track_id(self, track_ids, desired_index):
		if track_ids and 1 <= desired_index <= len(track_ids):
			return track_ids[desired_index - 1]
		return desired_index

	def _get_default_audio_track_index(self, track_ids):
		if len(track_ids) >= 2:
			return 2
		return 1

	def prepare_default_audio_track(self, file_path):
		_, track_ids, _ = self.vlcclient.get_audio_track_info(file_path=file_path)
		if not track_ids:
			return
		desired_index = self._get_default_audio_track_index(track_ids)
		with self.state_lock:
			self.audio_track_total = len(track_ids)
			self.audio_track_index = desired_index
			if desired_index > 1:
				self.pending_audio_track_index = desired_index
				self.pending_audio_track_cli_index = self._get_audio_track_id(track_ids, desired_index)

	def update_audio_track_status(self, xml=None):
		with self.state_lock:
			if self.switchingSong:
				return
			if not self.is_file_playing():
				self.audio_track_index = self.audio_track_total = 1
				self.audio_track_source = 'unknown'
				return
			file_path = self.now_playing_filename
			prev_index = self.audio_track_index
			generation = getattr(self.vlcclient, 'playback_generation', None)
		current_id, track_ids, source = self.vlcclient.get_audio_track_info(xml, file_path)
		with self.state_lock:
			if source == 'stale' or self.switchingSong or self.now_playing_filename != file_path or generation != getattr(self.vlcclient, 'playback_generation', None):
				return
			new_total = len(track_ids) or 1
			try:
				new_index = track_ids.index(current_id) + 1
			except ValueError:
				new_index = prev_index if 1 <= prev_index <= new_total else self._get_default_audio_track_index(track_ids)
			self.audio_track_index = new_index
			self.audio_track_total = new_total
			self.audio_track_source = source or 'unknown'

	def set_default_audio_track(self, xml=None):
		current_id, track_ids, source = self.vlcclient.get_audio_track_info(xml, self.now_playing_filename)
		with self.state_lock:
			self.audio_track_source = source or 'unknown'
			desired_index = self.pending_audio_track_index
			if desired_index is None:
				desired_index = self._get_default_audio_track_index(track_ids)
			self.audio_track_total = len(track_ids) or 1
			self.audio_track_index = min(max(desired_index, 1), self.audio_track_total)
			self.status_dirty = True
			return current_id, self._get_audio_track_id(track_ids, self.audio_track_index) if track_ids else None

	@serialized_playback
	def next_audio_track(self):
		with self.state_lock:
			if self.switchingSong or not self.is_file_playing():
				return False
			file_path = self.now_playing_filename
		_, track_ids, source = self.vlcclient.get_audio_track_info(file_path=file_path)
		if len(track_ids) <= 1:
			return False
		with self.state_lock:
			current_index = self.audio_track_index if 1 <= self.audio_track_index <= len(track_ids) else self._get_default_audio_track_index(track_ids)
			next_index = (current_index % len(track_ids)) + 1
			self.audio_track_source = source or 'unknown'
		return self.reload_current_track(audio_track_index=next_index)

	@synchronized_state
	def enforce_audio_track(self):
		if not self.is_file_playing():
			return
		if self.audio_track_total <= 1:
			return
		self.update_audio_track_status()

	@serialized_playback
	def reload_current_track(self, audio_track_index=None):
		with self.state_lock:
			if self.switchingSong or not self.is_file_playing():
				return False
			file_path = self.now_playing_filename
			was_paused = self.is_paused
			if audio_track_index is None:
				audio_track_index = self.audio_track_index
		status_xml = self.vlcclient.command().text
		info = self.vlcclient.get_info_xml(status_xml)
		try:
			position = max(0.0, float(info.get('time') or 0))
		except (ValueError, TypeError):
			position = 0.0
		if audio_track_index and audio_track_index >= 1:
			track_id = self._infer_audio_track_cli_index(audio_track_index)
			with self.state_lock:
				self.pending_audio_track_index = audio_track_index
				self.pending_audio_track_cli_index = track_id
		params = [f'--start-time={position}']
		if was_paused:
			params.append('--start-paused')
		return self.play_file(file_path, params)

	def get_state(self):
		def snapshot(include_cached=True):
			defaults = {'volume': self.volume, 'time': 0, 'length': 0, 'audiodelay': self.audio_delay,
				'subtitledelay': self.subtitle_delay, 'rate': self.play_speed, 'state': 'paused'}
			if include_cached:
				defaults.update(self.player_state)
			return defaultdict(lambda: None, defaults)
		with self.state_lock:
			if self.switchingSong or self.vlcclient is None or self.vlcclient.is_transposing:
				return snapshot()
			if not self.is_file_playing():
				return snapshot(include_cached=False)
			file_path = self.now_playing_filename
			generation = getattr(self.vlcclient, 'playback_generation', None)
		new_state = self.vlcclient.get_info_xml()
		with self.state_lock:
			if self.switchingSong or self.now_playing_filename != file_path or generation != getattr(self.vlcclient, 'playback_generation', None):
				return snapshot()
		self.update_audio_track_status()
		with self.state_lock:
			if not self.switchingSong and self.now_playing_filename == file_path and generation == getattr(self.vlcclient, 'playback_generation', None):
				self.player_state.update({key: value for key, value in new_state.items() if value is not None})
			return snapshot()


	@serialized_playback
	@synchronized_state
	def restart(self):
		if self.switchingSong:
			return False
		if self.is_file_playing():
			self.vlcclient.restart()
			self.is_paused = False
			return True
		else:
			logging.warning("Tried to restart, but no file is playing!")
			return False

	def stop(self):
		self.running = False

	def handle_run_loop(self):
		if self.hide_splash_screen:
			time.sleep(0.1)
			return
		for event in pygame.event.get():
			if event.type == pygame.QUIT:
				logging.warn("Window closed: Exiting pikaraoke...")
				self.running = False
			elif event.type == pygame.KEYDOWN:
				if event.key == pygame.K_ESCAPE:
					logging.warn("ESC pressed: Exiting pikaraoke...")
					self.running = False
				if event.key == pygame.K_f:
					self.toggle_full_screen()
		if not self.is_file_playing() or not self.has_video:
			self.render_splash_screen()
			pygame.display.update()
		pygame.time.wait(100)

	# Use this to reset the screen in case it loses focus
	# This seems to occur in windows after playing a video
	def pygame_reset_screen(self):
		if not self.hide_splash_screen:
			logging.debug("Resetting pygame screen...")
			pygame.display.quit()
			self.initialize_screen()
			self.render_splash_screen()

	@synchronized_state
	def reset_now_playing(self):
		self.auto_save_delays()
		self.now_playing = None
		self.now_playing_filename = None
		self.now_playing_user = None
		self.is_paused = True
		self.now_playing_transpose = 0
		self.audio_delay = 0
		self.subtitle_delay = 0
		self.show_subtitle = True
		self.has_subtitle = False
		self.has_video = True
		self.audio_track_index = 1
		self.audio_track_total = 1
		self.audio_track_source = "unknown"
		self.play_speed = 1
		self.player_state = {}
		self.pending_audio_track_index = None
		self.pending_audio_track_cli_index = None

	def get_mp3_volume(self, filename):
		"""Estimate relative loudness on demand, without hashing or loading PCM."""
		try:
			path_key = self.delay_key(filename)
			stat = os.stat(filename)
			cached = self.song2vol.get(path_key, {})
			if isinstance(cached, dict) and cached.get('size') == stat.st_size and cached.get('mtime_ns') == stat.st_mtime_ns:
				factor = float(cached['factor'])
				if math.isfinite(factor) and factor > 0:
					return factor
			result = subprocess.run(
				['ffmpeg', '-hide_banner', '-nostats', '-i', filename, '-t', '60', '-vn', '-sn', '-dn',
				 '-af', 'volumedetect', '-f', 'null', '-'],
				capture_output=True, text=True, timeout=45, check=True,
			)
			match = re.search(r'mean_volume:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dB', result.stderr)
			if not match:
				raise ValueError('FFmpeg returned no mean volume')
			mean_db = float(match.group(1))
			factor = min(max(10 ** ((mean_db + 18) / 20), 1 / 16), 16)
			self.song2vol[path_key] = {'factor': factor, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
			try:
				self.write_json_atomic(os.path.join(self.library_root, '.mp3_volume.json.gz'), self.song2vol, compressed=True)
			except OSError as exc:
				logging.warning('Could not save loudness cache: %s', exc)
			return factor
		except (OSError, ValueError, KeyError, TypeError, OverflowError, subprocess.SubprocessError) as exc:
			logging.warning('Could not measure loudness for %s: %s', filename, exc)
			return 1.0

	def load_volume_cache(self):
		cache_path = os.path.join(self.library_root, '.mp3_volume.json.gz')
		try:
			with gzip.open(cache_path, 'rt', encoding='utf-8') as fp:
				cache = json.load(fp)
				return cache if isinstance(cache, dict) else {}
		except FileNotFoundError:
			return {}
		except Exception:
			return {}

	def update_logical_vol(self):
		if hasattr(self, 'media_vol'):
			self.logical_volume = self.volume * self.media_vol

	@serialized_playback
	def enable_vol_norm(self, enable):
		if enable and shutil.which('ffmpeg') is None:
			logging.warning('Volume normalization requires ffmpeg on PATH')
			enable = False
		with self.state_lock:
			self.normalize_vol = bool(enable)
			file_path = self.now_playing_filename
		if enable and file_path:
			factor = self.get_mp3_volume(file_path)
			with self.state_lock:
				self.media_vol = factor
				self.update_logical_vol()
		with self.state_lock:
			self.status_dirty = True
		return str(self.logical_volume)

	def init_save_delays(self):
		self.delays_dirty = False
		self.delays = {}
		try:
			with open(self.save_delays, encoding='utf-8') as stream:
				content = stream.read()
			try:
				data = json.loads(content)
			except json.JSONDecodeError:
				data = ast.literal_eval(content)
			if not isinstance(data, dict):
				raise ValueError('Delay settings must be an object')
			for path, settings in data.items():
				if not isinstance(path, str) or not isinstance(settings, dict):
					continue
				valid = {}
				for key in ('audio_delay', 'subtitle_delay'):
					try:
						value = float(settings[key])
						if math.isfinite(value):
							valid[key] = value
					except (KeyError, TypeError, ValueError):
						pass
				if isinstance(settings.get('show_subtitle'), bool):
					valid['show_subtitle'] = settings['show_subtitle']
				self.delays[path] = valid
		except FileNotFoundError:
			self.delays_dirty = True
			self.auto_save_delays()
		except (OSError, ValueError, SyntaxError) as exc:
			logging.warning('Could not load delay settings %s: %s', self.save_delays, exc)

	@synchronized_state
	def set_save_delays(self, state):
		if state != bool(self.save_delays):
			if state:
				self.save_delays = self.dft_delays_file
				self.init_save_delays()
			else:
				self.auto_save_delays()
				self.save_delays = None

	def write_json_atomic(self, filename, data, compressed=False):
		directory = os.path.dirname(os.path.abspath(filename))
		descriptor, temporary = tempfile.mkstemp(prefix='.ktv-settings-', dir=directory)
		os.close(descriptor)
		try:
			if compressed:
				stream = gzip.open(temporary, 'wt', encoding='utf-8')
			else:
				stream = open(temporary, 'w', encoding='utf-8')
			with stream:
				json.dump(data, stream, ensure_ascii=False, indent=1)
			os.replace(temporary, filename)
		finally:
			if os.path.exists(temporary):
				os.remove(temporary)

	def auto_save_delays(self):
		if self.save_delays and self.delays_dirty:
			try:
				self.write_json_atomic(self.save_delays, self.delays)
				self.delays_dirty = False
			except OSError as exc:
				logging.warning('Could not save delay settings %s: %s', self.save_delays, exc)

	def run(self):
		logging.info("Starting PiKaraoke!")
		self.running = True
		try:
			while self.running:
				try:
					with self.state_lock:
						transitioning = self.switchingSong
						playing = self.is_file_playing()
						if not transitioning and not playing and self.now_playing is not None:
							self.reset_now_playing()
						start_next = bool(self.queue) and not transitioning and not playing
					if start_next:
						self.render_splash_screen()
						deadline = time.monotonic() + self.splash_delay
						while self.running and time.monotonic() < deadline:
							self.handle_run_loop()
						if not self.running:
							break
						with self.state_lock:
							head = self.queue.pop(0) if self.queue else None
							self.update_queue()
						if head is None:
							continue
						try:
							started = self.play_file(head['file'])
						except Exception:
							logging.exception('Skipping song after playback failure: %s', head['file'])
							continue
						if not started:
							with self.state_lock:
								self.queue.insert(0, head)
								self.update_queue()
							self.handle_run_loop()
							continue
						if started:
							with self.state_lock:
								self.firstSongStarted = True
								self.now_playing_user = head['user']
								self.update_queue()
					self.handle_run_loop()
				except KeyboardInterrupt:
					self.running = False
		finally:
			if self.vlcclient is not None:
				try:
					self.vlcclient.stop()
				finally:
					self.vlcclient.kill()
			self.auto_save_delays()
