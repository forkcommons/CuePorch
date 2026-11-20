type TickTime = { seconds: number };

type Sequence = {
  getEndTime(): Promise<TickTime>;
};

type Action = object;

type CompoundAction = {
  addAction(action: Action): void;
};

type Project = {
  getActiveSequence(): Promise<Sequence | null>;
  lockedAccess(callback: () => void): void;
  executeTransaction(
    callback: (compound: CompoundAction) => void,
    label: string
  ): boolean;
};

type Marker = {
  getComments(): string;
};

type Markers = {
  getMarkers(filters: string[]): Marker[];
  createAddMarkerAction(
    name: string,
    type: string,
    start: TickTime,
    duration: TickTime,
    comments: string
  ): Action;
};

export type PremiereHost = {
  Marker: { MARKER_TYPE_COMMENT: string };
  Project: {
    getActiveProject(): Promise<Project | null>;
  };
  Markers: {
    getMarkers(sequence: Sequence): Promise<Markers>;
  };
  TickTime: {
    createWithSeconds(seconds: number): TickTime;
  };
};
