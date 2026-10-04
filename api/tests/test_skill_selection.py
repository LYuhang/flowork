from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from vibecanvas_api.schemas.chat import MessagePostBody, SkillUseSelection
from vibecanvas_api.services.agent_runtime.protocol import RuntimeSkill
from vibecanvas_api.services.skill_selection import selected_skill_instruction


def skill(identifier, name='research'):
    return RuntimeSkill(skill_id=str(identifier),name=name,revision_hash='a'*64,root_path=f'/skills/{identifier}')


def test_selection_uses_stable_id_when_names_match():
    first,second=uuid4(),uuid4()
    selection=SkillUseSelection(skill_id=second,name='research')
    instruction=selected_skill_instruction(selection,[skill(first),skill(second)])
    assert f'/skills/{second}/SKILL.md' in instruction.content
    assert str(first) not in instruction.content
    assert instruction.activated_this_turn


@pytest.mark.parametrize('renamed',[False,True])
def test_unavailable_selection_never_falls_back_to_same_name(renamed):
    identifier=uuid4()
    available=[skill(identifier,'new-name')] if renamed else [skill(uuid4())]
    with pytest.raises(HTTPException) as error:
        selected_skill_instruction(SkillUseSelection(skill_id=identifier,name='research'),available)
    assert error.value.status_code==409
    assert error.value.detail['code']==('selected_skill_changed' if renamed else 'selected_skill_unavailable')


def test_valid_selected_command_and_management_are_distinct():
    identifier=uuid4()
    body=MessagePostBody(content='/skill-use:[research] Summarize the sources',skill_use={'skill_id':str(identifier),'name':'research'})
    assert body.skill_use.skill_id==identifier
    assert MessagePostBody(content='/skill list my skills').skill_use is None


@pytest.mark.parametrize('content,selection',[
    ('/skill-use:[research] do work',None),
    ('/skill-use',None),
    ('/skill list',{'name':'research'}),
    ('/skill-use:[other] do work',{'name':'research'}),
    ('/skill-use:[research]',{'name':'research'}),
    ('/skill-use:[research] do work',{'name':'research','path':'/etc/passwd'}),
])
def test_malformed_or_unbound_selection_rejected(content,selection):
    if selection is not None:
        selection={'skill_id':str(uuid4()),**selection}
    with pytest.raises(ValidationError):
        MessagePostBody(content=content,skill_use=selection)


def runtime_request(identifier, descriptors):
    from vibecanvas_api.services.agent_runtime.protocol import RuntimeTurnRequest
    selection=SkillUseSelection(skill_id=identifier,name='research')
    return RuntimeTurnRequest(
        tenant_id='tenant',user_id='user',chat_id='chat',turn_id='turn',
        runtime_type='codex',runtime_session_id='session',runtime_root='/runtime/.codex',
        runtime_state_ref='existing-thread',
        model={'id':'test','connection_type':'chatgpt_account'},
        message={'role':'user','content':'Summarize sources','additional_kwargs':{'skill_use':selection.model_dump(mode='json')}},
        skills=descriptors,instructions=[selected_skill_instruction(selection,descriptors)],
    )


def test_selected_skill_roundtrips_runtime_protocol_without_sticky_mode():
    from vibecanvas_api.services.agent_runtime.protocol import RuntimeTurnRequest
    first,second=uuid4(),uuid4()
    request=runtime_request(second,[skill(first),skill(second)])
    restored=RuntimeTurnRequest.model_validate_json(request.model_dump_json())
    assert restored.command_context.active_modes==[]
    assert restored.instructions[0].scope=='turn'
    assert restored.instructions[0].kind=='skill_selection'
    with pytest.raises(ValidationError,match='duplicate IDs'):
        runtime_request(first,[skill(first),skill(first)])


@pytest.mark.parametrize('recovered',[False,True])
def test_codex_input_includes_selected_skill_for_existing_and_recovered_thread(recovered):
    from vibecanvas_api.services.agent_runtime import codex
    identifier=uuid4()
    request=runtime_request(identifier,[skill(identifier)])
    import json
    payload=json.dumps(codex._turn_input(request,recovered_native_history=recovered))
    assert f'/skills/{identifier}/SKILL.md' in payload
    assert 'Summarize sources' in payload


def test_selected_skill_missing_mount_fails_before_native_setup(monkeypatch,tmp_path):
    from vibecanvas_api.services.agent_runtime import codex
    identifier=uuid4()
    descriptor=skill(identifier).model_copy(update={'root_path':str(tmp_path/'mount')})
    request=runtime_request(identifier,[descriptor])
    monkeypatch.setattr(codex,'_chat_home',lambda chat_id:str(tmp_path/'home'))
    with pytest.raises(RuntimeError,match='selected_skill_unmounted'):
        codex._prepare_codex_skills(request)
    assert not (tmp_path/'home').exists()
    (tmp_path/'mount').mkdir()
    (tmp_path/'mount'/'SKILL.md').write_text('Instructions')
    codex._prepare_codex_skills(request)
    assert (tmp_path/'home'/'.agents'/'skills'/str(identifier)/'SKILL.md').read_text()=='Instructions'


def test_skill_management_is_advertised_on_both_chat_surfaces():
    from vibecanvas_api.routes.chats import _available_commands
    for surface in ('chat','browser'):
        assert 'skill' in _available_commands(surface,'codex')


@pytest.mark.asyncio
async def test_selection_uses_the_same_chat_scoped_path_as_sandbox_mount(monkeypatch):
    from unittest.mock import AsyncMock
    from vibecanvas_api.services import runtime_skills
    from vibecanvas_api.authorization.types import AuthzRequestContext, ConsistencyPreference, PrincipalRef, PrincipalType
    identifier=uuid4()
    repo=SimpleNamespace(list_authorized=AsyncMock(return_value=[{
        'skill_id':identifier,'name':'research','revision_hash':'a'*64,
    }]))
    monkeypatch.setattr(runtime_skills,'SkillsRepo',lambda session:repo)
    service=SimpleNamespace(list_authorized_ids=AsyncMock(return_value=[identifier]))
    session=SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    descriptors=await runtime_skills.runtime_skill_descriptors(session=session,chat_id='chat-a',service=service,principal=PrincipalRef(PrincipalType.USER,str(uuid4())),context=AuthzRequestContext(active_organization_id=str(uuid4())))
    assert service.list_authorized_ids.await_args.args[-1].consistency==ConsistencyPreference.HIGHER_CONSISTENCY
    root=runtime_skills.runtime_skill_root('chat-a',str(identifier))
    assert root!=runtime_skills.runtime_skill_root('chat-b',str(identifier))
    assert root==f'/skills/{runtime_skills.runtime_skill_scope("chat-a")}/{identifier}'
    instruction=selected_skill_instruction(SkillUseSelection(skill_id=identifier,name='research'),descriptors)
    assert root+'/SKILL.md' in instruction.content
    assert f'/skills/{identifier}/SKILL.md' not in instruction.content
