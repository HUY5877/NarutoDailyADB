import express, { type ErrorRequestHandler } from 'express';
import { equalSecret, HttpError, type LoginRuntime } from './runtime.js';

export function createApp(runtime: LoginRuntime): express.Express {
  const app = express();
  app.disable('x-powered-by');
  app.use((_request, response, next) => {
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    next();
  });
  app.get('/health', (_request, response) => {
    response.json({ status: 'ok', simulation: true, storage: runtime.config.storageDriver, ...runtime.stats });
  });
  app.use('/internal', (request, response, next) => {
    if (!equalSecret(request.get('X-Service-Token'), runtime.config.serviceToken)) {
      response.status(401).json({ error: 'Unauthorized' });
      return;
    }
    next();
  });
  app.use(express.json({ limit: '8kb' }));
  app.post('/internal/tasks', (request, response) => {
    const id = runtime.submit(request.body as unknown);
    response.status(202).json({ id, status: 'queued' });
  });
  app.get('/internal/tasks/:id', (request, response) => {
    const task = runtime.getTask(request.params.id, request.get('X-Session-Token'));
    if (!task) { response.status(404).json({ error: 'Task not found' }); return; }
    response.json(task);
  });
  app.route('/internal/objects/:id/qr.png')
    .head(async (request, response) => {
      if (!runtime.objectAvailable(request.params.id)) { response.sendStatus(404); return; }
      const object = await runtime.storage.head(request.params.id.toLowerCase());
      if (!object || !runtime.objectAvailable(request.params.id)) { response.sendStatus(404); return; }
      response.set({ 'Content-Type': object.contentType, 'Content-Length': String(object.contentLength), 'X-Expires-At': object.expiresAt }).status(200).end();
    })
    .get(async (request, response) => {
      if (!runtime.objectAvailable(request.params.id)) { response.status(404).json({ error: 'Object not found' }); return; }
      const object = await runtime.storage.get(request.params.id.toLowerCase());
      if (!object || !runtime.objectAvailable(request.params.id)) { response.status(404).json({ error: 'Object not found' }); return; }
      response.set({ 'Content-Type': object.contentType, 'X-Expires-At': object.expiresAt }).send(object.body);
    });
  app.use((_request, response) => { response.status(404).json({ error: 'Not found' }); });
  const errorHandler: ErrorRequestHandler = (error: unknown, _request, response, _next) => {
    if (error instanceof HttpError) { response.status(error.status).json({ error: error.message }); return; }
    if (error instanceof SyntaxError) { response.status(400).json({ error: 'Invalid JSON' }); return; }
    if (typeof error === 'object' && error && 'status' in error && error.status === 413) {
      response.status(413).json({ error: 'Request body is too large' }); return;
    }
    response.status(500).json({ error: 'Internal service error' });
  };
  app.use(errorHandler);
  return app;
}
