"""Small stable-key locale catalog for user-facing universal flows."""

import json
from functools import lru_cache
from importlib.resources import files

SUPPORTED_LOCALES = frozenset({"en", "ru"})


def normalized_locale(locale: str) -> str:
    language = locale.split("-", 1)[0]
    return language if language in SUPPORTED_LOCALES else "en"


@lru_cache(maxsize=2)
def catalog(locale: str) -> dict[str, str]:
    language = normalized_locale(locale)
    path = files("garmin_ai").joinpath("locales", language + ".json")
    return json.loads(path.read_text(encoding="utf-8"))


def translate(key: str, locale: str, **values) -> str:
    template = catalog(locale).get(key) or catalog("en").get(key)
    if template is None:
        raise LookupError("Unknown locale resource key")
    return template.format(**values)
