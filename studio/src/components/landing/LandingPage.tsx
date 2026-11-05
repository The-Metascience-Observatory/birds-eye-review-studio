import Link from "next/link";
import type { CSSProperties } from "react";

import styles from "./landing.module.css";

/** The logged-out marketing page: hero + animated evidence funnel,
 *  features, and a metascience band. (Pricing is behind SHOW_PRICING.)
 *
 *  All "random" scatter (paper positions, heatmap gaps) comes from a seeded LCG
 *  evaluated once at module scope, so the server render and the client hydration
 *  see the identical flock — Math.random() here would be a hydration error. */

function lcg(seed: number) {
  let s = seed;
  return () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648;
}
const rnd = lcg(42);

/** Vertical centers of the five database nodes papers spawn from. */
const DB_Y = [87, 135, 183, 231, 279];

const PAPERS = Array.from({ length: 34 }, (_, i) => ({
  excluded: rnd() < 0.62, // most papers screen out
  y0: DB_Y[i % DB_Y.length] - 14 + (rnd() * 16 - 8),
  lane: 120 + rnd() * 120, // converge band before the gates
  delay: -(rnd() * 15), // negative: loop already in motion
  staticX: 150 + rnd() * 210, // reduced-motion resting spot
  lines: [0, 1, 2].map(() => 9 + rnd() * 6),
}));

/** Metascience heatmap: mostly-filled grid with deliberate gaps. */
const HEAT_COLS = 9;
const HEAT_ROWS = 6;
const HEAT = Array.from({ length: HEAT_ROWS * HEAT_COLS }, (_, i) => {
  const v = rnd();
  return {
    col: i % HEAT_COLS,
    row: Math.floor(i / HEAT_COLS),
    gap: v < 0.22,
    alpha: 0.18 + v * 0.75,
  };
});

/** Evidence-table cells that pop in one by one as papers are absorbed.
 *  kind picks the fill; co is the resting opacity the animation returns to. */
const TABLE_CELLS: Array<{
  cd: number;
  x: number;
  y: number;
  w: number;
  kind: "label" | "val" | "valSoft" | "inc" | "pend";
  co?: number;
}> = [
  { cd: 0.3, x: 686, y: 159, w: 34, kind: "label" },
  { cd: 1.1, x: 726, y: 159, w: 20, kind: "val", co: 0.6 },
  { cd: 2.0, x: 752, y: 159, w: 28, kind: "valSoft", co: 0.35 },
  { cd: 2.8, x: 786, y: 159, w: 22, kind: "inc", co: 0.55 },
  { cd: 3.9, x: 686, y: 177, w: 30, kind: "label" },
  { cd: 4.8, x: 726, y: 177, w: 26, kind: "val", co: 0.6 },
  { cd: 5.7, x: 758, y: 177, w: 18, kind: "pend", co: 0.6 },
  { cd: 6.5, x: 782, y: 177, w: 26, kind: "valSoft", co: 0.35 },
  { cd: 7.6, x: 686, y: 195, w: 38, kind: "label" },
  { cd: 8.5, x: 730, y: 195, w: 16, kind: "val", co: 0.6 },
  { cd: 9.3, x: 752, y: 195, w: 30, kind: "valSoft", co: 0.35 },
  { cd: 10.6, x: 686, y: 213, w: 28, kind: "label" },
  { cd: 11.4, x: 720, y: 213, w: 24, kind: "val", co: 0.6 },
  { cd: 12.2, x: 750, y: 213, w: 20, kind: "inc", co: 0.55 },
  { cd: 12.9, x: 776, y: 213, w: 30, kind: "valSoft", co: 0.35 },
];
const TABLE_DOTS: Array<{ cd: number; cy: number; fill: "inc" | "pend" }> = [
  { cd: 3.4, cy: 163.5, fill: "inc" },
  { cd: 7.0, cy: 181.5, fill: "inc" },
  { cd: 10.0, cy: 199.5, fill: "pend" },
  { cd: 13.5, cy: 217.5, fill: "inc" },
];
const CELL_FILL: Record<string, string> = {
  label: "var(--card-line)",
  val: "hsl(var(--primary))",
  valSoft: "hsl(var(--primary))",
  inc: "var(--inc)",
  pend: "var(--pend)",
};

const DATABASES = [
  "PubMed",
  "OpenAlex",
  "Europe PMC",
  "Semantic Scholar",
  "CyberLeninka",
];

