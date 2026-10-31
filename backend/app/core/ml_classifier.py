"""ML-assisted router classifier (Part B of the ML routing phase).

Wraps a scikit-learn classifier trained on prompt embeddings
(sentence-transformers "all-MiniLM-L6-v2") that predicts a probability
distribution over providers — a LEARNED replacement/blend for the
hand-tuned capability_match table.

Hard ML deps (sentence-transformers, scikit-learn, joblib) are imported
LAZILY inside methods: the app must start cleanly in environments where
they are not installed (CI, minimal deploys), in which case the
classifier simply never reaches is_loaded=True and the scorer keeps
using the rule-based CAPABILITY_MATCH table.

The pickle artifact (backend/app/ml_models/router_classifier.pkl) is
produced by scripts/train_router_model.py. Its absence is NOT an error —
ENABLE_ML_ROUTING with no model file behaves exactly like rule-based
routing.
"""

from pathlib import Path
from typing import Optional

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent / "ml_models"
MODEL_FILENAME = "router_classifier.pkl"

# Below this max-predicted-probability the model is "not confident" and
# predict_best_provider returns None, leaving the scorer with the pure
# rule-based capability table for that request.
CONFIDENCE_FLOOR = 0.45


class MLRouterClassifier:
    """Loadable wrapper around {classifier, embedder} for serving.

    Usage:
        classifier = MLRouterClassifier()
        classifier.load()          # never raises
        if classifier.is_loaded:
            probs = classifier.predict_best_provider(prompt)
    """

    def __init__(self, model_path: Optional[str] = None):
        self.model_path: Optional[str] = model_path
        self._model = None            # sklearn classifier (predict_proba)
        self._embedder = None         # sentence-transformers encoder
        self._embedder_source: Optional[str] = None  # pickled obj or name
        self._classes: Optional[list[str]] = None
        self.is_loaded = False

    def load(
        self,
        model_path: Optional[str] = None,
        *,
        _import_allowed: bool = True,
    ) -> bool:
        """Load the trained artifact. Always safe to call: any failure
        (missing file, missing deps, corrupt pickle, version drift) is
        swallowed and logged — the system falls back to rule-based.
        """
        path = Path(model_path or self.model_path or (MODEL_DIR / MODEL_FILENAME))
        self.model_path = str(path)

        if not path.exists():
            logger.info(
                "ml_model_not_found_rule_based_only",
                model_path=str(path),
            )
            return False

        try:
            import joblib  # lazy: heavy ML deps are optional
        except ImportError as e:
            logger.warning(
                "ml_deps_missing_rule_based_only",
                error=str(e),
                hint="install backend/requirements-ml.txt to enable ML routing",
            )
            return False

        try:
            artifact = joblib.load(path)
            if not isinstance(artifact, dict) or "classifier" not in artifact:
                raise ValueError(
                    f"{path} is not a router classifier artifact "
                    "(expected dict with 'classifier' key)"
                )
            self._model = artifact["classifier"]
            self._embedder = artifact.get("embedder")
            self._embedder_source = artifact.get("embedder_source")
            self._classes = [str(c) for c in artifact.get("classes", [])]
            if self._model is None or not self._classes:
                raise ValueError("artifact missing classifier or class list")
            # Probing predict_proba forces full deserialization now instead
            # of failing on the first live request.
            if not hasattr(self._model, "predict_proba"):
                raise ValueError("model does not support predict_proba")
        except Exception as e:
            logger.warning(
                "ml_model_load_failed_rule_based_only",
                model_path=str(path),
                error=str(e),
            )
            self._model = None
            self._classes = None
            return False

        # The embedder round-trip is where SentenceTransformer pickles can
        # drift (torch version upgrades). If unpickling is impossible we
        # could still load by name — handled by get_embedder(); loading the
        # classifier itself succeeded, but we only claim is_loaded when
        # BOTH parts are resolvable, verified lazily below.
        try:
            self._resolve_embedder()
        except Exception as e:
            logger.warning(
                "ml_embedder_unusable_rule_based_only", error=str(e)
            )
            self._model = None
            self._classes = None
            return False

        self.is_loaded = True
        logger.info(
            "ml_model_loaded",
            model_path=str(path),
            classes=self._classes,
        )
        return True

    def _resolve_embedder(self):
        """Return a usable embed() callable, importing lazily."""
        if self._embedder is not None:
            return self._embedder
        if self._embedder_source:
            from sentence_transformers import SentenceTransformer  # lazy

            self._embedder = SentenceTransformer(self._embedder_source)
            return self._embedder
        # A train script from the future may store only the classifier with
        # an external embedding pipeline; unknown → unusable.
        raise ValueError("artifact has no usable embedder")

    def embed(self, prompt: str):
        import numpy  # heavy dep, lazy

        embedder = self._resolve_embedder()
        vector = embedder.encode([prompt or ""], show_progress_bar=False)
        return numpy.asarray(vector)

    def predict_best_provider(self, prompt: str) -> Optional[dict[str, float]]:
        """Probability distribution across providers for this prompt.

        Returns None when the model is not loaded, not confident, or the
        prediction fails — callers must treat None as "keep the
        rule-based capability table".
        """
        if not self.is_loaded or not settings.ENABLE_ML_ROUTING:
            return None
        try:
            probs = self._model.predict_proba(self.embed(prompt))[0]
            pairs = sorted(
                zip(self._classes, (float(p) for p in probs)),
                key=lambda pair: pair[1],
                reverse=True,
            )
            top_prob = pairs[0][1]
            if top_prob < CONFIDENCE_FLOOR:
                logger.info(
                    "ml_prediction_low_confidence_ignored",
                    top_provider=pairs[0][0],
                    top_prob=round(top_prob, 3),
                )
                return None
            return {name: round(prob, 4) for name, prob in pairs}
        except Exception as e:
            logger.warning(
                "ml_prediction_failed_falling_back", error=str(e)
            )
            return None


# --------------------------------------------------------------------------
# Process-wide singleton (same pattern as the shared circuit breaker).
# --------------------------------------------------------------------------

_router_classifier: Optional[MLRouterClassifier] = None


def get_router_classifier() -> MLRouterClassifier:
    """Shared instance, loaded lazily on first use (and once at app
    startup). Safe to call from tests — an absent model simply yields a
    classifier with is_loaded=False.
    """
    global _router_classifier
    if _router_classifier is None:
        _router_classifier = MLRouterClassifier()
        # load() never raises; importing app must not depend on ML being
        # installed or trained.
        _router_classifier.load()
    return _router_classifier


def reset_router_classifier() -> None:
    """Test hook: drop the singleton so the next get() reloads from disk."""
    global _router_classifier
    _router_classifier = None
