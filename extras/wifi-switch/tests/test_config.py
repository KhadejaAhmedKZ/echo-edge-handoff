import json
from pathlib import Path

import config


def test_save_and_load(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    monkeypatch.setattr(config, "config_path", lambda: target)

    config.save_config({"network1": "Home", "network2": "Office"})
    assert config.load_config() == {"network1": "Home", "network2": "Office"}


def test_missing_config(tmp_path, monkeypatch):
    target = tmp_path / "missing.json"
    monkeypatch.setattr(config, "config_path", lambda: target)
    assert config.load_config() == {}


def test_clear_config(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    monkeypatch.setattr(config, "config_path", lambda: target)

    config.save_config({"network1": "A", "network2": "B"})
    assert target.exists()

    config.clear_config()
    assert not target.exists()
