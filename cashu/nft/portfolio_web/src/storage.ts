// On-device storage with a fallback. Some browsers expose no IndexedDB or
// refuse to open it (Safari Lockdown Mode, some privacy settings, in-app
// browsers). The wallets then keep their state in memory for the visit and
// rely on their encrypted server backups, which they restore on every open.

let probe: Promise<boolean> | null = null;

/** True when IndexedDB is present and can actually open a database. */
export function onDeviceStorage(): Promise<boolean> {
  probe ??= new Promise<boolean>((resolve) => {
    try {
      if (typeof indexedDB === 'undefined' || !indexedDB || typeof indexedDB.open !== 'function') { resolve(false); return; }
      const request = indexedDB.open('cashu-storage-probe', 1);
      request.onsuccess = () => { request.result.close(); resolve(true); };
      request.onerror = () => resolve(false);
      request.onblocked = () => resolve(true);
    } catch { resolve(false); }
  });
  return probe;
}

type Row = { id: string } & Record<string, unknown>;

/** One object store keyed by `id`, in IndexedDB or (fallback) memory. */
export interface RecordStore {
  readonly persistent: boolean;
  get<T extends Row>(id: string): Promise<T | undefined>;
  put(row: Row): Promise<void>;
  remove(id: string): Promise<void>;
  all<T extends Row>(): Promise<T[]>;
  close(): void;
}

function memoryStore(): RecordStore {
  const rows = new Map<string, Row>();
  return {
    persistent: false,
    async get<T extends Row>(id: string) { return rows.get(id) as T | undefined; },
    async put(row) { rows.set(row.id, structuredClone(row)); },
    async remove(id) { rows.delete(id); },
    async all<T extends Row>() { return [...rows.values()] as T[]; },
    close() { rows.clear(); },
  };
}

function idbStore(db: IDBDatabase, store: string): RecordStore {
  const run = <T>(mode: IDBTransactionMode, op: (s: IDBObjectStore) => IDBRequest<T>) => new Promise<T>((resolve, reject) => {
    const tx = db.transaction(store, mode), request = op(tx.objectStore(store));
    tx.oncomplete = () => resolve(request.result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error('Storage transaction aborted'));
  });
  return {
    persistent: true,
    get: <T extends Row>(id: string) => run<T | undefined>('readonly', (s) => s.get(id)),
    put: async (row) => { await run('readwrite', (s) => s.put(row)); },
    remove: async (id) => { await run('readwrite', (s) => s.delete(id)); },
    all: <T extends Row>() => run<T[]>('readonly', (s) => s.getAll()),
    close: () => db.close(),
  };
}

export async function openRecordStore(database: string, store: string): Promise<RecordStore> {
  if (!(await onDeviceStorage())) return memoryStore();
  return new Promise<RecordStore>((resolve, reject) => {
    const request = indexedDB.open(database, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(store, { keyPath: 'id' });
    request.onsuccess = () => resolve(idbStore(request.result, store));
    request.onerror = () => resolve(memoryStore());
    request.onblocked = () => reject(new Error('Close other tabs of this app and try again'));
  });
}

/** localStorage that never throws (it is blocked in some privacy modes). */
export const local = {
  get(key: string): string | null { try { return localStorage.getItem(key); } catch { return null; } },
  set(key: string, value: string) { try { localStorage.setItem(key, value); } catch { /* blocked */ } },
  remove(key: string) { try { localStorage.removeItem(key); } catch { /* blocked */ } },
};
/** sessionStorage (this tab only) that never throws. */
export const tab = {
  get(key: string): string | null { try { return sessionStorage.getItem(key); } catch { return null; } },
  set(key: string, value: string) { try { sessionStorage.setItem(key, value); } catch { /* blocked */ } },
  remove(key: string) { try { sessionStorage.removeItem(key); } catch { /* blocked */ } },
};
