"""Publish complete media source archives together with the exact build recipes."""
from pathlib import Path
import re
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
version = sys.argv[1]
assert re.fullmatch(r'\d+\.\d+\.\d+', version)
folder = root/'.build'/f'ctronics-viewer-{version}-corresponding-source'
folder.mkdir(parents=True, exist_ok=True)
project = folder/'viewer-and-build-scripts'
project.mkdir(exist_ok=True)
for name in subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0'):
    if name:
        destination = project/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/name, destination)
shutil.copytree(root/'.build/downloads', folder/'media-source-archives', dirs_exist_ok=True)
(root/'dist').mkdir(exist_ok=True)
print(shutil.make_archive(str(root/'dist'/folder.name), 'gztar', root_dir=folder.parent, base_dir=folder.name))
