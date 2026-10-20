"""Stable compatibility class for the legacy v3 two-stage joblib artifact."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


class TwoStagePredictionModel:
    """Two-stage LOW/not-LOW then MED/HIGH prediction wrapper.

    The original sealed artifact recorded this class as
    ``__main__.TwoStagePredictionModel``.  Keeping the implementation in an
    importable package lets entry points expose the legacy name without
    re-importing a training script or rewriting the artifact.
    """

    def __init__(
        self,
        s1: Any,
        s2: Any,
        s1_best: int,
        s2_best: int,
        feature_cols: list[str],
    ) -> None:
        self.s1 = s1
        self.s2 = s2
        self.s1_best = s1_best
        self.s2_best = s2_best
        self.feature_cols = list(feature_cols)

    def predict_proba_proxy(self, part: pd.DataFrame) -> np.ndarray:
        """Return joint-probability proxies for ``[LOW, MED, HIGH]``."""
        s1_proba = self.s1.predict_proba(
            part[self.feature_cols], num_iteration=self.s1_best
        )
        p_notlow = s1_proba[:, 1]
        joint_probs = np.zeros((len(part), 3), dtype=float)
        joint_probs[:, 0] = 1.0 - p_notlow
        notlow_idx = s1_proba.argmax(axis=1) == 1
        if notlow_idx.any():
            s2_proba = self.s2.predict_proba(
                part.loc[notlow_idx, self.feature_cols],
                num_iteration=self.s2_best,
            )
            joint_probs[notlow_idx, 1] = p_notlow[notlow_idx] * s2_proba[:, 0]
            joint_probs[notlow_idx, 2] = p_notlow[notlow_idx] * s2_proba[:, 1]
        return joint_probs

    def predict(self, part: pd.DataFrame) -> np.ndarray:
        """Return the most likely class index for each row."""
        return self.predict_proba_proxy(part).argmax(axis=1).astype(int)
