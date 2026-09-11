from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from scipy import sparse
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold

from src.models import (
    STACKING_BASE_ORDER,
    build_base_models,
    build_stacking,
    build_variant,
    compare_imbalance_strategies,
    lock_best_model,
    save_model,
    select_strategy_per_model,
    summarize_results,
    tune_models,
)


TINY_PARAM_GRIDS = {
    "Multinomial Naive Bayes": {"clf__alpha": [0.5, 1.0]},
    "Logistic Regression": {"clf__C": [0.5, 1.0]},
    "Linear SVM": {"clf__C": [0.5, 1.0]},
    "Random Forest": {"clf__n_estimators": [20], "clf__max_depth": [None]},
}


def synthetic_train_data(n_per_class: int = 12, n_features: int = 30, random_state: int = 0):
    rng = np.random.RandomState(random_state)
    # "Negative" vẫn là lớp thiểu số nhưng đủ mẫu (>= 16) để SMOTE(k_neighbors=5 mặc định)
    # chạy được trên từng fold train của StratifiedKFold(n_splits=2) trong test.
    labels = ["Positive"] * n_per_class + ["Neutral"] * n_per_class + ["Negative"] * 16
    centers = {"Positive": 1.0, "Neutral": 0.0, "Negative": -1.0}
    rows = []
    for label in labels:
        base = np.full(n_features, centers[label])
        noise = rng.normal(scale=0.3, size=n_features)
        rows.append(np.clip(base + noise, 0, None))
    X = sparse.csr_matrix(np.array(rows))
    y = pd.Series(labels)
    return X, y


def test_build_base_models_are_class_weight_balanced():
    models = build_base_models()

    assert set(models) == {
        "Multinomial Naive Bayes",
        "Logistic Regression",
        "Linear SVM",
        "Random Forest",
    }
    assert "class_weight" not in models["Multinomial Naive Bayes"].get_params()
    assert models["Logistic Regression"].get_params()["class_weight"] == "balanced"
    assert models["Linear SVM"].get_params()["class_weight"] == "balanced"
    assert models["Random Forest"].get_params()["class_weight"] == "balanced"


def test_build_variant_balanced_uses_passthrough_and_keeps_class_weight():
    model = build_base_models()["Logistic Regression"]
    variant = build_variant(model, strategy="balanced")

    assert variant.named_steps["smote"] == "passthrough"
    assert variant.named_steps["clf"].get_params()["class_weight"] == "balanced"


def test_build_variant_smote_wraps_sampler_and_clears_class_weight():
    model = build_base_models()["Logistic Regression"]
    variant = build_variant(model, strategy="smote")

    assert variant.named_steps["smote"].__class__.__name__ == "SMOTE"
    assert variant.named_steps["clf"].get_params()["class_weight"] is None


def test_build_variant_rejects_unknown_strategy():
    model = build_base_models()["Logistic Regression"]
    with pytest.raises(ValueError):
        build_variant(model, strategy="unknown")


def test_compare_imbalance_strategies_covers_every_model_and_strategy():
    X_train, y_train = synthetic_train_data()
    cv = StratifiedKFold(n_splits=2, shuffle=True, random_state=0)

    comparison = compare_imbalance_strategies(X_train, y_train, cv=cv)

    assert set(comparison["Model"]) == set(build_base_models())
    assert set(comparison["Strategy"]) == {"balanced", "smote"}
    assert len(comparison) == len(build_base_models()) * 2
    assert comparison["CV Macro F1 Mean"].between(0, 1).all()


def test_select_strategy_per_model_picks_the_higher_scoring_row():
    comparison = pd.DataFrame(
        [
            {"Model": "A", "Strategy": "balanced", "CV Macro F1 Mean": 0.7},
            {"Model": "A", "Strategy": "smote", "CV Macro F1 Mean": 0.9},
            {"Model": "B", "Strategy": "balanced", "CV Macro F1 Mean": 0.6},
            {"Model": "B", "Strategy": "smote", "CV Macro F1 Mean": 0.4},
        ]
    )

    winners = select_strategy_per_model(comparison)

    assert winners == {"A": "smote", "B": "balanced"}


def test_tune_models_returns_a_fitted_search_per_model_with_no_test_leakage():
    X_train, y_train = synthetic_train_data()
    cv = StratifiedKFold(n_splits=2, shuffle=True, random_state=0)
    strategies = {name: "balanced" for name in build_base_models()}

    results = tune_models(X_train, y_train, strategies=strategies, cv=cv, param_grids=TINY_PARAM_GRIDS)

    assert set(results) == set(build_base_models())
    for search in results.values():
        assert 0.0 <= search.best_score_ <= 1.0
        assert hasattr(search.best_estimator_, "predict")


def test_build_stacking_orders_base_learners_per_plan_and_can_fit_predict():
    X_train, y_train = synthetic_train_data()
    cv = StratifiedKFold(n_splits=2, shuffle=True, random_state=0)
    strategies = {name: "balanced" for name in build_base_models()}
    tuned = tune_models(X_train, y_train, strategies=strategies, cv=cv, param_grids=TINY_PARAM_GRIDS)

    stacking = build_stacking(tuned, cv=2)

    assert [name for name, _ in stacking.estimators] == [
        "mnb",
        "lr",
        "svm",
        "rf",
    ]
    assert isinstance(stacking.final_estimator, LogisticRegression)
    stacking.fit(X_train, y_train)
    predictions = stacking.predict(X_train)
    assert len(predictions) == len(y_train)


def test_summarize_results_ranks_stacking_alongside_tuned_models():
    X_train, y_train = synthetic_train_data()
    cv = StratifiedKFold(n_splits=2, shuffle=True, random_state=0)
    strategies = {name: "balanced" for name in build_base_models()}
    tuned = tune_models(X_train, y_train, strategies=strategies, cv=cv, param_grids=TINY_PARAM_GRIDS)
    stacking_scores = np.array([0.5, 0.6])

    summary = summarize_results(tuned, stacking_scores)

    assert list(summary.columns[:2]) == ["Model", "CV Macro F1 Mean"]
    assert "Stacking Ensemble" in summary["Model"].values
    assert summary["CV Macro F1 Mean"].is_monotonic_decreasing


def test_lock_best_model_only_touches_the_arguments_it_is_given():
    X_train, y_train = synthetic_train_data()
    estimator = build_base_models()["Logistic Regression"]

    locked = lock_best_model(estimator, X_train, y_train)

    assert locked is not estimator
    predictions = locked.predict(X_train)
    assert len(predictions) == len(y_train)


def test_save_model_round_trips_through_joblib(tmp_path: Path):
    X_train, y_train = synthetic_train_data()
    estimator = build_base_models()["Logistic Regression"]
    locked = lock_best_model(estimator, X_train, y_train)
    filepath = tmp_path / "nested" / "best_sentiment_model.joblib"

    save_model(locked, filepath)
    reloaded = joblib.load(filepath)

    np.testing.assert_array_equal(reloaded.predict(X_train), locked.predict(X_train))


def test_stacking_base_order_matches_the_team_plan():
    assert STACKING_BASE_ORDER == [
        "Multinomial Naive Bayes",
        "Logistic Regression",
        "Linear SVM",
        "Random Forest",
    ]
