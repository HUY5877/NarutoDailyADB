import 'dotenv/config';
import { createApp } from './app.js';
import { loadConfig } from './config.js';
import { LoginRuntime } from './runtime.js';

const config = loadConfig();
const runtime = new LoginRuntime(config);
await runtime.start();
const server = createApp(runtime).listen(config.port, config.host, () => {
  console.log(`Login service listening on http://${config.host}:${config.port} (emulator=${config.emulatorDriver}, storage=${config.storageDriver})`);
});
let shuttingDown = false;
async function shutdown(): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;
  server.close();
  await runtime.stop();
  server.closeAllConnections();
}
process.on('SIGINT', () => { void shutdown(); });
process.on('SIGTERM', () => { void shutdown(); });
server.on('error', error => {
  console.error(`Could not listen: ${error.message}`);
  process.exitCode = 1;
  void shutdown();
});
