import os
import sys
from collections import *


def is_raspberry_pi():
	try:
		return os.uname()[4][:3] == "arm" and sys.platform != "darwin"
	except AttributeError:
		return False


def get_platform():
	if sys.platform == "darwin":
		return "osx"
	elif is_raspberry_pi():
		return "raspberry_pi"
	elif sys.platform.startswith("linux"):
		return "linux"
	elif sys.platform.startswith("win"):
		return "windows"
	else:
		return "unknown"

punctuation = ',.?!:;，。？！：；'
lang_popularity = defaultdict(lambda: 0, {'English': 1, 'Simplified Chinese': 1})

lang_iso_name = """
English,en
Simplified Chinese,zh_CN
"""


def find_language(lang):
	check_lang = lambda L: L if L in os.langs else None
	lang = lang.replace('-', '_')
	found = check_lang(lang)
	if not found:
		prefix = lang.split("_")[0]
		found = check_lang(prefix)
	if not found:
		for k in sorted(os.langs.keys()):
			if k.startswith(prefix):
				found = check_lang(k)
	if not found:
		found = check_lang('en_US')
	return found


def set_language(lang):
	if not hasattr(os, 'langs'):
		iso2name = {p[1]: p[0] for L in lang_iso_name.strip().splitlines() for p in [L.strip().split(',')]}
		iso2popularity = defaultdict(lambda: 0, {k: lang_popularity[v] for k, v in iso2name.items()})
		loadf = lambda f: defaultdict(lambda: "", {ii: L for ii, L in enumerate([''] + open(f, 'rb').read().decode('utf-8', 'ignore').splitlines())})
		sorted_lang_list = sorted(os.listdir('lang'), key = lambda t: iso2popularity.get(t, iso2popularity[t.split('_')[0]]), reverse=True)
		os.langs = {f: loadf('lang/' + f) for f in sorted_lang_list if os.path.getsize('lang/' + f) and not f.startswith('.')}
	new_lang = find_language(lang)
	if not new_lang:
		raise Exception(f"Language file lang/{lang} not found")
	os.lang = new_lang
	os.texts = os.langs[new_lang]


def escape_win_filename(fn):
	return fn.replace('/', '\\').replace('^', '^^').replace('&', '^&').replace('(', '^(').replace(')', '^)').replace('%', '^%')

def asr_postprocess(txt):
	ret = txt.strip()
	for c in punctuation:
		ret = ret.strip(c)
	return ret
