"""UNI-01 dependency guardrails for the incremental universal-core migration."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "src" / "garmin_ai"

# Transport SDKs are allowed only in adapters and the current composition root.
# UNI-11 moves Telegram lifecycle ownership fully into the adapter boundary.
TRANSPORT_SDK_ALLOWED = {
    "pairing.py": "UNI-11",
    "runtime.py": "UNI-11",
    "telegram.py": "UNI-11",
    "telegram_format.py": "UNI-11",
}

# These exact application/infrastructure modules still read the legacy transport
# row. UNI-10 replaces those reads with neutral inbox/conversation structures.
TELEGRAM_DTO_EXCEPTIONS = {
    "jobs.py": "UNI-10",
    "personal_goals.py": "UNI-10",
    "proactive.py": "UNI-10",
    "retention.py": "UNI-10",
    "runtime.py": "UNI-10",
    "telegram.py": "UNI-10",
}


def imports(path: Path, package=PACKAGE):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    try:
        relative = path.relative_to(package)
    except ValueError:
        relative = None
    package_parts = [package.name, *(relative.parent.parts if relative else ())]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, None
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                keep = len(package_parts) - node.level + 1
                suffix = module.split(".") if module else []
                module = ".".join([*package_parts[:keep], *suffix])
            for alias in node.names:
                yield module, alias.name


def package_relative(path: Path, package=PACKAGE):
    try:
        return path.relative_to(package).as_posix()
    except ValueError:
        return path.name


def transport_sdk_violations(paths, package=PACKAGE):
    violations = []
    for path in paths:
        relative = package_relative(path, package)
        if relative in TRANSPORT_SDK_ALLOWED:
            continue
        for module, symbol in imports(path, package):
            if module == "telegram" or module.startswith("telegram."):
                violations.append(f"{relative}: {module}.{symbol or '*'}")
    return violations


def telegram_dto_importers(paths, package=PACKAGE):
    return {package_relative(path, package) for path in paths if uses_telegram_dto(path, package)}


def uses_telegram_dto(path, package=PACKAGE):
    if any(
        module == "garmin_ai.models" and symbol in {"TelegramUpdate", "*"}
        for module, symbol in imports(path, package)
    ):
        return True
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    model_aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "garmin_ai.models":
                    model_aliases.add(alias.asname or "garmin_ai.models")
        elif isinstance(node, ast.ImportFrom):
            resolved = list(imports_for_node(node, path, package))
            for module, symbol, local in resolved:
                if module == "garmin_ai" and symbol == "models":
                    model_aliases.add(local)

    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = dotted(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return None

    return any(
        isinstance(node, ast.Attribute)
        and node.attr == "TelegramUpdate"
        and dotted(node.value) in model_aliases
        for node in ast.walk(tree)
    )


def imports_for_node(node, path, package=PACKAGE):
    try:
        relative = path.relative_to(package)
    except ValueError:
        relative = None
    package_parts = [package.name, *(relative.parent.parts if relative else ())]
    module = node.module or ""
    if node.level:
        keep = len(package_parts) - node.level + 1
        suffix = module.split(".") if module else []
        module = ".".join([*package_parts[:keep], *suffix])
    for alias in node.names:
        yield module, alias.name, alias.asname or alias.name


def test_non_adapter_modules_do_not_import_telegram_sdk():
    violations = transport_sdk_violations(PACKAGE.rglob("*.py"))
    assert violations == [], "Telegram SDK crossed the adapter boundary: " + ", ".join(violations)


def test_forbidden_transport_import_is_detected(tmp_path):
    module = tmp_path / "core_example.py"
    module.write_text("from telegram import Bot\n", encoding="utf-8")

    assert transport_sdk_violations([module]) == ["core_example.py: telegram.Bot"]


def test_relative_transport_dto_import_is_detected(tmp_path):
    package = tmp_path / "garmin_ai"
    module = package / "domain" / "core_example.py"
    module.parent.mkdir(parents=True)
    module.write_text("from ..models import TelegramUpdate\n", encoding="utf-8")

    assert telegram_dto_importers([module], package) == {"domain/core_example.py"}


@pytest.mark.parametrize(
    "source",
    [
        "import garmin_ai.models as models\nvalue = models.TelegramUpdate\n",
        "from .. import models\nvalue = models.TelegramUpdate\n",
        "import garmin_ai.models\nvalue = garmin_ai.models.TelegramUpdate\n",
    ],
)
def test_module_alias_transport_dto_access_is_detected(tmp_path, source):
    package = tmp_path / "garmin_ai"
    module = package / "domain" / "core_example.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")

    assert telegram_dto_importers([module], package) == {"domain/core_example.py"}


def test_legacy_transport_dto_exceptions_are_exact_and_owned():
    actual = telegram_dto_importers(PACKAGE.rglob("*.py"))

    assert actual == set(TELEGRAM_DTO_EXCEPTIONS), (
        "TelegramUpdate dependencies changed; assign each temporary exception to UNI-10 "
        "or remove it from the allowlist"
    )
    assert set(TELEGRAM_DTO_EXCEPTIONS.values()) == {"UNI-10"}
