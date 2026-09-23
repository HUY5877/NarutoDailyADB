import { timingSafeEqual } from 'node:crypto';
import type { ChildProcess } from 'node:child_process';
import type { ServiceConfig } from './config.js';
import { MockRedisQueue } from './queue.js';
import { createStorage, type ObjectStorage } from './storage.js';
import { spawnMockEmulator } from './emulator.js';
import { MAX_TTL_MS, TOKEN_PATTERN, UUID_PATTERN, type TaskRecord, type TaskSubmission, type TaskView } from './types.js';

export class HttpError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}

export function equalSecret(actual: string | undefined, expected: string): boolean {
  if (actual === undefined) return false;
  const left = Buffer.from(actual);
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

function parseSubmission(body: unknown): TaskSubmission {
  if (typeof body !== 'object' || body === null) throw new HttpError(400, 'Invalid task body');
  const candidate = body as Record<string, unknown>;
  if (typeof candidate.id !== 'string' || !UUID_PATTERN.test(candidate.id) ||
      typeof candidate.accessToken !== 'string' || !TOKEN_PATTERN.test(candidate.accessToken) ||
      typeof candidate.createdAt !== 'string' || typeof candidate.expiresAt !== 'string') {
    throw new HttpError(400, 'Expected UUID id, 64-character hex accessToken and ISO timestamps');
  }
  const { id, accessToken, createdAt, expiresAt } = candidate as unknown as TaskSubmission;
  const created = Date.parse(createdAt);
  const expires = Date.parse(expiresAt);
  const now = Date.now();
  const iso = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/;
  if (!iso.test(createdAt) || !iso.test(expiresAt) || !Number.isFinite(created) || !Number.isFinite(expires) ||
      created > now + 5000 || created < now - MAX_TTL_MS || expires <= now || expires <= created ||
      expires - created > MAX_TTL_MS || expires - now > MAX_TTL_MS) {
    throw new HttpError(400, 'Task timestamps must be recent; expiry must be in the future and within 10 minutes');
  }
  return { id: id.toLowerCase(), accessToken, createdAt, expiresAt };
}

export class LoginRuntime {
  readonly storage: ObjectStorage;
  private readonly tasks = new Map<string, TaskRecord>();
  private readonly queue: MockRedisQueue<string>;
  private readonly children = new Map<string, ChildProcess>();
  private readonly pendingDeletes = new Set<string>();
  private workers: Promise<void>[] = [];
  private sweepTimer: NodeJS.Timeout | undefined;
  private sweepPromise: Promise<void> | undefined;
  private sweeping = false;
  private stopping = false;
  private initialized = false;
  private nextStorageSweepAt = 0;

  constructor(readonly config: ServiceConfig, storage?: ObjectStorage) {
    this.storage = storage ?? createStorage(config);
    this.queue = new MockRedisQueue(config.maxQueueSize);
  }

  async start(): Promise<void> {
    if (this.initialized) return;
    await this.storage.initialize();
    this.nextStorageSweepAt = Date.now() + this.config.storageSweepIntervalMs;
    this.initialized = true;
    this.workers = Array.from({ length: this.config.concurrency }, () => this.consume());
    this.sweepTimer = setInterval(() => {
      if (!this.sweepPromise) this.sweepPromise = this.sweep().finally(() => { this.sweepPromise = undefined; });
    }, this.config.sweepIntervalMs);
    this.sweepTimer.unref();
  }

  get stats(): { queued: number; active: number; tasks: number } {
    return { queued: this.queue.length, active: this.children.size, tasks: this.tasks.size };
  }

  submit(body: unknown): string {
    if (this.stopping || !this.initialized) throw new HttpError(503, 'Service is unavailable');
    const submission = parseSubmission(body);
    const existing = this.tasks.get(submission.id);
    if (existing) {
      if (equalSecret(submission.accessToken, existing.accessToken) &&
          submission.createdAt === existing.createdAt && submission.expiresAt === existing.expiresAt) return existing.id;
      throw new HttpError(409, 'Task id is already in use');
    }
    if (this.tasks.size >= this.config.maxTasks) throw new HttpError(429, 'Task capacity reached; retry later');
    const record: TaskRecord = { ...submission, status: 'queued', expiresAtMs: Date.parse(submission.expiresAt) };
    this.tasks.set(record.id, record);
    if (!this.queue.lpush(record.id)) {
      this.tasks.delete(record.id);
      throw new HttpError(429, 'Queue is full; retry later');
    }
    return record.id;
  }

  private expire(record: TaskRecord): void {
    if (record.status === 'expired') return;
    record.status = 'expired';
    delete record.error;
    this.children.get(record.id)?.kill();
    this.queue.remove(id => id === record.id);
    this.pendingDeletes.add(record.id);
  }

  private lookup(id: string): TaskRecord | undefined {
    if (!UUID_PATTERN.test(id)) return undefined;
    const record = this.tasks.get(id.toLowerCase());
    if (record && record.expiresAtMs <= Date.now()) this.expire(record);
    return record;
  }

  getTask(id: string, sessionToken: string | undefined): TaskView | null {
    const record = this.lookup(id);
    if (!record || !equalSecret(sessionToken, record.accessToken)) return null;
    return {
      id: record.id, status: record.status, createdAt: record.createdAt,
      expiresAt: record.expiresAt, simulation: true,
      ...(record.error ? { error: record.error } : {}),
    };
  }

  objectAvailable(id: string): boolean {
    const record = this.lookup(id);
    return record?.status === 'qr_ready';
  }

  private async consume(): Promise<void> {
    while (!this.stopping) {
      const id = await this.queue.brpop();
      if (id === null) return;
      const record = this.lookup(id);
      if (!record || record.status === 'expired' || this.stopping) continue;
      try {
        if (this.config.emulatorDriver !== 'mock') {
          throw new Error('LDPlayer adapter is not implemented; use EMULATOR_DRIVER=mock');
        }
        record.status = 'starting_emulator';
        const run = spawnMockEmulator(record, this.config.stepDelayMs, status => {
          if (record.expiresAtMs <= Date.now()) this.expire(record);
          if (record.status !== 'expired' && !this.stopping) record.status = status;
        });
        this.children.set(id, run.child);
        const png = await run.result;
        if (record.expiresAtMs <= Date.now() || this.stopping) { this.expire(record); continue; }
        await this.storage.put(id, png, record.expiresAt);
        if (record.expiresAtMs <= Date.now() || this.stopping) {
          this.expire(record);
          this.pendingDeletes.add(id);
          await this.storage.delete(id);
        } else record.status = 'qr_ready';
      } catch {
        if (record.expiresAtMs <= Date.now()) this.expire(record);
        else if (!this.stopping) {
          record.status = 'failed';
          record.error = this.config.emulatorDriver !== 'mock'
            ? 'LDPlayer adapter is not implemented. Set EMULATOR_DRIVER=mock.'
            : 'Could not prepare the simulated login QR code. Please create a new login request.';
          this.pendingDeletes.add(id);
        }
      } finally {
        this.children.get(id)?.kill();
        this.children.delete(id);
      }
    }
  }

  private async sweep(): Promise<void> {
    if (this.sweeping || this.stopping) return;
    this.sweeping = true;
    try {
      const now = Date.now();
      for (const [id, record] of this.tasks) {
        if (record.expiresAtMs <= now) this.expire(record);
        if (record.expiresAtMs + this.config.retentionMs <= now && !this.children.has(id) && !this.pendingDeletes.has(id)) this.tasks.delete(id);
      }
      for (const id of this.pendingDeletes) {
        try { await this.storage.delete(id); this.pendingDeletes.delete(id); }
        catch { /* Retried on the next sweep; no credential-bearing SDK errors are exposed. */ }
      }
      if (Date.now() >= this.nextStorageSweepAt) {
        this.nextStorageSweepAt = Date.now() + this.config.storageSweepIntervalMs;
        try { await this.storage.sweep(); }
        catch { /* Retried periodically, including orphan objects left by earlier local runs. */ }
      }
    } finally { this.sweeping = false; }
  }

  async stop(): Promise<void> {
    if (this.stopping) return;
    this.stopping = true;
    if (this.sweepTimer) clearInterval(this.sweepTimer);
    this.queue.close();
    for (const child of this.children.values()) child.kill();
    await Promise.allSettled(this.workers);
    await this.sweepPromise;
    await Promise.allSettled([...this.tasks.keys(), ...this.pendingDeletes].map(id => this.storage.delete(id)));
    this.tasks.clear();
  }
}
