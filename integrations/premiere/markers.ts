export type ReviewMarker = {
  id: string;
  seconds: number;
  author: string;
  body: string;
};

const MAX_TEXT_LENGTH = 4_000_000;
const MAX_MARKER_COUNT = 5_000;
const ID_PATTERN = /^[0-9a-f]{32}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function fail(message: string): never {
  throw new Error(message);
}

export function parseReviewMarkers(text: string): ReviewMarker[] {
  if (typeof text !== 'string') {
    fail('Input must be a string.');
  }

  if (text.length > MAX_TEXT_LENGTH) {
    fail(`Input is too long. Maximum is ${MAX_TEXT_LENGTH} characters.`);
  }

  let input = text;
  if (input.charCodeAt(0) === 0xfeff) {
    input = input.slice(1);
  }

  let root: unknown;
  try {
    root = JSON.parse(input);
  } catch {
    fail('Input is not valid JSON.');
  }

  if (!isRecord(root)) {
    fail('Root must be an object.');
  }

  if (root.schema !== 1) {
    fail('schema must be 1.');
  }

  if (typeof root.version !== 'string' || root.version.length === 0) {
    fail('version must be a nonempty string.');
  }

  if (!Array.isArray(root.markers)) {
    fail('markers must be an array.');
  }

  if (root.markers.length > MAX_MARKER_COUNT) {
    fail(`markers must contain at most ${MAX_MARKER_COUNT} items.`);
  }

  const seen = new Set<string>();
  const result: ReviewMarker[] = [];

  for (let i = 0; i < root.markers.length; i++) {
    const raw = root.markers[i];

    if (!isRecord(raw)) {
      fail(`markers[${i}] must be an object.`);
    }

    if (Object.prototype.hasOwnProperty.call(raw, 'link')) {
      fail(`markers[${i}] must not have a link property.`);
    }

    const { id, seconds, author, body } = raw;

    if (typeof id !== 'string' || !ID_PATTERN.test(id)) {
      fail(`markers[${i}].id must be 32 lowercase hex characters.`);
    }

    if (
      typeof seconds !== 'number' ||
      !Number.isFinite(seconds) ||
      seconds < 0
    ) {
      fail(`markers[${i}].seconds must be a finite number >= 0.`);
    }

    if (typeof author !== 'string') {
      fail(`markers[${i}].author must be a string.`);
    }

    if (typeof body !== 'string') {
      fail(`markers[${i}].body must be a string.`);
    }

    if (seen.has(id)) {
      fail(`Duplicate marker id: ${id}.`);
    }

    seen.add(id);
    result.push({ id, seconds, author, body });
  }

  return result;
}
