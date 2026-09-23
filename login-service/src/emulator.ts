import { fork, type ChildProcess } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import type { TaskRecord, TaskStatus } from './types.js';

export interface EmulatorRun {
  child: ChildProcess;
  result: Promise<Buffer>;
}

export function spawnMockEmulator(task: TaskRecord, stepDelayMs: number, stage: (status: TaskStatus) => void): EmulatorRun {
  const sourceMode = import.meta.url.endsWith('.ts');
  const file = fileURLToPath(new URL(sourceMode ? './mock-emulator.ts' : './mock-emulator.js', import.meta.url));
  const child = fork(file, [], {
    execArgv: sourceMode ? ['--import', 'tsx'] : [],
    stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
    windowsHide: true,
  });
  const result = new Promise<Buffer>((resolve, reject) => {
    let png: Buffer | undefined;
    const timer = setTimeout(() => child.kill(), Math.max(1, task.expiresAtMs - Date.now()));
    timer.unref();
    child.on('message', (message: unknown) => {
      if (typeof message !== 'object' || !message || !('type' in message)) return;
      if (message.type === 'stage' && 'status' in message &&
          ['starting_emulator', 'opening_game', 'preparing_qr'].includes(String(message.status))) {
        stage(message.status as TaskStatus);
      }
      if (message.type === 'qr' && 'png' in message && typeof message.png === 'string' && message.png.length < 1_000_000) {
        const candidate = Buffer.from(message.png, 'base64');
        if (candidate.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) png = candidate;
      }
    });
    child.once('error', () => { clearTimeout(timer); reject(new Error('Emulator child process could not start')); });
    child.once('exit', code => {
      clearTimeout(timer);
      if (code === 0 && png) resolve(png);
      else reject(new Error('Emulator process exited before a QR image was produced'));
    });
    child.send({ type: 'start', id: task.id, nonce: randomBytes(32).toString('hex'), stepDelayMs, expiresAt: task.expiresAt }, error => {
      if (error) { child.kill(); reject(new Error('Could not send task to emulator process')); }
    });
  });
  return { child, result };
}
