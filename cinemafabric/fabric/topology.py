"""Initial worker set: machines from the profile, the cloud and the in-process fallback."""

import logging

from ..config import Settings
from ..profile import profile
from . import machines
from .backends.base import Backend
from .backends.mock import MockBackend, build_mock_backends
from .backends.muse import MuseCloudBackend
from .backends.synth import SynthBackend

log = logging.getLogger("cinemafabric.topology")


def materialise(spec: machines.MachineSpec, mock: bool) -> Backend:
    backend = machines.build(spec)
    if mock:
        backend = MockBackend(backend)
    backend.source = "added"
    return backend


def build_backends(settings: Settings) -> list[Backend]:
    if settings.mock:
        return build_mock_backends(settings)
    backends: list[Backend] = []
    for entry in profile().get("machines", []):
        try:
            spec = machines.MachineSpec.model_validate(entry)
            spec.id = machines.slug(spec.id or spec.label)
            spec.base_url = machines.validate_url(spec.base_url)
            backend = machines.build(spec)
            backend.source = "profile"
            backends.append(backend)
        except ValueError as e:
            log.warning("skipping machine %s from profile: %s", entry.get("label"), e)
    cloud, synth = MuseCloudBackend(settings), SynthBackend()
    cloud.source = synth.source = "builtin"
    return backends + [cloud, synth]
