VERSION = "2.5.0"

# Discovery uses extensions only; keep audio files and archives out of the catalog.
media_types = frozenset({
    '.mkv', '.mpg', '.mpeg', '.mp4', '.avi', '.mov', '.webm',
    '.m4v', '.wmv', '.flv', '.vob', '.ts', '.m2ts',
})
