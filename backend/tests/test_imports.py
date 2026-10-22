"""Каркас: все пакеты слоёв из дизайна импортируются."""
import importlib

import pytest


@pytest.mark.parametrize("name", ["app", "app.sources", "app.services", "app.jobs", "app.api"])
def test_package_imports(name: str) -> None:
    assert importlib.import_module(name)
