import { describe, expect, it } from 'vitest';
import { slashCommandsFromCatalog } from './slash-commands';

describe('slashCommandsFromCatalog', () => {
  it('uses the backend catalog order without a duplicated frontend registry', () => {
    expect(slashCommandsFromCatalog([
      'task',
      'deployment',
      'knowledge',
      'workflow',
      'task',
    ])).toEqual([
      { trigger: '/task', descKey: 'composer.cmd.task' },
      { trigger: '/deployment', descKey: 'composer.cmd.deployment' },
      { trigger: '/knowledge', descKey: 'composer.cmd.knowledge' },
      { trigger: '/workflow', descKey: 'composer.cmd.workflow' },
    ]);
  });
});


it('offers use and management separately when Skill management is available', () => {
  expect(slashCommandsFromCatalog([' SKILL ', 'skill'])).toEqual([
    { trigger: '/skill-use', descKey: 'composer.cmd.skill-use' },
    { trigger: '/skill', descKey: 'composer.cmd.skill' },
  ]);
});
