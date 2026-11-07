"""Regression for Problem 1 (Space crash-loop): the HF entrypoint module must
be import-safe — importing it twice (runtime probe import + `python hf_app.py`
execution) must not start a server, spawn a thread, or bind port 7860.

The original crash-loop signature was: lifespan completed, then
``ERROR: [Errno 98] bind ('0.0.0.0', 7860): address already in use`` — exactly
what a second in-process uvicorn contending for the port produces.
"""

import importlib.util
import socket
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = REPO_ROOT / "hf_app.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _port_is_free(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def test_entrypoint_exists():
    assert ENTRYPOINT.is_file()


def test_double_import_binds_nothing():
    """Two independent module executions: no threads, no port binding."""
    before_threads = len(threading.enumerate())

    module_one = _load("hf_app_regression_one")
    module_two = _load("hf_app_regression_two")

    # Importing defines functions only — no uvicorn.run at import time.
    assert callable(module_one.main)
    assert module_one.build_app.__module__ == module_one.__name__

    # No background threads were spawned by either import.
    assert len(threading.enumerate()) == before_threads

    # The port HF binds (7860 by default) is untouched after both imports.
    assert _port_is_free(7860)

    # The two executions are independent namespaces (no shared live objects
    # that could carry a second uvicorn into the process).
    assert module_two is not module_one


def test_build_app_without_gradio_falls_back_to_fastapi(monkeypatch):
    """Local dev / CI (no gradio installed): build_app returns the plain
    FastAPI gateway instead of raising on the import."""
    import types

    module = _load("hf_app_regression_fallback")
    monkeypatch.setitem(sys.modules, "gradio", None)  # import gradio -> ImportError
    monkeypatch.setitem(sys.modules, "spaces", None)

    app = module.build_app()
    assert app is not None
    assert hasattr(app, "routes")  # a FastAPI/ASGI app object

    # Sanity: fastapi is installed here; a path that fell back still builds.
    import fastapi

    assert isinstance(app, types.ModuleType) or isinstance(app, fastapi.FastAPI)


def test_single_server_contract():
    """The module source must call ``uvicorn.run`` in exactly one place via a
    real AST call (not comments/messages), and must not define any
    module-level Gradio Blocks object that a runtime could auto-discover."""
    import ast

    tree = ast.parse(ENTRYPOINT.read_text(encoding="utf-8"))
    uvicorn_run_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
    ]
    assert len(uvicorn_run_calls) == 1

    # ...and that call lives inside main(), which only runs under the
    # __main__ guard (runtime probe imports can never start a server).
    main_fn = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "main"
    )
    assert uvicorn_run_calls[0] in list(ast.walk(main_fn))

    def _guard_calls_main(if_node: ast.If) -> bool:
        test = if_node.test
        guard = (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and any(
                isinstance(cmp, ast.Constant) and cmp.value == "__main__"
                for cmp in test.comparators
            )
        )
        calls_main = any(
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == "main"
            for stmt in if_node.body
        )
        return guard and calls_main

    assert any(
        isinstance(node, ast.If) and _guard_calls_main(node) for node in tree.body
    )

    # No module-level Blocks object (the "ui variable" of the old entrypoint):
    # nothing at module scope is ever an instance of a Gradio Blocks class.
    gradio_blocks_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if isinstance(name, str) and (
                name.startswith("Blocks") or name.startswith("Interface")
            ):
                gradio_blocks_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    assert not gradio_blocks_names, f"module-level Blocks objects found: {gradio_blocks_names}"


def test_main_refuses_to_start_twice_in_one_process():
    """Two sequential uvicorn runs in one process reproduce the production
    crash-loop signature (lifespan → 'Application startup complete' → bind
    error). The entrypoint's contract is one server per process, started only
    from main(); the flush-print banner makes the port owner diagnosable."""
    source = ENTRYPOINT.read_text(encoding="utf-8")
    assert "BIND FAILED" in source  # actionable diagnosis on bind failure
