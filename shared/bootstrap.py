"""Load private deployment data from one Render environment value, never Git."""
import json
import os
import re
import runpy


def configure():
    raw = os.environ.get('PRIVATE_CONFIG', '')
    if not raw or len(raw) > 500_000:
        raise RuntimeError('Set PRIVATE_CONFIG in the Render deployment form. Do not commit its contents.')
    try:
        config = json.loads(raw)
        seed, plan, passwords = config['seed'], config['plan'], config['passwords']
        if not isinstance(config, dict) or not isinstance(seed, dict) or not isinstance(plan, dict):
            raise ValueError()
        if seed.get('schemaVersion') != 1 or not isinstance(seed.get('tripId'), str):
            raise ValueError()
        if not all(isinstance(seed.get(k), list) for k in ('expenses', 'stays', 'transfers')):
            raise ValueError()
        if not all(isinstance(plan.get(k), list) for k in ('plan', 'laundry', 'points')):
            raise ValueError()
        if not seed['expenses'] or not plan['plan']:
            raise ValueError()
        for name in ('Leszek', 'Karolina'):
            value = passwords[name]
            if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}:[a-f0-9]{64}', value):
                raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise RuntimeError('PRIVATE_CONFIG is incomplete or invalid. Copy the complete JSON configuration.') from None
    os.environ['TRIP_SEED'] = json.dumps(seed, ensure_ascii=False)
    os.environ['TRIP_PLAN'] = json.dumps(plan, ensure_ascii=False)
    for name in ('Leszek', 'Karolina'):
        os.environ['PASSWORD_' + name.upper()] = passwords[name]


configure()
if __name__ == '__main__':
    # Build tests use an isolated transactional adapter, not the live database.
    runpy.run_module('shared.test_app', run_name='__main__')
else:
    from .app import app
