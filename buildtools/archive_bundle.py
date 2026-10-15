"""Archive only a successfully smoke-tested native bundle."""
from pathlib import Path
import shutil
import subprocess
import sys

folder = Path(sys.argv[1]).resolve()
if sys.platform == 'darwin':
    target = str(folder)+'.zip'
    subprocess.run(['ditto', '-c', '-k', '--keepParent', str(folder), target], check=True)
    print(target)
else:
    kind = 'gztar' if 'linux-' in folder.name else 'zip'
    print(shutil.make_archive(str(folder), kind, root_dir=folder.parent, base_dir=folder.name))
