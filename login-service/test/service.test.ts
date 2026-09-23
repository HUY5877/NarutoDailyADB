import assert from 'node:assert/strict';
import { randomBytes, randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Server } from 'node:http';
import { setTimeout as delay } from 'node:timers/promises';
import { test } from 'node:test';
import jsQR from 'jsqr';
import { PNG } from 'pngjs';
import { createApp } from '../src/app.js';
import { loadConfig, type ServiceConfig } from '../src/config.js';
import { LoginRuntime } from '../src/runtime.js';
import { MockRedisQueue } from '../src/queue.js';
import type { TaskSubmission, TaskView } from '../src/types.js';

// jsqr publishes a callable CommonJS entry alongside an ESM-style default type declaration.
const decodeQR = jsQR as unknown as typeof import('jsqr').default;

function task(ttl = 10000): TaskSubmission {
  const now = Date.now();
  return { id: randomUUID(), accessToken: randomBytes(32).toString('hex'), createdAt: new Date(now).toISOString(), expiresAt: new Date(now + ttl).toISOString() };
}

async function fixture(overrides: Partial<ServiceConfig> = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'naruto-login-test-'));
  const config = { ...loadConfig({}), localStorageDir: directory, stepDelayMs: 5, sweepIntervalMs: 20, ...overrides };
  const runtime = new LoginRuntime(config);
  await runtime.start();
  const server: Server = await new Promise(resolve => {
    const instance = createApp(runtime).listen(0, '127.0.0.1', () => resolve(instance));
  });
  const address = server.address();
  assert(address && typeof address !== 'string');
  const base = `http://127.0.0.1:${address.port}`;
  const headers = { 'X-Service-Token': config.serviceToken };
  return {
    runtime, directory, base, headers,
    post: (body: unknown, overrideHeaders: Record<string, string> = headers) => fetch(`${base}/internal/tasks`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...overrideHeaders }, body: JSON.stringify(body),
    }),
    get: (submission: TaskSubmission, token = submission.accessToken) => fetch(`${base}/internal/tasks/${submission.id}`, {
      headers: { ...headers, 'X-Session-Token': token },
    }),
    object: (submission: TaskSubmission, method = 'GET', overrideHeaders = headers) => fetch(`${base}/internal/objects/${submission.id}/qr.png`, {
      method, headers: overrideHeaders,
    }),
    async close() {
      await runtime.stop();
      server.closeAllConnections();
      await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
      await rm(directory, { recursive: true, force: true });
    },
  };
}

async function waitFor<T>(read: () => T | Promise<T>, predicate: (value: T) => boolean, timeout = 8000): Promise<T> {
  const deadline = Date.now() + timeout;
  let last: T;
  do {
    last = await read();
    if (predicate(last)) return last;
    await delay(25);
  } while (Date.now() < deadline);
  assert.fail(`Timed out waiting for expected state; last=${JSON.stringify(last!)}`);
}

test('mock Redis list preserves FIFO order, backpressure and blocking consumers', async () => {
  const queue = new MockRedisQueue<string>(2);
  assert.equal(queue.lpush('first'), true);
  assert.equal(queue.lpush('second'), true);
  assert.equal(queue.lpush('overflow'), false);
  assert.equal(await queue.brpop(), 'first');
  assert.equal(await queue.brpop(), 'second');
  const waiting = queue.brpop();
  assert.equal(queue.lpush('direct'), true);
  assert.equal(await waiting, 'direct');
  const stopped = queue.brpop();
  queue.close();
  assert.equal(await stopped, null);
  assert.equal(queue.lpush('after-close'), false);
});

