const vertex = `
  attribute vec2 corner;
  varying vec2 screenPoint;
  void main() {
    screenPoint = corner;
    gl_Position = vec4(corner, 0.0, 1.0);
  }
`;
const fragment = `
  precision highp float;
  varying vec2 screenPoint;
  uniform sampler2D frame;
  uniform vec2 viewport;
  uniform vec2 direction;
  uniform float halfSphere;
  uniform int stereo;
  const float PI = 3.14159265;
  void main() {
    // A 64-degree vertical field of view and a longitude centered on the source.
    vec3 ray = normalize(vec3(screenPoint * vec2(viewport.x / viewport.y, 1.0) * 0.625, 1.0));
    float pitch = direction.y;
    ray.yz = mat2(cos(pitch), -sin(pitch), sin(pitch), cos(pitch)) * ray.yz;
    float longitude = atan(ray.x, ray.z) + direction.x;
    longitude = mod(longitude + PI, 2.0 * PI) - PI;
    if (abs(longitude) > halfSphere) {
      gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0);
      return;
    }
    vec2 uv = vec2(0.5 + longitude / (2.0 * halfSphere), 0.5 + asin(ray.y) / PI);
    // In a conventional browser, preview the left eye of a stereo master.
    if (stereo == 1) uv.y = 0.5 + uv.y * 0.5;
    if (stereo == 2) uv.x *= 0.5;
    gl_FragColor = texture2D(frame, uv);
  }
`;
function renderer(canvas) {
    const gl = canvas.getContext("webgl", { alpha: false, antialias: false });
    if (!gl)
        throw new Error("WebGL is unavailable.");
    const shader = (kind, source) => {
        const result = gl.createShader(kind);
        if (!result)
            throw new Error("Unable to allocate a shader.");
        gl.shaderSource(result, source);
        gl.compileShader(result);
        if (!gl.getShaderParameter(result, gl.COMPILE_STATUS))
            throw new Error("Unable to compile a shader.");
        return result;
    };
    const program = gl.createProgram();
    const buffer = gl.createBuffer();
    const texture = gl.createTexture();
    if (!program || !buffer || !texture)
        throw new Error("Unable to allocate the panoramic renderer.");
    const vertexShader = shader(gl.VERTEX_SHADER, vertex);
    const fragmentShader = shader(gl.FRAGMENT_SHADER, fragment);
    gl.attachShader(program, vertexShader);
    gl.attachShader(program, fragmentShader);
    gl.linkProgram(program);
    gl.deleteShader(vertexShader);
    gl.deleteShader(fragmentShader);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS))
        throw new Error("Unable to link the panoramic renderer.");
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const corner = gl.getAttribLocation(program, "corner");
    gl.enableVertexAttribArray(corner);
    gl.vertexAttribPointer(corner, 2, gl.FLOAT, false, 0, 0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    const viewport = gl.getUniformLocation(program, "viewport");
    const direction = gl.getUniformLocation(program, "direction");
    const halfSphere = gl.getUniformLocation(program, "halfSphere");
    const stereo = gl.getUniformLocation(program, "stereo");
    const maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    return {
        upload(video) {
            if (video.videoWidth > maxTextureSize || video.videoHeight > maxTextureSize) {
                throw new Error("The video exceeds this browser’s panoramic texture limit.");
            }
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
            if (gl.getError() !== gl.NO_ERROR)
                throw new Error("Unable to use this video as a panoramic texture.");
        },
        draw(yaw, pitch, hemisphere, layout) {
            const ratio = Math.min(devicePixelRatio, 2);
            const width = Math.min(1920, Math.max(1, Math.round(canvas.clientWidth * ratio)));
            const height = Math.max(1, Math.round(width * canvas.clientHeight / Math.max(1, canvas.clientWidth)));
            if (canvas.width !== width || canvas.height !== height) {
                canvas.width = width;
                canvas.height = height;
            }
            gl.viewport(0, 0, width, height);
            gl.uniform2f(viewport, width, height);
            gl.uniform2f(direction, yaw, pitch);
            gl.uniform1f(halfSphere, hemisphere ? Math.PI / 2 : Math.PI);
            gl.uniform1i(stereo, layout);
            gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        },
    };
}
function connectPanorama(stage) {
    const video = stage.querySelector("video");
    const canvas = stage.querySelector("canvas");
    const captions = stage.querySelector("[data-panorama-captions]");
    const buttons = stage.parentElement.querySelector("[data-panorama-controls]");
    const toggle = buttons.querySelector("[data-panorama-toggle]");
    const reset = buttons.querySelector("[data-panorama-reset]");
    const status = buttons.querySelector("[role=status]");
    const player = stage.closest(".archive-player");
    const pip = player.querySelector("[data-control=pip]");
    const mode = stage.dataset.panorama || "";
    const layout = mode.endsWith("top-bottom") ? 1 : mode.endsWith("side-by-side") ? 2 : 0;
    const hemisphere = mode.startsWith("180");
    let surface;
    let ready = false;
    let active = false;
    let yaw = 0;
    let pitch = 0;
    let frameRequest;
    let pointer;
    function draw() {
        if (active && ready)
            surface?.draw(yaw, pitch, hemisphere, layout);
    }
    function showCaptions() {
        const text = [];
        for (const track of Array.from(video.textTracks)) {
            if (track.mode !== "showing")
                continue;
            for (const cue of Array.from(track.activeCues || [])) {
                if (cue instanceof VTTCue)
                    text.push(cue.getCueAsHTML().textContent || "");
            }
        }
        captions.textContent = text.join("\n");
        captions.hidden = !active || !text.length;
    }
    function update(enabled, message = "") {
        active = enabled;
        stage.dataset.active = String(enabled);
        canvas.hidden = !enabled;
        reset.disabled = !enabled;
        if (pip)
            pip.disabled = enabled;
        toggle.textContent = enabled ? "Watch flat" : "Explore panorama";
        status.textContent = message;
        showCaptions();
        if (!enabled && frameRequest !== undefined) {
            video.cancelVideoFrameCallback(frameRequest);
            frameRequest = undefined;
        }
    }
    function feed() {
        if (!active || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA)
            return;
        try {
            surface?.upload(video);
            ready = true;
            draw();
        }
        catch {
            update(false, "Panoramic rendering is unavailable. Flat playback is available.");
        }
    }
    function nextFrame() {
        if (!active || frameRequest !== undefined)
            return;
        frameRequest = video.requestVideoFrameCallback(() => {
            frameRequest = undefined;
            feed();
            nextFrame();
        });
    }
    function open() {
        try {
            surface ??= renderer(canvas);
            update(true);
            feed();
            nextFrame();
        }
        catch {
            update(false, "Panoramic rendering is unavailable. Flat playback is available.");
        }
    }
    function look(dx, dy) {
        yaw += dx;
        yaw = hemisphere ? Math.max(-Math.PI / 2, Math.min(Math.PI / 2, yaw))
            : ((yaw + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
        pitch = Math.max(-1.5, Math.min(1.5, pitch + dy));
        draw();
    }
    reset.onclick = () => { yaw = pitch = 0; draw(); };
    toggle.onclick = () => { if (active)
        update(false);
    else
        open(); };
    canvas.onkeydown = event => {
        const step = 0.18;
        switch (event.key) {
            case "ArrowLeft":
                look(-step, 0);
                break;
            case "ArrowRight":
                look(step, 0);
                break;
            case "ArrowUp":
                look(0, step);
                break;
            case "ArrowDown":
                look(0, -step);
                break;
            default: return;
        }
        event.preventDefault();
    };
    let x = 0;
    let y = 0;
    canvas.onpointerdown = event => {
        if (!event.isPrimary || event.button !== 0)
            return;
        pointer = event.pointerId;
        x = event.clientX;
        y = event.clientY;
        canvas.setPointerCapture(pointer);
    };
    canvas.onpointermove = event => {
        if (pointer !== event.pointerId)
            return;
        look((x - event.clientX) * 0.006, (event.clientY - y) * 0.006);
        x = event.clientX;
        y = event.clientY;
    };
    canvas.onlostpointercapture = () => { pointer = undefined; };
    if (player.dataset.context === "no")
        canvas.oncontextmenu = event => event.preventDefault();
    canvas.addEventListener("webglcontextlost", event => {
        event.preventDefault();
        ready = false;
        update(false, "Panoramic rendering is unavailable. Flat playback is available.");
    });
    canvas.addEventListener("webglcontextrestored", () => { surface = undefined; });
    new ResizeObserver(draw).observe(stage);
    for (const track of Array.from(video.textTracks))
        track.addEventListener("cuechange", showCaptions);
    video.textTracks.addEventListener("change", showCaptions);
    video.addEventListener("seeked", feed);
    video.addEventListener("loadeddata", feed);
    video.addEventListener("emptied", () => { ready = false; });
    buttons.hidden = false;
    open();
}
const stage = document.querySelector("[data-panorama]");
if (stage)
    connectPanorama(stage);
export {};
