/** In-memory Redis-list substitute: LPUSH producers + blocking RPOP consumers, FIFO. */
export class MockRedisQueue<T> {
  private readonly items: T[] = [];
  private readonly waiting: ((item: T | null) => void)[] = [];
  private closed = false;

  constructor(private readonly capacity: number) {}

  get length(): number { return this.items.length; }

  lpush(item: T): boolean {
    if (this.closed) return false;
    const waiter = this.waiting.shift();
    if (waiter) { waiter(item); return true; }
    if (this.items.length >= this.capacity) return false;
    this.items.unshift(item);
    return true;
  }

  async brpop(): Promise<T | null> {
    if (this.closed) return null;
    const item = this.items.pop();
    if (item !== undefined) return item;
    return new Promise(resolve => this.waiting.push(resolve));
  }

  remove(predicate: (item: T) => boolean): void {
    for (let index = this.items.length - 1; index >= 0; index--) {
      if (predicate(this.items[index]!)) this.items.splice(index, 1);
    }
  }

  close(): void {
    this.closed = true;
    this.items.length = 0;
    for (const resolve of this.waiting.splice(0)) resolve(null);
  }
}
