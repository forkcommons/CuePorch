type WpElement = {
  useState: <T>(initial: T) => [T, (value: T) => void];
  createElement: (
    type: string,
    props?: Record<string, unknown> | null,
    ...children: unknown[]
  ) => unknown;
};
type WpRuntime = {
  blocks: {
    registerBlockType: (
      name: string,
      settings: Record<string, unknown>,
    ) => void;
  };
  blockEditor: { useBlockProps: () => Record<string, unknown> };
  element: WpElement;
  i18n: { __: (text: string, domain: string) => string };
};
interface Window {
  cueporchPostSettings?: { origin_wp: string };
  wp: WpRuntime;
}
type BlockAttributes = { url: string };
type SetAttributes = (attrs: Partial<BlockAttributes>) => void;
type EditProps = { attributes: BlockAttributes; setAttributes: SetAttributes };
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
    edit: ({ attributes, setAttributes }: EditProps) => {
      const blockProps = useBlockProps();
      const [status, setStatus] = useState("");
      const open = () => {
        if (!origin) return;
        window.cueporchPickVideo(
          origin,
          (url: string) => setAttributes({ url }),
          setStatus,
        );
      };
      return h(
        "div",
        blockProps,
        h(
          "label",
          null,
          __("Video URL", "cueporch-video"),
          h("input", {
            type: "text",
            value: attributes.url ?? "",
            onChange: (event: Event) => {
              const target = event.target as HTMLInputElement;
              setAttributes({ url: target.value });
            },
          }),
        ),
        h(
          "button",
          { type: "button", onClick: open, disabled: !origin },
          __("Choose from CuePorch", "cueporch-video"),
        ),
        h("p", { role: "status" }, status),
      );
    },
  });
})();
