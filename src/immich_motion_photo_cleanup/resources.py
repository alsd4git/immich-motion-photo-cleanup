"""Installed package resources and versioned runtime fingerprints."""

import hashlib
from importlib.resources import files


def resource_bytes(name):
    if name not in RUNTIME_FILES:
        raise ValueError("unknown runtime resource")
    return files(__package__).joinpath(name).read_bytes()


RUNTIME_FILES = (
    "__init__.py",
    "__main__.py",
    "cli.py",
    "resources.py",
    "inventory.py",
    "inventory.sql",
    "media.py",
    "motion_audit.py",
    "run_batches.py",
    "sidecars.py",
    "trash_state.sql",
    "trash_plan.py",
    "immich_api.py",
    "trash.py",
)


def runtime_digest():
    # The domain separates packaged runs from pre-package recovery journals.
    digest = hashlib.sha256(b"immich-motion-photo-cleanup:packaged-runtime:1\0")
    from . import __version__

    digest.update(__version__.encode() + b"\0")
    for name in RUNTIME_FILES:
        content = resource_bytes(name)
        digest.update(name.encode() + b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