export function BirdGlyph({ scale }: { scale?: number }) {
  return (
    <g className={styles.bird} transform={scale ? `scale(${scale})` : undefined}>
      <path className={styles.wingL} d="M3,-2 C-2,-13 -2,-26 9,-38 C18,-29 13,-13 7,-2 Z" />
      <path className={styles.wingR} d="M3,2 C-2,13 -2,26 9,38 C18,29 13,13 7,2 Z" />
      <path d="M-16,0 C-8,-3.4 8,-3.6 18,0 C8,3.6 -8,3.4 -16,0 Z" />
      <circle cx="19.5" cy="0" r="3.2" />
      <path d="M-14,0 L-25,-5.5 L-21,0 L-25,5.5 Z" />
    </g>
  );
}

function FunnelFigure() {
  return (
    <figure className="mx-auto mb-14 mt-10 max-w-[1460px] px-3.5">
      <div className={styles.funnelFrame}>
        <svg
          viewBox="0 0 1200 360"
          role="img"
          aria-label="Diagram: papers stream out of five literature databases — PubMed, OpenAlex, Europe PMC, Semantic Scholar, CyberLeninka — through an AI screen where excluded papers fall away with logged reasons; included papers fill an evidence table cell by cell, are pooled into a forest plot with a summary diamond, and render as a live dashboard. Two birds fly above the scene — one gliding across, one circling the pooled results."
        >
          {/* gates */}
          <line className={styles.svGate} x1="400" y1="30" x2="400" y2="300" />
          <line className={styles.svGate} x1="660" y1="30" x2="660" y2="300" />

          {/* station labels */}
          <text className={`${styles.svLabel} ${styles.svLabelStrong}`} x="16" y="42">Search</text>
          <text className={styles.svLabel} x="16" y="58">every language</text>
          <text className={`${styles.svLabel} ${styles.svLabelStrong}`} x="412" y="42">Screen</text>
          <text className={styles.svLabel} x="412" y="58">verdict + reason, per paper</text>
          <text className={`${styles.svLabel} ${styles.svLabelStrong}`} x="672" y="42">Extract</text>
          <text className={styles.svLabel} x="672" y="58">structured, with provenance</text>
          <text className={`${styles.svLabel} ${styles.svLabelStrong}`} x="900" y="42">Pool</text>
          <text className={styles.svLabel} x="900" y="58">GRADE-rated</text>
          <text className={`${styles.svLabel} ${styles.svLabelStrong}`} x="1058" y="42">Publish</text>
          <text className={styles.svLabel} x="1058" y="58">live dashboard</text>

          {/* database sources */}
          <g>
            {DATABASES.map((name, i) => (
              <g className={styles.db} key={name}>
                <rect x="14" y={72 + i * 48} width="102" height="30" rx="6" />
                <text
                  x="65"
                  y={91 + i * 48}
                  style={name.length > 12 ? { fontSize: "10px" } : undefined}
                >
                  {name}
                </text>
              </g>
            ))}
          </g>

          {/* counts */}
          <text className={styles.svLabel} x="412" y="342" style={{ fill: "var(--exc)" }}>
            excluded — reason logged
          </text>

          {/* flowing papers */}
          <g>
            {PAPERS.map((p, i) => (
              <g
                key={i}
                className={p.excluded ? `${styles.paper} ${styles.exc}` : styles.paper}
                style={
                  {
                    "--y0": `${p.y0.toFixed(1)}px`,
                    "--lane": `${p.lane.toFixed(1)}px`,
                    "--d": `${p.delay.toFixed(2)}s`,
                    "--static-x": p.staticX.toFixed(0),
                  } as CSSProperties
                }
              >
                <rect className={styles.pCard} width="22" height="28" rx="2.5" />
                {p.lines.map((w, l) => (
                  <rect
                    key={l}
                    className={styles.pLine}
                    x="4"
                    y={8 + l * 6}
                    width={w.toFixed(1)}
                    height="2.5"
                    rx="1.25"
                  />
                ))}
                <circle className={styles.pDot} cx="17" cy="6" r="2.5" />
              </g>
            ))}
          </g>

          {/* evidence table: cells pop in as papers are absorbed */}
          <g>
            <rect x="678" y="130" width="150" height="104" rx="6" fill="hsl(var(--card))" stroke="var(--card-line)" />
            <line x1="678" y1="152" x2="828" y2="152" className={styles.glyphLine} />
            <rect x="686" y="138" width="52" height="6" rx="3" className={styles.pLine} />
            {TABLE_CELLS.map((c, i) => (
              <rect
                key={i}
                className={styles.cell}
                style={{ "--cd": `${c.cd}s`, "--co": c.co ?? 1 } as CSSProperties}
                x={c.x}
                y={c.y}
                width={c.w}
                height="9"
                rx="2.5"
                fill={CELL_FILL[c.kind]}
              />
            ))}
            {TABLE_DOTS.map((d, i) => (
              <circle
                key={i}
                className={styles.cell}
                style={{ "--cd": `${d.cd}s` } as CSSProperties}
                cx="817"
                cy={d.cy}
                r="3.5"
                fill={d.fill === "inc" ? "var(--inc)" : "var(--pend)"}
              />
            ))}
          </g>

          {/* arrow table -> forest */}
          <path d="M852 182 h34" stroke="hsl(var(--muted-foreground))" strokeWidth="1.5" fill="none" />
          <path d="M886 182 l-6 -4 v8 z" fill="hsl(var(--muted-foreground))" />

          {/* forest plot */}
          <g>
            <line x1="962" y1="120" x2="962" y2="240" stroke="var(--card-line)" strokeDasharray="2 3" />
            <g stroke="hsl(var(--muted-foreground))" strokeWidth="1.5">
              <line x1="912" y1="132" x2="988" y2="132" />
              <line x1="926" y1="152" x2="1006" y2="152" />
              <line x1="900" y1="172" x2="972" y2="172" />
              <line x1="934" y1="192" x2="1014" y2="192" />
              <line x1="918" y1="212" x2="984" y2="212" />
            </g>
            <g fill="hsl(var(--primary))">
              <rect x="944" y="127.5" width="9" height="9" />
              <rect x="962" y="147.5" width="9" height="9" />
              <rect x="930" y="167.5" width="9" height="9" />
              <rect x="970" y="187.5" width="9" height="9" />
              <rect x="946" y="207.5" width="9" height="9" />
            </g>
            <polygon className={styles.diamondPulse} points="936,232 952,224 968,232 952,240" fill="hsl(var(--primary))" />
          </g>

          {/* arrow forest -> dashboard */}
          <path d="M1022 182 h26" stroke="hsl(var(--muted-foreground))" strokeWidth="1.5" fill="none" />
          <path d="M1048 182 l-6 -4 v8 z" fill="hsl(var(--muted-foreground))" />

          {/* dashboard */}
          <g>
            <rect x="1056" y="122" width="118" height="120" rx="6" fill="hsl(var(--card))" stroke="var(--card-line)" />
            <rect x="1064" y="130" width="40" height="6" rx="3" className={styles.pLine} />
            <g>
              {[
                { x: 1066, y: 158, h: 44, o: 0.9, d: 0 },
                { x: 1082, y: 170, h: 32, o: 0.65, d: 0.3 },
                { x: 1098, y: 150, h: 52, o: 0.8, d: 0.6 },
                { x: 1114, y: 178, h: 24, o: 0.5, d: 0.9 },
                { x: 1130, y: 164, h: 38, o: 0.75, d: 1.2 },
              ].map((b) => (
                <rect
                  key={b.x}
                  className={styles.barGrow}
                  x={b.x}
                  y={b.y}
                  width="12"
                  height={b.h}
                  fill="hsl(var(--primary))"
                  opacity={b.o}
                  style={{ animationDelay: `${b.d}s` }}
                />
              ))}
            </g>
            <line x1="1064" y1="203" x2="1166" y2="203" className={styles.glyphLine} />
            <circle cx="1078" cy="222" r="4" fill="var(--inc)" />
            <circle cx="1102" cy="216" r="3" fill="hsl(var(--primary))" />
            <circle cx="1122" cy="226" r="5" fill="hsl(var(--primary))" opacity="0.6" />
            <circle cx="1148" cy="218" r="3.5" fill="var(--pend)" />
          </g>

          {/* the bird, surveying from above */}
          <g className={styles.birdFly} aria-hidden="true">
            <g className={styles.birdBob}>
              <BirdGlyph />
            </g>
          </g>

          {/* a smaller companion, circling the pooled results */}
          <g className={styles.fnOrbit} aria-hidden="true">
            <g className={styles.birdBob}>
              <BirdGlyph scale={0.5} />
            </g>
          </g>
        </svg>
      </div>
      <figcaption className={`${styles.mono} px-1 pt-2.5 text-xs text-muted-foreground`}>
        <b className="font-semibold text-foreground">Fig. 1</b> — one review, end to
        end: search → screen (every exclusion carries a reason) → extract → pool →
        publish. Green survives; red falls out, on the record.
      </figcaption>
    </figure>
  );
}

