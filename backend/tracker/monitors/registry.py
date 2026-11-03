"""Executable evidence adapters. Read paths consume the saved catalog, not this registry."""
from tracker.monitors import eol, license, yanked
from tracker.monitors.commits import monitor as commits
from tracker.monitors.contract import Adapter
from tracker.monitors.requires import monitor as requires
from tracker.monitors.security import monitor as security

REGISTRY: dict[str, Adapter] = {
    'commits': commits,
    'eol': eol,
    'security': security,
    'license': license,
    'yanked': yanked,
    'requires': requires,
}
