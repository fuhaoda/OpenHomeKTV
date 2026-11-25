#!/usr/bin/env python3

import argparse, json, locale, os, sys, shutil, math, signal
import threading, time, traceback
from functools import wraps

import psutil, tempfile
from flask import *
from flask.logging import logging
from flask_sock import Sock
from flask_paginate import Pagination, get_page_parameter
from simple_websocket.errors import ConnectionClosed

from karaoke import Karaoke
from constants import VERSION
from collections import defaultdict
from lib.get_platform import *
from lib.vlcclient import get_default_vlc_path, PlaybackError
from lib.media import media_title
from lib.names import normalize_user_name, UserNameError
from lib.events import (
	broadcast, flash, ip2websock, register_client, send_client, unregister_client,
)

try:
	from urllib.parse import quote, unquote
except ImportError:
	from urllib import quote, unquote

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True	##DEBUG
app.secret_key = os.urandom(24)
K = args = None
sock = Sock(app)
if not hasattr(os, 'texts'):
	os.texts = defaultdict(lambda: "")
getString1 = lambda lang, ii: os.langs[lang].get(ii, os.langs['en_US'][ii])
getString2 = lambda ii: getString1(request.client_lang, ii)


@app.errorhandler(PlaybackError)
def playback_error(error):
	logging.warning('Playback request failed: %s', error)
	flash(str(error), 'is-warning')
	return jsonify(success=False, error=str(error)), 503


@app.errorhandler(UserNameError)
def invalid_user_name(error):
	return jsonify(success=False, error=str(error)), 400


def get_system_locale_name():
	lang, _ = locale.getlocale()
	if lang:
		return lang
	try:
		current = locale.setlocale(locale.LC_CTYPE)
	except locale.Error:
		return None
	if not current:
		return None
	normalized = locale.normalize(current)
	if normalized:
		current = normalized
	if "." in current:
		current = current.split(".", 1)[0]
	if "@" in current:
		current = current.split("@", 1)[0]
	return current or None


# Websocket handler
@sock.route('/ws_init')
def ws_init(sock):
	client_ip = request.remote_addr or ''
	client_id = register_client(sock, client_ip)
	try:
		if K is not None:
			with K.state_lock:
				queue_snapshot = [dict(item) for item in K.queue]
			send_client(client_id, {'type': 'queue', 'data': queue_snapshot})
			status = get_nowplaying_snapshot()
			if status:
				send_client(client_id, {'type': 'nowplaying', 'data': status})
		while sock.connected:
			cmd = sock.receive()
			if cmd is None:
				break
			wscmd(client_ip, cmd)
	except ConnectionClosed:
		logging.debug("Browser disconnected: %s", client_id)
	except Exception:
		logging.exception("Browser connection failed: %s", client_id)
	finally:
		unregister_client(client_id)

def wscmd(client_ip, cmd):
	if not isinstance(cmd, str):
		return
	if cmd.startswith('pop_from_queue '):
		name = cmd.split(' ', 1)[1]
		K.queue_edit(name, 'delete')
	elif cmd.startswith('addsongs '):
		lst = cmd[9:].split('\t')
		for fn in lst[1:]:
			K.enqueue(fn, lst[0])

def status_thread():
	cached_status = cached_queue = None
	while True:
		K.event_dirty.wait(1)
		K.event_dirty.clear()
		try:
			status = get_nowplaying_snapshot()
			with K.state_lock:
				queue_snapshot = [dict(item) for item in K.queue]
			if status and status != cached_status:
				broadcast({'type': 'nowplaying', 'data': status})
				cached_status = status
			if queue_snapshot != cached_queue:
				broadcast({'type': 'queue', 'data': queue_snapshot})
				cached_queue = queue_snapshot
		except Exception:
			# One transient player response must not stop every phone's updates.
			logging.exception("Unable to publish player status")

# Define global symbols for Jinja templates 
@app.context_processor
def inject_stage_and_region():
	return {'getString': getString, 'getString1': getString}


@app.before_request
def preprocessor():
	client_lang = request.cookies.get('lang', None)
	if client_lang is None:
		lang_str = request.cookies.get('Accept-Language', os.lang)
		for k in [j for i in lang_str.split(';') for j in i.split(',')]:
			client_lang = find_language(k)
			if client_lang is not None:
				break
	request.client_lang = find_language(client_lang or os.lang)


def filename_from_path(file_path, strip_suffix = True):
	return media_title(file_path, strip_suffix)


