"""Check sharing policy against a real OpenFGA in a disposable store.

Requires OPENFGA_API_URL and optionally OPENFGA_API_TOKEN. Never changes the
application's store/model selection. Deletes only the store created by this run.
"""
import json
import os
from pathlib import Path
import uuid

import httpx


def main():
    model = json.loads((Path(__file__).resolve().parents[2] /
        'api/src/vibecanvas_api/authorization/model/model.json').read_text())
    headers = {'Authorization': 'Bearer ' + os.environ['OPENFGA_API_TOKEN']} if os.environ.get('OPENFGA_API_TOKEN') else {}
    checks = 0
    with httpx.Client(base_url=os.environ['OPENFGA_API_URL'].rstrip('/'), headers=headers, timeout=15) as client:
        def post(path, body):
            response = client.post(path, json=body)
            response.raise_for_status()
            return response.json() if response.content else {}
        store = post('/stores', {'name': 'sharing-qa-' + uuid.uuid4().hex})['id']
        base = '/stores/' + store
        try:
            model_id = post(base + '/authorization-models', model)['authorization_model_id']
            def edge(user, relation, resource):
                return {'user': user, 'relation': relation, 'object': resource}
            def write(edges, delete=False):
                post(base + '/write', {'authorization_model_id': model_id,
                    'deletes' if delete else 'writes': {'tuple_keys': edges}})
            def check(user, relation, resource, expected):
                nonlocal checks
                result = post(base + '/check', {'authorization_model_id': model_id,
                    'tuple_key': edge(user, relation, resource), 'consistency': 'HIGHER_CONSISTENCY'})
                assert result['allowed'] is expected, (user, relation, resource, expected, result['allowed'])
                checks += 1
            write([edge('user:member', 'member', 'organization:recipient'),
                   edge('user:admin', 'admin', 'organization:recipient'),
                   edge('user:owner', 'owner', 'organization:recipient'),
                   edge('user:guest', 'guest', 'organization:recipient'),
                   edge('user:member', 'direct_member', 'group:child'),
                   edge('group:child#member', 'descendant', 'group:parent')])
            for kind in ['workflow', 'task', 'deployment', 'skill_installation', 'knowledge_base']:
                roles = ['viewer', 'editor', 'manager'] + (['operator'] if kind in {'workflow', 'task', 'deployment'} else [])
                for role in roles:
                    for source in ['user:member', 'group:parent#member', 'organization:recipient#member']:
                        resource = kind + ':' + uuid.uuid4().hex
                        grant = edge(source, role, resource)
                        write([grant])
                        expected = {'can_view': True, 'can_update': role in {'editor', 'manager'},
                                    'can_manage_access': role == 'manager', 'can_delete': role == 'manager'}
                        if kind in {'workflow', 'task', 'deployment'}:
                            expected['can_execute'] = role in {'operator', 'manager'}
                        else:
                            expected['can_use'] = True
                        if kind == 'skill_installation':
                            expected['can_publish'] = role in {'editor', 'manager'}
                        for relation, allowed in expected.items():
                            check('user:member', relation, resource, allowed)
                            check('user:stranger', relation, resource, False)
                        check('user:guest', 'can_view', resource, False)
                        if source.startswith('organization:'):
                            for user in ['admin', 'owner']:
                                check('user:' + user, 'can_view', resource, True)
                        write([grant], delete=True)
                        check('user:member', 'can_view', resource, False)
                # Direct + group roles combine; removing one preserves the other.
                if 'operator' in roles:
                    resource = kind + ':union'
                    grants = [edge('user:member', 'editor', resource), edge('group:parent#member', 'operator', resource)]
                    write(grants)
                    check('user:member', 'can_update', resource, True)
                    check('user:member', 'can_execute', resource, True)
                    write(grants[:1], delete=True)
                    check('user:member', 'can_update', resource, False)
                    check('user:member', 'can_execute', resource, True)
                    write(grants[1:], delete=True)
            resource = 'workflow:membership-revoke'
            write([edge('group:parent#member', 'editor', resource)])
            check('user:member', 'can_update', resource, True)
            write([edge('user:member', 'direct_member', 'group:child')], delete=True)
            check('user:member', 'can_update', resource, False)
            company_resource = 'workflow:company-revoke'
            write([edge('organization:recipient#member', 'viewer', company_resource)])
            check('user:member', 'can_view', company_resource, True)
            write([edge('user:member', 'member', 'organization:recipient')], delete=True)
            check('user:member', 'can_view', company_resource, False)
            check('user:owner', 'can_view', company_resource, True)
            # A direct-member share excludes descendant department members.
            direct_resource = 'workflow:direct-department'
            write([edge('user:member', 'direct_member', 'group:child'),
                   edge('group:parent#direct_member', 'viewer', direct_resource)])
            check('user:member', 'can_view', direct_resource, False)
            write([edge('user:member', 'direct_member', 'group:parent')])
            check('user:member', 'can_view', direct_resource, True)
            print(json.dumps({'status': 'passed', 'checks': checks, 'disposable_store': True}))
        finally:
            response = client.delete(base)
            response.raise_for_status()


if __name__ == '__main__':
    main()
