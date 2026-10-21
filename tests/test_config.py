import json
import socket

import pytest

from lanshare.config import ConfigError, clean_display_name, load_settings


def test_peer_id_is_generated_once_and_persisted(tmp_path):
    cfg = tmp_path / "config.json"
    first = load_settings(cfg, share=tmp_path / "s", downloads=tmp_path / "d")
    assert len(first.peer_id) == 32
    second = load_settings(cfg)
    assert second.peer_id == first.peer_id
    assert json.loads(cfg.read_text())["peer_id"] == first.peer_id


def test_cli_values_are_remembered(tmp_path):
    cfg = tmp_path / "config.json"
    load_settings(cfg, share=tmp_path / "s", downloads=tmp_path / "d", port=9100, name="Den PC")
    again = load_settings(cfg)
    assert again.share_dir == (tmp_path / "s").absolute()
    assert again.downloads_dir == (tmp_path / "d").absolute()
    assert again.port == 9100
    assert again.name == "Den PC"


def test_folders_are_created(tmp_path):
    s = load_settings(tmp_path / "c.json", share=tmp_path / "new" / "share", downloads=tmp_path / "new" / "dl")
    assert s.share_dir.is_dir() and s.downloads_dir.is_dir()


def test_cli_passphrase_is_not_written_to_disk(tmp_path):
    cfg = tmp_path / "config.json"
    s = load_settings(cfg, share=tmp_path / "s", downloads=tmp_path / "d", passphrase="hunter2")
    assert s.passphrase == "hunter2"
    assert "hunter2" not in cfg.read_text()
    assert load_settings(cfg).passphrase is None


def test_passphrase_from_config_file(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"passphrase": "from-file", "share_dir": str(tmp_path / "s"),
                               "downloads_dir": str(tmp_path / "d")}))
    assert load_settings(cfg).passphrase == "from-file"
    assert load_settings(cfg, passphrase="override").passphrase == "override"


def test_default_name_is_hostname(tmp_path):
    s = load_settings(tmp_path / "c.json", share=tmp_path / "s", downloads=tmp_path / "d")
    assert s.name == socket.gethostname()


def test_clean_display_name():
    assert clean_display_name("  Zach's PC\x00\n ") == "Zach's PC"
    assert len(clean_display_name("x" * 500)) == 64
    assert clean_display_name("\x07") == socket.gethostname()


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_invalid_port(tmp_path, port):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "c.json", share=tmp_path / "s", downloads=tmp_path / "d", port=port)


def test_corrupt_config_is_an_error_not_a_silent_reset(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{oops")
    with pytest.raises(ConfigError):
        load_settings(cfg)


def test_share_path_that_is_a_file(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "c.json", share=f, downloads=tmp_path / "d")
