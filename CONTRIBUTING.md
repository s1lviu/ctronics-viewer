# Contributing

Keep the viewer small: browser APIs, the Python standard library, PyCryptodome and FFmpeg. Reuse existing helpers before adding dependencies.

Run the checks described in [README.md](README.md) before submitting a change. Protocol changes should include a synthetic regression fixture. Keep tests independent of real cameras and vendor services.

For bug reports, include your OS, browser, Python and FFmpeg versions, the camera model/firmware, the UID prefix and a description of the failure. Replace the UID serial and suffix with placeholders. Do not post passwords, complete UIDs, session URLs, private camera images or unredacted packet captures.

Contributions are licensed under the repository's MIT license. Preserve third-party notices.
