"""Live, authorized Skill package operations using the existing publication API."""
import base64
import hashlib
import io
import json
import uuid
import zipfile

from fastapi import HTTPException, UploadFile
from sqlalchemy import text

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.authorization.types import Action, ConsistencyPreference
from vibecanvas_api.flowork_cli.cli import error, uncertain_result
from vibecanvas_api.flowork_cli.skill_cli import WRITE_OPERATIONS, validate
from vibecanvas_api.routes import skills
from vibecanvas_api.services.agent_resources import context as agent_context
from vibecanvas_api.services.agent_resources.authorization import _require_active_chat_write
from vibecanvas_api.services.agent_runtime.resource_routes import resource_route_params
from vibecanvas_api.services.skill_bundle import validate_skill_files
from vibecanvas_api.storage.db import session_scope
from vibecanvas_api.storage.repo_skills import SkillsRepo


def package_bytes(args):
    frontmatter, files = validate_skill_files([
        (item['path'], None, base64.b64decode(item['data'], validate=True)) for item in args['files']
    ])
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, _type, data in files:
            archive.writestr(path, data)
    return frontmatter, files, output.getvalue()


async def authorize(params, identifier, *, deleting=False):
    auth = {key:value for key,value in params.items() if key != 'session'}
    if identifier is None:
        await skills._authorize_organization_create(**auth)
    else:
        for action in ((Action.DELETE,) if deleting else (Action.UPDATE, Action.PUBLISH)):
            await skills._authorize_skill(skill_id=identifier, action=action,
                consistency=ConsistencyPreference.HIGHER_CONSISTENCY, **auth)
        row = await SkillsRepo(params['session']).get(identifier)
        if deleting:
            if row is None:
                raise HTTPException(404, 'skill not found')
            if str(row['user_id']) != str(params['ctx'].user_id):
                raise HTTPException(403, 'Only your own Skill installation can be deleted.')
        else:
            skills._require_owned_custom_skill(row, params['ctx'].user_id)


async def download(ctx, identifier):
    async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
        params = resource_route_params(ctx, session)
        auth = {key:value for key,value in params.items() if key != 'session'}
        await skills._authorize_skill(skill_id=identifier, action=Action.USE,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY, **auth)
        repo = SkillsRepo(session)
        row = await repo.get(identifier)
        if row is None or not row.get('current_revision_id'):
            raise HTTPException(404, 'skill_version_unavailable')
        revision_id = row['current_revision_id']
        await skills._authorize_skill_revision(revision_id=revision_id, action=Action.USE,
            consistency=ConsistencyPreference.HIGHER_CONSISTENCY, **auth)
        files = await repo.read_revision_files(identifier, revision_id)
        if files is None:
            raise HTTPException(404, 'skill_version_unavailable')
        return {'status':'succeeded', 'skill_id':str(identifier), 'name':row['name'],
            'source':row['source'], 'version':row['version'], 'revision_hash':row['revision_hash'],
            'files':[{'path':path,'data':base64.b64encode(data).decode('ascii')} for path,_type,data in files],
            'file_count':len(files)}


