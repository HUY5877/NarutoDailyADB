import { resolve } from 'node:path';

export interface ServiceConfig {
  host: string;
  port: number;
  serviceToken: string;
  storageDriver: 'local' | 'oss';
  localStorageDir: string;
  keyPrefix: string;
  concurrency: number;
  maxQueueSize: number;
  maxTasks: number;
  retentionMs: number;
  stepDelayMs: number;
  emulatorDriver: 'mock' | 'ldplayer';
  sweepIntervalMs: number;
  storageSweepIntervalMs: number;
  oss: {
    region: string;
    bucket: string;
    accessKeyId: string;
    accessKeySecret: string;
    stsToken?: string;
  };
}

function integer(env: NodeJS.ProcessEnv, name: string, fallback: number, min: number, max: number): number {
  const value = env[name] === undefined ? fallback : Number(env[name]);
  if (!Number.isInteger(value) || value < min || value > max) throw new Error(`Invalid ${name}`);
  return value;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): ServiceConfig {
  const storageDriver = env.STORAGE_DRIVER ?? 'local';
  if (storageDriver !== 'local' && storageDriver !== 'oss') throw new Error('STORAGE_DRIVER must be local or oss');
  const emulatorDriver = env.EMULATOR_DRIVER ?? 'mock';
  if (emulatorDriver !== 'mock' && emulatorDriver !== 'ldplayer') throw new Error('EMULATOR_DRIVER must be mock or ldplayer');
  const keyPrefix = (env.OSS_KEY_PREFIX ?? 'login').replace(/^\/+|\/+$/g, '');
  if (!/^[a-zA-Z0-9_-]+(?:\/[a-zA-Z0-9_-]+)*$/.test(keyPrefix)) throw new Error('Invalid OSS_KEY_PREFIX');
  const serviceToken = env.SERVICE_TOKEN ?? 'local-development-token';
  if (serviceToken.length < 16) throw new Error('SERVICE_TOKEN must contain at least 16 characters');
  const host = env.HOST ?? '127.0.0.1';
  if (!['127.0.0.1', '::1', 'localhost'].includes(host.toLowerCase()) && serviceToken === 'local-development-token') {
    throw new Error('Set a unique SERVICE_TOKEN before binding HOST outside loopback');
  }
  const oss: ServiceConfig['oss'] = {
    region: env.OSS_REGION ?? '',
    bucket: env.OSS_BUCKET ?? '',
    accessKeyId: env.OSS_ACCESS_KEY_ID ?? '',
    accessKeySecret: env.OSS_ACCESS_KEY_SECRET ?? '',
    ...(env.OSS_STS_TOKEN ? { stsToken: env.OSS_STS_TOKEN } : {}),
  };
  if (storageDriver === 'oss' && Object.values(oss).some(value => !value)) {
    throw new Error('OSS_REGION, OSS_BUCKET, OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET are required for OSS');
  }
  return {
    host,
    port: integer(env, 'PORT', 4320, 0, 65535),
    serviceToken,
    storageDriver,
    localStorageDir: resolve(env.LOCAL_STORAGE_DIR ?? '.data/objects'),
    keyPrefix,
    concurrency: integer(env, 'WORKER_CONCURRENCY', 2, 1, 16),
    maxQueueSize: integer(env, 'MAX_QUEUE_SIZE', 100, 1, 10000),
    maxTasks: integer(env, 'MAX_TASKS', 500, 1, 100000),
    retentionMs: integer(env, 'TASK_RETENTION_MS', 60000, 0, 3600000),
    stepDelayMs: integer(env, 'MOCK_STEP_DELAY_MS', 600, 0, 10000),
    emulatorDriver,
    sweepIntervalMs: 250,
    storageSweepIntervalMs: 10000,
    oss,
  };
}
