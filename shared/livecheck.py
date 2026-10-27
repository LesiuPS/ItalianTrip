"""Runtime integration check. Uses a disposable schema, never user expense rows.

Run before the web worker starts accepting requests. The ordinary database is
initialized first. Real API writes are exercised with independent sessions and
connections inside a private test schema, which is removed in finally.
"""
import copy
import secrets
from psycopg import sql


def verify(module):
    original_connect = module.connect
    production = module.load_state()
    original_initialized = module._initialized
    schema = 'trip_check_' + secrets.token_hex(10)
    created = False
    try:
        with original_connect() as con:
            con.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        created = True

        def isolated_connect():
            con = original_connect()
            try:
                con.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
                if con.execute('SELECT current_schema()').fetchone()[0] != schema:
                    raise RuntimeError('Test database isolation failed')
                return con
            except Exception:
                con.close()
                raise

        module.connect = isolated_connect
        module._initialized = False
        root = 'https://localhost'
        leszek, karolina = module.app.test_client(), module.app.test_client()
        tokens = {}
        for client, user in ((leszek, 'Leszek'), (karolina, 'Karolina')):
            token = secrets.token_urlsafe(24)
            tokens[user] = token
            with client.session_transaction(base_url=root) as session:
                session['user'] = user
                session['csrf'] = token

        def require(condition, message):
            if not condition:
                raise RuntimeError('Live database check failed: ' + message)

        def post(client, user, operation, changes):
            return client.post('/api/changes', base_url=root,
                json={'operation': operation, 'changes': changes},
                headers={'X-CSRF-Token': tokens[user], 'Origin': root})

        require(module.app.test_client().get('/api/state', base_url=root).status_code == 401,
                'anonymous access must be denied')
        start_response = leszek.get('/api/state', base_url=root)
        require(start_response.status_code == 200, 'initial read')
        initial = start_response.json['state']
        require(leszek.get('/', base_url=root).status_code == 200, 'authenticated planner page')
        count = len(initial['expenses'])
        row = {'id': 'runtime-check-only', 'date': None, 'paymentDate': None,
            'title': 'Synthetic integration-test expense', 'category': 'inne',
            'amountMinor': 123, 'currency': 'EUR', 'payer': 'Leszek',
            'method': 'unknown', 'status': 'paid', 'split': 'exclude',
            'location': '', 'notes': '', 'stayId': None, 'liters': None,
            'odometer': None, 'deletedAt': None}
        add = {'collection': 'expenses', 'id': row['id'], 'version': 0, 'value': row}
        saved = post(leszek, 'Leszek', 'runtime-check-add', [add])
        require(saved.status_code == 200, 'first user write')
        second_read = karolina.get('/api/state', base_url=root)
        require(second_read.status_code == 200, 'second user read')
        received = next(x for x in second_read.json['state']['expenses'] if x['id'] == row['id'])
        require(received['amountMinor'] == 123 and received['createdBy'] == 'Leszek',
                'second user must see first user persisted write and author')
        changed = copy.deepcopy(received)
        changed['amountMinor'] = 456
        edit = {'collection': 'expenses', 'id': row['id'], 'version': received['_v'], 'value': changed}
        edited = post(karolina, 'Karolina', 'runtime-check-edit', [edit])
        require(edited.status_code == 200, 'second user edit')
        after = leszek.get('/api/state', base_url=root).json['state']
        observed = next(x for x in after['expenses'] if x['id'] == row['id'])
        require(observed['amountMinor'] == 456 and observed['updatedBy'] == 'Karolina',
                'first user must see second user edit')
        repeated = post(karolina, 'Karolina', 'runtime-check-edit', [edit])
        require(repeated.status_code == 200 and repeated.json.get('duplicate') is True,
                'retry must be idempotent')
        require(repeated.json['state']['revision'] == after['revision'], 'retry changed revision')
        require(post(leszek, 'Leszek', 'runtime-check-stale', [edit]).status_code == 409,
                'stale edit must not overwrite another user')
        module._initialized = False
        reloaded = karolina.get('/api/state', base_url=root).json['state']
        require(len(reloaded['expenses']) == count + 1, 'reinitialization duplicated or lost data')
        require(next(x for x in reloaded['expenses'] if x['id'] == row['id'])['amountMinor'] == 456,
                'reinitialization lost persisted edit')
        export = leszek.get('/api/offline', base_url=root)
        require(export.status_code == 200 and 'attachment' in export.headers.get('Content-Disposition', ''),
                'offline export')
        note = {'collection': 'notes', 'id': '2099-01-01', 'before': None, 'value': 'Synthetic shared note'}
        require(post(karolina, 'Karolina', 'runtime-check-note', [note]).status_code == 200, 'shared note write')
        require(leszek.get('/api/state', base_url=root).json['state']['notes']['2099-01-01'] == note['value'],
                'shared note read')
    finally:
        module.connect = original_connect
        module._initialized = original_initialized
        if created:
            with original_connect() as con:
                con.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    restored = module.load_state()
    if restored['tripId'] != production['tripId']:
        raise RuntimeError('Production database context was not restored')
    print('TRIP LIVE CHECK PASS: PostgreSQL read/write; two independent authenticated sessions; '
          'cross-session edits and notes; conflict rejection; idempotent retry; '
          'persistence after reinitialization; export. Disposable test schema removed. '
          'User expense rows were not modified.', flush=True)
