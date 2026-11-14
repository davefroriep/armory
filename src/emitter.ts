export class Emitter<T = void> {
  private fns = new Set<(v: T) => void>();
  on(fn: (v: T) => void): () => void {
    this.fns.add(fn);
    return () => this.fns.delete(fn);
  }
  emit(v: T): void {
    for (const f of [...this.fns]) f(v);
  }
}

export const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
