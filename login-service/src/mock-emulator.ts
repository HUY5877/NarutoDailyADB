import QRCode from 'qrcode';
import { setTimeout as delay } from 'node:timers/promises';
import { TOKEN_PATTERN, UUID_PATTERN } from './types.js';

// This is a process-isolated emulator simulator. It never opens LDPlayer or logs in to a real account.
interface StartMessage { type: 'start'; id: string; nonce: string; stepDelayMs: number; expiresAt: string }
let started = false;

async function run(message: StartMessage): Promise<void> {
  process.send?.({ type: 'stage', status: 'starting_emulator', pid: process.pid });
  await delay(message.stepDelayMs);
  process.send?.({ type: 'stage', status: 'opening_game' });
  await delay(message.stepDelayMs);
  // Adapter seam: Android friends -> switch account -> QR login. Real UI clicks are deferred.
  process.send?.({ type: 'stage', status: 'preparing_qr' });
  await delay(message.stepDelayMs);
  const payload = JSON.stringify({
    kind: 'naruto-login-demo',
    simulation: true,
    taskId: message.id,
    nonce: message.nonce,
    expiresAt: message.expiresAt,
    message: 'Mock QR only. This code cannot authenticate a game account.',
  });
  const png = await QRCode.toBuffer(payload, { type: 'png', width: 360, margin: 3, errorCorrectionLevel: 'M' });
  process.send?.({ type: 'qr', png: png.toString('base64') }, error => {
    process.exitCode = error ? 1 : 0;
    process.disconnect();
  });
}

process.on('message', (message: unknown) => {
  if (started) return;
  if (typeof message !== 'object' || !message || !('type' in message) || message.type !== 'start') return;
  const input = message as StartMessage;
  if (!UUID_PATTERN.test(input.id) || !TOKEN_PATTERN.test(input.nonce) ||
      !Number.isInteger(input.stepDelayMs) || input.stepDelayMs < 0 || input.stepDelayMs > 10000) {
    process.exit(1);
  }
  started = true;
  void run(input).catch(() => process.exit(1));
});
process.on('disconnect', () => process.exit());
process.on('SIGTERM', () => process.exit());
// Parent must send a task promptly. This also prevents manually started orphan processes.
setTimeout(() => { if (!started) process.exit(1); }, 10000).unref();