def filename_for_search(file_path):
	name = filename_from_path(file_path)
	if isinstance(name, bytes):
		try:
			name = name.decode("utf-8", "ignore")
		except Exception:
			name = str(name)
	return name


def url_escape(filename):
	return quote(filename.encode("utf8"))


app.jinja_env.globals.update(filename_from_path=filename_from_path, url_escape=quote)


def get_player_template_context(client_lang):
	with K.state_lock:
		generation = (K.now_playing_filename, getattr(K.vlcclient, 'playback_generation', None))
	s = K.get_state()
	with K.state_lock:
		if K.switchingSong or generation != (K.now_playing_filename, getattr(K.vlcclient, 'playback_generation', None)):
			s = {'volume': K.volume, 'time': 0, 'length': 0, 'audiodelay': K.audio_delay, 'rate': K.play_speed}
		return {
			"getString1": lambda ii: getString1(client_lang, ii),
			"show_transpose": True,
			"transpose_value": K.now_playing_transpose,
			"volume": s['volume'],
			"seektrack_value": s['time'],
			"seektrack_max": s['length'],
			"audio_delay": s['audiodelay'],
			"play_speed": s['rate'],
			"audio_track_index": K.audio_track_index,
			"audio_track_total": K.audio_track_total,
		}


def get_nowplaying_snapshot():
	with K.state_lock:
		generation = (K.now_playing_filename, getattr(K.vlcclient, 'playback_generation', None))
	s = K.get_state()
	with K.state_lock:
		if K.switchingSong or generation != (K.now_playing_filename, getattr(K.vlcclient, 'playback_generation', None)):
			return {}
		next_song = K.queue[0]["title"] if K.queue else None
		next_user = K.queue[0]["user"] if K.queue else None
		rc = {
			"now_playing": K.now_playing,
			"now_playing_user": K.now_playing_user,
			"up_next": next_song,
			"next_user": next_user,
			"is_paused": s.get('state', 'paused') == 'paused',
			"volume": s['volume'],
			"transpose_value": K.now_playing_transpose,
			"seektrack_value": s['time'],
			"seektrack_max": s['length'],
			"audio_delay": s['audiodelay'],
			"vol_norm": K.normalize_vol,
			"play_speed": s['rate'],
			"audio_track_index": K.audio_track_index,
			"audio_track_total": K.audio_track_total,
		}
		if K.has_subtitle:
			rc['subtitle_delay'] = s['subtitledelay']
			rc['show_subtitle'] = K.show_subtitle
		return rc



@app.route("/")
def root():
	return render_template("index.html", **get_player_template_context(request.client_lang))

@app.route("/home")
def home():
	return render_template("home.html", **get_player_template_context(request.client_lang))
@app.route("/f_home")
def f_home():
	return render_template("f_home.html", **get_player_template_context(request.client_lang))


@app.route("/nowplaying")
def nowplaying(return_json=True):
	try:
		rc = get_nowplaying_snapshot()
		if not rc:
			return "" if return_json else {}
		return json.dumps(rc) if return_json else rc
	except Exception as e:
		logging.error(f"Problem loading /nowplaying, pikaraoke may still be starting up: {e}\n{traceback.print_exc()}")
		return "" if return_json else {}


@app.route("/get_lang_list")
def get_lang_list():
	return json.dumps({k: v[1] for k, v in os.langs.items()}, sort_keys = False)


@app.route("/auto_username")
def auto_username():
	return f'user-{len(ip2websock)+1}'


@app.route("/change_language/<language>")
def change_language(language):
	try:
		set_language(language)
	except:
		logging.error(f"Failed to set server language to {language}")
	return os.lang


@app.route("/user_rename", methods=['POST'])
@app.route("/user_rename/<old_name>/<new_name>")
def user_rename(old_name=None, new_name=None):
	if request.method == 'POST':
		data = request.get_json(silent=True) if request.is_json else request.form
		if not hasattr(data, 'get'):
			raise UserNameError('Both user names are required.')
		old_name, new_name = data.get('old_name'), data.get('new_name')
	if not isinstance(old_name, str):
		raise UserNameError('The previous user name is required.')
	new_name = normalize_user_name(new_name)
	with K.state_lock:
		for q in K.queue:
			if q['user'] == old_name:
				q['user'] = new_name
		if K.now_playing_user == old_name:
			K.now_playing_user = new_name
		K.update_queue()
	return ''


