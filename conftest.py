"""Repo-root pytest hooks shared by every test tree (tests/, tools/workflow/tests, skills/)."""


def pytest_configure(config):
    # tests/ is on the pytest pythonpath (pyproject.toml). Binding the config lets the frozen
    # v2.7 resolver extract its committed fixture under this session's basetemp.
    import frozen_v27

    frozen_v27.bind_pytest_config(config)
