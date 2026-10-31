"""Tests for the ML RouterClassifier wrapper.

CI has NO trained model and NO sentence-transformers/sklearn installed —
every test here must pass in that environment. All load()/predict() paths
are exercised via mocks, temp dirs, and missing files.
"""

from pathlib import Path

import pytest

from app.core.ml_classifier import (
    MLRouterClassifier,
    get_router_classifier,
    reset_router_classifier,
)


@pytest.fixture(autouse=True)
def _fresh_singleton(monkeypatch, tmp_path):
    """Point the singleton's model dir at a temp path per test."""
    reset_router_classifier()
    yield
    reset_router_classifier()


@pytest.fixture
def empty_model_dir(monkeypatch, tmp_path) -> Path:
    """Redirect the artifact directory to a guaranteed-empty temp dir."""
    fake_dir = tmp_path / "ml_models"
    fake_dir.mkdir()
    monkeypatch.setattr(
        "app.core.ml_classifier.MODEL_DIR", fake_dir
    )
    monkeypatch.setattr(
        "app.core.ml_classifier.MODEL_FILENAME", "router_classifier.pkl"
    )
    return fake_dir


async def test_missing_model_file_gracefully_not_loaded(empty_model_dir):
    """No artifact on disk → is_loaded=False, never raises."""
    classifier = MLRouterClassifier(model_path=str(
        empty_model_dir / "router_classifier.pkl"
    ))
    assert classifier.load() is False
    assert classifier.is_loaded is False
    assert classifier.predict_best_provider("Write a python function") is None


async def test_singleton_with_no_model_behaves_rule_based(empty_model_dir):
    """get_router_classifier() with no model file: safe, unloaded, None preds."""
    classifier = get_router_classifier()
    assert isinstance(classifier, MLRouterClassifier)
    assert classifier.is_loaded is False
    assert (
        classifier.predict_best_provider("summarize this long text") is None
    )


async def test_load_missing_ml_deps_is_graceful(monkeypatch, tmp_path):
    """Artifact exists but joblib/sentence-transformers aren't installed →
    is_loaded stays False (path triggers ImportError branch)."""
    fake_artifact = tmp_path / "router_classifier.pkl"
    fake_artifact.write_bytes(b"not-a-real-pickle-but-joblib-is-mocked")

    # Simulate missing deps: importing joblib raises ImportError.
    import builtins

    real_import = builtins.__import__

    def _no_joblib(name, *args, **kwargs):
        if name == "joblib":
            raise ImportError("No module named 'joblib'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_joblib)

    classifier = MLRouterClassifier(model_path=str(fake_artifact))
    assert classifier.load() is False
    assert classifier.is_loaded is False
    assert classifier.predict_best_provider("hi") is None


async def test_corrupt_pickle_is_graceful(tmp_path):
    """A real joblib import + a garbage file → load fails cleanly."""
    joblib = pytest.importorskip("joblib")
    garbage = tmp_path / "bad.pkl"
    garbage.write_bytes(b"\x00\x01\x02 definitively not joblib")

    classifier = MLRouterClassifier(model_path=str(garbage))
    assert classifier.load() is False
    assert classifier.is_loaded is False
    assert classifier.predict_best_provider("hi") is None
