"""Group a setup form's fields under headed sections.

A flat list of settings is hard to read, and Home Assistant has no plain
subtitle between fields: the one way to get a heading is a form section. The
pages are still BUILT flat, and stored flat - a page names which fields sit
under which section (``GROUPS``), and this module nests the form on the way
out and flattens what comes back, so no handler and no stored option changes.

Copied by hand between Load Insights and Load Juggler (sections.py in each) -
keep the two the same.
"""
from __future__ import annotations

import functools
from typing import Any

import voluptuous as vol
from homeassistant.data_entry_flow import section

# A group: (section key, the field keys under it, collapsed). The section key is
# the translation key too: step -> sections -> <key> -> name, data, data_description.
Group = tuple[str, tuple[str, ...], bool]


def _by_field(groups) -> dict[str, str]:
    return {key: name for name, keys, _ in groups for key in keys}


class SectionedSchema(vol.Schema):
    """A form schema whose fields sit under sections.

    It also takes the fields FLAT (a caller that never saw the sections, a
    test), nesting them before it validates; the frontend sends them nested.
    """

    def __init__(self, schema, groups, **kwargs):
        super().__init__(schema, **kwargs)
        self.groups = groups

    def __call__(self, data):
        if isinstance(data, dict):
            where = _by_field(self.groups)
            nested: dict[str, Any] = {}
            for key, value in data.items():
                name = where.get(key)
                if name is None:
                    nested[key] = value
                else:
                    nested.setdefault(name, {})
                    if isinstance(nested[name], dict):
                        nested[name][key] = value
            for name, _, _ in self.groups:
                if any(str(marker) == name for marker in self.schema):
                    nested.setdefault(name, {})
            data = nested
        return super().__call__(data)


def sectioned(schema: vol.Schema, groups) -> vol.Schema:
    """``schema`` with its fields moved under the sections of ``groups``.

    Fields in no group stay at the top, first; a section none of whose fields
    the form has (a field that is only offered sometimes) is left out.
    """
    where = _by_field(groups)
    top: dict = {}
    inner: dict[str, dict] = {name: {} for name, _, _ in groups}
    for marker, validator in schema.schema.items():
        name = where.get(str(marker))
        if name is None:
            top[marker] = validator
        else:
            inner[name][marker] = validator
    out = dict(top)
    for name, _, collapsed in groups:
        if inner[name]:
            out[vol.Required(name)] = section(vol.Schema(inner[name]), {"collapsed": collapsed})
    return SectionedSchema(out, groups)


def flat_fields(schema: vol.Schema) -> dict:
    """Every field marker of ``schema`` with its validator, sections opened."""
    out: dict = {}
    for marker, validator in schema.schema.items():
        if isinstance(validator, section):
            out.update(validator.schema.schema)
        else:
            out[marker] = validator
    return out


def flatten(user_input: dict | None, groups) -> dict | None:
    """What a handler reads: the section dicts of ``user_input`` merged up."""
    if not user_input:
        return user_input
    names = {name for name, _, _ in groups}
    out: dict = {}
    for key, value in user_input.items():
        if key in names and isinstance(value, dict):
            out.update(value)
        else:
            out[key] = value
    return out


class SectionedFlowMixin:
    """Put it first among a flow's bases and give the class ``GROUPS``,
    ``{step id: [groups]}``: forms of those steps are shown sectioned and their
    handlers receive the input flat, as before."""

    GROUPS: dict[str, list[Group]] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for attr, method in list(vars(cls).items()):
            if attr.startswith("async_step_") and callable(method):
                setattr(cls, attr, _flat_input(attr[len("async_step_"):], method))

    def async_show_form(self, *, step_id=None, data_schema=None, errors=None, **kwargs):
        groups = self.GROUPS.get(step_id)
        if groups and data_schema is not None and not isinstance(data_schema, SectionedSchema):
            data_schema = sectioned(data_schema, groups)
            # the frontend shows no error under a field inside a section, only one
            # keyed by the section itself (above it)
            if errors:
                fields = _by_field(groups)
                errors = {fields.get(key, key): code for key, code in errors.items()}
        return super().async_show_form(step_id=step_id, data_schema=data_schema, errors=errors, **kwargs)


def _flat_input(step_id: str, method):
    @functools.wraps(method)
    async def wrapper(self, user_input=None):
        groups = self.GROUPS.get(step_id)
        return await method(self, flatten(user_input, groups) if groups else user_input)
    return wrapper