@app.route("/save_delays/<state>")
def set_save_delays(state):
	K.set_save_delays(state.lower() == 'true')
	return ''

@app.route("/norm_vol/<mode>", methods = ["GET"])
def norm_vol(mode):
	K.enable_vol_norm(mode.lower() == 'true')
	return ''


@app.route("/queue")
def queue():
	return render_template("queue.html", getString1 = lambda ii: getString1(request.client_lang, ii), queue = K.queue)
@app.route("/f_queue")
def f_queue():
	return render_template("f_queue.html", getString1 = lambda ii: getString1(request.client_lang, ii), queue = K.queue)


@app.route("/get_queue", methods = ["GET"])
def get_queue():
	with K.state_lock:
		return json.dumps(K.queue)


@app.route("/queue/addrandom", methods = ["GET"])
def add_random():
	amount = request.args.get('amount', type=int)
	if amount is None or amount < 1 or amount > 100:
		return 'Amount must be between 1 and 100', 400
	user = normalize_user_name(request.args.get('user'))
	rc = K.queue_add_random(amount, user=user)
	if rc:
		flash(getString(4) % amount, "is-success")
	else:
		flash(getString(5), "is-warning")
	return ''


@app.route("/queue/edit", methods = ["GET", "POST"])
def queue_edit():
	is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
	values = request.values
	action = values["action"]
	if action not in ('clear', 'move', 'up', 'down', 'delete'):
		return 'Unknown queue action', 400
	if action == "clear":
		if K.queue_clear() is False:
			return jsonify(success=False, error='The waiting queue was cleared. The current song is still starting; try Skip again after startup finishes.'), 409
		flash(getString(6), "is-warning")
		return '' if is_xhr else redirect(url_for("queue"))
	elif action == "move":
		try:
			id_from = values['from']
			id_to = values['to']
			id_size = values['size']
		except KeyError:
			return 'Move requires from, to and size', 400
		expected_order = None
		if 'order' in values:
			try:
				expected_order = json.loads(values['order'])
			except (ValueError, TypeError):
				return 'Invalid queue order', 400
			if not isinstance(expected_order, list) or not all(isinstance(path, str) for path in expected_order):
				return 'Invalid queue order', 400
		result = K.queue_edit(None, "move", src=id_from, tgt=id_to, size=id_size, expected_order=expected_order)
		if result:
			flash(f"{getString(8)} {id_from}->{id_to}/{id_size}")
		else:
			return jsonify(success=False, error='The queue changed while you were dragging. The list will refresh; please try again.'), 409
	else:
		song = values["song"]
		song_title = filename_from_path(song)
		# Flask has already decoded the query once; a second unquote corrupts
		# real filenames containing strings such as "%20".
		if action == "down":
			result = K.queue_edit(song, "down")
			if result:
				flash(getString(10) + song_title, "is-success")
			else:
				flash(getString(11) + song_title, "is-danger")
		elif action == "up":
			result = K.queue_edit(song, "up")
			if result:
				flash(getString(12) + song_title, "is-success")
			else:
				flash(getString(13) + song_title, "is-danger")
		elif action == "delete":
			result = K.queue_edit(song, "delete")
			if result:
				flash(getString(14) + song_title, "is-success")
			else:
				flash(getString(15) + song_title, "is-danger")
	return '' if is_xhr else redirect(url_for("queue"))


@app.route("/enqueue", methods = ["POST", "GET"])
def enqueue():
	d = request.values.to_dict()
	song = d.get('song', d.get('song-to-add'))
	user = d.get('user', d.get('song-added-by'))
	if not song:
		return 'Song is required', 400
	user = normalize_user_name(user)
	rc = K.enqueue(song, user)
	song_title = filename_from_path(song)
	return json.dumps({"song": song_title, "success": rc})


@app.route("/skip")
def skip():
	if not K.skip():
		return control_unavailable()
	return ''


@app.route("/pause")
def pause():
	if not K.pause():
		return control_unavailable()
	return json.dumps(K.is_paused)


@app.route("/transpose/<semitones>", methods = ["GET"])
def transpose(semitones):
	if not K.play_transposed(semitones):
		return control_unavailable()
	return ''


@app.route("/play_speed/<speed>", methods = ["GET"])
def play_speed(speed):
	if K.play_speed_set(speed) is False:
		return control_unavailable()
	return ''


