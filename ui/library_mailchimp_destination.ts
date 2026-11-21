import {
  formFields,
  loadFieldPages,
  readMapping,
  renderMapping,
  type FormSettings,
  type MailchimpRequest,
} from "./mailchimp_registration_fields.js";

export async function libraryMailchimpDestination(
  request: MailchimpRequest,
  form: FormSettings,
  audience: string,
  preferred: Record<string, string>,
  remove: () => void,
) {
  const element = document.createElement("fieldset");
  element.dataset.mailchimpAudience = audience;

  const legend = document.createElement("legend");
  const mapping = document.createElement("div");
  const receipt = document.createElement("p");
  const removeButton = document.createElement("button");
  removeButton.type = "button";
  removeButton.textContent = "Remove audience";
  removeButton.addEventListener("click", remove);
  element.append(legend, mapping, receipt, removeButton);

  let loaded: Awaited<ReturnType<typeof loadFieldPages>>;
  try {
    loaded = await loadFieldPages(request, audience);
  } catch (caught) {
    const error = caught instanceof Error ? caught : new Error(String(caught));
    legend.textContent = audience;
    receipt.textContent = `Could not load this Mailchimp audience: ${error.message}`;
    return {
      element,
      payload(): never {
        throw error;
      },
      updateCounts(): void {},
    };
  }

  const name = loaded.audienceName || audience;
  legend.textContent = name;
  const fields = formFields(form);
  renderMapping(mapping, fields, loaded.fields, preferred);

  function payload() {
    if (loaded.marketingPermissions) throw new Error("This audience requires marketing permission mapping.");
    const values = readMapping(mapping);
    const tags = Object.values(values).filter(Boolean);
    if (tags.length > 20) throw new Error("Map up to 20 fields per audience.");
    if (new Set(tags).size !== tags.length) throw new Error("Each Mailchimp tag can only be mapped once.");
    const missing = loaded.fields
      .filter((field) => field.required && !tags.includes(field.tag))
      .map((field) => field.name);
    if (missing.length) throw new Error(`Map required fields: ${missing.join(", ")}.`);
    return { audience, mapping: values };
  }

  function updateCounts(states: Record<string, number>): void {
    receipt.textContent = Object.entries(states)
      .map(([label, count]) => `${label}: ${count}`)
      .join(" · ");
  }

  return { element, payload, updateCounts };
}
