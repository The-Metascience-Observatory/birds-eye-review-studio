/** The job engine: list / poll / cancel jobs and stream a job's log. */

import { ApiError, authFetch, request } from "./client";
import type { JobOut } from "../types";

export function listJobs(id: string): Promise<JobOut[]> {
  return request<JobOut[]>(`/reviews/${id}/jobs`);
}

export function getJob(jobId: string): Promise<JobOut> {
  return request<JobOut>(`/jobs/${jobId}`);
}

/** Stop a queued or running job. Without this a stuck job blocks every run on
 *  the review, since only one may be active at a time. */
export function cancelJob(jobId: string): Promise<JobOut> {
  return request<JobOut>(`/jobs/${jobId}/cancel`, { method: "POST" });
}

/**
 * Stream a job's log over SSE, replacing the old poll-the-whole-file loop.
 *
 * EventSource can't send an Authorization header, so this uses fetch + a
 * ReadableStream reader. `onLine` gets each log line as it arrives; `onEnd`
 * fires once with the job's terminal state. Returns an abort function.
 */
export function streamJobLog(
  jobId: string,
  onLine: (line: string) => void,
  onEnd?: (state: string) => void,
  onError?: (err: Error) => void,
): () => void {
  const controller = new AbortController();

  (async () => {
    try {
      const res = await authFetch(`/jobs/${jobId}/logs`, {
        signal: controller.signal,
        errorPrefix: "Log stream failed",
      });
      if (!res.body) throw new ApiError(res.status, "Log stream had no body");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line.
        const frames = buf.split("\n\n");
        buf = frames.pop() ?? "";
        for (const frame of frames) {
          let event = "message";
          const data: string[] = [];
          for (const raw of frame.split("\n")) {
            if (raw.startsWith("event:")) event = raw.slice(6).trim();
            else if (raw.startsWith("data:")) data.push(raw.slice(5).replace(/^ /, ""));
          }
          if (event === "end") {
            onEnd?.(data.join("\n"));
            controller.abort();
            return;
          }
          if (data.length) onLine(data.join("\n"));
        }
      }
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return; // caller unmounted
      onError?.(err as Error);
    }
  })();

  return () => controller.abort();
}
