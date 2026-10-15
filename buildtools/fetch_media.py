"""Fetch and verify the exact source archives used by every native build."""
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    pins = json.loads((ROOT/'buildtools/media-sources.json').read_text())
    downloads, sources = ROOT/'.build/downloads', ROOT/'.build/source'
    downloads.mkdir(parents=True, exist_ok=True)
    sources.mkdir(parents=True, exist_ok=True)
    for name, pin in pins.items():
        target = downloads/pin['filename']
        if not target.exists():
            request = urllib.request.Request(pin['url'], headers={'User-Agent': 'CtronicsViewer-build'})
            with urllib.request.urlopen(request, timeout=120) as response:
                target.write_bytes(response.read())
        if hashlib.sha256(target.read_bytes()).hexdigest() != pin['sha256']:
            raise ValueError('Source checksum mismatch: '+name)
        destination = sources/name
        if destination.exists():
            shutil.rmtree(destination)
        temporary = sources/(name+'-extract')
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir()
        with tarfile.open(target) as archive:
            archive.extractall(temporary, filter='data')
        entries = list(temporary.iterdir())
        assert len(entries) == 1 and entries[0].is_dir()
        entries[0].rename(destination)
        temporary.rmdir()
        print('Verified source:', name, pin['sha256'], flush=True)


if __name__ == '__main__':
    main()
