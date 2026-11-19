export interface RecordingDraft {
  id: string;
  mime: string;
  created: number;
  title: string;
  folder: string;
}

interface Part {id: string; sequence: number; data: Blob}

function result<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function completed(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = transaction.onerror = () => reject(transaction.error || new Error("Recording storage was interrupted."));
  });
}

function partRange(id: string) {
  return IDBKeyRange.bound([id, 0], [id, Number.MAX_SAFE_INTEGER]);
}

export async function openRecordingStorage() {
  const request = indexedDB.open("cueporch-recordings", 1);
  request.onupgradeneeded = () => {
    request.result.createObjectStore("drafts", {keyPath: "id"});
    request.result.createObjectStore("parts", {keyPath: ["id", "sequence"]});
  };
  const db = await result(request);
  return {
    async list() {
      const entries: RecordingDraft[] = await result(db.transaction("drafts").objectStore("drafts").getAll());
      return entries.sort((a, b) => b.created - a.created);
    },
    async save(draft: RecordingDraft) {
      const transaction = db.transaction("drafts", "readwrite");
      transaction.objectStore("drafts").put(draft);
      await completed(transaction);
    },
    async append(draft: RecordingDraft, sequence: number, data: Blob) {
      const transaction = db.transaction("parts", "readwrite");
      transaction.objectStore("parts").add({id: draft.id, sequence, data});
      await completed(transaction);
    },
    async load(draft: RecordingDraft) {
      const parts: Part[] = await result(db.transaction("parts").objectStore("parts").getAll(partRange(draft.id)));
      return new Blob(parts.map(chunk => chunk.data), {type: draft.mime});
    },
    async remove(draft: RecordingDraft) {
      const transaction = db.transaction(["drafts", "parts"], "readwrite");
      transaction.objectStore("drafts").delete(draft.id);
      transaction.objectStore("parts").delete(partRange(draft.id));
      await completed(transaction);
    },
  };
}
