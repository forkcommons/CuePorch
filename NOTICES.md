# Source and notices

**CuePorch by ForkCommons · Version 0.1.0**

ForkCommons offers the CuePorch software in this directory under the GNU Affero General Public License, version 3 or later. The complete license is in LICENSE. The LPM media, captions, test data from third parties and linked documentation retain their respective rights. An operator should distribute software and demonstration media separately.

The deployment runs the upstream PeerTube 8.3.2 image by digest. PeerTube is developed by Framasoft and contributors under the GNU Affero General Public License. Its corresponding source and build instructions are available at https://github.com/Chocobozzz/PeerTube/tree/v8.3.2. PostgreSQL and Redis run as separate upstream images. The Python dependencies are installed from their named upstream packages. Retain their distributed notices when packaging dependencies. FFmpeg is supplied by the operator.

CuePorch reads the public Vimeo API. The simulated source used in acceptance is a ForkCommons fixture. The optional CivicCast destination works with the station installation. No Vimeo branding or customer account data ships with CuePorch.
