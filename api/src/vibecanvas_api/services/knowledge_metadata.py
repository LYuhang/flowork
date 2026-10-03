"""README metadata shared by Knowledge publication, drafts and CLI validation."""
from __future__ import annotations

import yaml

from vibecanvas_api.services.knowledge_packages import PackageFile


def _readme(files):
    return next(item for item in files if item.path.casefold() == 'readme.md')


def _frontmatter(data):
    content = data.decode('utf-8-sig')
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        return None, content
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == '---'), None)
    if end is None:
        raise ValueError('README.md YAML frontmatter is missing its closing --- delimiter.')
    header = ''.join(lines[1:end])
    if len(header.encode('utf-8')) > 16384:
        raise ValueError('README.md frontmatter must not exceed 16 KiB.')
    try:
        value = yaml.safe_load(header)
    except yaml.YAMLError as exc:
        raise ValueError('README.md contains invalid YAML frontmatter.') from exc
    if not isinstance(value, dict):
        raise ValueError('README.md frontmatter must be a mapping.')
    return value, ''.join(lines[end + 1:])


def validate_metadata(name, description):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        raise ValueError('README.md name must be a non-empty string up to 200 characters.')
    if not isinstance(description, str) or len(description) > 2000:
        raise ValueError('README.md description must be a string up to 2000 characters (empty is allowed).')
    return {'name': name.strip(), 'description': description}


def package_metadata(files, *, required=True):
    value, _ = _frontmatter(_readme(files).data)
    if not required and (value is None or not ({"name", "description"} & value.keys())):
        return None
    if value is None:
        raise ValueError('README.md requires YAML frontmatter with name and description.')
    return validate_metadata(value.get('name'), value.get('description'))


def with_metadata(files, *, name, description, only_if_missing=False):
    """Return new bytes, leaving the input and retained historical files untouched."""
    readme = _readme(files)
    value, body = _frontmatter(readme.data)
    metadata = validate_metadata(name, description or '')
    if only_if_missing and package_metadata(files, required=False) is not None:
        return files
    if value is not None and all(value.get(key) == item for key, item in metadata.items()):
        return files  # Preserve exact bytes for already-valid CLI publications.
    value = {**(value or {}), **metadata}
    content = ('---\n' + yaml.safe_dump(value, allow_unicode=True, sort_keys=False) + '---\n' + body).encode('utf-8')
    return [PackageFile(item.path, content, item.content_type) if item is readme else item for item in files]