@app.route("/next_audio_track", methods = ["GET"])
def next_audio_track():
	if not K.next_audio_track():
		return control_unavailable()
	return ''


@app.route("/seek/<goto_sec>", methods = ["GET"])
def seek(goto_sec):
	if not K.seek(goto_sec):
		return control_unavailable()
	return ''


@app.route("/audio_delay/<delay_val>", methods = ["GET"])
def audio_delay(delay_val):
	res = K.set_audio_delay(delay_val)
	if res is False:
		return control_unavailable()
	return json.dumps(res)


@app.route("/subtitle_delay/<delay_val>", methods = ["GET"])
def subtitle_delay(delay_val):
	res = K.set_subtitle_delay(delay_val)
	if res is False:
		return control_unavailable()
	return json.dumps(res)


@app.route("/toggle_subtitle")
def toggle_subtitle():
	if not K.toggle_subtitle():
		return control_unavailable()
	return ''


@app.route("/restart")
def restart():
	if not K.restart():
		return control_unavailable()
	return redirect(url_for("home"))


@app.route("/vol_up")
def vol_up():
	result = K.vol_up()
	return control_unavailable() if result is False else str(result)


@app.route("/vol_down")
def vol_down():
	result = K.vol_down()
	return control_unavailable() if result is False else str(result)


@app.route("/vol/<volume>")
def vol_set(volume):
	result = K.vol_set(volume)
	return control_unavailable() if result is False else str(result)


def control_unavailable():
	return jsonify(success=False, error='The player is busy, no song is playing, or the requested value is unavailable. Please try again.'), 409


