"""Create a portable, native PyInstaller folder with its licenses and FFmpeg."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main(version, target):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid release version')
    if target not in ('windows-x86_64', 'linux-x86_64', 'macos-arm64', 'macos-x86_64'):
        raise ValueError('Unsupported target')
    machine = {'amd64': 'x86_64', 'aarch64': 'arm64'}.get(platform.machine().lower(), platform.machine().lower())
    assert machine == ('arm64' if target.endswith('arm64') else 'x86_64'), 'Runner architecture mismatch'
    assert platform.system() == ('Windows' if target.startswith('windows') else 'Darwin' if target.startswith('macos') else 'Linux')
    executable = ROOT/'.build/media/bin'/('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
                    '--name', 'CtronicsViewer', '--distpath', str(ROOT/'.build/dist'),
                    '--workpath', str(ROOT/'.build/pyinstaller'), '--specpath', str(ROOT/'.build'),
                    '--add-data', str(ROOT/'index.html')+os.pathsep+'.',
                    '--add-binary', str(executable)+os.pathsep+'.',
                    '--copy-metadata', 'pycryptodome', str(ROOT/'viewer.py')], check=True, cwd=ROOT)
    output = ROOT/'dist'
    output.mkdir(exist_ok=True)
    name = f'ctronics-viewer-{version}-{target}'
    folder = output/name
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(ROOT/'.build/dist/CtronicsViewer', folder, symlinks=True)
    licenses = folder/'licenses'
    licenses.mkdir()
    for source, dest in [(ROOT/'LICENSE', 'PROJECT.txt'), (ROOT/'THIRD_PARTY.md', 'THIRD_PARTY.md'),
                         (ROOT/'.build/source/ffmpeg/COPYING.GPLv2', 'FFMPEG-GPLv2.txt'),
                         (ROOT/'.build/source/x264/COPYING', 'X264-GPLv2.txt')]:
        shutil.copy2(source, licenses/dest)
    for name_dist in ('pycryptodome', 'pyinstaller'):
        distribution = importlib.metadata.distribution(name_dist)
        for item in distribution.files or []:
            if any(word in item.name.lower() for word in ('license', 'copying', 'notice')):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    shutil.copy2(source, licenses/(name_dist+'-'+str(item).replace('/', '_').replace('\\', '_')))
    shutil.copytree(ROOT/'buildtools/runtime-notices', licenses/'runtime')
    if os.name == 'nt':
        shutil.copy2(Path(sys.base_prefix)/'LICENSE.txt', licenses/'PYTHON-DISTRIBUTION.txt')
    elif sys.platform.startswith('linux'):
        for component in ('libbz2-1.0', 'libffi8', 'liblzma5', 'libssl3', 'zlib1g'):
            shutil.copy2(Path('/usr/share/doc')/component/'copyright', licenses/(component+'-COPYRIGHT.txt'))
    python_version = platform.python_version()
    for url, dest in [(f'https://raw.githubusercontent.com/python/cpython/v{python_version}/LICENSE', 'PYTHON.txt'),
                      (f'https://docs.python.org/{sys.version_info.major}.{sys.version_info.minor}/license.html', 'PYTHON-ACKNOWLEDGMENTS.html')]:
        with urllib.request.urlopen(url, timeout=60) as response:
            (licenses/dest).write_bytes(response.read())
    (folder/'SOURCE.txt').write_text(
        f'FFmpeg and x264 corresponding source, configure/build scripts, and viewer source:\n'
        f'https://github.com/s1lviu/ctronics-viewer/releases/tag/v{version}\n'
        f'Asset: ctronics-viewer-{version}-corresponding-source.tar.gz\n'
        f'The bundled FFmpeg/x264 executable is GPL-2.0-or-later. See licenses/.\n',
        encoding='utf-8')
    entry = 'CtronicsViewer.exe' if os.name == 'nt' else 'CtronicsViewer'
    if os.name != 'nt':
        launcher = folder/('Launch.command' if sys.platform == 'darwin' else 'Launch.sh')
        launcher.write_text('#!/bin/sh\ncd -- "$(dirname "$0")"\nexec ./CtronicsViewer\n')
        launcher.chmod(0o755)
    (folder/'START-HERE.txt').write_text(
        f'Ctronics Viewer {version}\n\n'
        f'Extract the entire archive before launching {entry}.\n'
        'On macOS you can double-click Launch.command. On Linux run ./Launch.sh.\n'
        'Python and FFmpeg are included; no separate installation is needed.\n'
        'Keep the _internal folder beside the executable.\n'
        'The viewer opens your browser. Enter your camera UID, username and password.\n'
        'Close this console window or press Ctrl+C to exit the application.\n'
        'These builds are unsigned; Windows/macOS may require first-launch OS approval.\n'
        'Source, licenses and known compatibility limits are on the release page.\n',
        encoding='utf-8')
    info = {'version': version, 'target': target, 'python': python_version,
            'pyinstaller': importlib.metadata.version('pyinstaller'),
            'pycryptodome': importlib.metadata.version('pycryptodome'),
            'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'media_sources': json.loads((ROOT/'buildtools/media-sources.json').read_text()),
            'ffmpeg_build': subprocess.check_output([str(executable), '-buildconf'], stderr=subprocess.STDOUT, text=True),
            'binary_sha256': hashlib.sha256((folder/entry).read_bytes()).hexdigest()}
    (folder/'BUILDINFO.json').write_text(json.dumps(info, indent=2)+'\n', encoding='utf-8')
    print(folder, flush=True)


if __name__ == '__main__':
    main(*sys.argv[1:])
