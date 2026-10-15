"""Prepare only a local virtual environment, then open the camera viewer."""
from pathlib import Path
import shutil
import subprocess
import sys
import venv

BASE = Path(__file__).resolve().parent
CHECK = """import importlib.metadata as m, sys
from Crypto.Cipher import AES
assert sys.version_info >= (3, 11)
for spec in sys.argv[1:]:
    name, version = spec.split('==')
    assert m.version(name) == version
"""


def prepare(base=BASE):
    folder = base / '.venv'
    python = folder / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if not python.exists():
        print('Preparing the local Python environment…', flush=True)
        venv.EnvBuilder(with_pip=True).create(folder)
    requirements = base / 'requirements.txt'
    pins = [line.strip() for line in requirements.read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    if not pins or any(len(pin.split('==')) != 2 for pin in pins):
        raise ValueError('requirements.txt must contain exact version pins.')
    check = [str(python), '-I', '-c', CHECK, *pins]
    if subprocess.run(check, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        print('Installing local dependencies; internet access is required…', flush=True)
        subprocess.run([str(python), '-I', '-m', 'pip', '--isolated', 'install',
                        '--disable-pip-version-check', '--no-cache-dir', '-r', str(requirements)], check=True)
        subprocess.run(check, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return python


def main():
    if sys.version_info < (3, 11):
        print('Python 3.11 or newer is required: https://www.python.org/downloads/')
        return 1
    if not shutil.which('ffmpeg'):
        print('FFmpeg is missing from PATH. Install it and reopen the terminal: https://ffmpeg.org/download.html')
        return 1
    try:
        python = prepare()
        return subprocess.call([str(python), str(BASE / 'viewer.py')], cwd=BASE)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, subprocess.SubprocessError):
        print('Startup failed. Check internet access and your Python installation with venv/pip. '
              'If you moved the viewer to another computer, delete only the .venv folder and try again.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
