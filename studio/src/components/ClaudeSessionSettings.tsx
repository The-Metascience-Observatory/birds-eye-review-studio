"use client";

/** The host's shared Claude CLI login.
 *
 *  The `claude` CLI authenticates from the SERVER's home directory, not per
 *  request — so LLM work runs under one Claude account for the whole machine.
 *  Rather than let that happen silently (one person spending another's
 *  subscription), the server hands out a single "lease": one user at a time
 *  claims the CLI, runs their work, and releases it.
 *
 *  Logging in cannot happen from this page: `claude auth login` is interactive.
 *  We show the exact command to run on the host instead. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Loader2, LogOut, Terminal } from "lucide-react";
import { toast } from "@/lib/toast";

import { claimLlmSession, getLlmSession, releaseLlmSession } from "@/lib/api";

export function ClaudeSessionSettings() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["llm-session"],
    queryFn: getLlmSession,
    // The holder can change from another browser; keep this reasonably fresh.
    refetchInterval: 30_000,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["llm-session"] });

  const claim = useMutation({
    mutationFn: (force: boolean) => claimLlmSession(force),
    onSuccess: (d) => {
      toast.success(
        d.claude_account
          ? `You now hold the shared host CLI (${d.claude_account})`
          : "You now hold the shared host CLI",
      );
      invalidate();
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Could not claim the CLI"),
  });

  const release = useMutation({
    mutationFn: releaseLlmSession,
    onSuccess: () => {
      toast.success("Released — someone else can now use it");
      invalidate();
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Could not release"),
  });

  if (q.isLoading) {
    return (
      <section className="mt-6 rounded-lg border border-border bg-card p-4">
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 size={14} className="animate-spin" /> Loading…
        </p>
      </section>
    );
  }
  if (!q.data) return null;
  const s = q.data;

  // OpenRouter users carry their own key and never touch the shared CLI.
  if (s.provider === "openrouter") {
    return (
      <section className="mt-6 rounded-lg border border-border bg-card p-4">
        <h2 className="text-sm font-semibold">Claude CLI</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          Not used — your account runs on OpenRouter with your own API key, so
          you&rsquo;re never queued behind anyone else.
        </p>
      </section>
    );
  }

  const cliName = s.provider === "codex_cli" ? "Codex" : "Claude";
  const heldByOther = s.held && !s.held_by_me;

  return (
    <section className="mt-6 rounded-lg border border-border bg-card p-4">
      <h2 className="text-sm font-semibold">{cliName} CLI login</h2>
      <p className="mt-1 text-xs text-muted-foreground">
        Claude and Codex share <strong>one</strong> host CLI reservation, so one
        person uses it at a time. Work uses the selected CLI&rsquo;s host login.
      </p>

      {!s.cli_installed && (
        <Banner tone="bad">
          The {cliName} CLI isn&rsquo;t installed on this server. AI steps cannot run
          until it is.
        </Banner>
      )}
      {s.cli_installed && !s.cli_logged_in && (
        <Banner tone="bad">
          The CLI is installed but not logged in. {s.cli_detail} Run the command
          below on the server, then reload this page.
        </Banner>
      )}

      <dl className="mt-4 space-y-2 text-sm">
        <Row label="Signed in as">
          {s.cli_account ? (
            <span className="font-mono text-xs">{s.cli_account}</span>
          ) : (
            <span className="text-muted-foreground">unknown</span>
          )}
        </Row>
        <Row label="Currently held by">
          {!s.held ? (
            <span className="text-muted-foreground">nobody — free to claim</span>
          ) : s.held_by_me ? (
            <span className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400">
              <Check size={13} /> you
            </span>
          ) : (
            <span className="text-amber-700 dark:text-amber-400">
              {s.holder_email}
              {s.expired && " (idle — you can take over)"}
            </span>
          )}
        </Row>
      </dl>

      {heldByOther && !s.expired && (
        <Banner tone="warn">
          AI steps you start will be refused until {s.holder_email} releases the
          CLI, or it goes idle for {s.ttl_minutes} minutes. This is deliberate —
          it stops your runs from billing a shared account.
        </Banner>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {!s.held_by_me && (
          <button
            type="button"
            onClick={() => claim.mutate(s.expired)}
            disabled={claim.isPending || (heldByOther && !s.expired)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
          >
            {claim.isPending && <Loader2 size={13} className="animate-spin" />}
            {s.expired && heldByOther ? "Take over" : "Claim the CLI"}
          </button>
        )}
        {s.held_by_me && (
          <button
            type="button"
            onClick={() => release.mutate()}
            disabled={release.isPending}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-muted/40 disabled:opacity-50"
          >
            {release.isPending ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <LogOut size={13} />
            )}
            Release
          </button>
        )}
      </div>

      <details className="mt-4 rounded-md border border-border bg-muted/30 p-3">
        <summary className="cursor-pointer text-xs font-medium">
          Switch to a different {cliName} account
        </summary>
        <p className="mt-2 text-[11px] text-muted-foreground">
          Logging in is interactive, so it can&rsquo;t be done from this page. Open
          a terminal <strong>on the server</strong> and run:
        </p>
        <pre className="mt-2 overflow-x-auto rounded bg-background p-2 text-[11px]">
          <code className="flex items-center gap-1.5">
            <Terminal size={11} className="shrink-0" />
            {s.login_hint}
          </code>
        </pre>
        <p className="mt-2 text-[11px] text-muted-foreground">
          Then come back and click <strong>Claim the CLI</strong>. Everyone
          else&rsquo;s AI steps will run under that account until it&rsquo;s changed
          again.
        </p>
      </details>
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-right">{children}</dd>
    </div>
  );
}

function Banner({
  tone,
  children,
}: {
  tone: "warn" | "bad";
  children: React.ReactNode;
}) {
  const cls =
    tone === "bad"
      ? "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400"
      : "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400";
  return (
    <p className={`mt-3 flex items-start gap-2 rounded-md border p-2.5 text-[11px] ${cls}`}>
      <AlertTriangle size={13} className="mt-px shrink-0" />
      <span>{children}</span>
    </p>
  );
}
