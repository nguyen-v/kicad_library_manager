#!/usr/bin/env python3
"""Build the PCM payload used for local testing and tagged releases."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from library_manager import __version__

def build(output: Path, *, status: str = 'testing', tag: str | None = None,
          schemas: Path | None = None) -> Path:
    if not re.fullmatch(r'\d+\.\d+\.\d+', __version__):
        raise ValueError('Expected a MAJOR.MINOR.PATCH source version')
    if tag is not None and tag != 'v' + __version__:
        raise ValueError(f'Tag must match source version v{__version__}')
    if status not in ('testing', 'stable'):
        raise ValueError('Package status must be testing or stable')
    metadata = json.loads((ROOT / 'pcm/metadata.in-zip.json').read_text())
    metadata['versions'] = [{
        'version': __version__, 'status': status, 'runtime': 'ipc',
        'kicad_version': '9.0', 'kicad_version_max': '10.99',
        'platforms': ['linux', 'windows', 'macos'],
    }]
    manifest = json.loads((ROOT / 'plugin.json').read_text())
    if metadata['identifier'] != manifest['identifier']:
        raise ValueError('IPC and PCM package identifiers must match')
    if schemas:
        import jsonschema
        for document, name in ((metadata, 'pcm'), (manifest, 'api')):
            schema = json.loads((schemas / f'{name}.v1.schema.json').read_text())
            jsonschema.Draft7Validator(schema).validate(document)

    payload = {
        'metadata.json': ((json.dumps(metadata, indent=2, ensure_ascii=False) + '\n').encode(), 0o644),
        'resources/icon.png': ((ROOT / 'pcm/icon.png').read_bytes(), 0o644),
    }
    files = [ROOT / name for name in (
        'README.md', 'CHANGELOG.md', 'plugin.json', 'run_library_manager.py', 'requirements.txt', 'pcm/icon.png',
    )]
    # Include nested scaffold .github/workflows: initialization needs these files.
    # Tests, release tooling and repository-level workflows are not plugin content.
    for folder in ('library_manager', 'kicad_footprint_generator'):
        files.extend(path for path in (ROOT / folder).rglob('*') if path.is_file())
    files.extend((ROOT / 'scripts').glob('*.sh'))
    for file in files:
        if '__pycache__' in file.parts or file.suffix in ('.pyc', '.pyo'):
            continue
        if file.is_symlink():
            raise ValueError(f'Symlink in payload: {file}')
        relative = file.relative_to(ROOT).as_posix()
        # Preserve executable setup scripts consistently across build platforms.
        mode = 0o755 if file.suffix == '.sh' else 0o644
        payload[f'plugins/kicad_library_manager/{relative}'] = (file.read_bytes(), mode)
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / f'kicad-library-manager-{__version__}.zip'
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, (value, mode) in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, value)
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schemas', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--status', choices=('testing', 'stable'), default='testing')
    parser.add_argument('--tag')
    args = parser.parse_args()
    archive = build(args.output, status=args.status, tag=args.tag, schemas=args.schemas)
    print(f'Built {args.status} package {archive}; no release or upload performed')


if __name__ == '__main__':
    main()
