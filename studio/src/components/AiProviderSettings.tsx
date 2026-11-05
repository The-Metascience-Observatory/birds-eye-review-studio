"use client";

/** Account-level AI-provider settings.
 *
 *  Five choices: this machine's shared Claude or Codex login, or one of three
 *  bring-your-own credentials. The BYO options exist because the host's `claude`
 *  CLI authenticates from the SERVER's home directory — one account for
 *  everybody — so without them every AI step on a shared instance spends the
 *  operator's subscription. A user who supplies their own credential is billed
 *  to their own account and is never queued behind the shared-CLI lease.
 *
 *  Secrets are write-only from the client's point of view: they are encrypted
 *  server-side and never sent back, so the UI can only ever report WHETHER one
 *  is stored, plus the account it verified as.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "@/lib/toast";

import { getUserSettings, updateUserSettings } from "@/lib/api";
import type { LlmProvider, UserSettingsIn, UserSettingsOut } from "@/lib/types";

const PROVIDER_LABEL: Record<LlmProvider, string> = {
  claude_cli: "this machine's Claude login",
  codex_cli: "this machine's Codex login (Luna)",
  claude_token: "your Claude subscription",
  anthropic_key: "your Anthropic API key",
  openrouter: "OpenRouter",
};

/** Everything that differs between the three bring-your-own credentials, so the
 *  field, the save patch, and the "is one stored" check stay in step. */
type SecretField = {
  provider: LlmProvider;
  patchKey: keyof UserSettingsIn;
  label: string;
  placeholder: string;
  help: React.ReactNode;
  stored: (s: UserSettingsOut) => boolean;
  account: (s: UserSettingsOut) => string | null;
};

const SECRET_FIELDS: SecretField[] = [
  {
    provider: "claude_token",
    patchKey: "claude_token",
    label: "Claude token",
    placeholder: "sk-ant-oat…",
    help: (
      <>
        Run <code className="rounded bg-muted px-1">claude setup-token</code> on
        your own machine, approve the browser prompt, and paste the result. It is
        valid for one year and bills your own Claude plan.
      </>
    ),
    stored: (s) => s.has_claude_token,
    account: (s) => s.claude_token_account,
  },
  {
    provider: "anthropic_key",
    patchKey: "anthropic_key",
    label: "Anthropic API key",
    placeholder: "sk-ant-…",
    help: <>From platform.claude.com. Billed per token to your own account.</>,
    stored: (s) => s.has_anthropic_key,
    account: (s) => s.anthropic_key_account,
  },
  {
    provider: "openrouter",
    patchKey: "openrouter_key",
    label: "OpenRouter API key",
    placeholder: "sk-or-v1-…",
    help: <>Get a key at openrouter.ai/keys.</>,
    stored: (s) => s.has_openrouter_key,
    account: () => null,
  },
];

/** Days until the stored subscription token stops working, or null. */
function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  return Math.ceil(ms / 86_400_000);
}

