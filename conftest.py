"""Repo-root pytest hooks shared by every test tree (tests/, tools/workflow/tests, skills/)."""

import runpy
from pathlib import Path

# Focused skill runs do not collect tests/conftest.py. Expose the same helpers
# here so their longstanding `from conftest import ...` imports work regardless
# of collection order, while retaining the controller's frozen-fixture hook.
_HELPER_NAMES = (
    "REPO_ROOT",
    "SKILLS_ROOT",
    "TINY_PNG",
    "load_skill",
    "run_skill",
    "make_gridded",
    "make_forecast",
    "make_point_obs",
    "write_zarr",
)


def __getattr__(name):
    # Slim container-harness CI intentionally does not install the scientific
    # stack. Import it only when a skill test requests one of these helpers.
    if name not in _HELPER_NAMES:
        raise AttributeError(name)
    helpers = runpy.run_path(str(Path(__file__).parent / "tests" / "conftest.py"))
    globals().update({key: helpers[key] for key in _HELPER_NAMES})
    return globals()[name]


def pytest_configure(config):
    # tests/ is on the pytest pythonpath (pyproject.toml). Binding the config lets the frozen
    # v2.7 resolver extract its committed fixture under this session's basetemp.
    import frozen_v27

    frozen_v27.bind_pytest_config(config)
