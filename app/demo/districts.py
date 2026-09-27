"""An explicitly labelled copy of the frozen SYNTHETIC Moscow preset.

This adapter is only for the bundled, manifest-verified preset. It is not a
geocoder or an address parser for customer data. Job districts come from its
recorded CSV column; home districts come from recorded start labels.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.domain.models import PlanRequest


def district_demo_request() -> PlanRequest:
    data = Path(__file__).resolve().parent / 'data'
    name = 'realistic.request.json'
    manifest = json.loads((data/'realistic_manifest.json').read_text(encoding='utf-8'))
    content = (data/name).read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest['files'][name]['sha256']:
        raise ValueError('The bundled synthetic dataset differs from its manifest')
    raw = json.loads(content)
    prefix, suffix = 'Москва, ', ' · старт смены'
    homes = {}
    for engineer in raw['engineers']:
        label = engineer['start_location']['label']
        if not label.startswith(prefix) or not label.endswith(suffix):
            raise ValueError('Unexpected synthetic home label')
        homes[label] = label[len(prefix):-len(suffix)]
    for engineer in raw['engineers']:
        engineer['district_id'] = homes[engineer['start_location']['label']]
    for job in raw['jobs']:
        district = job['metadata']['source']['fields']['Район']
        if district not in homes.values():
            raise ValueError('Unexpected synthetic district label')
        job['district_id'] = district
        job['metadata']['district_assignment_source'] = 'bundled_synthetic_csv_Район'
    raw['district_mode'] = 'strict'
    return PlanRequest.model_validate(raw)
