# Native release builds

End users should use the downloads on the Releases page. The following steps are for building the application.

The manual Native release builds workflow produces Windows x64, Linux x64, macOS Apple Silicon and macOS Intel archives. It builds FFmpeg and x264 from the SHA-256-pinned archives in buildtools/media-sources.json, then freezes the Python application with PyInstaller.

Each archive is extracted to a directory containing spaces and tested with Python and FFmpeg absent from PATH. Checks cover the packaged HTML/server, AES extension, bundled media codecs, and H.264/H.265 conversion followed by decoding all 45 synthetic frames.

macOS uses PyInstaller onefile packaging so Python libraries are embedded rather than distributed as separately quarantined files. Archive checks verify the executable signature and reject external Python frameworks/modules. These checks do not certify Gatekeeper approval: the builds lack Developer ID signing/notarization and still require the operating system's first-launch approval after browser downloads.

## Build requirements

Python 3.13, a C toolchain, make, pkg-config and NASM are needed for source builds. Windows uses MSYS2 UCRT64; macOS uses the Xcode command-line tools. The workflow installs its build requirements automatically.

From a source checkout:

    python -m pip install -r requirements-build.txt
    python buildtools/fetch_media.py
    bash buildtools/build_media.sh
    python buildtools/bundle.py 0.1.1 linux-x86_64
    python buildtools/archive_bundle.py dist/ctronics-viewer-0.1.1-linux-x86_64
    python buildtools/smoke_bundle.py dist/ctronics-viewer-0.1.1-linux-x86_64.tar.gz

Use the matching target name on Windows or macOS. Run the media build inside the MSYS2 UCRT64 shell on Windows, then run the packaging steps with native Windows Python.

## Corresponding source and notices

The corresponding-source release archive contains the complete original FFmpeg and x264 source archives and this project's exact source/build recipes. The same release provides the binary archives. Each binary archive includes a SOURCE.txt link, GPL notices for FFmpeg/x264, Python's license/acknowledgments, package notices and BUILDINFO.json with versions, source checksums, configuration and the project commit.

The Python application source remains MIT. The separately invoked bundled FFmpeg/x264 executable is GPL-2.0-or-later. No vendor camera executable or library is included.

These builds are unsigned. There is no signing/notarization certificate configured in this repository.
