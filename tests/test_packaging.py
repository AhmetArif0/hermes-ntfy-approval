"""Packaging checks: what the catalog installs, what its docs page renders, and what the code may touch."""

from __future__ import annotations

import ast
from pathlib import Path

from conftest import plugin

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "ntfy-approval"
SOURCES = sorted(PLUGIN.glob("*.py"))


def _manifest() -> str:
    return (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path) -> list:
    names = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append(node.module or "")
    return names


def test_plugin_readme_matches_repo_readme():
    """The catalog docs page renders <subdir>/README.md; GitHub renders the root one. Keep them identical."""
    assert (PLUGIN / "README.md").read_bytes() == (ROOT / "README.md").read_bytes()


def test_readme_links_are_absolute():
    """The catalog page renders the README away from the repo, so relative links would 404."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for part in readme.split("](")[1:]:
        assert part.startswith("https://"), part[:60]


def test_manifest_version_is_the_one_the_readme_documents():
    version = next(line.split(":", 1)[1].strip() for line in _manifest().splitlines() if line.startswith("version:"))
    assert f"### {version}" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_manifest_secrets_are_the_env_names_the_code_reads():
    manifest = _manifest()
    assert f"env: {plugin.TOPIC_ENV}" in manifest and f"env: {plugin.TOKEN_ENV}" in manifest
    assert "required:" not in manifest  # a required secret makes Hermes warn on every load
    assert "provides_tools" not in manifest
    assert "provides_hooks:\n  - post_approval_response\n" in manifest  # the one hook register() adds


def test_registers_only_the_transport_the_command_and_one_hook():
    calls = []
    for node in ast.walk(_tree(PLUGIN / "__init__.py")):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr.startswith("register"):
            calls.append(node.func.attr)
    assert sorted(calls) == ["register_approval_transport", "register_cli_command", "register_hook"]


def test_network_code_lives_only_in_client_py():
    network = {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ssl"}
    for path in SOURCES:
        used = {name.split(".")[0] for name in _imports(path)} & network
        if path.name == "client.py":
            assert used == {"urllib"}, used
        elif path.name == "transport.py":
            assert used <= {"urllib", "http"}, used  # exception classes and URL parsing only
        else:
            assert not used, f"{path.name} imports {used}"


def test_no_subprocess_threads_or_file_writes():
    banned_modules = {"subprocess", "threading", "multiprocessing", "shutil", "tempfile", "sqlite3",
                      "pickle", "ctypes", "asyncio"}
    banned_calls = {"open", "exec", "eval", "compile", "__import__", "system", "popen", "remove", "unlink",
                    "rmdir", "rename", "replace", "write_text", "write_bytes", "mkdir", "Thread", "chmod"}
    for path in SOURCES:
        for name in _imports(path):
            assert name.split(".")[0] not in banned_modules, f"{path.name} imports {name}"
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if (path.name == "client.py" and isinstance(func, ast.Attribute) and called == "open"
                        and isinstance(func.value, ast.Name) and func.value.id == "_OPENER"):
                    continue  # the urllib opener: a network request, not a file
                assert called not in banned_calls, f"{path.name}:{node.lineno} calls {called}()"


def test_only_documented_hermes_imports():
    allowed = {"hermes_constants", "agent.secret_scope"}  # get_hermes_home; the documented secret path
    for path in SOURCES:
        for name in _imports(path):
            top = name.split(".")[0]
            if top in {"hermes_cli", "gateway", "tools", "run_agent", "hermes_state", "plugins"}:
                raise AssertionError(f"{path.name} imports {name}")
            if top.startswith("hermes") or top == "agent":
                assert name in allowed, f"{path.name} imports {name}"
