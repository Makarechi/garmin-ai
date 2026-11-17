"""Export the compatibility profile from the pinned FIT reference package."""

import json
import sys
from importlib.metadata import version

from fitdecode import profile


def component(item):
    return {
        "number": item.def_num,
        "scale": item.scale,
        "offset": item.offset,
        "accumulate": bool(item.accumulate),
        "bits": item.bits,
        "bit_offset": item.bit_offset,
    }


def field(item):
    result = {"number": item.def_num, "name": item.name, "type": item.type.name}
    for name in ("scale", "offset", "units"):
        value = getattr(item, name)
        if value is not None:
            result[name] = value
    if item.components:
        result["components"] = [component(part) for part in item.components]
    refs = getattr(item, "ref_fields", None)
    if refs:
        result["references"] = [[ref.def_num, ref.raw_value] for ref in refs]
    subfields = getattr(item, "subfields", None)
    if subfields:
        result["subfields"] = [field(subfield) for subfield in subfields]
    return result


def export(output):
    if version("fitdecode") != "0.11.0":
        raise RuntimeError("FIT profile export requires fitdecode 0.11.0")

    def emit(key, rows):
        print(f'  "{key}": [', file=output)
        print(",\n".join(f"    {json.dumps(row, ensure_ascii=False)}" for row in rows), file=output)
        print("  ]", end="", file=output)

    print("{", file=output)
    print('  "version": "21.171",', file=output)
    emit(
        "types",
        (
            [item.name, item.base_type.identifier, item.enum or {}]
            for item in profile.FIELD_TYPES.values()
        ),
    )
    print(",", file=output)
    emit(
        "messages",
        (
            [number, msg.name, [field(item) for item in msg.fields.values()]]
            for number, msg in profile.MESSAGE_TYPES.items()
        ),
    )
    print("\n}", file=output)


if __name__ == "__main__":
    export(sys.stdout)
