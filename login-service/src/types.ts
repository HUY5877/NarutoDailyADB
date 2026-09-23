export const statuses = [
  'queued', 'starting_emulator', 'opening_game', 'preparing_qr',
  'qr_ready', 'failed', 'expired',
] as const;
export type TaskStatus = typeof statuses[number];

export interface TaskSubmission {
  id: string;
  accessToken: string;
  createdAt: string;
  expiresAt: string;
}

export interface TaskView {
  id: string;
  status: TaskStatus;
  createdAt: string;
  expiresAt: string;
  simulation: true;
  error?: string;
}

export interface TaskRecord extends TaskSubmission {
  status: TaskStatus;
  error?: string;
  expiresAtMs: number;
}

export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export const TOKEN_PATTERN = /^[0-9a-f]{64}$/i;
export const MAX_TTL_MS = 10 * 60 * 1000;