export function AiProviderSettings() {
  const qc = useQueryClient();
  const settings = useQuery({
    queryKey: ["user-settings"],
    queryFn: getUserSettings,
    staleTime: 5 * 60 * 1000,
  });

  const [provider, setProvider] = useState<LlmProvider>("codex_cli");
  // One buffer per credential kind, so switching options doesn't carry a typed
  // secret across to a different provider's field.
  const [secrets, setSecrets] = useState<Partial<Record<LlmProvider, string>>>({});
  const [dirty, setDirty] = useState(false);

  // Seed local state once the server value arrives (and whenever it changes and
  // the user hasn't started editing).
  useEffect(() => {
    if (settings.data && !dirty) setProvider(settings.data.llm_provider);
  }, [settings.data, dirty]);

  const data = settings.data;

  const save = useMutation({
    mutationFn: (patch: UserSettingsIn) => updateUserSettings(patch),
    onSuccess: () => {
      toast.success("Settings saved");
      setSecrets({});
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["user-settings"] });
      qc.invalidateQueries({ queryKey: ["me"] });
      qc.invalidateQueries({ queryKey: ["llm-session"] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Could not save settings"),
  });

  function onSave() {
    if (!data) return;
    const patch: UserSettingsIn = { llm_provider: provider };
    // Send only fields the user actually typed into — an empty string CLEARS a
    // stored credential server-side, so an untouched field must be omitted.
    for (const f of SECRET_FIELDS) {
      const typed = (secrets[f.provider] ?? "").trim();
      if (typed) (patch as Record<string, string>)[f.patchKey as string] = typed;
    }
    // Mirror the server's 422 locally for an instant message.
    const field = SECRET_FIELDS.find((f) => f.provider === provider);
    if (field && !field.stored(data) && !(secrets[provider] ?? "").trim()) {
      toast.error(`Add your ${field.label} before selecting it.`);
      return;
    }
    save.mutate(patch);
  }

  if (settings.isLoading || !data) {
    return (
      <section className="mt-6 rounded-lg border border-border bg-card p-4">
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 size={14} className="animate-spin" /> Loading…
        </p>
      </section>
    );
  }

  const expiresInDays = daysUntil(data.claude_token_expires_at);

  return (
    <section className="mt-6 rounded-lg border border-border bg-card p-4">
      <h2 className="text-sm font-semibold">AI provider</h2>
      <p className="mt-1 text-xs text-muted-foreground">
        Which account runs the AI steps — classification, extraction, the review
        assistant, and search-query drafting.
      </p>

      <div className="mt-4 space-y-2">
        <ProviderOption
          checked={provider === "claude_cli"}
          onSelect={() => {
            setProvider("claude_cli");
            setDirty(true);
          }}
          title="This machine's Claude login (shared)"
          desc="Uses the host's own Claude Code login. No credential needed, but everyone shares one account, so AI work queues behind whoever is using it."
        />
        <ProviderOption
          checked={provider === "codex_cli"}
          onSelect={() => {
            setProvider("codex_cli");
            setDirty(true);
          }}
          title="This machine's Codex login — Luna (shared)"
          desc="Uses the host's Codex CLI login and gpt-5.6-luna for existing model presets. Shares the host CLI reservation with Claude."
        />
        <ProviderOption
          checked={provider === "claude_token"}
          onSelect={() => {
            setProvider("claude_token");
            setDirty(true);
          }}
          title="Your own Claude subscription (recommended)"
          desc="Runs on your own Claude plan with a token you generate. Never queued behind anyone, and billed to you."
        />
        <ProviderOption
          checked={provider === "anthropic_key"}
          onSelect={() => {
            setProvider("anthropic_key");
            setDirty(true);
          }}
          title="Your own Anthropic API key"
          desc="Pay-per-token on your own Anthropic account. No Claude subscription required."
        />
        <ProviderOption
          checked={provider === "openrouter"}
          onSelect={() => {
            setProvider("openrouter");
            setDirty(true);
          }}
          title="OpenRouter"
          desc="Runs on your own OpenRouter account and API key (billed to you)."
        />
      </div>

      {SECRET_FIELDS.map((f) => {
        const stored = f.stored(data);
        // Shown when it is the pending choice, or when one is already stored so
        // it can be rotated or removed.
        if (provider !== f.provider && !stored) return null;
        const account = f.account(data);
        return (
          <div key={f.provider} className="mt-4">
            <label className="block text-xs font-medium">{f.label}</label>
            <div className="mt-1.5 flex items-center gap-2">
              <input
                type="password"
                autoComplete="off"
                value={secrets[f.provider] ?? ""}
                onChange={(e) => {
                  setSecrets((prev) => ({ ...prev, [f.provider]: e.target.value }));
                  setDirty(true);
                }}
                placeholder={stored ? "•••••••••• (saved)" : f.placeholder}
                className="min-w-0 flex-1 rounded border border-border bg-background px-2 py-1.5 text-sm placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-primary"
              />
              {stored && (
                <span
                  className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-emerald-600 dark:text-emerald-400"
                  title={account ? `Verified as ${account}` : "A credential is stored"}
                >
                  <Check size={13} /> {account ?? "saved"}
                </span>
              )}
            </div>
            <p className="mt-1 text-[11px] text-muted-foreground">
              {f.help} Stored encrypted; checked against your account before it is
              saved.
              {stored && (
                <>
                  {" "}
                  <button
                    type="button"
                    onClick={() =>
                      save.mutate({ [f.patchKey]: "" } as UserSettingsIn)
                    }
                    disabled={save.isPending}
                    className="underline hover:text-foreground disabled:opacity-50"
                  >
                    Remove
                  </button>
                </>
              )}
            </p>
            {/* The subscription token has a fixed 12-month life, so warn before
                it lapses rather than after a run has already failed. */}
            {f.provider === "claude_token" &&
              stored &&
              expiresInDays !== null &&
              expiresInDays <= 30 && (
                <p className="mt-1.5 flex items-start gap-1.5 text-[11px] text-amber-700 dark:text-amber-400">
                  <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                  {expiresInDays <= 0
                    ? "This token has expired. Run `claude setup-token` again and paste the new one."
                    : `This token expires in ${expiresInDays} day${expiresInDays === 1 ? "" : "s"}. Run \`claude setup-token\` again to replace it.`}
                </p>
              )}
          </div>
        );
      })}

      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          onClick={onSave}
          disabled={save.isPending || !dirty}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
        >
          {save.isPending && <Loader2 size={13} className="animate-spin" />}
          Save
        </button>
        {!dirty && (
          <span className="text-xs text-muted-foreground">
            Using {PROVIDER_LABEL[data.llm_provider] ?? data.llm_provider}
          </span>
        )}
      </div>
    </section>
  );
}

function ProviderOption({
  checked,
  onSelect,
  title,
  desc,
}: {
  checked: boolean;
  onSelect: () => void;
  title: string;
  desc: string;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-md border border-border p-3 hover:bg-muted/40">
      <input
        type="radio"
        name="llm-provider"
        checked={checked}
        onChange={onSelect}
        className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
      />
      <span className="min-w-0">
        <span className="block text-sm font-medium">{title}</span>
        <span className="mt-0.5 block text-xs text-muted-foreground">{desc}</span>
      </span>
    </label>
  );
}
