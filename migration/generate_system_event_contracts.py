"""Export the pinned Python event contracts and hashes for the native registry."""

import json
import sys

from garmin_ai.definitions import _system_contract, _system_payload_models, contract_hash


def export(output):
    models = _system_payload_models()
    records = []
    for kind, model in sorted(models.items()):
        contract = _system_contract(kind, model)
        records.append({"kind": kind, "hash": contract_hash(contract), "contract": contract})
    json.dump(records, output, indent=2, ensure_ascii=True)
    output.write("\n")


if __name__ == "__main__":
    export(sys.stdout)
