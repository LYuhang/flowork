"""Stdlib-only Skill package management; platform validates the full bundle."""
import os
import tempfile
from uuid import UUID

try:
    from . import knowledge_cli
except ImportError:
    import knowledge_cli

READ_OPERATIONS = frozenset({'skill.check', 'skill.download', 'skill.refresh'})
WRITE_OPERATIONS = frozenset({'skill.create', 'skill.update', 'skill.delete'})
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS
ACTIONS = {'check', 'download', 'create', 'refresh', 'publish', 'delete'}


def add_commands(commands):
    descriptions = {
        'delete': 'Delete your own Skill installation. For catalog Skills, only your installation is removed. Local downloads are preserved. Uses the configured write approval policy.',
        'check': 'Validate ALL files in --source-dir against platform Skill rules. Does not publish a version.',
        'create': 'Validate and publish a new custom Skill from ALL files in --source-dir, including hidden files. Root SKILL.md is required; symlinks are rejected.',
        'publish': 'Replace the ENTIRE package of your OWN custom Skill with --source-dir. Require --expected-version from download; newer publications or unpublished drafts reject the write. Validate and publish the next version automatically. Absent files are removed. Catalog or other users\' Skills cannot be edited. Inspect get after unknown outcomes before retrying.',
        'refresh': 'Refresh this Chat sandbox Skill folder from the latest authorized published version. Does not publish or modify the platform Skill.',
        'download': 'Download the latest authorized Skill package into a NEW directory. Never overwrite existing local edits. Read SKILL.md; edit files, check, then publish your own custom Skill.',
    }
    for action, description in descriptions.items():
        leaf = commands.add_parser(action, help=description, description=description, allow_abbrev=False)
        if action in {'download', 'refresh', 'publish', 'delete'}:
            leaf.add_argument('--skill-id', required=True)
        if action in {'check', 'create', 'publish'}:
            leaf.add_argument('--source-dir', required=True)
        if action == 'publish':
            leaf.add_argument('--expected-version', type=int, required=True, help='Published version from download; rejects newer publications or unpublished drafts.')
        if action == 'download':
            leaf.add_argument('--output-dir')


def validate(operation, arguments):
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise ValueError('Unsupported Skill package operation.')
    allowed = {'files'} if operation in {'skill.check', 'skill.create'} else {'skill_id'}
    if operation == 'skill.update':
        allowed.update({'files', 'expected_version'})
    if arguments.keys() != allowed:
        raise ValueError('Provide exactly the documented Skill package parameters.')
    value = dict(arguments)
    if operation == 'skill.update' and (type(value['expected_version']) is not int or value['expected_version'] < 1):
        raise ValueError('--expected-version must be a positive integer from download.')
    if 'skill_id' in value:
        value['skill_id'] = str(UUID(value['skill_id']))
    if 'files' in value:
        knowledge_cli.validate_files(value['files'], entrypoint='SKILL.md')
        if not any(item['path'] == 'SKILL.md' for item in value['files']):
            raise ValueError('Use the exact root filename SKILL.md.')
    return value


def execute(args, endpoint, cli):
    operation = 'skill.update' if args.action == 'publish' else 'skill.' + args.action
    dispatched = False
    destination = None
    try:
        value = {key:val for key,val in vars(args).items() if key not in {'resource','action','source_dir','output_dir'} and val is not None}
        if getattr(args, 'source_dir', None):
            value['files'] = knowledge_cli.collect(args.source_dir, entrypoint='SKILL.md')
        value = validate(operation, value)
        if not endpoint:
            raise ValueError('No active Flowork Agent execution is connected.')
        if args.action == 'download':
            if args.output_dir:
                destination = os.path.abspath(args.output_dir)
                os.mkdir(destination, 0o700)
            else:
                os.makedirs('/data/skills', mode=0o700, exist_ok=True)
                destination = tempfile.mkdtemp(prefix=value['skill_id']+'-', dir='/data/skills')
        dispatched = True
        result = cli.request(endpoint, value, operation=operation)
        if destination and 'error' not in result:
            files = result.pop('files')
            knowledge_cli.materialize(destination, files, entrypoint='SKILL.md')
            result.update(local_directory=destination, entrypoint=os.path.join(destination, 'SKILL.md'))
    except (ValueError, OSError, KeyError, TypeError, KeyboardInterrupt) as exc:
        result = cli.uncertain_result() if dispatched and operation in WRITE_OPERATIONS else cli.error(
            'read_failed' if dispatched else 'invalid_arguments', str(exc),
            'Read skill '+args.action+' --help. Downloads require a new directory; partial local files may remain after failure.')
        if destination:
            result['local_directory'] = destination
    result.setdefault('status', 'failed' if 'error' in result else 'succeeded')
    return cli.emit_result(result, exit_code=2 if result.get('error') == 'invalid_arguments' else (1 if 'error' in result else 0))
