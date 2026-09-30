export interface SlashCommand {
  trigger: string;
  descKey: string;
}

/** Convert the backend-owned command catalog into composer menu entries. */
export function slashCommandsFromCatalog(names: readonly string[]): SlashCommand[] {
  const commands = names
    .map((name) => name.trim().toLowerCase())
    .filter((name, index, values) => !!name && values.indexOf(name) === index)
    .map((name) => ({
      trigger: `/${name}`,
      descKey: `composer.cmd.${name}`,
    }));
  if (commands.some((command) => command.trigger === '/skill')) commands.unshift({ trigger: '/skill-use', descKey: 'composer.cmd.skill-use' });
  return commands;
}
