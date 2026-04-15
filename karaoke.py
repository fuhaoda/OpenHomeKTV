import os, sys, random, time, json
import logging, socket, subprocess, threading
import shutil
import gzip
import warnings
from subprocess import check_output
from collections import *
from functools import wraps

import numpy as np

from constants import media_types

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
from flask import request
from lib import vlcclient
from lib.get_platform import *
from app import getString

if get_platform() != "windows":
	from signal import SIGALRM, alarm, signal, SIGTERM
	signal(SIGTERM, lambda signum, stack_frame: os.K.stop())

STD_VOL = 65536/8/np.sqrt(2)
ip2websock, ip2pane = {}, {}

ws_send = lambda ip, msg: ip2websock[ip].send(msg) if ip in ip2websock else None

def synchronized_state(method):
	@wraps(method)
	def wrapper(self, *args, **kwargs):
		with self.state_lock:
			return method(self, *args, **kwargs)
	return wrapper

def flash(message: str, category: str = "message", client_ip = ''):
	ws_send(client_ip or request.remote_addr, f'showNotification("{message}", "{category}")')

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
						+ [pygame.freetype.Font(f'font/{name}', size) for name in ['arial-unicode-ms.ttf', 'unifont.ttf']]

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
		self.songname_trans = {}
		ordered_files = []
		for media_dir in self.media_paths:
			if not os.path.isdir(media_dir):
				logging.warning("Media path not found: %s", media_dir)
				continue
			files_in_dir = []
			for bn in os.listdir(media_dir):
				fn = os.path.join(media_dir, bn)
				if not bn.startswith('.') and os.path.isfile(fn):
					if os.path.splitext(fn)[1].lower() in media_types:
						files_in_dir.append(fn)
						trans = unidecode(self.filename_from_path(fn)).lower()
						while trans and not trans[0].islower() and not trans[0].isdigit():
							trans = trans[1:]
						self.songname_trans[fn] = trans
			files_in_dir.sort(key=lambda f: self.songname_trans.get(f, os.path.basename(f).lower()))
			ordered_files.extend(files_in_dir)

		self.available_songs = ordered_files

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
		rc = os.path.basename(file_path)
		rc = os.path.splitext(rc)[0]
		rc = rc.split("---")[0]
		return rc

	def kill_player(self):
		logging.debug("Killing old VLC processes")
		if self.vlcclient is not None:
			self.vlcclient.kill()

	@synchronized_state
	def play_file(self, file_path, extra_params = []):
		self.switchingSong = True
		is_new_media = file_path != self.now_playing_filename
		if self.save_delays:
			saved_delays = self.delays.get(os.path.basename(file_path), {})
			self.audio_delay = self.audio_delay if self.audio_delay else saved_delays.get('audio_delay', 0)
			self.subtitle_delay = self.subtitle_delay if self.subtitle_delay else saved_delays.get('subtitle_delay', 0)
			self.show_subtitle = False if self.show_subtitle==False else saved_delays.get('show_subtitle', True)
		if is_new_media and self.pending_audio_track_index is None and self.pending_audio_track_cli_index is None:
			self.prepare_default_audio_track(file_path)
		extra_params1 = []
		logging.info("Playing video in VLC: " + file_path)
		if self.platform != 'osx':
			extra_params1 += ['--drawable-hwnd' if self.platform == 'windows' else '--drawable-xid',
			                  hex(pygame.display.get_wm_info()['window'])]
		if self.audio_delay:
			extra_params1 += [f'--audio-desync={self.audio_delay * 1000}']
		if self.subtitle_delay:
			extra_params1 += [f'--sub-delay={self.subtitle_delay * 10}']
		if self.show_subtitle:
			extra_params1 += [f'--sub-track=0']
		if self.pending_audio_track_cli_index is not None:
			# VLC's --audio-track uses a 0-based ordinal, while the IDs returned by
			# status/pl_info/ffprobe are stream IDs. Use --audio-track-id so reloads
			# preserve the intended track reliably across containers/codecs.
			extra_params1 += [f'--audio-track-id={self.pending_audio_track_cli_index}']
		if self.play_speed != 1:
			extra_params1 += [f'--rate={self.play_speed}']
		self.now_playing = self.filename_from_path(file_path)
		self.now_playing_filename = file_path
		self.is_paused = ('--start-paused' in extra_params1)
		if self.normalize_vol and self.logical_volume is not None:
			self.volume = self.logical_volume / np.sqrt(self.get_mp3_volume(file_path))
		if self.now_playing_transpose == 0:
			xml = self.vlcclient.play_file(file_path, self.volume, extra_params + extra_params1)
		else:
			xml = self.vlcclient.play_file_transpose(file_path, self.now_playing_transpose, self.volume, extra_params + extra_params1)
		self.has_subtitle = "<info name='Type'>Subtitle</info>" in xml
		self.has_video = "<info name='Type'>Video</info>" in xml
		self.volume = round(float(self.vlcclient.get_val_xml(xml, 'volume')))
		if self.normalize_vol:
			self.media_vol = self.get_mp3_volume(self.now_playing_filename)
			self.logical_volume = self.volume * np.sqrt(self.media_vol)
		self.set_default_audio_track(xml)
		self.pending_audio_track_index = None
		self.pending_audio_track_cli_index = None

		self.switchingSong = False
		self.status_dirty = True
		self.render_splash_screen()  # remove old previous track

	@synchronized_state
	def play_transposed(self, semitones):
		self.now_playing_transpose = semitones
		status_xml = self.vlcclient.command().text if self.is_paused else self.vlcclient.pause(False).text
		info = self.vlcclient.get_info_xml(status_xml)
		posi = info['position']*info['length']
		if self.audio_track_index and self.audio_track_index >= 1:
			self.pending_audio_track_index = self.audio_track_index
			self.pending_audio_track_cli_index = self._infer_audio_track_cli_index(self.audio_track_index)
		self.play_file(self.now_playing_filename, [f'--start-time={posi}'] + (['--start-paused'] if self.is_paused else []))

	def is_file_playing(self):
		if self.vlcclient is not None and self.vlcclient.is_running():
			return True
		elif self.now_playing_filename:
			self.now_playing = self.now_playing_filename = None
		return False

	def is_song_in_queue(self, song_path):
		return song_path in map(lambda t: t['file'], self.queue)

	@synchronized_state
	def enqueue(self, song_path, user = "Pikaraoke"):
		if (self.is_song_in_queue(song_path)):
			logging.warn("Song is already in queue, will not add: " + song_path)
			return False
		else:
			logging.info("'%s' is adding song to queue: %s" % (user, song_path))
			self.queue.append({"user": user, "file": song_path, "title": self.filename_from_path(song_path)})
			self.update_queue()
			return True

	@synchronized_state
	def queue_add_random(self, amount):
		logging.info("Adding %d random songs to queue" % amount)
		songs = list(self.available_songs)  # make a copy
		if len(songs) == 0:
			logging.warn("No available songs!")
			return False
		i = 0
		while i < amount:
			r = random.randint(0, len(songs) - 1)
			if self.is_song_in_queue(songs[r]):
				logging.warn("Song already in queue, trying another... " + songs[r])
			else:
				self.queue.append({"user": "Random", "file": songs[r], "title": self.filename_from_path(songs[r])})
				i += 1
			songs.pop(r)
			if len(songs) == 0:
				self.update_queue()
				logging.warn("Ran out of songs!")
				return False
		self.update_queue()
		return True

	@synchronized_state
	def update_queue(self):
		self.queue_json = json.dumps(self.queue)
		self.status_dirty = True

	@synchronized_state
	def queue_clear(self):
		logging.info("Clearing queue!")
		self.queue = []
		self.update_queue()
		self.skip()

	@synchronized_state
	def queue_edit(self, song_file, action, **kwargs):
		if action == "move":
			try:
				src, tgt, size = [int(kwargs[n]) for n in ['src', 'tgt', 'size']]
				if size > len(self.queue):
					# new songs have started while dragging the list
					diff = size - len(self.queue)
					src -= diff
					tgt -= diff
				song = self.queue.pop(src)
				self.queue.insert(tgt, song)
			except:
				logging.error("Invalid move song request: " + str(kwargs))
				return False
		else:
			match = [(ii,each) for ii,each in enumerate(self.queue) if song_file in each["file"]]
			index, song = match[0] if match else (-1, None)
			if song == None:
				logging.error("Song not found in queue: " + song["file"])
				return False
			if action == "up":
				if index < 1:
					logging.warn("Song is up next, can't bump up in queue: " + song["file"])
					return False
				else:
					logging.info("Bumping song up in queue: " + song["file"])
					del self.queue[index]
					self.queue.insert(index - 1, song)
			elif action == "down":
				if index == len(self.queue) - 1:
					logging.warn("Song is already last, can't bump down in queue: " + song["file"])
					return False
				else:
					logging.info("Bumping song down in queue: " + song["file"])
					del self.queue[index]
					self.queue.insert(index + 1, song)
			elif action == "delete":
				logging.info("Deleting song from queue: " + song["file"])
				del self.queue[index]
			else:
				logging.error("Unrecognized direction: " + action)
				return False
		self.update_queue()
		return True

	@synchronized_state
	def skip(self):
		if self.is_file_playing():
			logging.info("Skipping: " + self.now_playing)
			self.vlcclient.stop()
			self.reset_now_playing()
			return True
		logging.warning("Tried to skip, but no file is playing!")
		return False

	@synchronized_state
	def seek(self, seek_sec):
		if self.is_file_playing():
			self.vlcclient.seek(seek_sec)
			self.enforce_audio_track()
			return True
		logging.warning("Tried to seek, but no file is playing!")
		return False

	def set_delays_dict(self, filename, key, val, dft_val=0):
		basename = os.path.basename(filename)
		delays = self.delays.get(basename, {})
		if val == dft_val:
			delays.pop(key, None)
		else:
			delays[key] = val
		if delays:
			self.delays[basename] = delays
		else:
			self.delays.pop(basename, {})
		self.delays_dirty = True

	@synchronized_state
	def set_audio_delay(self, delay):
		if delay == '+':
			self.audio_delay += 0.1
		elif delay == '-':
			self.audio_delay -= 0.1
		elif delay == '':
			self.audio_delay = 0
		else:
			try:
				self.audio_delay = float(delay)
			except:
				logging.warning(f"Tried to set audio delay to an invalid value {delay}, ignored!")
				return False

		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'audio_delay', self.audio_delay)

		if self.is_file_playing():
			self.vlcclient.command(f"audiodelay&val={self.audio_delay}")
			self.status_dirty = True
			return self.audio_delay
		logging.warning("Tried to set audio delay, but no file is playing!")
		return False

	@synchronized_state
	def set_subtitle_delay(self, delay):
		if delay == '+':
			self.subtitle_delay += 0.1
		elif delay == '-':
			self.subtitle_delay -= 0.1
		elif delay == '':
			self.subtitle_delay = 0
		else:
			try:
				self.subtitle_delay = float(delay)
			except:
				logging.warning(f"Tried to set subtitle delay to an invalid value {delay}, ignored!")
				return False

		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'subtitle_delay', self.subtitle_delay)

		if self.is_file_playing():
			self.vlcclient.command(f"subdelay&val={self.subtitle_delay}")
			self.status_dirty = True
			return self.subtitle_delay
		logging.warning("Tried to set subtitle delay, but no file is playing!")
		return False

	@synchronized_state
	def toggle_subtitle(self):
		self.show_subtitle = not self.show_subtitle
		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'show_subtitle', self.show_subtitle, True)
		self.reload_current_track()

	@synchronized_state
	def pause(self):
		if self.is_file_playing():
			logging.info("Toggling pause: " + self.now_playing)
			if self.vlcclient.is_playing():
				self.vlcclient.pause()
				self.is_paused = True
			else:
				self.vlcclient.play()
				self.is_paused = False
			self.status_dirty = True
			return True
		else:
			logging.warning("Tried to pause, but no file is playing!")
			return False

	@synchronized_state
	def vol_up(self):
		if self.is_file_playing():
			self.vlcclient.vol_up()
			xml = self.vlcclient.command().text
			self.volume = int(self.vlcclient.get_val_xml(xml, 'volume'))
			self.update_logical_vol()
			return self.volume
		else:
			logging.warning("Tried to volume up, but no file is playing!")
			return False

	@synchronized_state
	def vol_down(self):
		if self.is_file_playing():
			self.vlcclient.vol_down()
			xml = self.vlcclient.command().text
			self.volume = int(self.vlcclient.get_val_xml(xml, 'volume'))
			self.update_logical_vol()
			return self.volume
		else:
			logging.warning("Tried to volume down, but no file is playing!")
			return False

	@synchronized_state
	def vol_set(self, volume):
		if self.is_file_playing():
			self.vlcclient.vol_set(volume)
			xml = self.vlcclient.command().text
			self.volume = int(self.vlcclient.get_val_xml(xml, 'volume'))
			self.update_logical_vol()
			return self.volume
		else:
			logging.warning("Tried to set volume, but no file is playing!")
			return False

	@synchronized_state
	def play_speed_set(self, speed):
		if self.is_file_playing():
			self.vlcclient.playspeed_set(speed)
			xml = self.vlcclient.command().text
			self.play_speed = float(self.vlcclient.get_val_xml(xml, 'rate'))
			logging.info(f"Playback speed set to {self.play_speed}")
			return self.play_speed
		else:
			logging.warning("Tried to set play speed, but no file is playing!")
			return False

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

	@synchronized_state
	def prepare_default_audio_track(self, file_path):
		_, track_ids, _ = self.vlcclient.get_audio_track_info(file_path=file_path)
		if not track_ids:
			return
		desired_index = self._get_default_audio_track_index(track_ids)
		self.audio_track_total = len(track_ids)
		self.audio_track_index = desired_index
		if desired_index > 1:
			self.pending_audio_track_index = desired_index
			self.pending_audio_track_cli_index = self._get_audio_track_id(track_ids, desired_index)

	@synchronized_state
	def update_audio_track_status(self, xml=None):
		prev_index = self.audio_track_index
		if not self.is_file_playing():
			self.audio_track_index = 1
			self.audio_track_total = 1
			self.audio_track_source = "unknown"
			return
		current_id, track_ids, source = self.vlcclient.get_audio_track_info(xml, self.now_playing_filename)
		new_index = 1
		new_total = 1
		if track_ids:
			new_total = len(track_ids)
			try:
				new_index = track_ids.index(current_id) + 1
			except ValueError:
				if 1 <= prev_index <= new_total:
					new_index = prev_index
				else:
					new_index = self._get_default_audio_track_index(track_ids)
		self.audio_track_index = new_index
		self.audio_track_total = new_total
		self.audio_track_source = source or "unknown"

	@synchronized_state
	def set_default_audio_track(self, xml=None):
		_, track_ids, source = self.vlcclient.get_audio_track_info(xml, self.now_playing_filename)
		self.audio_track_source = source or "unknown"
		desired_index = self.pending_audio_track_index
		if desired_index is None:
			desired_index = self._get_default_audio_track_index(track_ids)
		if not track_ids:
			self.update_audio_track_status(xml)
			return

		desired_index = min(max(desired_index, 1), len(track_ids))
		# On macOS/VLC the HTTP status often omits the active audio track entirely.
		# Keep the app's selected track as the source of truth and let reloads choose
		# the stream via --audio-track-id, rather than mixing in HTTP audio_track calls.
		self.audio_track_total = len(track_ids)
		self.audio_track_index = desired_index
		self.status_dirty = True

	@synchronized_state
	def next_audio_track(self):
		if not self.is_file_playing():
			return False
		_, track_ids, source = self.vlcclient.get_audio_track_info(file_path=self.now_playing_filename)
		if not track_ids or len(track_ids) == 1:
			self.update_audio_track_status()
			return False
		current_index = self.audio_track_index if 1 <= self.audio_track_index <= len(track_ids) else self._get_default_audio_track_index(track_ids)
		next_index = (current_index % len(track_ids)) + 1
		self.audio_track_source = source or "unknown"
		self.pending_audio_track_index = next_index
		self.pending_audio_track_cli_index = self._get_audio_track_id(track_ids, next_index)
		self.reload_current_track(audio_track_index=next_index)
		self.audio_track_index = next_index
		self.audio_track_total = len(track_ids)
		self.status_dirty = True
		return True

	@synchronized_state
	def enforce_audio_track(self):
		if not self.is_file_playing():
			return
		if self.audio_track_total <= 1:
			return
		self.update_audio_track_status()

	@synchronized_state
	def reload_current_track(self, audio_track_index=None):
		if not self.is_file_playing():
			logging.warning("Tried to reload, but no file is playing!")
			return False
		status_xml = self.vlcclient.command().text if self.is_paused else self.vlcclient.pause(False).text
		info = self.vlcclient.get_info_xml(status_xml)
		posi = info['position']*info['length']
		if audio_track_index is None:
			audio_track_index = self.audio_track_index
		if audio_track_index and audio_track_index >= 1:
			self.pending_audio_track_index = audio_track_index
			self.pending_audio_track_cli_index = self._infer_audio_track_cli_index(audio_track_index)
		self.play_file(self.now_playing_filename, [f'--start-time={posi}'] + (['--start-paused'] if self.is_paused else []))
		self.update_audio_track_status()
		return True

	@synchronized_state
	def get_state(self):
		if self.vlcclient.is_transposing:
			return defaultdict(lambda: None, self.player_state)
		if not self.is_file_playing():
			self.player_state['now_playing'] = None
			return defaultdict(lambda: None, self.player_state)
		new_state = self.vlcclient.get_info_xml()
		self.player_state.update(new_state)
		self.update_audio_track_status()
		return defaultdict(lambda: None, self.player_state)

	@synchronized_state
	def restart(self):
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

	def get_mp3_volume(self, filename):
		try:
			basename, md5, fsize = os.path.basename(filename), md5sum(filename), os.stat(filename).st_size
			vol_fsize_md5 = self.song2vol.get(basename, [0]*3)
			if fsize == vol_fsize_md5[1] and md5 == vol_fsize_md5[2]:
				return vol_fsize_md5[0]
			pcm_data = subprocess.check_output(['ffmpeg', '-i', filename, '-vn', '-f', 's16le', '-acodec', 'pcm_s16le', '-'], stderr = subprocess.DEVNULL)
			volume_val = np.clip(np.sqrt(np.std(np.frombuffer(pcm_data, dtype = np.int16))/STD_VOL), 1/16, 16)
			self.song2vol[basename] = [volume_val, fsize, md5]
			cache_path = os.path.join(self.library_root, '.mp3_volume.json.gz')
			with gzip.open(cache_path, 'wt', encoding='utf-8') as fp:
				json.dump(self.song2vol, fp, indent=1)
			return volume_val
		except:
			self.normalize_vol = False
			return 1

	def load_volume_cache(self):
		cache_path = os.path.join(self.library_root, '.mp3_volume.json.gz')
		try:
			with gzip.open(cache_path, 'rt', encoding='utf-8') as fp:
				return json.load(fp)
		except FileNotFoundError:
			return {}
		except Exception:
			return {}

	def update_logical_vol(self):
		if hasattr(self, 'media_vol'):
			self.logical_volume = self.volume * self.media_vol

	def enable_vol_norm(self, enable):
		self.normalize_vol = enable
		if enable and shutil.which('ffmpeg') is None:
			self.normalize_vol = enable = False
		if enable and self.now_playing_filename:
			self.volume = self.vlcclient.get_info_xml()['volume']
			self.media_vol = self.get_mp3_volume(self.now_playing_filename)
			self.update_logical_vol()
		return str(self.logical_volume)

	def init_save_delays(self):
		self.delays_dirty = False
		try:
			self.delays = eval(open(self.save_delays).read())
		except:
			self.delays = {}
			with open(self.save_delays, 'w') as fp:
				fp.write(str(self.delays))

	def set_save_delays(self, state):
		if state != bool(self.save_delays):
			if state:
				self.save_delays = self.dft_delays_file
				self.init_save_delays()
			else:
				self.save_delays = None
				self.delete_if_exist(self.dft_delays_file)

	def auto_save_delays(self):
		if self.save_delays and self.delays_dirty:
			self.delays_dirty = False
			with open(self.save_delays, 'w') as fp:
				fp.write(str(self.delays))

	def run(self):
		logging.info("Starting PiKaraoke!")
		self.running = True

		while self.running:
			try:
				if not self.is_file_playing() and self.now_playing != None:
					self.reset_now_playing()
				if self.queue:
					if not self.is_file_playing():
						self.reset_now_playing()
						self.render_splash_screen()
						tm = time.time()
						while time.time()-tm < self.splash_delay:
							self.handle_run_loop()
						head = self.queue.pop(0)
						self.play_file(head['file'])
						if not self.firstSongStarted:
							self.firstSongStarted = True
						self.now_playing_user = head["user"]
						self.update_queue()
				self.handle_run_loop()
			except KeyboardInterrupt:
				logging.warn("Keyboard interrupt: Exiting pikaraoke...")
				self.running = False

		# Clean up before quit
		if self.vlcclient is not None:
			self.vlcclient.stop()
		self.auto_save_delays()
		time.sleep(1)
		if self.vlcclient is not None:
			self.vlcclient.kill()
