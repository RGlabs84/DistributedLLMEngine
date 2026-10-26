/**
 * Keeps a request that has sent its prompt and heard nothing back from waiting in silence. While the
 * first reply byte is outstanding it tells the user either where they stand in line (all coding slots
 * busy) or, if the swarm is not busy, simply that the answer is being waited for. No vscode import.
 *
 * The coordinator may say where the request stands itself (`dlne_queue`, see queue.ts), in which case
 * `report` is called and the watcher stops asking. Otherwise it reads the queue counts from
 * /v1/cluster/status, which every coordinator since 2.1 serves.
 */
import { CodingQueueStats, PlaceEstimator, QueueInfo, QueueSource, QueueUpdate, isBusy, queueFromStats } from './queue';

export interface WaitTiming {
  /** Silence before the first message. */
  firstMs: number;
  /** Between messages after that. */
  everyMs: number;
}

export const DEFAULT_WAIT_TIMING: WaitTiming = { firstMs: 2000, everyMs: 4000 };

export interface WaitWatcherOptions {
  /** The coding queue as the coordinator reports it now; null when it does not. */
  fetchQueue: () => Promise<CodingQueueStats | null>;
  /** The request's priority class (0 = GPU contributors and keys, 1 = CPU pool, 2 = free trial). */
  priority: number;
  /** A line of plain progress. */
  progress?: (text: string) => void;
  /** The request is queued: here is its place. */
  queue: (update: QueueUpdate) => void;
  timing?: WaitTiming;
}

export class WaitWatcher {
  private timer: NodeJS.Timeout | null = null;
  private stopped = false;
  private coordinatorSpeaks = false;
  private readonly startedAt = Date.now();
  private readonly estimator = new PlaceEstimator();

  constructor(private readonly options: WaitWatcherOptions) {}

  start(): void {
    this.schedule((this.options.timing ?? DEFAULT_WAIT_TIMING).firstMs);
  }

  /** The coordinator itself said where the request stands: that beats any estimate, and polling stops. */
  report(info: QueueInfo, source: Exclude<QueueSource, 'status'>): void {
    if (this.stopped) return;
    this.coordinatorSpeaks = true;
    this.options.queue({ ...info, source, waitedMs: Date.now() - this.startedAt, priority: this.options.priority });
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private schedule(ms: number): void {
    if (this.stopped) return;
    this.timer = setTimeout(() => void this.tick(), ms);
    this.timer.unref?.();
  }

  private async tick(): Promise<void> {
    if (this.stopped) return;
    const waitedMs = Date.now() - this.startedAt;
    if (!this.coordinatorSpeaks) {
      let stats: CodingQueueStats | null = null;
      try {
        stats = await this.options.fetchQueue();
      } catch {
        // the status is a courtesy; the wait goes on without it
      }
      if (this.stopped) return;
      if (isBusy(stats)) {
        this.options.queue(queueFromStats(stats, this.estimator, this.options.priority, waitedMs));
      } else {
        this.options.progress?.(
          `⏳ Waiting for the swarm to start your answer (${Math.round(waitedMs / 1000)} s). Long prompts take longer to read.`
        );
      }
    }
    this.schedule((this.options.timing ?? DEFAULT_WAIT_TIMING).everyMs);
  }
}
