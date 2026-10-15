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
        subprocess.run([str(entry), '--self-test'], env=env, check=True, timeout=30)
        ffmpeg = relocated/'_internal'/('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
        for codec in ('h264', 'hevc'):
            movie = Path(temporary)/(codec+'.mp4')
            subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-fflags', '+genpts',
                            '-r', '15', '-f', codec, '-i', str(ROOT/'tests/fixtures'/('video.'+codec)),
                            '-an', '-c:v', 'copy' if codec == 'h264' else 'libx264',
                            '-movflags', 'empty_moov+default_base_moof+frag_keyframe',
                            '-f', 'mp4', str(movie)], env=env, check=True, timeout=30)
            result = subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-xerror',
                                     '-i', str(movie), '-map', '0:v:0', '-progress', 'pipe:1',
                                     '-f', 'null', '-'], env=env, capture_output=True, check=True, timeout=30)
            frames = [int(line.split('=')[1]) for line in result.stdout.decode().splitlines() if line.startswith('frame=')]
            assert frames[-1] == 45, (codec, frames)
            print('Bundled video round trip passed:', codec, flush=True)


if __name__ == '__main__':
    main(Path(sys.argv[1]))
