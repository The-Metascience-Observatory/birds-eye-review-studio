"use client";

/** The paper-detail drawer's "Extraction" section: everything one pass-2
 *  record captured (outcomes + effects with their verbatim quotes, sample
 *  sizes, arms, demographics, adverse events, follow-up, risk of bias,
 *  subgroups, custom answers, extractor notes, provenance), as data-gated
 *  collapsible blocks — lighter extraction tiers simply show fewer of them. */

import { ChevronRight } from "lucide-react";

import type {
  ExtractionArmDetail,
  ExtractionOutcome,
  PaperExtraction,
} from "@/lib/api";
import { titleCase } from "@/lib/format";

/** A verbatim quote copied from the paper — the sentence a number came from. */
function Quote({ text }: { text?: string | null }) {
  if (!text) return null;
  return (
    <blockquote className="mt-1 border-l-2 border-border pl-2 text-[11px] italic leading-relaxed text-muted-foreground">
      &ldquo;{text}&rdquo;
    </blockquote>
  );
}

/** One collapsible subsection of the extraction block. */
function Sub({
  title,
  defaultOpen = false,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  return (
    <details open={defaultOpen} className="group border-t border-border/60 py-2 first:border-t-0 first:pt-0">
      <summary className="flex cursor-pointer list-none items-center gap-1 text-xs font-semibold text-foreground/80 hover:text-foreground [&::-webkit-details-marker]:hidden">
        <ChevronRight
          size={12}
          className="shrink-0 transition-transform group-open:rotate-90"
        />
        {title}
      </summary>
      <div className="mt-2 space-y-2 pl-4 text-xs text-foreground/80">{children}</div>
    </details>
  );
}

const num = (v: number | null | undefined): string | null =>
  v == null ? null : Number(v).toLocaleString();

function fmtP(p: number | null | undefined): string {
  if (p == null) return "";
  return p < 0.001 ? "p<0.001" : `p=${p}`;
}

/** "MD −4.2 (95% CI −7.1 to −1.3), p=0.004 [adjusted]" from one effect entry. */
function fmtEffect(e: NonNullable<ExtractionOutcome["between_group_effects"]>[number]): string {
  const parts: string[] = [];
  if (e.effect_measure || e.effect_value != null) {
    parts.push(
      [e.effect_measure, e.effect_value != null ? String(e.effect_value) : null]
        .filter(Boolean)
        .join(" "),
    );
  }
  if (e.ci_95_low != null && e.ci_95_high != null) {
    parts.push(`(95% CI ${e.ci_95_low} to ${e.ci_95_high})`);
  }
  const p = fmtP(e.p_value);
  if (p) parts.push(p);
  if (e.adjusted) parts.push("[adjusted]");
  return parts.join(" ");
}

/** "n=40: mean 4.2 ± 1.1" or "12/40 events" from one arm-result entry. */
function fmtArmResult(r: NonNullable<ExtractionOutcome["arm_results"]>[number]): string {
  const bits: string[] = [];
  if (r.mean != null) bits.push(`mean ${r.mean}${r.sd != null ? ` ± ${r.sd}` : ""}`);
  if (r.events != null) bits.push(`${r.events}${r.total != null ? `/${r.total}` : ""} events`);
  if (r.n != null) bits.push(`n=${r.n}`);
  return bits.join(", ");
}

/** A compact "label: value · label: value" line; skips empty entries. */
function Facts({ items }: { items: [string, string | null | undefined][] }) {
  const kept = items.filter(([, v]) => v != null && v !== "");
  if (!kept.length) return null;
  return (
    <p>
      {kept.map(([k, v], i) => (
        <span key={k}>
          {i > 0 && " · "}
          <span className="text-muted-foreground">{k}: </span>
          {v}
        </span>
      ))}
    </p>
  );
}

function OutcomeBlock({ o }: { o: ExtractionOutcome }) {
  const timepointLabel = (id?: string | null) =>
    o.timepoints?.find((t) => t.timepoint_id === id)?.label ?? id ?? null;
  return (
    <details open={!!o.is_primary} className="group/outcome rounded border border-border/60 px-2 py-1.5">
      <summary className="flex cursor-pointer list-none items-start gap-1 [&::-webkit-details-marker]:hidden">
        <ChevronRight
          size={12}
          className="mt-0.5 shrink-0 transition-transform group-open/outcome:rotate-90"
        />
        <span className="min-w-0">
          <span className="font-medium">{o.name || o.outcome_id || "Outcome"}</span>
          {o.is_primary && (
            <span className="ml-1.5 rounded bg-blue-600/10 px-1 py-0.5 text-[10px] font-semibold text-blue-700 dark:text-blue-400">
              Primary
            </span>
          )}
        </span>
      </summary>
      <div className="mt-1.5 space-y-1.5 pl-4">
        <Facts
          items={[
            ["Instrument", o.measurement_instrument],
            ["Domain", o.symptom_domain ? titleCase(o.symptom_domain) : null],
            [
              "Timepoints",
              o.timepoints?.length
                ? o.timepoints.map((t) => t.label ?? t.timepoint_id).filter(Boolean).join(", ")
                : null,
            ],
          ]}
        />
        {(o.arm_results ?? []).map((r, i) => (
          <div key={i}>
            <p>
              <span className="text-muted-foreground">
                {[r.arm_id, timepointLabel(r.timepoint_id)].filter(Boolean).join(" @ ")}:{" "}
              </span>
              {fmtArmResult(r) || "—"}
            </p>
            <Quote text={r.result_quote} />
          </div>
        ))}
        {(o.between_group_effects ?? []).map((e, i) => (
          <div key={i}>
            <p>
              <span className="text-muted-foreground">
                Effect{e.comparison ? ` (${e.comparison}` : ""}
                {e.comparison && timepointLabel(e.timepoint_id)
                  ? ` @ ${timepointLabel(e.timepoint_id)})`
                  : e.comparison
                    ? ")"
                    : ""}
                :{" "}
              </span>
              {fmtEffect(e) || "—"}
            </p>
            <Quote text={e.effect_quote} />
          </div>
        ))}
      </div>
    </details>
  );
}

function ArmSizeRow({ a }: { a: ExtractionArmDetail }) {
  return (
    <div>
      <p>
        <span className="font-medium">{a.arm_id ?? "arm"}</span>
        {": "}
        {[
          num(a.n_randomized ?? a.n_enrolled) &&
            `${num(a.n_randomized ?? a.n_enrolled)} ${a.n_randomized != null ? "randomized" : "enrolled"}`,
          num(a.n_completed) && `${num(a.n_completed)} completed`,
          a.n_analyzed_primary != null &&
            `${num(a.n_analyzed_primary)} analyzed${a.analysis_population ? ` (${a.analysis_population})` : ""}`,
          a.n_lost_to_followup != null && `${num(a.n_lost_to_followup)} lost to follow-up`,
          a.n_withdrawn != null && `${num(a.n_withdrawn)} withdrew`,
        ]
          .filter(Boolean)
          .join(" · ") || "—"}
      </p>
      {!!a.withdrawal_reasons?.length && (
        <p className="text-[11px] text-muted-foreground">
          Withdrawals: {a.withdrawal_reasons.join("; ")}
        </p>
      )}
      <Quote text={a.n_randomized_quote ?? a.n_enrolled_quote} />
    </div>
  );
}

function DemographicsRow({ a, label }: { a: ExtractionArmDetail; label: string }) {
  return (
    <Facts
      items={[
        ["Arm", label],
        ["Age", a.age_mean != null ? `${a.age_mean}${a.age_sd != null ? ` ± ${a.age_sd}` : ""}` : null],
        ["% female", a.percent_female != null ? String(a.percent_female) : null],
        ["BMI", a.bmi_mean != null ? String(a.bmi_mean) : null],
        ["Comorbidities", a.comorbidities?.length ? a.comorbidities.join(", ") : null],
      ]}
    />
  );
}

/** Everything one pass-2 record captured, as data-gated collapsible blocks. */
export function ExtractionDetail({ rec }: { rec: PaperExtraction }) {
  const sd = rec.study_design ?? {};
  const arms = sd.arms ?? [];
  const sizes = rec.sample_sizes ?? {};
  const demo = rec.baseline_demographics;
  const demoRows: [string, ExtractionArmDetail][] = [
    ...(demo?.per_arm ?? []).map(
      (a): [string, ExtractionArmDetail] => [String(a.arm_id ?? "arm"), a],
    ),
    ...(demo?.overall ? ([["overall", demo.overall]] as [string, ExtractionArmDetail][]) : []),
  ].filter(([, a]) => a.age_mean != null || a.percent_female != null || a.bmi_mean != null);
  const outcomes = rec.outcomes ?? [];
  const ae = rec.adverse_events;
  const fu = rec.follow_up;
  const rob = rec.risk_of_bias;
  const subs = (rec.subgroup_analyses ?? []) as {
    variable?: string;
    subgroups?: string[];
    outcome_name?: string;
    interaction_p_value?: number | null;
    pre_specified?: boolean;
    results_summary?: string;
  }[];
  const prov = rec._provenance;

  return (
    <div>
      {outcomes.length > 0 && (
        <Sub title={`Outcomes & effects (${outcomes.length})`} defaultOpen>
          {outcomes.map((o, i) => (
            <OutcomeBlock key={o.outcome_id ?? i} o={o} />
          ))}
        </Sub>
      )}

      {(sizes.n_randomized_total != null ||
        sizes.n_enrolled_total != null ||
        !!sizes.per_arm?.length) && (
        <Sub title="Sample sizes">
          <Facts
            items={[
              ["Screened", num(sizes.n_screened)],
              ["Eligible", num(sizes.n_eligible)],
              ["Randomized", num(sizes.n_randomized_total)],
              ["Enrolled", num(sizes.n_enrolled_total)],
            ]}
          />
          {(sizes.per_arm ?? []).map((a, i) => (
            <ArmSizeRow key={a.arm_id ?? i} a={a} />
          ))}
        </Sub>
      )}

      {arms.length > 0 && (
        <Sub title="Study design & arms">
          <Facts
            items={[
              ["Design", sd.design_type ? titleCase(sd.design_type) : null],
              ["Blinding", sd.blinding ? titleCase(sd.blinding) : null],
              ["Setting", sd.setting],
              ["Countries", sd.countries?.length ? sd.countries.join(", ") : null],
            ]}
          />
          {arms.map((a, i) => (
            <p key={a.arm_id ?? i}>
              <span className="font-medium">{a.label ?? a.arm_id ?? "arm"}</span>
              {a.type ? (
                <span className="text-muted-foreground"> ({titleCase(a.type)})</span>
              ) : null}
              {": "}
              {[
                a.intervention_name,
                a.dose,
                a.route,
                a.frequency,
                a.duration_value != null
                  ? `${a.duration_value} ${a.duration_unit ?? ""}`.trim()
                  : null,
              ]
                .filter(Boolean)
                .join(" · ") || "—"}
            </p>
          ))}
        </Sub>
      )}

      {demoRows.length > 0 && (
        <Sub title="Baseline demographics">
          {demoRows.map(([label, a], i) => (
            <DemographicsRow key={i} a={a} label={label} />
          ))}
        </Sub>
      )}

      {!!ae?.per_arm?.length && (
        <Sub title="Adverse events">
          {ae.reporting_quality && (
            <p className="text-muted-foreground">
              Reporting quality: {titleCase(ae.reporting_quality)}
            </p>
          )}
          {ae.per_arm.map((a, i) => (
            <div key={a.arm_id ?? i}>
              <Facts
                items={[
                  ["Arm", String(a.arm_id ?? "arm")],
                  ["Any AE", num(a.n_any_ae)],
                  ["Serious", num(a.n_serious_ae)],
                  ["Withdrew due to AE", num(a.n_withdrawn_due_to_ae)],
                  ["Deaths", num(a.n_deaths)],
                ]}
              />
              {!!a.notable_aes?.length && (
                <p className="text-[11px] text-muted-foreground">
                  Notable: {a.notable_aes.join("; ")}
                </p>
              )}
            </div>
          ))}
        </Sub>
      )}

      {fu &&
        (fu.treatment_duration_weeks != null ||
          fu.total_duration_weeks != null ||
          fu.post_treatment_followup_weeks != null) && (
          <Sub title="Follow-up">
            <Facts
              items={[
                ["Treatment", fu.treatment_duration_weeks != null ? `${fu.treatment_duration_weeks} wk` : null],
                ["Total", fu.total_duration_weeks != null ? `${fu.total_duration_weeks} wk` : null],
                ["Post-treatment", fu.post_treatment_followup_weeks != null ? `${fu.post_treatment_followup_weeks} wk` : null],
              ]}
            />
          </Sub>
        )}

      {rob && (rob.overall_judgment || rob.key_concern) && (
        <Sub title="Risk of bias">
          <Facts
            items={[
              ["Tool", rob.tool],
              ["Overall", rob.overall_judgment ? titleCase(rob.overall_judgment) : null],
            ]}
          />
          {rob.domain_judgments && (
            <p className="text-[11px] text-muted-foreground">{rob.domain_judgments}</p>
          )}
          {rob.key_concern && <p>{rob.key_concern}</p>}
        </Sub>
      )}

      {subs.length > 0 && (
        <Sub title={`Subgroup analyses (${subs.length})`}>
          {subs.map((s, i) => (
            <div key={i}>
              <p>
                <span className="font-medium">
                  {[s.outcome_name, s.variable && `by ${s.variable}`]
                    .filter(Boolean)
                    .join(" ") || "Subgroup"}
                </span>
                {s.pre_specified != null && (
                  <span className="text-muted-foreground">
                    {" "}
                    ({s.pre_specified ? "pre-specified" : "post-hoc"})
                  </span>
                )}
                {s.interaction_p_value != null && (
                  <span> · interaction {fmtP(s.interaction_p_value)}</span>
                )}
              </p>
              {s.results_summary && (
                <p className="text-[11px] text-muted-foreground">{s.results_summary}</p>
              )}
            </div>
          ))}
        </Sub>
      )}

      {!!rec.custom_answers?.length && (
        <Sub title={`Custom questions (${rec.custom_answers.length})`} defaultOpen>
          {rec.custom_answers.map((qa, i) => (
            <div key={i}>
              <p className="font-medium">{qa.question || `Question ${i + 1}`}</p>
              <p className={qa.answer ? "" : "text-muted-foreground"}>
                {qa.answer || "Not addressed in this paper."}
              </p>
            </div>
          ))}
        </Sub>
      )}

      {rec.extraction_metadata?.extractor_notes && (
        <Sub title="Extractor notes">
          <p className="whitespace-pre-wrap text-[11px] leading-relaxed text-muted-foreground">
            {rec.extraction_metadata.extractor_notes}
          </p>
        </Sub>
      )}

      {prov && prov.n_quoted != null && prov.n_quoted > 0 && (
        <p
          className="mt-2 border-t border-border/60 pt-2 text-[11px] text-muted-foreground"
          title="Each extracted number carries a verbatim quote; the pipeline checks every quote against the converted paper text (exact or fuzzy match)."
        >
          Provenance: {(prov.n_exact ?? 0) + (prov.n_fuzzy ?? 0)} of {prov.n_quoted}{" "}
          quotes verified in source
          {prov.n_fuzzy ? ` (${prov.n_exact ?? 0} exact, ${prov.n_fuzzy} fuzzy)` : ""}
          {prov.n_absent ? ` · ${prov.n_absent} not found` : ""}
        </p>
      )}
    </div>
  );
}
