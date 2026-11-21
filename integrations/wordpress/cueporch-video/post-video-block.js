"use strict";
(() => {
    const wp = window.wp;
    const { registerBlockType } = wp.blocks;
    const { useBlockProps } = wp.blockEditor;
    const { useState, createElement: h } = wp.element;
    const { __ } = wp.i18n;
    const origin = window.cueporchPostSettings?.origin_wp ?? "";
    registerBlockType("cueporch/video", {
        apiVersion: 3,
        title: __("CuePorch Video", "cueporch-video"),
        category: "embed",
        attributes: { url: { type: "string", default: "" } },
        save: () => null,
        edit: ({ attributes, setAttributes }) => {
            const blockProps = useBlockProps();
            const [status, setStatus] = useState("");
            const open = () => {
                if (!origin)
                    return;
                window.cueporchPickVideo(origin, (url) => setAttributes({ url }), setStatus);
            };
            return h("div", blockProps, h("label", null, __("Video URL", "cueporch-video"), h("input", {
                type: "text",
                value: attributes.url ?? "",
                onChange: (event) => {
                    const target = event.target;
                    setAttributes({ url: target.value });
                },
            })), h("button", { type: "button", onClick: open, disabled: !origin }, __("Choose from CuePorch", "cueporch-video")), h("p", { role: "status" }, status));
        },
    });
})();
