import type { ReviewMarker } from "./markers";
import type { PremiereHost } from "./host-types";

export async function applyReviewMarkers(
  host: PremiereHost,
  rows: ReviewMarker[]
): Promise<{ added: number; skipped: number }> {
  const project = await host.Project.getActiveProject();
  if (!project) {
    throw new Error("No active project found. Please open a project and try again.");
  }
  const sequence = await project.getActiveSequence();
  if (!sequence) {
    throw new Error("No active sequence found. Please open a sequence and try again.");
  }
  const end = await sequence.getEndTime();
  for (const row of rows) {
    if (row.seconds >= end.seconds) {
      throw new Error("Cannot add marker because the marker time is beyond the end of the sequence.");
    }
  }
  const markers = await host.Markers.getMarkers(sequence);

  let added = 0;
  let skipped = 0;

  project.lockedAccess(() => {
    const existing = new Set(
      markers.getMarkers([host.Marker.MARKER_TYPE_COMMENT]).map((m) =>
        m.getComments().split("\n").at(-1)
      )
    );
    const toAdd = rows.filter(
      (r) => !existing.has("CuePorch marker: " + r.id)
    );
    skipped = rows.length - toAdd.length;
    if (toAdd.length > 0) {
      const ok = project.executeTransaction((compound) => {
        for (const row of toAdd) {
          compound.addAction(
            markers.createAddMarkerAction(
              row.author,
              host.Marker.MARKER_TYPE_COMMENT,
              host.TickTime.createWithSeconds(row.seconds),
              host.TickTime.createWithSeconds(0),
              `${row.body}\n\nCuePorch marker: ${row.id}`
            )
          );
        }
      }, "Import CuePorch markers");
      if (!ok) {
        throw new Error("Could not import CuePorch markers because the transaction failed.");
      }
    }
    added = toAdd.length;
  });

  return { added, skipped };
}
