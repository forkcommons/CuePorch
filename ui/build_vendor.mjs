import {copyFile, mkdir} from "node:fs/promises";

const source = new URL("node_modules/hls.js/", import.meta.url);
const output = new URL("../static/vendor/", import.meta.url);
await mkdir(output, {recursive: true});
await copyFile(new URL("dist/hls.min.js", source), new URL("hls.min.js", output));
await copyFile(new URL("LICENSE", source), new URL("hls-LICENSE.txt", output));
