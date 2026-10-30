"""A translation step's labels and help texts, whether they sit at the top of
the step or under one of its sections (config_flow/groups.py)."""


def labels(block: dict) -> dict:
    out = dict(block.get("data") or {})
    for sec in (block.get("sections") or {}).values():
        out.update(sec.get("data") or {})
    return out


def helps(block: dict) -> dict:
    out = dict(block.get("data_description") or {})
    for sec in (block.get("sections") or {}).values():
        out.update(sec.get("data_description") or {})
    return out
