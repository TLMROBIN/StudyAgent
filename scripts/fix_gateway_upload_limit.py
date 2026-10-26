#!/usr/bin/env python3
"""Align only the school gateway's StudyAgent routes with nginx/nginx.conf.

Run on 159.62 with sudo. Validates nginx before reload and restores on failure.
"""
from pathlib import Path
import shutil
import subprocess
from datetime import datetime


def patch_config(source: str) -> str:
    for route in ('/studyagent/', '/api/'):
        marker = f'    location {route} {{'
        if marker not in source:
            raise ValueError(f'Missing expected StudyAgent route: {route}')
        start = source.index(marker)
        end = source.index('\n    }', start)
        block = source[start:end]
        if 'proxy_pass http://10.50.159.63:8080' not in block:
            raise ValueError(f'Unexpected upstream for {route}')
        import re
        if 'client_max_body_size' in block:
            block = re.sub(r'client_max_body_size\s+[^;]+;', 'client_max_body_size 64m;', block)
        else:
            block = block.replace(marker, marker + '\n        client_max_body_size 64m;', 1)
        source = source[:start] + block + source[end:]
    return source


def main():
    config = Path('/etc/nginx/sites-enabled/school-platform.conf').resolve()
    original = config.read_text()
    updated = patch_config(original)
    if original == updated:
        print('StudyAgent gateway upload limit is already 64m')
        return
    backup = Path('/etc/nginx/codex-backups') / f'school-platform.before-studyagent-upload-{datetime.now():%Y%m%dT%H%M%S}'
    backup.parent.mkdir(exist_ok=True)
    shutil.copy2(config, backup)
    try:
        config.write_text(updated)
        subprocess.run(['/usr/sbin/nginx', '-t'], check=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
    except Exception:
        shutil.copy2(backup, config)
        raise
    print(f'StudyAgent upload limit: 64m; backup: {backup}')


if __name__ == '__main__':
    main()
