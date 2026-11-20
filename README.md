# CuePorch by ForkCommons
Version 0.2.0 · Windows, macOS and Linux

Project site source: [docs/index.html](docs/index.html). Plain static HTML and CSS with no build step. To preview it, run `python -m http.server -d docs` and open http://localhost:8000/. It is written to be served by GitHub Pages from `/docs` on the default branch.

CuePorch preserves and publishes video for public media stations and arts institutions. TypeScript drives the operator, creator and screening interfaces. Python owns the institutional service, Vimeo importer, grants and publication queue. The Rust desktop launcher starts the bundled service and opens your browser. PeerTube provides public hosting. CivicCast is optional.

## Release baseline

The installation and usage instructions below describe the public 0.2.0 release. This branch contains later development work.

## Windows installation
Double-click the signed Setup EXE, check the publisher is Scott Converse, and follow the wizard. Keep the suggested video library folder for a new institution. Open CuePorch from the Start menu. Enter your institution name and select **Save setup**, then **Open operator console**. To publish online, enter the HTTPS address and publishing credentials of an existing PeerTube host. Local library and exhibition workflows can start before a host is connected. A Windows desktop installation includes Python and FFmpeg. To provision a local video host, start a Docker-compatible Linux container engine and select **Set up a local video host**. Public hosting requires institution-controlled domains and HTTPS.

## Upgrade an existing institution
Stop the previous version and back up its complete private data directory and video host. In Windows Setup select the existing folder containing operator.json, institution.sqlite and collection. The application reuses the keys, routes, jobs, grants and media in that directory. Reconnect the same PeerTube destination when prompted. Keep the backup for rollback. Uninstall removes program files separately from the library.

## macOS and Linux
Install the native macOS package and open CuePorch in Applications, or install the Debian package and launch CuePorch from the applications menu. The Mac package uses an ad-hoc application signature and has no Apple notarization. The native launcher accepts --data PATH for a previous library and --headless for unattended runs. macOS defaults to ~/Library/Application Support/ForkCommons/CuePorch and Linux to $XDG_DATA_HOME/cueporch (normally ~/.local/share/cueporch).

## Use
In **Move from Vimeo**, preview the owner account, transfer privately, then load the library and publish eligible imports. Supply the actual account owner's token when migrating from Vimeo. Check retrieval quality, captions, visibility and missing files before replacing embeds. For a museum loan, submit a private master, select its screening derivative and issue a separate timed grant for each exhibitor. The nine human approval switches default to automatic. Use **Quit CuePorch** on the desktop page before updating or backing up. Keep the private library folder and credentials outside the program installation folder.

## Build
Install Python 3.13, Rust, Node.js with npm, the dependencies in requirements.txt, PyInstaller 6.17.0 and imageio-ffmpeg 0.6.0. Run python packaging/build.py build and then python packaging/build.py package on the target OS. Windows uses Inno Setup 6. The release process signs and verifies the native executables and the setup program through Scott Converse’s established Azure signing workflow before distributing them. The v0.1.0 tag preserves the prior runnable source.

CuePorch is licensed under AGPL-3.0-or-later. The bundled FFmpeg binary comes from the platform-specific imageio-ffmpeg 0.6.0 distribution. See the [upstream source and build information](https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0) and the [FFmpeg distribution project](https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0/binaries). Other components retain their respective licenses.

## Documentation and package tests

The [user manual](USER-MANUAL.md) covers station imports, exhibitions, review settings and system architecture. The [operations guide](OPERATIONS.md) covers public deployments and recovery. The [installed test record](evidence/ACCEPTANCE.md) records migration, upgrades and playback for the three native packages. The [Vimeo feature comparison](evidence/vimeo-comparison.md) records remaining gaps.
