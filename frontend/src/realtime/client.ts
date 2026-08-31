import { authStore } from "@/auth/authStore";
import { attemptRefresh } from "@/api/client";
import { SUBPROTOCOL, type MonitoringEvent, type ServerFrame } from "./protocol";

export type RealtimeStatus = "idle" | "connecting" | "open" | "reconnecting" | "degraded" | "closed";

interface Options {
  onEvent: (event: MonitoringEvent) => void;
  onStatus: (status: RealtimeStatus, detail?: { attempts: number; lastError?: string }) => void;
  onHello?: () => void; // fired on every (re)connect once hello lands -> caller resyncs via REST
}

const BACKOFF_BASE_MS = 1000;
const BACKOFF_CAP_MS = 30000;
const DEGRADED_AFTER_ATTEMPTS = 4;
const SEEN_SEQ_LIMIT = 4096;

function wsUrl(): string {
  const base = authStore.get().baseUrl;
  if (base) return base.replace(/^http/, "ws").replace(/\/+$/, "") + "/api/ws";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/ws`;
}

export class RealtimeClient {
  private ws: WebSocket | null = null;
  private status: RealtimeStatus = "idle";
  private attempts = 0;
  private stopped = false;
  private reconnectTimer: number | null = null;
  private pingTimer: number | null = null;
  private livenessTimer: number | null = null;
  private lastFrameAt = 0;
  private clientTimeoutMs = 60000;
  private pingIntervalMs = 20000;
  private triedRefresh = false;
  private seen = new Set<number>();

  constructor(private opts: Options) {}

  start(): void {
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    this.setStatus("closed");
    if (this.ws) {
      this.ws.onclose = null;
      try {
        this.ws.close(1000, "client stop");
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
  }

  getStatus(): RealtimeStatus {
    return this.status;
  }

  private setStatus(s: RealtimeStatus): void {
    if (this.status === s) return;
    this.status = s;
    this.opts.onStatus(s, { attempts: this.attempts });
  }

  private open(): void {
    if (this.stopped) return;
    const token = authStore.get().accessToken;
    if (!token) {
      // Not signed in — nothing to stream. Poll-only.
      this.setStatus("degraded");
      this.scheduleReconnect();
      return;
    }
    this.setStatus(this.attempts === 0 ? "connecting" : "reconnecting");
    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl(), [SUBPROTOCOL, `bearer.${token}`]);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.lastFrameAt = Date.now();
    };
    ws.onmessage = (ev) => this.onMessage(ev);
    ws.onerror = () => {
      /* close handler drives reconnect */
    };
    ws.onclose = (ev) => this.onClose(ev);
  }

  private onMessage(ev: MessageEvent): void {
    this.lastFrameAt = Date.now();
    let frame: ServerFrame;
    try {
      frame = JSON.parse(ev.data);
    } catch {
      return;
    }
    switch (frame.type) {
      case "hello": {
        this.attempts = 0;
        this.triedRefresh = false;
        this.clientTimeoutMs = (frame.client_timeout || 60) * 1000;
        this.pingIntervalMs = Math.max(5000, (frame.ping_interval || 20) * 1000 - 5000);
        this.setStatus("open");
        this.startTimers();
        this.opts.onHello?.();
        return;
      }
      case "ping":
        this.sendRaw({ type: "pong", ts: new Date().toISOString() });
        return;
      case "pong":
      case "subscribed":
        return;
      case "error": {
        if (frame.code === "unauthorized" && !this.triedRefresh) {
          this.triedRefresh = true;
          void attemptRefresh().finally(() => {
            /* onclose will fire next; reconnect picks up the new token */
          });
        } else if (frame.code === "unauthorized") {
          this.setStatus("degraded");
        }
        return;
      }
      default: {
        const monitoring = frame as MonitoringEvent;
        if (typeof monitoring.seq === "number") {
          if (this.seen.has(monitoring.seq)) return; // no duplicate events
          this.seen.add(monitoring.seq);
          if (this.seen.size > SEEN_SEQ_LIMIT) {
            // drop the oldest ~half
            this.seen = new Set(Array.from(this.seen).slice(-SEEN_SEQ_LIMIT / 2));
          }
        }
        this.opts.onEvent(monitoring);
      }
    }
  }

  private onClose(_ev: CloseEvent): void {
    this.clearTimers();
    this.ws = null;
    if (this.stopped) return;
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer != null) return;
    this.attempts += 1;
    this.setStatus(this.attempts >= DEGRADED_AFTER_ATTEMPTS ? "degraded" : "reconnecting");
    const delay = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** (this.attempts - 1));
    const jitter = Math.random() * 0.3 * delay;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delay + jitter);
  }

  private startTimers(): void {
    this.clearTimers();
    this.pingTimer = window.setInterval(() => {
      this.sendRaw({ type: "ping", ts: new Date().toISOString() });
    }, this.pingIntervalMs);
    this.livenessTimer = window.setInterval(() => {
      if (Date.now() - this.lastFrameAt > this.clientTimeoutMs) {
        // server went silent — force a reconnect
        try {
          this.ws?.close(4000, "liveness timeout");
        } catch {
          /* onclose drives reconnect */
        }
      }
    }, Math.max(5000, this.clientTimeoutMs / 3));
  }

  private clearTimers(): void {
    if (this.reconnectTimer != null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.pingTimer != null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.livenessTimer != null) {
      clearInterval(this.livenessTimer);
      this.livenessTimer = null;
    }
  }

  private sendRaw(obj: unknown): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify(obj));
      } catch {
        /* ignore */
      }
    }
  }
}
