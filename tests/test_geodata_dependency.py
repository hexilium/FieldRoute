import builtins
from pathlib import Path

import pytest

from app.geodata.build import _import_osm


@pytest.mark.parametrize("cause", [
    ModuleNotFoundError("No module named 'osmium'"),
    ImportError("libexpat.so.1: cannot open shared object file"),
])
def test_osmium_import_failure_preserves_original_cause(monkeypatch, cause):
    original = builtins.__import__
    def fail_osmium(name, *args, **kwargs):
        if name == "osmium":
            raise cause
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", fail_osmium)
    with pytest.raises(RuntimeError) as error:
        _import_osm(Path("unused.osm"), None, "flex_mem")
    assert str(cause) in str(error.value)
    assert "geodata-prepare" in str(error.value)
    assert error.value.__cause__ is cause
