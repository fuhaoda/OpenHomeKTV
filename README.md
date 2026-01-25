# Open Home KTV Local

Local-file karaoke player with a simple web UI and VLC playback.

## Features
- Browse and queue local media files (no recursion).
- VLC playback.
- Next Audio Track button with current track display (for example `1/3`).

## Requirements
- Python 3
- VLC installed (or pass a custom path via `--vlc-path`)

## Install
```
python3 -m pip install -r requirements.txt
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
- Only the root of each folder is scanned (no recursion).
- Files are sorted by filename within each folder, then appended in the order listed in `media_paths.conf`.
- Supported extensions: `.mp4`, `.mp3`, `.zip`, `.mkv`, `.avi`, `.webm`, `.mov`, `.m4a`.

If `media_paths.local.conf` is missing or empty, the app uses `media_paths.conf`. If both are missing or empty, it falls back to `~/openhomekaraoke-media/`.

## Run
```
python3 app.py
```
To use a different port:
```
python3 app.py --port 5010
```
To quickly point at a single media folder (instead of editing the config files):
```
python3 app.py --media-path /path/to/your/mkv-folder
```

## Audio tracks
The UI shows a "Next Audio Track" button and the current track count (for example `1/2`).
- Clicking cycles through tracks: 1 -> 2 -> 3 -> 1 ...
- If only one track exists, it stays at `1/1` and the button is disabled.
- On song change, it resets to track 1.