// Pricing is hidden until the tiers are finalised — flip to true to restore the
// nav link and the pricing section below.
const SHOW_PRICING = false;

const EYEBROW = `${styles.mono} mb-3.5 text-[11px] uppercase tracking-[0.14em] text-primary`;

const FEATURES = [
  {
    title: "Live evidence table",
    body: "Results fill in while the jobs producing them are still running. The table is the same projection the published dashboard uses — preview and publication can never disagree.",
    icon: <path d="M3 5h18M3 10h18M3 15h18M3 20h12" />,
  },
  {
    title: "Two-pass extraction",
    body: "A cheap full-text eligibility read filters first; the expensive deep extraction runs only on survivors. On real corpora that's the difference between $40 and $400 a run.",
    icon: <path d="M22 3H2l8 9.5V19l4 2v-8.5z" />,
  },
  {
    title: "A real meta-analysis engine",
    body: "Random-effects pooling with Hartung–Knapp intervals, leave-one-out sensitivity, Egger's test, and GRADE downgrades with stated reasons — not a summary paragraph.",
    icon: <path d="M4 4v16M4 8h10M4 12h14M4 16h8M20 10v4l-2 2" />,
  },
  {
    title: "Dashboards that answer questions",
    body: "Where is the research happening? Is the field growing or abandoned? Do open-label trials report more positive results? Every panel exists to answer one question.",
    icon: (
      <>
        <rect x="3" y="3" width="7" height="9" rx="1" />
        <rect x="14" y="3" width="7" height="5" rx="1" />
        <rect x="14" y="12" width="7" height="9" rx="1" />
        <rect x="3" y="16" width="7" height="5" rx="1" />
      </>
    ),
  },
  {
    title: "Living reviews",
    body: "Re-run any review incrementally: only genuinely new papers cost time or money. The published page shows what changed — “786 trials · 142 new since”.",
    icon: (
      <>
        <path d="M21 12a9 9 0 1 1-3-6.7" />
        <path d="M21 3v6h-6" />
      </>
    ),
  },
  {
    title: "A publish guard",
    body: "Owner proposes, admin approves — against an evidence packet showing exactly what changes. The export refuses to ship a feed that silently drops papers or shrinks a metric.",
    icon: (
      <>
        <path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z" />
        <path d="M9 12l2 2 4-4" />
      </>
    ),
  },
];

