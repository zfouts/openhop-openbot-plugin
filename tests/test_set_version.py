import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from set_version import VERSION_RE, set_version  # noqa: E402


def _tree(tmp_path):
    (tmp_path / "openhop_openbot").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    (tmp_path / "openhop-plugin.json").write_text(
        '{\n  "id": "x",\n  "version": "1.2.3",\n  "a": 1\n}\n')
    (tmp_path / "openhop_openbot/__init__.py").write_text('"""d"""\n\n__version__ = "1.2.3"\n')
    return tmp_path


def test_rewrites_all_three(tmp_path):
    root = _tree(tmp_path)
    set_version("1.2.3+edge.7", root)
    assert 'version = "1.2.3+edge.7"' in (root / "pyproject.toml").read_text()
    assert json.loads((root / "openhop-plugin.json").read_text())["version"] == "1.2.3+edge.7"
    assert '__version__ = "1.2.3+edge.7"' in (root / "openhop_openbot/__init__.py").read_text()


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3", "01.2.3", "1.2.3+", "1.2.3 edge", "1.2.3+edge_1"])
def test_rejects_bad_versions(tmp_path, bad):
    root = _tree(tmp_path)
    with pytest.raises(SystemExit):
        set_version(bad, root)
    assert 'version = "1.2.3"' in (root / "pyproject.toml").read_text()


def test_missing_location_fails_and_writes_nothing(tmp_path):
    root = _tree(tmp_path)
    (root / "openhop_openbot/__init__.py").write_text('"""d"""\n')
    with pytest.raises(SystemExit):
        set_version("1.2.4", root)
    assert 'version = "1.2.3"' in (root / "pyproject.toml").read_text()


def test_regex_accepts_edge():
    assert VERSION_RE.match("2.3.0+edge.12")
