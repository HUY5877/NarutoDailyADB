import { mkdir, readFile, readdir, rename, rm, stat, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import OSS from 'ali-oss';
import type { ServiceConfig } from './config.js';
import { UUID_PATTERN } from './types.js';

export interface StoredObject {
  contentLength: number;
  contentType: 'image/png';
  expiresAt: string;
}

export interface ObjectStorage {
  initialize(): Promise<void>;
  put(id: string, png: Buffer, expiresAt: string): Promise<void>;
  head(id: string): Promise<StoredObject | null>;
  get(id: string): Promise<(StoredObject & { body: Buffer }) | null>;
  delete(id: string): Promise<void>;
  sweep(): Promise<void>;
}

function missing(error: unknown): boolean {
  const candidate = error as { code?: string; status?: number };
  return candidate.code === 'ENOENT' || candidate.code === 'NoSuchKey' || candidate.status === 404;
}

function safeId(id: string): void {
  if (!UUID_PATTERN.test(id)) throw new Error('Invalid object id');
}

export class LocalObjectStorage implements ObjectStorage {
  private readonly root: string;

  constructor(directory: string, prefix: string) { this.root = join(directory, ...prefix.split('/')); }

  private directory(id: string): string { safeId(id); return join(this.root, id); }
  private file(id: string): string { return join(this.directory(id), 'qr.png'); }

  async initialize(): Promise<void> {
    await mkdir(this.root, { recursive: true });
    await this.sweep();
  }

  async put(id: string, png: Buffer, expiresAt: string): Promise<void> {
    const file = this.file(id);
    await mkdir(dirname(file), { recursive: true });
    const nonce = randomUUID();
    const temporaryImage = `${file}.${nonce}.tmp`;
    const metadata = join(this.directory(id), 'metadata.json');
    const temporaryMetadata = `${metadata}.${nonce}.tmp`;
    try {
      await writeFile(temporaryImage, png, { flag: 'wx' });
      await writeFile(temporaryMetadata, JSON.stringify({ expiresAt }), { flag: 'wx' });
      // Image becomes visible only after metadata and the final image name both exist.
      await rename(temporaryMetadata, metadata);
      await rename(temporaryImage, file);
    } finally {
      await Promise.all([rm(temporaryImage, { force: true }), rm(temporaryMetadata, { force: true })]);
    }
  }

  async head(id: string): Promise<StoredObject | null> {
    try {
      const metadata: unknown = JSON.parse(await readFile(join(this.directory(id), 'metadata.json'), 'utf8'));
      if (typeof metadata !== 'object' || metadata === null || !('expiresAt' in metadata) ||
          typeof metadata.expiresAt !== 'string' || !Number.isFinite(Date.parse(metadata.expiresAt)) ||
          Date.parse(metadata.expiresAt) <= Date.now()) return null;
      const info = await stat(this.file(id));
      return { contentLength: info.size, contentType: 'image/png', expiresAt: metadata.expiresAt };
    } catch (error) {
      if (missing(error)) return null;
      throw error;
    }
  }

  async get(id: string): Promise<(StoredObject & { body: Buffer }) | null> {
    const metadata = await this.head(id);
    if (!metadata) return null;
    try { return { ...metadata, body: await readFile(this.file(id)) }; }
    catch (error) { if (missing(error)) return null; throw error; }
  }

  async delete(id: string): Promise<void> { await rm(this.directory(id), { recursive: true, force: true }); }

  async sweep(): Promise<void> {
    const entries = await readdir(this.root, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory() || !UUID_PATTERN.test(entry.name)) continue;
      // Only remove task directories with expired/corrupt metadata; avoid interrupting active atomic writes.
      try {
        const metadata: unknown = JSON.parse(await readFile(join(this.directory(entry.name), 'metadata.json'), 'utf8'));
        if (typeof metadata !== 'object' || metadata === null || !('expiresAt' in metadata) ||
            typeof metadata.expiresAt !== 'string' || !Number.isFinite(Date.parse(metadata.expiresAt)) || Date.parse(metadata.expiresAt) <= Date.now()) {
          await this.delete(entry.name);
        }
      } catch (error) {
        if (!missing(error) && !(error instanceof SyntaxError)) throw error;
        const info = await stat(this.directory(entry.name)).catch(() => null);
        if (info && Date.now() - info.mtimeMs > 10 * 60 * 1000) await this.delete(entry.name);
      }
    }
  }
}

export class AliyunObjectStorage implements ObjectStorage {
  private readonly client: OSS;
  private readonly knownExpiries = new Map<string, number>();

  constructor(private readonly prefix: string, options: ServiceConfig['oss']) {
    this.client = new OSS({ ...options, secure: true, timeout: 10000 });
  }

  private key(id: string): string { safeId(id); return `${this.prefix}/${id}/qr.png`; }
  async initialize(): Promise<void> {}

  async put(id: string, png: Buffer, expiresAt: string): Promise<void> {
    await this.client.put(this.key(id), png, {
      headers: {
        'Content-Type': 'image/png',
        'Cache-Control': 'no-store',
        'x-oss-object-acl': 'private',
        'x-oss-meta-expires-at': expiresAt,
      },
    });
    this.knownExpiries.set(id, Date.parse(expiresAt));
  }

  async head(id: string): Promise<StoredObject | null> {
    try {
      const result = await this.client.head(this.key(id));
      const headers = result.res.headers as Record<string, string | number | undefined>;
      const expiresAt = String(headers['x-oss-meta-expires-at'] ?? '');
      if (!Number.isFinite(Date.parse(expiresAt)) || Date.parse(expiresAt) <= Date.now()) return null;
      return { contentLength: Number(headers['content-length']), contentType: 'image/png', expiresAt };
    } catch (error) { if (missing(error)) return null; throw error; }
  }

  async get(id: string): Promise<(StoredObject & { body: Buffer }) | null> {
    const metadata = await this.head(id);
    if (!metadata) return null;
    try {
      const result = await this.client.get(this.key(id));
      return { ...metadata, body: Buffer.from(result.content as Uint8Array) };
    } catch (error) { if (missing(error)) return null; throw error; }
  }

  async delete(id: string): Promise<void> {
    await this.client.delete(this.key(id));
    this.knownExpiries.delete(id);
  }

  async sweep(): Promise<void> {
    for (const [id, expiresAt] of this.knownExpiries) {
      if (expiresAt <= Date.now()) await this.delete(id);
    }
  }
}

export function createStorage(config: ServiceConfig): ObjectStorage {
  return config.storageDriver === 'oss'
    ? new AliyunObjectStorage(config.keyPrefix, config.oss)
    : new LocalObjectStorage(config.localStorageDir, config.keyPrefix);
}
