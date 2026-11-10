import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "catalogue_entry.py"
REV = "a" * 40


def _wheel(tmp_path):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    w = tmp_path / f"openhop_openbot_plugin-{version}-py3-none-any.whl"
    w.write_bytes(b"not really a wheel")
    return w, version


def _entry(*args):
    out = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                         check=True).stdout
    return json.loads(out)


def test_wheel_url_defaults_to_the_version_tag(tmp_path):
    w, version = _wheel(tmp_path)
    e = _entry(str(w), "--revision", REV)
    assert e["wheel_url"].endswith(f"/releases/download/v{version}/{w.name}")
    assert e["source_revision"] == REV and e["version"] == version


def test_wheel_url_follows_an_explicit_release_tag(tmp_path):
    w, _ = _wheel(tmp_path)
    e = _entry(str(w), "--revision", REV, "--tag", "edge")
    assert e["wheel_url"] == ("https://github.com/zfouts/openhop-openbot-plugin"
                              f"/releases/download/edge/{w.name}")
