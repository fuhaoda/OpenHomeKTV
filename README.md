# Open Home KTV Local

Local-file karaoke player with a simple web UI and VLC playback.

## Features
- Browse and queue local video files from folders and all their subfolders.
- VLC playback.
- Next Audio Track button with current track display (for example `1/3`).

## Requirements
- Python 3
- VLC installed (or pass a custom path via `--vlc-path`)

## Install
```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Configure media folders
Use `media_paths.conf` as the template. For your local paths, create `media_paths.local.conf` in the repo root (it is ignored by git). One path per line. Lines starting with `#` are ignored.

```
# key=value format is allowed
mv_path_0=/path/to/your/mkv-folder
mv_path_1=/path/to/another-folder
# or plain paths
/path/to/extra-folder
```

Notes:
- Each configured folder is scanned recursively at startup and when the existing Refresh action is used. You only need to list the parent folder: `/KTV/Artist/Album/song.mkv` is found when `/KTV` is configured.
- Only video extensions are included (case-insensitive): `.mkv`, `.mpg`, `.mpeg`, `.mp4`, `.avi`, `.mov`, `.webm`, `.m4v`, `.wmv`, `.flv`, `.vob`, `.ts`, `.m2ts`. Audio files, ZIP archives, text, HTML, subtitles, and unknown extensions are excluded.
- Scanning uses filenames and filesystem metadata without opening media files or probing their contents.
- Hidden files and hidden subfolders are skipped. Directory symlinks are not followed. Unreadable folders are logged and skipped so scanning can continue.
- Videos are sorted by song title across each configured folder and its subfolders, with the full path breaking ties. The configured folder order is preserved.
- Overlapping configured folders list each file path once. Separate files with the same filename remain separate songs. The phone displays song titles from filenames, without directory names.
- Date sorting reads filesystem timestamps only when needed and caches them until Refresh. Searching first narrows the files whose timestamps are needed. Refresh updates the library after adding, removing, or moving files.

If `media_paths.local.conf` is missing or empty, the app uses `media_paths.conf`. If both are missing or empty, it falls back to `~/openhomekaraoke-media/`.

## Run
From the repository folder, start with one command:
```
./start.sh
```
The script activates `.venv`, changes to the repository directory, and passes arguments to the app. It also works from another directory when invoked by its full path. Stop the app with Ctrl+C.

The app starts at volume **250** by default, including when run directly with Python. To use a different port or override the initial volume (VLC volume scale `0..512`):
```
./start.sh --port 5010 --volume 128
```
To quickly point at a single media folder (instead of editing the config files):
```
./start.sh --media-path "/path/to/your/video folder"
```
For a different virtual environment, set `OPENHOMEKTV_VENV=/path/to/venv` before running the script. If the environment is missing, the script prints setup commands and exits. It does not install packages automatically. Direct startup with `.venv/bin/python app.py` also works.

## Audio tracks
The UI shows a "Next Audio Track" button and the current track count (for example `1/2`).
- Clicking cycles through tracks: 1 -> 2 -> 3 -> 1 ...
- If only one track exists, it stays at `1/1` and the button is disabled.
- On a new song, the second track is selected when available; a single-track file uses track 1.
- Switching audio tracks or changing pitch while paused keeps playback paused.

## Playback and phone controls
- A failed or unavailable video is logged and skipped so the remaining queue can continue. Player startup and media probes have timeouts.
- Phone queues fit narrow screens, wrap long song and singer names, and keep the usual browser zoom available.
- Queue rows are compact, with the singer name and song title on the same line and horizontal move controls. Long titles still wrap within the screen.
- Before the first song selection, enter a name of 1–10 characters. Names are trimmed and remembered on that browser; blank or overlong names are rejected. The same rule applies when renaming or adding random songs, and the server checks it too.
- Browser updates use JSON messages with reconnect and polling fallback. Multiple tabs on one phone have independent connections.
- Saved audio/subtitle delays distinguish videos with identical filenames in different folders. Existing filename-based settings remain readable; updates are saved by full path.
- Optional volume normalization needs FFmpeg on `PATH`. It analyzes up to the first 60 seconds of a selected song on demand, with a 45-second timeout, and caches the result. It does not analyze the whole library during scanning.

## Development checks
```
.venv/bin/python -m unittest discover -s tests -v
node tests/test_frontend.js
node tests/test_names.js
bash -n start.sh
```

During forced WebSocket-disconnect testing, the bundled Flask/Werkzeug server can produce an `Invalid frame header` browser-console message. Automatic reconnect and subsequent queue updates have been verified. The upstream project documents [the same close-time behavior](https://github.com/miguelgrinberg/flask-sock/issues/20).
