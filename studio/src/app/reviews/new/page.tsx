"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "@/lib/toast";
import { createReview, ApiError } from "@/lib/api";
import { loginUrlForHere, useAuth } from "@/lib/auth";
import { TopBar } from "@/components/TopBar";

/** Parse a comma- or newline-separated textarea into a trimmed string list. */
function parseList(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function NewReviewPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { isAuthenticated, ready } = useAuth();

  useEffect(() => {
    if (ready && !isAuthenticated) router.replace(loginUrlForHere());
  }, [ready, isAuthenticated, router]);

  const [title, setTitle] = useState("");
  const [topicName, setTopicName] = useState("");
  const [synonyms, setSynonyms] = useState("");
  const [question, setQuestion] = useState("");
  const [description, setDescription] = useState("");
  const [notAbout, setNotAbout] = useState("");
  const [includeObservational, setIncludeObservational] = useState(true);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const review = await createReview({
        title,
        topic_name: topicName,
        question: question.trim(),
        synonyms: parseList(synonyms),
        description,
        not_about: parseList(notAbout),
        include_observational: includeObservational,
      });
      await queryClient.invalidateQueries({ queryKey: ["reviews"] });
      // No success toast: landing on the review page IS the confirmation.
      router.push(`/reviews/${review.id}`);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Create failed";
      toast.error(msg);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <TopBar />
      <main className="mx-auto max-w-2xl px-4 py-10">
        <h1 className="text-2xl font-bold">New review</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Define the topic. The pipeline workspace is created on save.
        </p>

        <form onSubmit={onSubmit} className="mt-6 space-y-5">
          <div>
            <label htmlFor="title" className="block text-sm font-medium">
              Title
            </label>
            <input
              id="title"
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Melatonin for insomnia"
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="question" className="block text-sm font-medium">
              Research question{" "}
              <span className="text-muted-foreground">(optional)</span>
            </label>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Ask in plain language — the AI drafts search terms from it. Leave
              blank to work from the topic and synonyms alone.
            </p>
            <textarea
              id="question"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              rows={2}
              placeholder="Does transcutaneous vagus nerve stimulation improve sleep quality in adults with chronic insomnia?"
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="topic" className="block text-sm font-medium">
              Topic name
            </label>
            <input
              id="topic"
              required
              value={topicName}
              onChange={(e) => setTopicName(e.target.value)}
              placeholder="Melatonin"
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="synonyms" className="block text-sm font-medium">
              Synonyms{" "}
              <span className="text-muted-foreground">
                (comma or newline separated)
              </span>
            </label>
            <textarea
              id="synonyms"
              rows={2}
              value={synonyms}
              onChange={(e) => setSynonyms(e.target.value)}
              placeholder="N-acetyl-5-methoxytryptamine, MT"
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="description" className="block text-sm font-medium">
              Description
            </label>
            <textarea
              id="description"
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Randomized and observational studies of melatonin for primary insomnia in adults."
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label htmlFor="notabout" className="block text-sm font-medium">
              Not about{" "}
              <span className="text-muted-foreground">
                (comma or newline separated)
              </span>
            </label>
            <textarea
              id="notabout"
              rows={2}
              value={notAbout}
              onChange={(e) => setNotAbout(e.target.value)}
              placeholder="jet lag, shift work, pediatric"
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            />
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={includeObservational}
              onChange={(e) => setIncludeObservational(e.target.checked)}
              className="h-4 w-4 rounded border-border"
            />
            Include observational studies
          </label>

          <div className="flex gap-3 pt-2">
            <button
              type="submit"
              disabled={busy}
              className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {busy ? "Creating…" : "Create review"}
            </button>
            <button
              type="button"
              onClick={() => router.push("/dashboard")}
              className="rounded-md border border-border px-4 py-2 text-sm font-medium hover:bg-muted"
            >
              Cancel
            </button>
          </div>
        </form>
      </main>
    </div>
  );
}
