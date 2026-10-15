"""Check the relocated native bundle with Python/FFmpeg absent from PATH."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main(folder):
    with tempfile.TemporaryDirectory(prefix='viewer portable check ') as temporary:
        extracted = Path(temporary)/'application with spaces'
        extracted.mkdir()
        if folder.is_dir():
            relocated = extracted/folder.name
            shutil.copytree(folder, relocated, symlinks=True)
        else:
            if folder.name.endswith('.tar.gz'):
                with tarfile.open(folder) as archive:
                    archive.extractall(extracted, filter='data')
            elif sys.platform == 'darwin':
                subprocess.run(['ditto', '-x', '-k', str(folder), str(extracted)], check=True)
            else:
                with zipfile.ZipFile(folder) as archive:
                    archive.extractall(extracted)
            entries = list(extracted.iterdir())
            assert len(entries) == 1 and entries[0].is_dir()
            relocated = entries[0]
        env = dict(os.environ)
        env['PATH'] = str(Path(env.get('SystemRoot', 'C:/Windows'))/'System32') if os.name == 'nt' else '/usr/bin:/bin'
        entry = relocated/('CtronicsViewer.exe' if os.name == 'nt' else 'CtronicsViewer')
        if sys.platform == 'darwin':
            assert not (relocated/'_internal').exists(), 'Mac runtime must be embedded to avoid separate library quarantine'
            assert not list(relocated.rglob('*.framework')) and not list(relocated.rglob('*.so'))
            subprocess.run(['codesign', '--verify', '--strict', '--verbose=2', str(entry)], check=True)
        subprocess.run([str(entry), '--self-test', str(ROOT/'tests/fixtures')],
                       env=env, check=True, timeout=90)


if __name__ == '__main__':
    main(Path(sys.argv[1]))
