"""Resolve an explicit, turn-local Skill selection without trusting client paths."""
from __future__ import annotations

import json
import re
from collections.abc import Sequence

from fastapi import HTTPException
from vibecanvas_api.services.agent_runtime.protocol import RuntimeInstruction, RuntimeSkill

SKILL_USE_PATTERN = re.compile(r'^/skill-use:\[([^\]\r\n]+)\](?:\s+([\s\S]*))?$')


def selected_skill_instruction(selection, skills: Sequence[RuntimeSkill]) -> RuntimeInstruction:
    selected = next((skill for skill in skills if skill.skill_id == str(selection.skill_id)), None)
    if selected is None:
        raise HTTPException(409, detail={
            'code': 'selected_skill_unavailable',
            'message': 'The selected Skill is unavailable. Select an accessible published Skill again.',
        })
    if selected.name != selection.name:
        raise HTTPException(409, detail={
            'code': 'selected_skill_changed',
            'message': 'The selected Skill was renamed. Select it again before sending.',
        })
    description = json.dumps({
        'skill_id': selected.skill_id, 'name': selected.name,
        'entrypoint': selected.root_path + '/SKILL.md',
    }, ensure_ascii=False)
    return RuntimeInstruction(
        instruction_id='command:skill_use', kind='skill_selection', scope='turn',
        name='skill_use', version=1, activated_this_turn=True,
        content=('The user explicitly selected the following Skill for this turn: ' + description
                 + '\nRead its SKILL.md and use it for the accompanying task. Resolve supporting files relative '
                   'to that Skill directory. If it cannot be read, report the problem; do not substitute another '
                   'Skill or guess a path. This selection requests use, not editing or publishing the Skill.'),
    )
