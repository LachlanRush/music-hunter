// The Pi's API (unchanged from the multi-page app), and what it returns.

export interface Track {
  id: number;
  artist: string;
  title: string;
  duration_s: number | null;
  gain_db: number | null;
  bpm: number | null;
  bpm_shown: number | null;
  stars: number | null;
  status: "member" | "added";
  audio: string | null;     // "/audio/<id>", or null while it has none
  art: string | null;
  rate: number;             // what it plays at under the playlist's Set to BPM (1 = its own tempo)
  tempo_note: string;
  starred_at: string | null;
  added_at: string | null;
  position: number | null;
  bpm_override: number | null;
}

export interface Tempo {
  on: boolean;
  bpm: number | null;
  target: number;
  auto: number | null;
  max_stretch: number;
}

export interface Failure {
  artist: string;
  title: string;
  reason: string;
}

export interface Job {
  id: number;
  kind: string;                // "import", "add", …
  state: "queued" | "running" | "done" | "failed";
  progress: {done?: number; total?: number; message?: string; failures?: Failure[]} | null;
  error: string | null;
  payload_link?: string | null;
}

export interface DeletedTrack extends Track {
  deleted_at: string;
}

export interface Playlist {
  id: number;
  name: string;
  description: string | null;
  image: string | null;
  tracks: Track[];
  deleted?: DeletedTrack[];
  tempo: Tempo;
  min_stars: number;
  back_restart_s: number;
  phone_headroom_db: number;
  folder_id: number | null;
  folders?: Folder[];
  others?: {id: number; name: string}[];
  job?: Job | null;
  can_refresh?: boolean;
  sync?: {on: boolean; tracks: number; ready: number};
}

export const busy = (j: Job | null | undefined): boolean => !!j && (j.state === "queued" || j.state === "running");

export interface PlaylistSummary {
  id: number;
  name: string;
  description: string | null;
  image: string | null;
  folder_id: number | null;
  position: number | null;
  tracks: number;
  job?: Job | null;
}

export interface Folder {
  id: number;
  name: string;
  position: number | null;
  builtin?: boolean;
  tracks?: number;
  duration_s?: number;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export async function api<T>(path: string, body?: unknown, method?: string): Promise<T> {
  const init: RequestInit = body === undefined && !method ? {} : {
    method: method ?? "POST",
    headers: body === undefined ? undefined : {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body),
  };
  const r = await fetch(path, init);
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new ApiError((d as {detail?: string}).detail || r.statusText, r.status);
  }
  return (r.status === 204 ? null : await r.json()) as T;
}

// Shown at once from what was seen last (kept on the device), then refreshed from the Pi.
export function cached<T>(key: string): T | null {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : null;
  } catch {
    return null;
  }
}

export function keep(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage full or blocked: only a speed-up */
  }
}
