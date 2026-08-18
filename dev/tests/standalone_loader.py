"""Stub-package loader for the standalone (pure Python) test tier.

The component's package root (``custom_components/dynamic_ocpp_evse/__init__.py``)
imports ``homeassistant``, so the pure calculation/engine modules cannot be
imported the normal way on a machine without Home Assistant. This module builds
just enough of the package hierarchy in ``sys.modules`` — stub namespace
packages plus the real modules loaded straight from their files with their
fully-qualified names — for the relative imports inside those modules
(``from .models``, ``from ..const``) to resolve.

Shared by dev/tests/run_tests.py and every standalone-capable unit test file
(the ~70-line copy each of them used to carry). Deliberately NOT named
``test_*`` so pytest never collects it as a test module.

Every load is guarded on ``sys.modules``: under the Docker/CI pytest tier the
real package tree is already imported (dev/tests/conftest.py imports the
component), so the loader is a no-op there — the test files just import the
already-real modules. It also keeps repeat calls from different test files in
one pytest process from re-executing modules and forking class identities.

Paths are derived from ``__file__``, so the loader works from any CWD.

Usage (top of a standalone test file, before the component imports)::

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from standalone_loader import load_pure_modules

    load_pure_modules(engine_modules=("fleet",))
    from custom_components.dynamic_ocpp_evse.engine.fleet import ...
"""

import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "dynamic_ocpp_evse"

PKG_ROOT = "custom_components"
PKG_COMP = f"{PKG_ROOT}.dynamic_ocpp_evse"
PKG_CONST = f"{PKG_COMP}.const"
PKG_CALC = f"{PKG_COMP}.calculations"
PKG_ENGINE = f"{PKG_COMP}.engine"
PKG_CONTROL = f"{PKG_COMP}.control"

# Dependency order — common is the leaf every other const module may import;
# the aggregator __init__ loads last and re-exports every name.
CONST_SUBMODULES = (
    "common",
    "hub",
    "inverter",
    "group",
    "evse",
    "plug",
    "hot_water_tank",
    "power_station",
    "modes",
)

DEFAULT_CALC_MODULES = ("models", "utils", "target_calculator")

# engine/hub_calculation.py is loadable without Home Assistant, but its import
# chain touches these HA-importing siblings and these engine modules, in this
# order. helpers.py / forecast_reader.py import homeassistant at module level,
# which _ensure_ha_stubs() satisfies when HA is not installed.
_HUB_CALC_ENGINE_ORDER = ("auto_detect", "fleet", "forecast_reader", "hub_calculation")


def _ensure_stub_package(name, search_path=None):
    """Register an empty namespace-package stub unless the (real or stub)
    package is already imported. Returns the module now in sys.modules."""
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [] if search_path is None else [str(search_path)]
        pkg.__package__ = name
        pkg.__standalone_stub__ = True
        sys.modules[name] = pkg
    return sys.modules[name]


