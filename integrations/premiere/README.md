# CuePorch Review for Premiere Pro

CuePorch 0.3.0 development source includes a UXP panel for importing open CuePorch review threads as markers in Adobe Premiere Pro 25.6 or newer.

## Prepare a development copy

From the CuePorch repository root, install the UI dependencies and compile the panel with the same TypeScript compiler as the CuePorch browser interface.

```sh
npm --prefix ui ci --ignore-scripts --no-audit
node ui/node_modules/typescript/bin/tsc --strict --target ES2022 --module commonjs --outDir build/premiere-panel integrations/premiere/panel.ts
node -e "for (const f of ['manifest.json','index.html']) require('node:fs').copyFileSync('integrations/premiere/'+f,'build/premiere-panel/'+f)"
```

Open the generated manifest in Adobe UXP Developer Tool and load it into Premiere with developer mode enabled. The panel appears in Premiere’s **Window > UXP Plugins** menu. [Adobe development setup](https://developer.adobe.com/premiere-pro/uxp/plugins/)

## Import the feedback

1. In CuePorch, open the video’s **Review link** dialog and select **Comments** on the relevant link.
2. Choose the video version and select **Download Premiere markers**.
3. In Premiere, activate the matching sequence. The export uses elapsed time from the first frame of the reviewed video. Use the matching complete sequence so those positions line up.
4. In **CuePorch Review**, select **Import review markers** and open the downloaded JSON file.

The panel imports each open top-level thread as a Comment marker and reports how many markers it added or skipped. Marker import is one undoable action. Reimporting the same comments keeps any existing imported markers in place. The panel reads the chosen local file under Adobe’s file picker permission. The exported file includes the author and original text and should be handled with the same care as the review itself.

The CuePorch browser and a simulated Premiere host exercise the workflow in source acceptance. Adobe development loading and marketplace distribution are pending.