test('isolated worker writes a PNG; authenticated HEAD/GET and idempotent submissions work', async () => {
  const service = await fixture();
  try {
    const submission = task();
    assert.equal((await service.post(submission, {})).status, 401);
    assert.equal((await service.post(submission)).status, 202);
    assert.equal((await service.post(submission)).status, 202);
    assert.equal(service.runtime.stats.tasks, 1);
    assert.equal((await service.post({ ...submission, accessToken: randomBytes(32).toString('hex') })).status, 409);
    assert.equal((await service.get(submission, 'wrong')).status, 404);
    assert.equal((await fetch(`${service.base}/internal/tasks/${submission.id}`, { headers: service.headers })).status, 404);
    const ready = await waitFor(async () => (await service.get(submission)).json() as Promise<TaskView>, view => view.status === 'qr_ready');
    assert.equal(ready.simulation, true);
    assert.equal('accessToken' in ready, false);
    const head = await service.object(submission, 'HEAD');
    assert.equal(head.status, 200);
    assert.equal(head.headers.get('content-type'), 'image/png');
    assert.equal(head.headers.get('cache-control'), 'no-store');
    assert.equal((await service.object(submission, 'GET', { 'X-Service-Token': 'wrong' })).status, 401);
    const response = await service.object(submission);
    const png = Buffer.from(await response.arrayBuffer());
    assert.deepEqual(png.subarray(0, 8), Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
    assert.equal(png.length, Number(head.headers.get('content-length')));
    assert.deepEqual(png, await readFile(join(service.directory, 'login', submission.id, 'qr.png')));
    const decodedImage = PNG.sync.read(png);
    const decoded = decodeQR(new Uint8ClampedArray(decodedImage.data), decodedImage.width, decodedImage.height);
    assert(decoded, 'Generated image must contain a readable QR code');
    const payload = JSON.parse(decoded.data) as { simulation: boolean; taskId: string; kind: string };
    assert.equal(payload.simulation, true);
    assert.equal(payload.taskId, submission.id);
    assert.equal(payload.kind, 'naruto-login-demo');
    assert.equal(decoded.data.includes(submission.accessToken), false);
    assert.equal(service.runtime.stats.active, 0);
  } finally { await service.close(); }
});

test('rejects malformed IDs, traversal, invalid lifetimes and bad JSON', async () => {
  const service = await fixture();
  try {
    const valid = task();
    for (const invalid of [
      { ...valid, id: '../other' }, { ...valid, accessToken: 'secret' },
      { ...valid, expiresAt: new Date(Date.now() - 1).toISOString() },
      { ...valid, expiresAt: new Date(Date.now() + 700000).toISOString() },
      { ...valid, createdAt: new Date(Date.now() + 10000).toISOString() },
      { ...valid, createdAt: 'not-a-date' },
    ]) assert.equal((await service.post(invalid)).status, 400);
    const malformed = await fetch(`${service.base}/internal/tasks`, {
      method: 'POST', headers: { ...service.headers, 'Content-Type': 'application/json' }, body: '{',
    });
    assert.equal(malformed.status, 400);
    assert.equal((await fetch(`${service.base}/internal/objects/not-a-uuid/qr.png`, { headers: service.headers })).status, 404);
  } finally { await service.close(); }
});

test('concurrency and capacity limits apply without duplicate workers', async () => {
  const service = await fixture({ concurrency: 1, maxQueueSize: 1, maxTasks: 3, stepDelayMs: 350 });
  try {
    const first = task();
    const second = task();
    assert.equal((await service.post(first)).status, 202);
    await waitFor(() => service.runtime.stats.active, active => active === 1);
    assert.equal((await service.post(second)).status, 202);
    assert.equal((await service.post(task())).status, 429);
    assert.equal((await service.post(first)).status, 202);
    assert.equal(service.runtime.stats.active, 1);
    assert.equal(service.runtime.stats.queued, 1);
    await waitFor(async () => (await service.get(second)).json() as Promise<TaskView>, view => view.status === 'qr_ready');
    assert.equal((await service.get(first)).status, 200);
    assert.equal((await service.object(second)).status, 200);
  } finally { await service.close(); }
});

test('expires ready objects, terminates running children and forgets retained jobs', async () => {
  const service = await fixture({ retentionMs: 200, stepDelayMs: 5 });
  try {
    const submission = task(1500);
    assert.equal((await service.post(submission)).status, 202);
    await waitFor(async () => (await service.get(submission)).json() as Promise<TaskView>, view => view.status === 'qr_ready');
    await waitFor(async () => (await service.get(submission)).json() as Promise<TaskView>, view => view.status === 'expired');
    assert.equal((await service.object(submission)).status, 404);
    await waitFor(() => service.runtime.stats.tasks, count => count === 0);
    await assert.rejects(readFile(join(service.directory, 'login', submission.id, 'qr.png')), { code: 'ENOENT' });
    assert.equal((await service.get(submission)).status, 404);
  } finally { await service.close(); }

  const slow = await fixture({ stepDelayMs: 1000, retentionMs: 200 });
  try {
    const submission = task(300);
    assert.equal((await slow.post(submission)).status, 202);
    await waitFor(() => slow.runtime.stats.active, count => count === 1);
    await waitFor(() => slow.runtime.stats.active, count => count === 0);
    assert.equal((await slow.object(submission)).status, 404);
  } finally { await slow.close(); }
});

test('LDPlayer mode explicitly fails instead of pretending to perform real login', async () => {
  const service = await fixture({ emulatorDriver: 'ldplayer' });
  try {
    const submission = task();
    await service.post(submission);
    const failed = await waitFor(async () => (await service.get(submission)).json() as Promise<TaskView>, view => view.status === 'failed');
    assert.match(failed.error ?? '', /not implemented/);
    assert.equal((await service.object(submission)).status, 404);
  } finally { await service.close(); }
});

test('shutdown kills active child processes and drains the queue', async () => {
  const service = await fixture({ concurrency: 1, stepDelayMs: 1000 });
  try {
    await service.post(task());
    await service.post(task());
    await waitFor(() => service.runtime.stats.active, count => count === 1);
    await service.runtime.stop();
    assert.deepEqual(service.runtime.stats, { active: 0, queued: 0, tasks: 0 });
    assert.equal((await service.post(task())).status, 503);
  } finally { await service.close(); }
});

test('OSS mode rejects incomplete credentials without echoing secrets', () => {
  assert.throws(() => loadConfig({ STORAGE_DRIVER: 'oss', OSS_ACCESS_KEY_SECRET: 'sensitive-example' }), /OSS_REGION/);
  assert.throws(() => loadConfig({ OSS_KEY_PREFIX: '../unsafe' }), /Invalid OSS_KEY_PREFIX/);
  assert.throws(() => loadConfig({ HOST: '0.0.0.0' }), /unique SERVICE_TOKEN/);
  assert.equal(loadConfig({ HOST: '0.0.0.0', SERVICE_TOKEN: 'a-unique-private-development-token' }).host, '0.0.0.0');
});

test('periodic storage sweep removes orphan objects after their expiry', async () => {
  const service = await fixture({ storageSweepIntervalMs: 30 });
  try {
    const id = randomUUID();
    await service.runtime.storage.put(id, Buffer.from('orphan-file'), new Date(Date.now() + 100).toISOString());
    assert.equal(service.runtime.stats.tasks, 0);
    await waitFor(async () => {
      try { await readFile(join(service.directory, 'login', id, 'qr.png')); return false; }
      catch (error) { return (error as NodeJS.ErrnoException).code === 'ENOENT'; }
    }, deleted => deleted);
  } finally { await service.close(); }
});

test('corrupt null metadata is cleaned without blocking service startup', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'naruto-login-corrupt-'));
  const id = randomUUID();
  const objectDir = join(directory, 'login', id);
  await mkdir(objectDir, { recursive: true });
  await writeFile(join(objectDir, 'metadata.json'), 'null');
  const runtime = new LoginRuntime({ ...loadConfig({}), localStorageDir: directory });
  try {
    await runtime.start();
    await assert.rejects(readFile(join(objectDir, 'metadata.json')), { code: 'ENOENT' });
    assert.equal(runtime.stats.tasks, 0);
  } finally {
    await runtime.stop();
    await rm(directory, { recursive: true, force: true });
  }
});