def render_browse(template_name, use_sort_cookie=False):
	query = request.args.get('q', '').strip()
	letter = None if query else request.args.get('letter', '').lower()
	if letter != 'numeric' and (not letter or len(letter) != 1 or letter not in 'abcdefghijklmnopqrstuvwxyz'):
		letter = None
	page = max(1, request.args.get(get_page_parameter(), type=int, default=1))
	sort_mode = request.args.get('sort', request.cookies.get('sort', 'alpha') if use_sort_cookie else 'alpha')
	by_date = sort_mode == 'date'
	# One catalog reference gives each request a consistent snapshot, even while
	# a Refresh builds and publishes the next catalog in another thread.
	catalog = K.song_catalog
	songs = catalog.select(query=query, letter=letter, by_date=by_date)
	found = len(songs)
	results_per_page = 200
	page = min(page, max(1, (found + results_per_page - 1) // results_per_page))
	pagination = Pagination(
		css_framework='bulma', page=page, total=found, found=found,
		search=bool(query), search_msg=getString2(103), record_name=getString2(101),
		display_msg=getString2(102), per_page=results_per_page,
	)
	start_index = (page - 1) * results_per_page
	return render_template(
		template_name, getString1=getString2, pagination=pagination,
		query=query, results_per_page=results_per_page,
		sort_order='Date' if by_date else 'Alphabetical',
		sort_order_text=getString2(99 if by_date else 100), letter=letter,
		title=getString2(98), songs=songs[start_index:start_index + results_per_page],
	)


@app.route("/browse", methods=["GET"])
def browse():
	return render_browse('files.html')


@app.route("/f_browse", methods=["GET"])
def f_browse():
	return render_browse('f_browse.html', use_sort_cookie=True)


@app.route("/qrcode")
def qrcode():
	return send_file(K.qr_code_path, mimetype = "image/png")

@app.route("/logo")
def logo():
	return send_file(K.logo_path, mimetype="image/png")

@app.route("/splash")
def splash():
	return render_template(
		"splash.html",
		getString1 = lambda ii: getString1(request.client_lang, ii),
		blank_page=True,
		url=request.url_root
	)

@app.route("/info")
def info():
	url = K.url

	# cpu
	cpu = str(psutil.cpu_percent()) + "%"

	# mem
	memory = psutil.virtual_memory()
	available = round(memory.available / 1024.0 / 1024.0, 1)
	total = round(memory.total / 1024.0 / 1024.0, 1)
	memory = str(available) + "MB free / " + str(total) + "MB total ( " + str(memory.percent) + "% )"

	# disk
	disk = psutil.disk_usage("/")
	# Divide from Bytes -> KB -> MB -> GB
	free = round(disk.free / 1024.0 / 1024.0 / 1024.0, 1)
	total = round(disk.total / 1024.0 / 1024.0 / 1024.0, 1)
	disk = str(free) + "GB free / " + str(total) + "GB total ( " + str(disk.percent) + "% )"

	is_pi = get_platform() == "raspberry_pi"

	return render_template(
		"info.html",
		getString1 = getString2,
		langs = os.langs, lang = os.lang,
		ostype = sys.platform.upper(),
		url = url,
		memory = memory,
		cpu = cpu,
		disk = disk,
		is_pi = is_pi,
		norm_vol = K.normalize_vol,
		pikaraoke_version = VERSION,
		media_paths = K.media_paths,
		num_of_songs = len(K.available_songs),
		platform = K.platform,
		save_delays = bool(K.save_delays)
	)
@app.route("/f_info")
def f_info():
	url = K.url

	# cpu
	cpu = str(psutil.cpu_percent()) + "%"

	# mem
	memory = psutil.virtual_memory()
	available = round(memory.available / 1024.0 / 1024.0, 1)
	total = round(memory.total / 1024.0 / 1024.0, 1)
	memory = str(available) + "MB free / " + str(total) + "MB total ( " + str(memory.percent) + "% )"

	# disk
	disk = psutil.disk_usage("/")
	# Divide from Bytes -> KB -> MB -> GB
	free = round(disk.free / 1024.0 / 1024.0 / 1024.0, 1)
	total = round(disk.total / 1024.0 / 1024.0 / 1024.0, 1)
	disk = str(free) + "GB free / " + str(total) + "GB total ( " + str(disk.percent) + "% )"

	is_pi = get_platform() == "raspberry_pi"

	return render_template(
		"f_info.html",
		getString1 = getString2,
		langs = os.langs, lang = os.lang,
		ostype = sys.platform.upper(),
		url = url,
		memory = memory,
		cpu = cpu,
		disk = disk,
		is_pi = is_pi,
		norm_vol = K.normalize_vol,
		pikaraoke_version = VERSION,
		media_paths = K.media_paths,
		num_of_songs = len(K.available_songs),
		platform = K.platform,
		save_delays = bool(K.save_delays)
	)

# Delay system commands to allow redirect to render first
@app.route("/refresh")
def refresh():
	K.get_available_songs()
	return redirect(url_for("browse"))


def get_default_media_dir():
	return os.path.expanduser("~/openhomekaraoke-media")

def get_default_tmp_dir():
	return '/dev/shm' if os.path.isdir('/dev/shm') else tempfile.gettempdir()

def load_media_paths(base_dir):
	base_dir = os.path.abspath(base_dir)
	for name in ('media_paths.local.conf', 'media_paths.conf'):
		config_path = os.path.join(base_dir, name)
		paths = []
		if not os.path.isfile(config_path):
			continue
		try:
			with open(config_path, 'r', encoding='utf-8') as fp:
				for line in fp:
					raw = line.strip()
					if not raw or raw.startswith('#'):
						continue
					if '=' in raw:
						_, raw = raw.split('=', 1)
					raw = raw.strip()
					if not raw:
						continue
					path = os.path.expanduser(raw)
					if not os.path.isabs(path):
						path = os.path.abspath(os.path.join(base_dir, path))
					if path not in paths:
						paths.append(path)
		except OSError:
			logging.exception('Unable to read media configuration %s', config_path)
			continue
		if paths:
			logging.info('Loaded %d media folders from %s', len(paths), config_path)
			return paths
	return []


if __name__ == "__main__":
	platform = get_platform()
	default_port = 5232
	default_volume = 250
	default_splash_delay = 3
	default_log_level = logging.INFO
	default_lang = get_system_locale_name()

	default_media_dir = get_default_media_dir()
	default_vlc_path = get_default_vlc_path(platform)
	default_vlc_port = 5002

	# parse CLI args
	parser = argparse.ArgumentParser()

	parser.add_argument(
		"-p", "--port", type=int,
		help = f"Desired http port (default: {default_port})",
		default = default_port,
	)
	parser.add_argument(
		"--media-path",
		help = f"Optional single media folder (default: {default_media_dir})",
		default = None,
	)
	parser.add_argument(
		"-sd", "--save-delays",
		help = f"Filename for saving subtitle/audio/etc. delays for each song, can be: 1. auto(default): if <media-path>/.delays exist, then enable; 2. yes: save; 3. no: do not save; 4. <filename>: specific file for storing the delays",
		default = 'auto',
	)
	parser.add_argument(
		"-v", "--volume",
		type=int, choices=range(0, 513), metavar='0..512',
		help = f"Initial player volume (default: {default_volume})",
		default = default_volume,
	)
	parser.add_argument(
		"-nv", "--normalize-vol",
		help = "Enable volume normalization",
		action = 'store_true',
	)
	parser.add_argument(
		"-s", "--splash-delay",
		help = f"Delay during splash screen between songs (in secs). (default: {default_splash_delay} )",
		type = float,
		default = default_splash_delay,
	)
	parser.add_argument(
		"-L", "--lang",
		help = f"Set display language (default: None, set according to the current system locale {default_lang})",
		default = default_lang,
	)
	parser.add_argument(
		"-l", "--log-level",
		help = f"Logging level int value (DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50). (default: {default_log_level} )",
		default = default_log_level,
	)
	parser.add_argument(
		"--hide-ip",
		action = "store_true",
		help = "Hide IP address from the screen.",
	)
	parser.add_argument(
		"--hide-raspiwifi-instructions",
		action = "store_true",
		help = "Hide RaspiWiFi setup instructions from the splash screen.",
	)
	parser.add_argument(
		"--hide-splash-screen",
		action = "store_true",
		help = "Hide splash screen before/between songs.",
	)
	parser.add_argument(
		"--vlc-path",
		help = f"Full path to VLC (Default: {default_vlc_path})",
		default = default_vlc_path,
	)
	parser.add_argument(
		"--vlc-port",
		type=int,
		help = f"HTTP port for VLC remote control api (Default: {default_vlc_port})",
		default = default_vlc_port,
	)
	parser.add_argument(
		"--logo-path",
		help = "Path to a custom logo image file for the splash screen. Recommended dimensions ~ 500x500px",
		default = None,
	)
	parser.add_argument(
		"--show-overlay",
		action = "store_true",
		help = "Show overlay on top of video with pikaraoke QR code and IP",
	)
	parser.add_argument(
		'-w', "--windowed",
		action = "store_true",
		help = "Start PiKaraoke in windowed mode",
	)
	parser.add_argument(
		"--temp", "-tp",
		default = None,
		help = "Temporary folder location",
	)
	args = parser.parse_args()
	args.ssl = False
	if not math.isfinite(args.splash_delay) or args.splash_delay < 0:
		parser.error('--splash-delay must be a finite nonnegative number')
	if not 1 <= args.port <= 65535 or not 1 <= args.vlc_port <= 65535:
		parser.error('--port and --vlc-port must be between 1 and 65535')

	set_language(args.lang)

	args.tmp_dir = os.path.expanduser(args.temp or get_default_tmp_dir())
	args.media_paths = load_media_paths(os.path.dirname(__file__))
	if args.media_path:
		media_path = os.path.expanduser(args.media_path.strip())
		if not os.path.isabs(media_path):
			media_path = os.path.abspath(os.path.join(os.path.dirname(__file__), media_path))
		args.media_paths = [media_path]

	args.vlc_path = shutil.which(args.vlc_path) or os.path.abspath(os.path.expanduser(args.vlc_path))
	if not os.path.isfile(args.vlc_path):
		print(getString(45) + args.vlc_path)
		sys.exit(1)

	# setup/create default media directory if necessary
	use_default_media_dir = False
	if not args.media_paths:
		args.media_paths = [default_media_dir]
		use_default_media_dir = True
	primary_media = os.path.abspath(os.path.expanduser(args.media_paths[0]))
	if not os.path.exists(primary_media):
		if use_default_media_dir:
			print(getString(47) + primary_media)
			os.makedirs(primary_media)
		else:
			print("Media path not found: " + primary_media)
	args.library_root = primary_media

	# determine whether to save/load delays
	args.dft_delays_file = os.path.join(args.library_root, '.delays')
	if args.save_delays == 'auto':
		args.save_delays = args.dft_delays_file if os.path.exists(args.dft_delays_file) else None
	elif args.save_delays == 'yes':
		args.save_delays = args.dft_delays_file
	elif args.save_delays == 'no':
		args.save_delays = None

	# Configure karaoke process
	os.K = K = Karaoke(args)
	signal.signal(signal.SIGTERM, lambda signum, frame: K.stop())

	threading.Thread(target=lambda:app.run(host='0.0.0.0', port=args.port, threaded = True)).start()

	threading.Thread(target=status_thread, daemon=True).start()

	K.run()
	os._exit(0)	# force-stop all flask threads and exit
