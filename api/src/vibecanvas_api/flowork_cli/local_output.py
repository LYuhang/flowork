"""Public local-execution status contract shared by submission and queries."""
TERMINAL = {'completed', 'completed_with_errors', 'failed', 'cancelled', 'interrupted'}


def describe(summary):
    value = dict(summary)
    state = value['status']
    run_id = value['run_id']
    status_command = f'flowork-cli workflow status --run-id {run_id}'
    result_command = f'flowork-cli workflow result --run-id {run_id}'
    value.update(execution_status=state, terminal=None if state == 'unknown' else state in TERMINAL,
                 status_command=status_command, result_command=result_command)
    if state == 'waiting_approval':
        action, message, command = ('request_human_approval',
            'Show approval links to the user. Other samples may still be running. Query the same run later.', status_command)
    elif state in {'preparing', 'running'}:
        action, message, command = ('check_status_later',
            'Execution is active. Query this run again; do not submit another execution.', status_command)
    elif state == 'completed':
        action, message, command = ('read_results', 'Read the result pages for this execution.', result_command)
    elif state == 'unknown':
        action, message, command = ('verify_runtime',
            'Runtime evidence is unavailable in this sandbox. Preserve the run ID; do not automatically rerun.', status_command)
    else:
        action, message, command = ('inspect_errors',
            'Inspect execution errors and available sample results. Earlier side effects are not undone; do not automatically rerun.', result_command)
    value['next_action'] = {'type': action, 'message': message, 'command': command}
    return value