export function LandingPage({ authenticated = false }: { authenticated?: boolean }) {
  return (
    <div className={styles.landing}>
      {/* ---- nav ---- */}
      <header className="border-b border-border">
        <div className="mx-auto flex max-w-7xl items-center gap-7 px-6 py-4">
          <Link href="/" className="flex items-center gap-2.5 text-[17px] font-semibold">
            <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">
              <circle cx="13" cy="13" r="11" fill="none" stroke="hsl(var(--primary))" strokeWidth="2" />
              <circle cx="13" cy="13" r="3.2" fill="hsl(var(--primary))" />
              <path d="M13 2 v4 M13 20 v4 M2 13 h4 M20 13 h4" stroke="hsl(var(--primary))" strokeWidth="2" strokeLinecap="round" />
            </svg>
            Bird&rsquo;s Eye Review Studio
          </Link>
          <nav className="ml-auto hidden gap-6 text-sm md:flex" aria-label="Site">
            <a className="text-muted-foreground hover:text-primary" href="#features">Features</a>
            <a className="text-muted-foreground hover:text-primary" href="#metascience">Metascience</a>
            {SHOW_PRICING && (
              <a className="text-muted-foreground hover:text-primary" href="#pricing">Pricing</a>
            )}
          </nav>
          {authenticated ? (
            <Link
              href="/dashboard"
              className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:opacity-90"
            >
              Open studio
            </Link>
          ) : (
            <>
              <Link
                href="/login"
                className="rounded-md border border-border px-4 py-2 text-sm font-medium hover:border-primary hover:text-primary"
              >
                Log in
              </Link>
              <Link
                href="/signup"
                className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:opacity-90"
              >
                Request access
              </Link>
            </>
          )}
        </div>
      </header>

      <main>
        {/* ---- hero ---- */}
        <div className="relative mx-auto max-w-7xl px-6 pb-6 pt-16">
          <svg className={styles.heroBird} viewBox="0 0 780 300" aria-hidden="true">
            <g className={styles.hbFly}>
              <g className={styles.birdBob}>
                <BirdGlyph scale={0.58} />
              </g>
            </g>
          </svg>
          <h1
            className={`${styles.serif} relative z-10 mb-5 max-w-[18ch] text-4xl font-semibold leading-[1.08] sm:text-5xl lg:text-6xl`}
            style={{ textWrap: "balance" }}
          >
            Survey the literature landscape from the{" "}
            <em className="italic text-primary">highest level</em>.
          </h1>
          <div className="relative z-10 flex flex-wrap items-center gap-3">
            <Link
              href={authenticated ? "/dashboard" : "/signup"}
              className="rounded-md bg-primary px-[18px] py-2.5 text-sm font-semibold text-primary-foreground hover:opacity-90"
            >
              {authenticated ? "Open the studio" : "Request access"}
            </Link>
          </div>
        </div>

        <FunnelFigure />

        {/* ---- features ---- */}
        <section id="features" className="py-20">
          <div className="mx-auto max-w-7xl px-6">
            <h2 className={`${styles.serif} mb-10 max-w-[24ch] text-3xl font-semibold sm:text-4xl`}>
              A working pipeline, not a chat window.
            </h2>
            <div className="grid gap-5 md:grid-cols-2 lg:grid-cols-3">
              {FEATURES.map((f) => (
                <div key={f.title} className="rounded-lg border border-border p-5">
                  <svg
                    width="22"
                    height="22"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="text-primary"
                    aria-hidden="true"
                  >
                    {f.icon}
                  </svg>
                  <h3 className="mb-1.5 mt-2.5 text-base font-semibold">{f.title}</h3>
                  <p className="text-sm text-muted-foreground">{f.body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ---- metascience band ---- */}
        <section id="metascience" className={`${styles.band} py-20`}>
          <div className="mx-auto grid max-w-7xl items-center gap-14 px-6 md:grid-cols-2">
            <div>
              <p className={`${styles.mono} mb-3.5 text-[11px] uppercase tracking-[0.14em]`} style={{ color: "#6fb3e8" }}>
                For metascience
              </p>
              <h2 className={`${styles.serif} mb-3.5 text-3xl font-semibold sm:text-4xl`}>
                The byproduct of every review is a dataset about science itself.
              </h2>
              <p className="max-w-[56ch] text-base" style={{ color: "#a9bccc" }}>
                Because we keep every paper, each review doubles as a map of
                scientific production: which treatments are studied versus merely
                popular, which questions were abandoned, where whole cells of the
                intervention&nbsp;&times;&nbsp;outcome landscape sit empty. Funders
                find the gaps; metascientists find the biases.
              </p>
              <p className="mt-4 max-w-[56ch] text-base" style={{ color: "#a9bccc" }}>
                Bird&rsquo;s Eye Review grew out of the{" "}
                <a href="https://metascienceobservatory.org">Metascience Observatory</a>{" "}
                — infrastructure for understanding research, not just summarizing it.
              </p>
            </div>
            <figure className="m-0">
              <svg
                viewBox="0 0 380 272"
                role="img"
                aria-label="A heatmap of interventions by outcomes. Most cells are filled blue where trials exist; several cells are empty with dashed outlines, marking treatment–outcome pairs no one has studied."
              >
                <text className={styles.heatLabel} x="6" y="16">
                  intervention × outcome landscape
                </text>
                {HEAT.map((c, i) =>
                  c.gap ? (
                    <rect
                      key={i}
                      x={6 + c.col * 41}
                      y={28 + c.row * 35}
                      width="38"
                      height="32"
                      rx="3"
                      fill="none"
                      stroke="#5b7a93"
                      strokeDasharray="3 3"
                    />
                  ) : (
                    <rect
                      key={i}
                      x={6 + c.col * 41}
                      y={28 + c.row * 35}
                      width="38"
                      height="32"
                      rx="3"
                      fill="#4a90c9"
                      opacity={c.alpha.toFixed(2)}
                    />
                  ),
                )}
                <rect x="6" y="250" width="12" height="12" rx="2" fill="#4a90c9" />
                <text className={styles.heatLabel} x="24" y="260">trials exist</text>
                <rect x="110" y="250" width="12" height="12" rx="2" fill="none" stroke="#5b7a93" strokeDasharray="3 3" />
                <text className={styles.heatLabel} x="128" y="260">nothing studied — a gap</text>
              </svg>
              <figcaption className={`${styles.mono} pt-2.5 text-xs`} style={{ color: "#8aa2b6" }}>
                <b className="font-semibold" style={{ color: "#dfe9f2" }}>Fig. 3</b> — the map has holes. That&rsquo;s the point.
              </figcaption>
            </figure>
          </div>
        </section>

        {/* ---- pricing ---- */}
        {SHOW_PRICING && (
          <section id="pricing" className="py-20">
            <div className="mx-auto max-w-7xl px-6">
              <p className={EYEBROW}>Pricing</p>
              <h2 className={`${styles.serif} mb-3 max-w-[24ch] text-3xl font-semibold sm:text-4xl`}>
                Run your first review this week.
              </h2>
              <p className={`${styles.mono} mb-6 text-[11px] uppercase tracking-[0.1em]`} style={{ color: "var(--pend)" }}>
                Illustrative tiers — final pricing under discussion
              </p>
              <div className="grid items-stretch gap-5 md:grid-cols-3">
                {[
                  {
                    name: "Researcher",
                    price: "$49",
                    per: "/mo",
                    lead: false,
                    items: [
                      "1 active review",
                      "Search, screen & extract up to 10,000 papers",
                      "PRISMA flow + evidence table export",
                      "Private dashboards",
                    ],
                    cta: "Request access",
                  },
                  {
                    name: "Lab",
                    price: "$299",
                    per: "/mo",
                    lead: true,
                    items: [
                      "5 active reviews, shared with your team",
                      "100,000+ papers per review",
                      "Full meta-analysis engine with GRADE",
                      "Living-review updates on a schedule",
                      "Two-party publish workflow",
                    ],
                    cta: "Request access",
                  },
                  {
                    name: "Enterprise",
                    price: "Custom",
                    per: "",
                    lead: false,
                    items: [
                      "Evidence teams, HTA & guideline groups",
                      "Unlimited reviews & corpora",
                      "On-prem or private-cloud deployment",
                      "Audit trail & SSO",
                    ],
                    cta: "Talk to us",
                  },
                ].map((t) => (
                  <div
                    key={t.name}
                    className={`flex flex-col gap-2 rounded-lg border p-6 ${
                      t.lead ? "border-primary ring-1 ring-primary" : "border-border"
                    }`}
                  >
                    <h3 className="text-base font-semibold">{t.name}</h3>
                    <div className={`${styles.mono} text-[28px] font-bold`}>
                      {t.price}
                      {t.per && <small className="text-[13px] font-normal text-muted-foreground">{t.per}</small>}
                    </div>
                    <ul className="mb-4 mt-2 grid list-disc gap-1.5 pl-4 text-sm text-muted-foreground">
                      {t.items.map((it) => (
                        <li key={it}>{it}</li>
                      ))}
                    </ul>
                    <Link
                      href="/signup"
                      className={`mt-auto rounded-md px-4 py-2 text-center text-sm font-semibold ${
                        t.lead
                          ? "bg-primary text-primary-foreground hover:opacity-90"
                          : "border border-border hover:border-primary hover:text-primary"
                      }`}
                    >
                      {t.cta}
                    </Link>
                  </div>
                ))}
              </div>
            </div>
          </section>
        )}
      </main>

      {/* ---- footer ---- */}
      <footer className="border-t border-border py-11 text-sm text-muted-foreground">
        <div className="mx-auto max-w-7xl px-6">
          <div className="flex flex-wrap items-start justify-between gap-6">
            {/* The Metascience Observatory globe (same mark as ChartWatermark). */}
            <span className="flex items-center gap-2.5">
              <svg
                width="22"
                height="22"
                viewBox="0 0 16 16"
                aria-hidden="true"
                fill="none"
                stroke="hsl(var(--primary))"
                strokeWidth="1.1"
                strokeLinecap="round"
              >
                <circle cx="8" cy="8" r="7.35" />
                <ellipse cx="8" cy="8" rx="3" ry="7.35" />
                <ellipse cx="8" cy="8" rx="7.35" ry="3" />
              </svg>
              <span className="font-semibold text-foreground">Bird&rsquo;s Eye Review Studio</span>
              <span className="text-muted-foreground">from The Metascience Observatory</span>
            </span>
            <nav className="flex flex-wrap gap-6" aria-label="Footer">
              <a className="hover:text-primary" href="#">About</a>
              <a className="hover:text-primary" href="#">Research ethics</a>
              <a className="hover:text-primary" href="#">Security</a>
              <a className="hover:text-primary" href="#">Contact</a>
            </nav>
          </div>
        </div>
      </footer>
    </div>
  );
}
