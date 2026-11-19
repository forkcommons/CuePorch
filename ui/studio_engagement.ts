import {
  setupProducerEngagement, type Engagement, type EngagementProjection, type GraphicKind, type Poll, type Question,
} from "./event_engagement.js";

interface Stage {
  id: string;
  brand?: {primary: string; text: string};
}

type Graphic = {kind: "qa"; item: Question} | {kind: "poll"; item: Poll};

function wrap(context: CanvasRenderingContext2D, value: string, width: number, limit: number) {
  const lines: string[] = [];
  let line = "";
  for (const word of value.trim().split(/\s+/)) {
    const candidate = line ? line + " " + word : word;
    if (line && context.measureText(candidate).width > width) {
      lines.push(line);
      line = word;
    } else {
      line = candidate;
    }
  }
  if (line) lines.push(line);
  if (lines.length <= limit) return lines;
  return [...lines.slice(0, limit - 1), lines.slice(limit - 1).join(" ")];
}

function paint(context: CanvasRenderingContext2D, value: Graphic, brand: Stage["brand"]) {
  const {width, height} = context.canvas;
  const boxWidth = width * .63;
  const x = width * .038;
  const padding = height * .022;
  const inner = boxWidth - padding * 2;
  const textX = x + padding;
  const eyebrow = value.kind === "qa"
    ? "Audience question · " + (value.item.name || "Anonymous")
    : value.item.state === "results" ? "Poll results" : "Audience poll";
  const choices = value.kind === "poll" ? value.item.choices : [];
  const poll = value.kind === "poll" && value.item.state === "results" ? value.item : undefined;
  const votes = poll?.counts?.reduce((left, right) => left + right, 0) ?? 0;
  const titleHeight = height * .047;
  const optionHeight = height * .052;
  context.save();
  context.textAlign = "left";
  context.textBaseline = "middle";
  context.font = `650 ${Math.round(height * .037)}px system-ui`;
  const title = wrap(context, value.item.question, inner, 3);
  const total = padding * 2 + height * .041 + title.length * titleHeight + optionHeight * choices.length;
  const top = height * .90 - total;
  context.fillStyle = brand?.primary || "#164b49";
  context.fillRect(x, top, boxWidth, total);
  context.fillStyle = brand?.text || "#fff";
  let y = top + padding + height * .016;
  context.font = `500 ${Math.round(height * .026)}px system-ui`;
  context.fillText(eyebrow, textX, y, inner);
  context.font = `650 ${Math.round(height * .037)}px system-ui`;
  y += height * .042;
  for (const line of title) {
    context.fillText(line, textX, y, inner);
    y += titleHeight;
  }
  context.font = `500 ${Math.round(height * .027)}px system-ui`;
  for (const [index, choice] of choices.entries()) {
    const portion = votes ? (poll?.counts?.[index] ?? 0) / votes : 0;
    const name = (index + 1) + ". " + choice + (poll?.correct === index ? " · Correct" : "");
    if (poll) {
      context.globalAlpha = .28;
      context.fillRect(textX, y - height * .020, inner * portion, height * .042);
      context.globalAlpha = 1;
    }
    context.fillText(name, textX + height * .006, y, inner - (poll ? height * .089 : height * .012));
    if (poll) {
      context.textAlign = "right";
      context.fillText(Math.round(portion * 100) + "%", textX + inner - height * .009, y);
      context.textAlign = "left";
    }
    y += optionHeight;
  }
  context.restore();
}

class StudioProjection implements EngagementProjection {
  private readonly selected = new Map<string, string>();
  private readonly graphics = new Map<string, Graphic>();

  constructor(private stage: () => Stage) {}

  sync(state?: Engagement) {
    this.graphics.clear();
    if (!state) return;
    if (state.qa.policy.enabled) {
      for (const item of state.qa.questions) {
        if (!item.archived && (item.state === "published" || item.state === "answered")) {
          this.graphics.set("qa:" + item.id, {kind: "qa", item});
        }
      }
    }
    for (const item of state.polls) {
      if (item.state === "open" || item.state === "results") this.graphics.set("poll:" + item.id, {kind: "poll", item});
    }
    for (const [scene, key] of this.selected) {
      if (!this.graphics.has(key)) this.selected.delete(scene);
    }
  }

  revision() {
    const scene = this.stage().id;
    return JSON.stringify([scene, this.selected.get(scene)]);
  }

  shown(kind: GraphicKind, id: string) {
    return this.selected.get(this.stage().id) === kind + ":" + id;
  }

  toggle(kind: GraphicKind, id: string) {
    const scene = this.stage().id;
    const key = kind + ":" + id;
    if (this.selected.get(scene) === key) this.selected.delete(scene);
    else if (this.graphics.has(key)) this.selected.set(scene, key);
  }

  watching() { return Boolean(this.selected.size); }

  current() {
    const stage = this.stage();
    const chosen = this.selected.get(stage.id);
    const graphic = chosen ? this.graphics.get(chosen) : undefined;
    return graphic ? {graphic, brand: stage.brand} : undefined;
  }
}

export function setupStudioEngagement(path: string, credential: () => string, stage: () => Stage) {
  const projection = new StudioProjection(stage);

  async function authorized(url: string, method = "GET", body?: object) {
    const headers: Record<string, string> = {Authorization: "Bearer " + credential()};
    if (body) headers["Content-Type"] = "application/json";
    const response = await fetch(url, {method, headers, body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(10000)});
    if (!response.ok) {
      const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p")?.textContent;
      throw new Error(detail || "Could not load event engagement.");
    }
    return response;
  }

  const calls = setupProducerEngagement(() => path,
    async (url, method, body) => (await authorized(url, method, body)).json(),
    async (feature, filename) => {
      const report = document.getElementById("operatorEngagementStatus")!;
      try {
        const response = await authorized(path + "/" + feature + "/export");
        const link = document.createElement("a");
        link.href = URL.createObjectURL(await response.blob());
        link.download = filename;
        link.click();
        setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      } catch (error) {
        report.textContent = error instanceof Error ? error.message : String(error);
      }
    }, projection);

  return {...calls, graphic: () => projection.current(),
    paint(context: CanvasRenderingContext2D, value: NonNullable<ReturnType<StudioProjection["current"]>>) {
      paint(context, value.graphic, value.brand);
    }};
}