async def execute(call, arguments):
    cap, operation = call.capability, call.operation
    started = False
    request = None
    try:
        args = validate(operation, arguments)
        ctx = await agent_context.resolve_context(cap)
        if operation == 'skill.download':
            return await download(ctx, uuid.UUID(args['skill_id']))
        deleting = operation == 'skill.delete'
        frontmatter, files, bundle = ({}, [], b'') if deleting else package_bytes(args)
        if operation == 'skill.check':
            await skills.require_clean_upload(bundle)
            return {'status':'succeeded','valid':True,'name':frontmatter.get('name'),
                'file_count':len(files),'size_bytes':sum(len(data) for _,_,data in files), 'published':False}
        if operation not in WRITE_OPERATIONS:
            raise ValueError('Unsupported Skill operation')
        if cap.approval_mode not in {'agent','always_ask','always_allow'}:
            raise ToolError('invalid_approval_mode', 'Unknown approval mode. No changes were made.')
        identifier = uuid.UUID(args['skill_id']) if 'skill_id' in args else None
        async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
            await authorize(resource_route_params(ctx, session), identifier, deleting=deleting)
            await session.execute(text("""INSERT INTO skill_cli_leases(call_id,tenant_id,run_id,operation,expires_at)
                VALUES (:id,CAST(:tenant AS uuid),:run,:operation,now()+interval '30 seconds')"""),
                {'id':call.call_id,'tenant':cap.tenant_id,'run':cap.turn_id,'operation':operation})
        call.durable_lease = True
        if cap.approval_mode != 'always_allow':
            from .cli_delete import _approve
            summary = {'skill_id':str(identifier) if identifier else None, 'name':frontmatter.get('name'),
                'file_count':len(files),'size_bytes':sum(len(data) for _,_,data in files),
                'content_sha256':hashlib.sha256(json.dumps(args.get('files', []),sort_keys=True).encode()).hexdigest()}
            if deleting:
                summary = {'skill_id': str(identifier), 'operation': 'delete installation'}
            await _approve(call, summary, prompt='Approve '+operation.replace('.', ' ')+'? '
                +json.dumps(summary)+(' Removes your platform installation; local downloads and the catalog source are preserved.' if deleting else ' Updating replaces the complete package and unpublished draft, then publishes a new version.'))
        ctx = await agent_context.resolve_context(cap)
        async with session_scope(tenant_id=ctx.tenant_id, user_id=ctx.username) as session:
            await _require_active_chat_write(session, ctx)
            if not (await session.execute(text('SELECT 1 FROM skill_cli_leases WHERE call_id=:id AND expires_at>now()'), {'id':call.call_id})).first():
                raise ToolError('approval_cancelled', 'The CLI command is no longer active.')
            params = resource_route_params(ctx, session)
            request = params['request']
            await authorize(params, identifier, deleting=deleting)
            started = True
            if deleting:
                await skills.delete_skill(skill_id=str(identifier), **params)
                return {'status':'succeeded','skill_id':str(identifier),'deleted':True,
                    'message':'Skill installation deleted. Local downloads are preserved.'}
            upload = UploadFile(filename='skill.zip', file=io.BytesIO(bundle))
            if identifier is None:
                result = await skills.create_custom_skill(bundle=upload, **params)
            else:
                result = await skills.update_custom_skill_bundle(skill_id=str(identifier), bundle=upload, **params)
            await session.commit()
            return {'status':'succeeded','skill_id':result.id,'name':result.name,'source':result.source,
                'version':result.version,'revision_hash':result.revision_hash,'file_count':len(files),
                'message':'Skill package validated and published.'}
    except Exception as exc:
        deletion = getattr(request.state, 'skill_deletion_receipt', None) if request is not None else None
        if deletion:
            return {'status':'succeeded',**deletion,'warning':'authorization_cleanup_pending',
                'message':'Skill installation deleted; authorization cleanup is pending.'}
        receipt = getattr(request.state, 'skill_publication_receipt', None) if request is not None else None
        if receipt:
            return {'status':'succeeded',**receipt,'warning':'publication_followup_pending',
                'message':'The Skill was created, but follow-up failed. Inspect skill list/get before retrying.'}
        if isinstance(exc, ToolError):
            return {'status':'failed',**error(str(exc),exc.message,'Respect approval decisions; inspect Skill permissions before retrying.')}
        if isinstance(exc, HTTPException):
            code = {403:'permission_denied',404:'resource_unavailable',409:'state_conflict',422:'invalid_arguments'}.get(exc.status_code,'skill_error')
            return {'status':'failed',**error(code,str(exc.detail),'Check skill list/get and command help before retrying.')}
        if isinstance(exc, (ValueError, UnicodeError)):
            return {'status':'failed',**error('invalid_arguments',str(exc),'Correct the complete Skill package, then run skill check.')}
        if isinstance(exc, PermissionError):
            return {'status':'failed',**error('permission_denied','This command is no longer authorized.','Use an authorized active Agent turn.')}
        return uncertain_result() if started else error('skill_unavailable','The Skill operation is unavailable.','Inspect permissions and retry the read-only check.')
