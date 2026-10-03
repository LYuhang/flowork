"""Stable, content-free optimistic concurrency errors shared by API and CLI."""


from fastapi import HTTPException


class WriteConflict(HTTPException):
    def __init__(self, *, expected_version=None, current_version=None, draft=False):
        self.detail = {
            'error': 'draft_conflict' if draft else 'version_conflict',
            'message': ('Unpublished edits exist. Nothing was published; review and publish the existing draft in the editor first.'
                        if draft else 'The resource changed. Nothing was saved; download the latest version and merge your edits before submitting again.'),
            'expected_version': expected_version,
            'current_version': current_version,
        }
        super().__init__(status_code=409, detail=self.detail)

    def cli_result(self):
        return {'status': 'failed', **self.detail, 'hint': self.detail['message']}
