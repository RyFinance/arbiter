# Full pytest regression — 2026-08-11

This note records the earlier failure and its resolution on 2026-08-11.

| Run | Result |
| --- | --- |
| Earlier 1 | Exit 139, deterministic PyTorch segmentation fault during state-dict loading after approximately 16% progress |
| Earlier 2 | Exit 139, same deterministic fault location |
| Earlier 3 | Exit 139, same deterministic fault location |

The fault was reproduced with `PYTHONFAULTHANDLER=1 pytest -q --maxfail=1`; the stack repeatedly entered `torch.nn.modules.module._load_from_state_dict`. This is an environment/runtime failure, not a flaky assertion. Targeted tests after the quick Streamlit/depth test fixes passed: `36 passed` for `tests/test_depth_capture.py` and `tests/test_streamlit_macro_vol.py`.

Root cause: `tests/test_depth_capture_integration.py` starts and then cancels a
paper bot solely to verify isolated logs. That startup also loaded the optional
Kronos PyTorch checkpoint; cancellation could leave native checkpoint loading
active, producing the SIGSEGV. The fixture now supplies the existing MED
fallback because the integration test does not validate Kronos inference.

The persisted `lightgbm_two_stage_v3.pkl` also referenced
`__main__.TwoStagePredictionModel`; a compatibility alias now permits joblib
deserialization in pytest. Two stale paper-model test assumptions were adjusted
to allow the normal 60-row causal feature warm-up and to sample 100 eligible
not-LOW rows from the full eligible frame.

After these repairs, three fresh full-suite runs completed cleanly:

| Final run | Result |
| --- | --- |
| 1 | 450 passed, 6 warnings, 92.03s |
| 2 | 450 passed, 6 warnings, 90.76s |
| 3 | 450 passed, 6 warnings, 89.35s |
