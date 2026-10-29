"""Syntactic BCP 47 language tags shared by settings and model output.

This checks well-formedness, not registration or availability of translations.
"""

import re
from typing import Annotated

from pydantic import AfterValidator, Field

_GRANDFATHERED = {
    tag.lower(): tag
    for tag in (
        "en-GB-oed",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "sgn-BE-FR",
        "sgn-BE-NL",
        "sgn-CH-DE",
        "art-lojban",
        "cel-gaulish",
        "no-bok",
        "no-nyn",
        "zh-guoyu",
        "zh-hakka",
        "zh-min",
        "zh-min-nan",
        "zh-xiang",
    )
}


def normalize_language_tag(value: str) -> str:
    """Validate a well-formed tag and apply conventional subtag casing."""
    if len(value) > 255 or not re.fullmatch(r"[A-Za-z0-9]{1,8}(?:-[A-Za-z0-9]{1,8})*", value):
        raise ValueError("Invalid language tag")
    lowered = value.lower()
    if lowered in _GRANDFATHERED:
        return _GRANDFATHERED[lowered]
    parts = lowered.split("-")
    if parts[0] == "x":
        if len(parts) < 2:
            raise ValueError("Private-use tag requires a subtag")
        return lowered
    language = parts[0]
    if not language.isalpha() or not 2 <= len(language) <= 8:
        raise ValueError("Invalid language subtag")
    result = [language]
    index = 1
    if len(language) <= 3:
        for _ in range(3):
            if index < len(parts) and len(parts[index]) == 3 and parts[index].isalpha():
                result.append(parts[index])
                index += 1
            else:
                break
    if index < len(parts) and len(parts[index]) == 4 and parts[index].isalpha():
        result.append(parts[index].title())
        index += 1
    if index < len(parts) and (
        len(parts[index]) == 2
        and parts[index].isalpha()
        or len(parts[index]) == 3
        and parts[index].isdigit()
    ):
        result.append(parts[index].upper())
        index += 1
    variants = set()
    while index < len(parts) and (
        5 <= len(parts[index]) <= 8 or len(parts[index]) == 4 and parts[index][0].isdigit()
    ):
        if parts[index] in variants:
            raise ValueError("Duplicate language variant")
        variants.add(parts[index])
        result.append(parts[index])
        index += 1
    extensions = set()
    while index < len(parts) and len(parts[index]) == 1 and parts[index] != "x":
        singleton = parts[index]
        if singleton in extensions:
            raise ValueError("Duplicate language extension")
        extensions.add(singleton)
        result.append(singleton)
        index += 1
        start = index
        while index < len(parts) and len(parts[index]) >= 2:
            result.append(parts[index])
            index += 1
        if index == start:
            raise ValueError("Language extension requires a subtag")
    if index < len(parts) and parts[index] == "x":
        if index + 1 == len(parts):
            raise ValueError("Private-use tag requires a subtag")
        result.extend(parts[index:])
        index = len(parts)
    if index != len(parts):
        raise ValueError("Invalid language tag")
    return "-".join(result)


LanguageTag = Annotated[
    str,
    Field(min_length=2, max_length=255, pattern=r"^[A-Za-z0-9]{1,8}(?:-[A-Za-z0-9]{1,8})*$"),
    AfterValidator(normalize_language_tag),
]
