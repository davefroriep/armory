// IndexedDB: workouts_meta + workout_samples (1 Hz), with crash recovery and an upload retry queue.
import { Emitter } from '../emitter';
import type { Sample, WorkoutMeta } from '../types';

export const metaSaved = new Emitter<WorkoutMeta>();

const DB_NAME = 'armory-fit';
let dbp: Promise<IDBDatabase> | null = null;

function open(): Promise<IDBDatabase> {
  if (dbp) return dbp;
  dbp = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 2);
    req.onupgradeneeded = () => {
      const db = req.result;
      const mk = (name: string, opts: IDBObjectStoreParameters): void => {
        if (!db.objectStoreNames.contains(name)) db.createObjectStore(name, opts);
      };
      mk('workouts_meta', { keyPath: 'id' });
      mk('workout_samples', { keyPath: ['id', 't'] });
      // v2: lift tracker
      mk('lift_sessions', { keyPath: 'id' });
      mk('lift_routines', { keyPath: 'id' });
      mk('lift_ex', { keyPath: 'id' });
      mk('lift_videos', { keyPath: 'id' });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbp;
}

function wrap<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((res, rej) => {
    req.onsuccess = () => res(req.result);
    req.onerror = () => rej(req.error);
  });
}

async function tx(stores: string[], mode: IDBTransactionMode): Promise<IDBTransaction> {
  return (await open()).transaction(stores, mode);
}

export async function putMeta(m: WorkoutMeta, emit = true): Promise<void> {
  const t = await tx(['workouts_meta'], 'readwrite');
  await wrap(t.objectStore('workouts_meta').put(m));
  if (emit && m.status === 'saved') metaSaved.emit(m);
}

export async function putSample(id: string, s: Sample): Promise<void> {
  const t = await tx(['workout_samples'], 'readwrite');
  await wrap(t.objectStore('workout_samples').put({ id, ...s }));
}

export async function putSamples(id: string, samples: Sample[]): Promise<void> {
  if (!samples.length) return;
  const t = await tx(['workout_samples'], 'readwrite');
  const os = t.objectStore('workout_samples');
  for (const s of samples) os.put({ id, ...s });
  await done(t);
}

export async function replaceSamples(id: string, samples: Sample[]): Promise<void> {
  const t = await tx(['workout_samples'], 'readwrite');
  const os = t.objectStore('workout_samples');
  os.delete(IDBKeyRange.bound([id, 0], [id, Infinity]));
  for (const s of samples) os.put({ id, ...s });
  await done(t);
}

export async function getMeta(id: string): Promise<WorkoutMeta | undefined> {
  const t = await tx(['workouts_meta'], 'readonly');
  return wrap(t.objectStore('workouts_meta').get(id));
}

export async function allMeta(): Promise<WorkoutMeta[]> {
  const t = await tx(['workouts_meta'], 'readonly');
  return wrap(t.objectStore('workouts_meta').getAll());
}

export async function getSamples(id: string): Promise<Sample[]> {
  const t = await tx(['workout_samples'], 'readonly');
  const rows = await wrap(t.objectStore('workout_samples').getAll(IDBKeyRange.bound([id, 0], [id, Infinity])));
  return rows.map(({ id: _id, ...s }) => s as Sample).sort((a, b) => a.t - b.t);
}

export async function deleteWorkout(id: string): Promise<void> {
  const t = await tx(['workouts_meta', 'workout_samples'], 'readwrite');
  t.objectStore('workouts_meta').delete(id);
  t.objectStore('workout_samples').delete(IDBKeyRange.bound([id, 0], [id, Infinity]));
  await new Promise<void>((res, rej) => {
    t.oncomplete = () => res();
    t.onerror = () => rej(t.error);
  });
}

// ---------- generic helpers (lift tracker stores) ----------
export type LiftStoreName = 'lift_sessions' | 'lift_routines' | 'lift_ex' | 'lift_videos';

async function done(t: IDBTransaction): Promise<void> {
  await new Promise<void>((res, rej) => {
    t.oncomplete = () => res();
    t.onerror = () => rej(t.error);
    t.onabort = () => rej(t.error);
  });
}

export async function idbPut<T>(store: LiftStoreName, value: T): Promise<void> {
  const t = await tx([store], 'readwrite');
  t.objectStore(store).put(value);
  await done(t);
}

export async function idbPutMany<T>(store: LiftStoreName, values: T[]): Promise<void> {
  const t = await tx([store], 'readwrite');
  for (const v of values) t.objectStore(store).put(v);
  await done(t);
}

export async function idbGet<T>(store: LiftStoreName, key: string): Promise<T | undefined> {
  const t = await tx([store], 'readonly');
  return wrap(t.objectStore(store).get(key));
}

export async function idbAll<T>(store: LiftStoreName): Promise<T[]> {
  const t = await tx([store], 'readonly');
  return wrap(t.objectStore(store).getAll());
}

export async function idbDelete(store: LiftStoreName, key: string): Promise<void> {
  const t = await tx([store], 'readwrite');
  t.objectStore(store).delete(key);
  await done(t);
}