"""Shared singer-name rules for HTTP, WebSocket and queue operations."""

import unicodedata


MAX_USER_NAME_LENGTH = 10


class UserNameError(ValueError):
    pass


def normalize_user_name(value):
    if not isinstance(value, str):
        raise UserNameError('Please enter a name with 1–10 characters.')
    name = unicodedata.normalize('NFC', value.strip())
    if not 1 <= len(name) <= MAX_USER_NAME_LENGTH:
        raise UserNameError('Please enter a name with 1–10 characters.')
    if any(unicodedata.category(character) == 'Cc' for character in name):
        raise UserNameError('The name must fit on one line without control characters.')
    return name
