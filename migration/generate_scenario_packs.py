"""Export the trusted scenario pack catalog from the pinned Python worktree."""

import json

from garmin_ai.scenario_packs import PACKS


def main():
    fields = ("definitions", "forms", "rules", "analysis")
    catalog = [
        {
            "key": pack.key,
            "labels": pack.labels,
            **{field: sorted(getattr(pack, field)) for field in fields},
        }
        for pack in PACKS.values()
    ]
    print(json.dumps(catalog, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