def _exec_module_as(fqn, path):
    """Load a module file under its fully-qualified name (no sys.modules guard)."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location(fqn, str(path))
    module = importlib.util.module_from_spec(spec)
    if path.name == "__init__.py":
        # Package: __package__ is the package itself; expose __path__ so
        # `from .sibling import ...` inside it resolves normally.
        module.__package__ = fqn
        module.__path__ = [str(path.parent)]
    else:
        # Module: __package__ is the parent package so `from .x`/`from ..x` work.
        module.__package__ = fqn.rsplit(".", 1)[0] if "." in fqn else fqn
    sys.modules[fqn] = module
    spec.loader.exec_module(module)
    # Mirror the real import system: expose the module as an attribute of its
    # parent package (makes `from . import fleet` / `from .. import units` work).
    if "." in fqn:
        parent_name, _, child = fqn.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, child, module)
    return module


def _load_module_once(fqn, path):
    """Load ``path`` as ``fqn`` unless something (the real package under
    pytest, or an earlier test file) already put it in sys.modules."""
    if fqn in sys.modules:
        return sys.modules[fqn]
    return _exec_module_as(fqn, path)


def _load_calc_init():
    """Replace the stub calculations package with the real ``__init__.py``
    (re-exports models/target_calculator/forecast names). No-op when the real
    package is already imported."""
    pkg = sys.modules.get(PKG_CALC)
    if pkg is not None and not getattr(pkg, "__standalone_stub__", False):
        return pkg
    return _exec_module_as(PKG_CALC, COMPONENT_DIR / "calculations" / "__init__.py")


def _ensure_ha_stubs():
    """Provide the few ``homeassistant`` modules the HA-importing siblings of
    hub_calculation.py touch at import time. Real HA wins when installed."""
    try:
        import homeassistant.config_entries  # noqa: F401
        import homeassistant.helpers.entity_registry  # noqa: F401
        import homeassistant.util.dt  # noqa: F401
        return
    except ImportError:
        pass

    ha = _ensure_stub_package("homeassistant")
    helpers = _ensure_stub_package("homeassistant.helpers")
    util = _ensure_stub_package("homeassistant.util")

    if "homeassistant.config_entries" not in sys.modules:
        config_entries = types.ModuleType("homeassistant.config_entries")

        class ConfigEntry:  # placeholder — only referenced in annotations
            pass

        config_entries.ConfigEntry = ConfigEntry
        sys.modules["homeassistant.config_entries"] = config_entries
    if "homeassistant.helpers.entity_registry" not in sys.modules:
        sys.modules["homeassistant.helpers.entity_registry"] = types.ModuleType(
            "homeassistant.helpers.entity_registry"
        )
    if "homeassistant.util.dt" not in sys.modules:
        sys.modules["homeassistant.util.dt"] = types.ModuleType(
            "homeassistant.util.dt"
        )

    ha.config_entries = sys.modules["homeassistant.config_entries"]
    ha.helpers = helpers
    ha.util = util
    helpers.entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
    util.dt = sys.modules["homeassistant.util.dt"]


def load_pure_modules(
    calc_modules=DEFAULT_CALC_MODULES,
    engine_modules=(),
    control_modules=(),
    load_calc_init=False,
):
    """Make the requested component modules importable without Home Assistant.

    Args:
        calc_modules: calculations/ modules to load, in dependency order
            (subset of "models", "utils", "target_calculator" — models first).
        engine_modules: engine/ modules to load (any of "auto_detect", "fleet",
            "forecast_reader", "hub_calculation"). Requesting
            "hub_calculation" pulls in its whole import chain (the other three
            plus helpers.py/units.py and the calc __init__) automatically.
        control_modules: control/ modules to load. Only the ones that import
            nothing but const/helpers/units qualify — which is the actuation
            layer's own rule (see AGENTS.md), so "inverter" loads while the
            ones reaching for HA service helpers do not.
        load_calc_init: also execute calculations/__init__.py so the package
            itself re-exports the public names (needed by callers that do
            ``from ..calculations import X`` beyond PhaseValues).

    The const package (all submodules + __init__) is always loaded — every
    caller needs at least part of it and it is pure and cheap.
    """
    engine_modules = tuple(engine_modules)
    control_modules = tuple(control_modules)
    if engine_modules or control_modules:
        # helpers.py (preloaded below for every engine/control request) imports
        # homeassistant at module level, so the stubs go in first.
        _ensure_ha_stubs()
    if "hub_calculation" in engine_modules:
        load_calc_init = True
        # Full calc tier + the ordered engine chain.
        calc_modules = tuple(
            dict.fromkeys(tuple(calc_modules) + DEFAULT_CALC_MODULES)
        )
        engine_modules = tuple(
            dict.fromkeys(_HUB_CALC_ENGINE_ORDER[:-1] + engine_modules)
        )

    _ensure_stub_package(PKG_ROOT)
    _ensure_stub_package(PKG_COMP)
    _ensure_stub_package(PKG_CALC)
    if engine_modules:
        # The engine stub gets the REAL search path: other test files sharing
        # a pytest process import real engine submodules (e.g.
        # engine.hub_calculation), and an empty stub path would break them.
        _ensure_stub_package(PKG_ENGINE, COMPONENT_DIR / "engine")
    if control_modules:
        _ensure_stub_package(PKG_CONTROL, COMPONENT_DIR / "control")

    const_dir = COMPONENT_DIR / "const"
    for sub in CONST_SUBMODULES:
        _load_module_once(f"{PKG_CONST}.{sub}", const_dir / f"{sub}.py")
    _load_module_once(PKG_CONST, const_dir / "__init__.py")

    calc_dir = COMPONENT_DIR / "calculations"
    for mod in calc_modules:
        _load_module_once(f"{PKG_CALC}.{mod}", calc_dir / f"{mod}.py")

    # engine/fleet.py does `from ..calculations import PhaseValues`; let the
    # stub calculations package satisfy that even without the real __init__.
    calc_pkg = sys.modules[PKG_CALC]
    models = sys.modules.get(f"{PKG_CALC}.models")
    if models is not None and not hasattr(calc_pkg, "PhaseValues"):
        calc_pkg.PhaseValues = models.PhaseValues

    if load_calc_init:
        _load_calc_init()

    if engine_modules or control_modules:
        # Preloaded for ANY engine request, not just hub_calculation: the engine
        # modules reach for `from .. import units` (the unit converters and the
        # availability predicates), and resolving that through the stub parent
        # package would fail. helpers.py rides along for the same reason. The
        # control modules need exactly the same two.
        _load_module_once(f"{PKG_COMP}.helpers", COMPONENT_DIR / "helpers.py")
        _load_module_once(f"{PKG_COMP}.units", COMPONENT_DIR / "units.py")

    engine_dir = COMPONENT_DIR / "engine"
    for mod in engine_modules:
        _load_module_once(f"{PKG_ENGINE}.{mod}", engine_dir / f"{mod}.py")

    control_dir = COMPONENT_DIR / "control"
    for mod in control_modules:
        _load_module_once(f"{PKG_CONTROL}.{mod}", control_dir / f"{mod}.py")
