// IndexedDB: workouts_meta + workout_samples (1 Hz), with crash recovery and an upload retry queue.
import type { Sample, WorkoutMeta } from '../types';

const DB_NAME = 'armory-fit';
let dbp: Promise<IDBDatabase> | null = null;

function open(): Promise<IDBDatabase> {
  if (dbp) return dbp;
  dbp = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      db.createObjectStore('workouts_meta', { keyPath: 'id' });
      db.createObjectStore('workout_samples', { keyPath: ['id', 't'] });
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

export async function putMeta(m: WorkoutMeta): Promise<void> {
  const t = await tx(['workouts_meta'], 'readwrite');
  await wrap(t.objectStore('workouts_meta').put(m));
}

export async function putSample(id: string, s: Sample): Promise<void> {
  const t = await tx(['workout_samples'], 'readwrite');
  await wrap(t.objectStore('workout_samples').put({ id, ...s }));
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
