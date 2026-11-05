"""Meta-analysis engine — the single source of truth for pooling.

Shared by ``export_dashboard.py --meta-analysis`` and ``pool_meta_analysis.py``
so both call the *exact same* statistics (no drift). Pure Python:
no hard third-party dependency. ``scipy.stats`` is used for distribution
functions when importable, otherwise the module falls back to its own
implementations (``math.erf`` + Acklam for the normal; regularized incomplete
beta/gamma continued fractions for the t and chi-square), so it runs headless
in the pipeline exactly as before.

What it computes
----------------
* Effect sizes: validated native contrasts or explicitly mapped continuous SMDs.
  All contrasts retain intervention-versus-comparator orientation.
* Between-study variance tau^2: DerSimonian-Laird (DL), Paule-Mandel (PM), REML.
* Pooling: fixed- or random-effects inverse-variance, with Wald or
  Knapp-Hartung-Sidik-Jonkman (HKSJ) confidence intervals.
* Heterogeneity: Cochran's Q (+ p), I^2, H^2, tau, and the prediction interval.
* Publication bias: Egger's regression test, funnel coordinates, Duval &
  Tweedie trim-and-fill (L0).
* Sensitivity: leave-one-out. Subgroups: per-level pooling + Q-between test.

Backward compatibility: ``build_meta_analysis`` returns the same JSON shape as
the old ``_build_meta_analysis`` — every previously-emitted key (``effect``,
``ci_low``, ``ci_high``, ``i2``, ``model``, ``n_trials``, ``trials`` ...) is
preserved; the new statistics are *added* alongside them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict

# Distribution functions: prefer scipy (exact, fast), fall back to pure Python.
try:  # pragma: no cover - exercised by whichever branch the env provides
    from scipy import stats as _sps  # type: ignore

    def norm_cdf(x: float) -> float:
        return float(_sps.norm.cdf(x))

    def norm_ppf(p: float) -> float:
        return float(_sps.norm.ppf(p))

    def t_cdf(x: float, df: float) -> float:
        return float(_sps.t.cdf(x, df))

    def t_ppf(p: float, df: float) -> float:
        return float(_sps.t.ppf(p, df))

    def chi2_sf(x: float, df: float) -> float:
        return float(_sps.chi2.sf(x, df))

    _HAVE_SCIPY = True
except Exception:  # pragma: no cover
    _HAVE_SCIPY = False

    # ---- Normal ------------------------------------------------------------
    def norm_cdf(x: float) -> float:
        return 0.5 * math.erfc(-x / math.sqrt(2.0))

    def norm_ppf(p: float) -> float:
        """Inverse normal CDF (Acklam's rational approximation, |err| < 1.2e-9)."""
        if p <= 0.0:
            return -math.inf
        if p >= 1.0:
            return math.inf
        a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
             1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
        b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
             6.680131188771972e+01, -1.328068155288572e+01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
             -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
        d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
             3.754408661907416e+00]
        plow, phigh = 0.02425, 1 - 0.02425
        if p < plow:
            q = math.sqrt(-2 * math.log(p))
            return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                   ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        if p <= phigh:
            q = p - 0.5
            r = q * q
            return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
                   (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)

    # ---- Log-gamma + regularized incomplete beta/gamma (Numerical Recipes) --
    def _gammln(xx: float) -> float:
        cof = [76.18009172947146, -86.50532032941677, 24.01409824083091,
               -1.231739572450155, 0.1208650973866179e-2, -0.5395239384953e-5]
        x = xx
        y = xx
        tmp = x + 5.5
        tmp -= (x + 0.5) * math.log(tmp)
        ser = 1.000000000190015
        for c in cof:
            y += 1.0
            ser += c / y
        return -tmp + math.log(2.5066282746310005 * ser / x)

    def _betacf(a: float, b: float, x: float) -> float:
        MAXIT, EPS, FPMIN = 200, 3.0e-12, 1.0e-300
        qab, qap, qam = a + b, a + 1.0, a - 1.0
        c = 1.0
        d = 1.0 - qab * x / qap
        if abs(d) < FPMIN:
            d = FPMIN
        d = 1.0 / d
        h = d
        for m in range(1, MAXIT + 1):
            m2 = 2 * m
            aa = m * (b - m) * x / ((qam + m2) * (a + m2))
            d = 1.0 + aa * d
            if abs(d) < FPMIN:
                d = FPMIN
            c = 1.0 + aa / c
            if abs(c) < FPMIN:
                c = FPMIN
            d = 1.0 / d
            h *= d * c
            aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
            d = 1.0 + aa * d
            if abs(d) < FPMIN:
                d = FPMIN
            c = 1.0 + aa / c
            if abs(c) < FPMIN:
                c = FPMIN
            d = 1.0 / d
            de = d * c
            h *= de
            if abs(de - 1.0) < EPS:
                break
        return h

    def _betai(a: float, b: float, x: float) -> float:
        """Regularized incomplete beta I_x(a, b)."""
        if x <= 0.0:
            return 0.0
        if x >= 1.0:
            return 1.0
        bt = math.exp(_gammln(a + b) - _gammln(a) - _gammln(b) +
                      a * math.log(x) + b * math.log(1.0 - x))
        if x < (a + 1.0) / (a + b + 2.0):
            return bt * _betacf(a, b, x) / a
        return 1.0 - bt * _betacf(b, a, 1.0 - x) / b

    def _gser(a: float, x: float) -> float:
        EPS, ITMAX = 3.0e-12, 300
        if x <= 0.0:
            return 0.0
        ap = a
        s = 1.0 / a
        de = s
        for _ in range(ITMAX):
            ap += 1.0
            de *= x / ap
            s += de
            if abs(de) < abs(s) * EPS:
                break
        return s * math.exp(-x + a * math.log(x) - _gammln(a))

    def _gcf(a: float, x: float) -> float:
        EPS, ITMAX, FPMIN = 3.0e-12, 300, 1.0e-300
        b = x + 1.0 - a
        c = 1.0 / FPMIN
        d = 1.0 / b
        h = d
        for i in range(1, ITMAX + 1):
            an = -i * (i - a)
            b += 2.0
            d = an * d + b
            if abs(d) < FPMIN:
                d = FPMIN
            c = b + an / c
            if abs(c) < FPMIN:
                c = FPMIN
            d = 1.0 / d
            de = d * c
            h *= de
            if abs(de - 1.0) < EPS:
                break
        return math.exp(-x + a * math.log(x) - _gammln(a)) * h

    def _gammp(a: float, x: float) -> float:
        """Regularized lower incomplete gamma P(a, x)."""
        if x < 0.0 or a <= 0.0:
            return 0.0
        if x < a + 1.0:
            return _gser(a, x)
        return 1.0 - _gcf(a, x)

    def t_cdf(x: float, df: float) -> float:
        ib = _betai(df / 2.0, 0.5, df / (df + x * x))
        return 1.0 - 0.5 * ib if x > 0 else 0.5 * ib

    def t_ppf(p: float, df: float) -> float:
        """Inverse Student-t CDF via bisection on t_cdf."""
        if p <= 0.0:
            return -math.inf
        if p >= 1.0:
            return math.inf
        lo, hi = -1.0e4, 1.0e4
        for _ in range(200):
            mid = 0.5 * (lo + hi)
            if t_cdf(mid, df) < p:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    def chi2_sf(x: float, df: float) -> float:
        """Chi-square survival function 1 - CDF."""
        if x <= 0.0:
            return 1.0
        return 1.0 - _gammp(df / 2.0, x / 2.0)


_TAU2_METHODS = ("DL", "PM", "REML")
_CI_METHODS = ("wald", "hksj", "modified_hksj")
_MODELS = ("fixed", "random")


# ── Options ──────────────────────────────────────────────────────────────────

@dataclass
class MetaOptions:
    """User-selectable methodology. Defaults reproduce the legacy pooler
    (DL random-effects, Wald CIs) so existing outputs are unchanged."""
    model: str = "random"          # "random" | "fixed"
    tau2_method: str = "DL"        # "DL" | "PM" | "REML"
    ci_method: str = "wald"        # "wald" | "hksj"
    level: float = 0.95
    min_trials: int = 2
    prediction_interval: bool = False
    publication_bias: bool = False
    leave_one_out: bool = False
    subgroup_by: str | None = None
    # Dotted path to a CONTINUOUS trial-level moderator for meta-regression
    # (e.g. "derived.mean_serum_li_mmol_L"). None disables it.
    regress_on: str | None = None

    def normalized(self) -> "MetaOptions":
        model = self.model if self.model in _MODELS else "random"
        tau2 = self.tau2_method if self.tau2_method in _TAU2_METHODS else "DL"
        ci = self.ci_method if self.ci_method in _CI_METHODS else "wald"
        level = self.level if 0.5 < self.level < 1.0 else 0.95
        mt = max(2, int(self.min_trials or 2))
        sub = self.subgroup_by or None
        reg = self.regress_on or None
        return MetaOptions(model, tau2, ci, level, mt,
                           bool(self.prediction_interval),
                           bool(self.publication_bias),
                           bool(self.leave_one_out), sub, reg)

    def to_dict(self) -> dict:
        return asdict(self)


# ── tau^2 estimators ─────────────────────────────────────────────────────────

def _fixed_weights(ses: list[float]) -> list[float]:
    return [1.0 / (se * se) for se in ses]


def tau2_dl(ys: list[float], ses: list[float]) -> float:
    """DerSimonian-Laird moment estimator."""
    k = len(ys)
    if k < 2:
        return 0.0
    w = _fixed_weights(ses)
    sw = sum(w)
    fixed = sum(wi * y for wi, y in zip(w, ys)) / sw
    q = sum(wi * (y - fixed) ** 2 for wi, y in zip(w, ys))
    df = k - 1
    c = sw - sum(wi * wi for wi in w) / sw
    return max(0.0, (q - df) / c) if c > 0 else 0.0


def tau2_paule_mandel(ys: list[float], ses: list[float],
                      max_iter: int = 100, tol: float = 1e-9) -> float:
    """Paule-Mandel: solve sum w_i (y_i - mu)^2 = k - 1 for tau^2 (w = 1/(v+tau^2))."""
    k = len(ys)
    if k < 2:
        return 0.0
    df = k - 1
    tau2 = max(0.0, tau2_dl(ys, ses))  # warm start
    for _ in range(max_iter):
        w = [1.0 / (se * se + tau2) for se in ses]
        sw = sum(w)
        mu = sum(wi * y for wi, y in zip(w, ys)) / sw
        f = sum(wi * (y - mu) ** 2 for wi, y in zip(w, ys)) - df
        # derivative of F wrt tau2 (mu treated as constant is the standard PM step)
        dfdt = -sum(wi * wi * (y - mu) ** 2 for wi, y in zip(w, ys))
        if dfdt == 0:
            break
        step = f / dfdt
        new = tau2 - step
        if new < 0:
            new = 0.0
        if abs(new - tau2) < tol:
            tau2 = new
            break
        tau2 = new
    return max(0.0, tau2)


def tau2_reml(ys: list[float], ses: list[float],
              max_iter: int = 200, tol: float = 1e-9) -> float:
    """REML via the standard fixed-point iteration (Viechtbauer 2005)."""
    k = len(ys)
    if k < 2:
        return 0.0
    vs = [se * se for se in ses]
    tau2 = max(0.0, tau2_dl(ys, ses))
    for _ in range(max_iter):
        w = [1.0 / (v + tau2) for v in vs]
        sw = sum(w)
        mu = sum(wi * y for wi, y in zip(w, ys)) / sw
        sw2 = sum(wi * wi for wi in w)
        num = sum(wi * wi * ((y - mu) ** 2 - v) for wi, y, v in zip(w, ys, vs)) + 1.0 / sw * 0.0
        # REML update: tau2_new = [sum w^2((y-mu)^2 - v) + tr term] / sum w^2
        # tr term = sum w^2 * (1/sw)  (accounts for estimating mu)
        num = sum(wi * wi * ((y - mu) ** 2 - v) for wi, y, v in zip(w, ys, vs)) + sw2 / sw
        new = num / sw2
        if new < 0:
            new = 0.0
        if abs(new - tau2) < tol:
            tau2 = new
            break
        tau2 = new
    return max(0.0, tau2)


def estimate_tau2(ys: list[float], ses: list[float], method: str) -> float:
    if method == "PM":
        return tau2_paule_mandel(ys, ses)
    if method == "REML":
        return tau2_reml(ys, ses)
    return tau2_dl(ys, ses)


# ── Core pooling ─────────────────────────────────────────────────────────────

def pool(points: list[tuple[float, float]], *, model: str = "random",
         tau2_method: str = "DL", ci_method: str = "wald",
         level: float = 0.95, prediction_interval: bool = False) -> dict:
    """Pool ``points = [(effect_on_pool_scale, se), ...]``.

    Returns pooled effect + SE, CI, p-value, tau^2/tau, Cochran's Q (+p), df,
    I^2, H^2, per-study random-effects weights, and (optionally) the prediction
    interval. All on the pool scale (caller exponentiates ratio measures)."""
    if not points or any(not math.isfinite(y) or not math.isfinite(se) or se <= 0 for y, se in points):
        raise ValueError("Pooling requires finite estimates and positive finite standard errors")
    k = len(points)
    ys = [p[0] for p in points]
    ses = [p[1] for p in points]
    df = k - 1

    # Heterogeneity (Q always from fixed-effect weights).
    wf = _fixed_weights(ses)
    swf = sum(wf)
    fixed_mean = sum(wi * y for wi, y in zip(wf, ys)) / swf
    q = sum(wi * (y - fixed_mean) ** 2 for wi, y in zip(wf, ys))
    q_p = chi2_sf(q, df) if df >= 1 else None
    i2 = max(0.0, (q - df) / q) * 100 if q > 0 and df >= 1 else 0.0
    h2 = (q / df) if df >= 1 and q > 0 else 1.0

    tau2 = estimate_tau2(ys, ses, tau2_method) if model == "random" else 0.0

    # Random- (or fixed-) effects weights and pooled estimate.
    rw = [1.0 / (se * se + tau2) for se in ses]
    srw = sum(rw)
    pooled = sum(wi * y for wi, y in zip(rw, ys)) / srw
    var_iv = 1.0 / srw  # inverse-variance sampling variance of the pooled effect

    alpha = 1.0 - level
    if ci_method in {"hksj", "modified_hksj"} and df >= 1:
        # Knapp-Hartung: se^2 = (1/(k-1)) * sum w (y - theta)^2 / sum w, t_{k-1}.
        qgen = sum(wi * (y - pooled) ** 2 for wi, y in zip(rw, ys))
        variance = qgen / (df * srw)
        if ci_method == "modified_hksj":
            variance = max(variance, var_iv)
        se_pooled = math.sqrt(variance)
        crit = t_ppf(1.0 - alpha / 2.0, df)
        stat = pooled / se_pooled if se_pooled > 0 else 0.0
        p_value = 2.0 * (1.0 - t_cdf(abs(stat), df)) if se_pooled > 0 else None
        ci_dist = "t"
    else:
        se_pooled = math.sqrt(var_iv)
        crit = norm_ppf(1.0 - alpha / 2.0)
        stat = pooled / se_pooled if se_pooled > 0 else 0.0
        p_value = 2.0 * (1.0 - norm_cdf(abs(stat))) if se_pooled > 0 else None
        ci_dist = "z"

    out = {
        "pooled": pooled,
        "pooled_se": se_pooled,
        "ci_low": pooled - crit * se_pooled,
        "ci_high": pooled + crit * se_pooled,
        "stat": stat,
        "p_value": p_value,
        "ci_dist": ci_dist,
        "tau2": tau2,
        "tau": math.sqrt(tau2),
        "q": q,
        "q_p": q_p,
        "df": df,
        "i2": round(i2, 1),
        "h2": round(h2, 3),
        "k": k,
        "weights": rw,
        "total_weight": srw,
        "model": model,
        "tau2_method": tau2_method if model == "random" else None,
        "ci_method": ci_method,
        "level": level,
    }

    # Prediction interval (IntHout 2016): theta +/- t_{k-2} sqrt(tau^2 + SE^2).
    if prediction_interval and k >= 3:
        pi_df = k - 2
        tcrit = t_ppf(1.0 - alpha / 2.0, pi_df)
        spread = tcrit * math.sqrt(tau2 + var_iv)
        out["prediction_low"] = pooled - spread
        out["prediction_high"] = pooled + spread
    else:
        out["prediction_low"] = None
        out["prediction_high"] = None
    return out


# ── Record preparation shared by the export and pool_meta_analysis ──────────────

from ber_core import grading as effects  # noqa: E402  (RoB, GRADE, author labels)


def attach_canonical_interventions(records: list[dict], canon: dict, categories: dict | None = None) -> int:
    """Stamp ``intervention_canonical`` (and any category override) on every
    intervention arm, in place. Returns the number of arms stamped.

    ``canon`` is canonicalize_interventions.py's raw-name -> canonical map;
    ``categories`` is categorize_interventions.py's canonical -> category map.
    The canonical name is the pooling identity (``scientific.treatment_identity``),
    so this must run before ``build_meta_analysis`` in every caller — the export
    and pool_meta_analysis.py both do — or naming variants pool separately.
    """
    categories = categories or {}
    if not canon and not categories:
        return 0
    n = 0
    for r in records:
        for a in ((r.get("study_design") or {}).get("arms") or []):
            if a.get("type") != "intervention":
                continue
            raw = (a.get("intervention_name") or "").strip()
            if not raw:
                continue
            name = canon.get(raw, raw)
            a["intervention_canonical"] = name
            n += 1
            if name in categories:
                a["intervention_category"] = categories[name]
    return n


def load_name_map(path) -> dict[str, str]:
    """A flat {str: str} JSON map (canonical names, category overrides); {} if absent/invalid."""
    import json
    from pathlib import Path
    path = Path(path)
    if not path.exists():
        return {}
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in m.items()} if isinstance(m, dict) else {}


def _study_n(record: dict):
    """Randomized (else enrolled) participants of a study, for the GRADE
    optimal-information-size check when a contrast carries no analysed n."""
    ss = record.get("sample_sizes") or {}
    n = ss.get("n_randomized_total") or ss.get("n_enrolled_total")
    return n if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0 else None


def _dotted_get(record: dict, path: str):
    """Fetch a possibly-nested field like 'risk_of_bias.overall_judgment'."""
    cur = record
    for key in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


# ── Publication bias ─────────────────────────────────────────────────────────

def eggers_test(points: list[tuple[float, float]]) -> dict | None:
    """Egger's regression test for funnel asymmetry (Egger 1997).

    Regress the standard normal deviate (y/se) on precision (1/se) by OLS; the
    intercept's t-test flags asymmetry. Needs >= 3 studies."""
    k = len(points)
    if k < 3:
        return None
    xs = [1.0 / se for (_y, se) in points]        # precision
    ys = [y / se for (y, se) in points]           # standard normal deviate
    n = k
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    # residual variance -> SEs of the OLS coefficients
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    dfree = n - 2
    if dfree < 1:
        return None
    s2 = sum(r * r for r in resid) / dfree
    se_intercept = math.sqrt(s2 * sxx / denom)
    if se_intercept <= 0:
        return None
    t_stat = intercept / se_intercept
    p = 2.0 * (1.0 - t_cdf(abs(t_stat), dfree))
    return {
        "intercept": round(intercept, 4),
        "se": round(se_intercept, 4),
        "t": round(t_stat, 4),
        "df": dfree,
        "p_value": round(p, 4),
    }


def trim_and_fill(points: list[tuple[float, float]], *, model: str = "random",
                  tau2_method: str = "DL", side: str | None = None,
                  max_iter: int = 50) -> dict:
    """Duval & Tweedie trim-and-fill (L0 estimator).

    Iteratively estimates the number of studies suppressed on one side of the
    funnel, imputes their mirror images, and re-pools. ``side`` is the side
    MISSING studies are added to ('left' or 'right'); auto-detected if None.
    Returns imputed count, the imputed (y, se) points, and the adjusted pool."""
    k = len(points)
    base = {"n_imputed": 0, "imputed": [], "side": side, "adjusted": None}
    if k < 3:
        return base

    ys = [p[0] for p in points]
    ses = [p[1] for p in points]

    def _pooled_mean(sub_pts):
        yy = [p[0] for p in sub_pts]
        ss = [p[1] for p in sub_pts]
        t2 = estimate_tau2(yy, ss, tau2_method) if model == "random" else 0.0
        w = [1.0 / (s * s + t2) for s in ss]
        return sum(wi * y for wi, y in zip(w, yy)) / sum(w)

    # Auto side: pool all, if effects skew right of the estimate the suppressed
    # studies are on the left, and vice versa.
    theta0 = _pooled_mean(points)
    if side is None:
        right = sum(1 for y in ys if y > theta0)
        left = sum(1 for y in ys if y < theta0)
        side = "left" if right >= left else "right"

    order = sorted(range(k), key=lambda i: ys[i])  # ascending by effect
    k0 = 0
    for _ in range(max_iter):
        n_trim = k - k0
        if n_trim < 1:
            break
        # Trim the k0 most extreme studies on the OVER-represented side (opposite
        # of `side`): if missing-left, the right tail is over-represented.
        trimmed = order[:n_trim] if side == "left" else order[k0:]
        theta = _pooled_mean([points[i] for i in trimmed])
        centered = [(ys[i] - theta, i) for i in range(k)]
        # Signed ranks of |centered|.
        ranks = sorted(range(k), key=lambda j: abs(centered[j][0]))
        rank_of = [0] * k
        for r, j in enumerate(ranks, start=1):
            rank_of[j] = r
        if side == "left":
            tn = sum(rank_of[j] for j in range(k) if centered[j][0] > 0)
        else:
            tn = sum(rank_of[j] for j in range(k) if centered[j][0] < 0)
        l0 = (4.0 * tn - k * (k + 1)) / (2.0 * k - 1)
        new_k0 = max(0, int(round(l0)))
        if new_k0 == k0:
            break
        k0 = new_k0

    imputed = []
    if k0 > 0:
        theta = _pooled_mean([points[i] for i in (order[:k - k0] if side == "left" else order[k0:])])
        # Mirror the k0 most extreme observed studies across theta.
        extreme = order[-k0:] if side == "left" else order[:k0]
        for i in extreme:
            imputed.append((2 * theta - ys[i], ses[i]))

    adjusted = None
    if k0 > 0:
        full = list(points) + imputed
        adjusted = _pooled_mean(full)
    return {"n_imputed": k0, "imputed": imputed, "side": side, "adjusted": adjusted}


def funnel_coordinates(points: list[tuple[float, float]], pooled_effect: float) -> dict:
    """Per-study (effect, se) plus the pseudo-95% CI funnel bounds around the
    pooled effect, for drawing a funnel plot."""
    ses = [se for (_y, se) in points]
    max_se = max(ses) if ses else 0.0
    return {
        "points": [{"effect": y, "se": se} for (y, se) in points],
        "pooled_effect": pooled_effect,
        "max_se": max_se,
        "ci95": 1.959964,
    }


# ── Grouping + full build ────────────────────────────────────────────────────

def _trial_out(t: dict, weight):
    out = {
        "paper_id": t["paper_id"], "first_author": t["first_author"],
        "year": t["year"], "effect": t["effect"],
        "ci_low": t["ci_low"], "ci_high": t["ci_high"],
        "p_value": t["p_value"], "n_total": t["n_total"],
        "outcome_name": t["outcome_name"],
        "weight": round(weight, 4) if weight is not None else None,
    }
    if t.get("_flipped"):
        out["oriented_flipped"] = True
    if t.get("_selection"):
        out["selection"] = {k: v for k, v in t["_selection"].items() if not k.startswith("_")}
    if t.get("_subgroup") is not None:
        out["subgroup"] = t["_subgroup"]
    if t.get("_rob"):
        out["rob"] = t["_rob"]
    return out


def _pooled_block(trials: list[dict], opts: MetaOptions, log_scale: bool) -> dict:
    """Run pool() over a set of trial points and format the on-scale result."""
    stats = pool([(t["_y"], t["_se"]) for t in trials], model=opts.model,
                 tau2_method=opts.tau2_method, ci_method=opts.ci_method,
                 level=opts.level, prediction_interval=opts.prediction_interval)

    def unscale(v):
        if v is None:
            return None
        return math.exp(v) if log_scale else v

    block = {
        "effect": round(unscale(stats["pooled"]), 4),
        "ci_low": round(unscale(stats["ci_low"]), 4),
        "ci_high": round(unscale(stats["ci_high"]), 4),
        "i2": stats["i2"],
        "model": "fixed_effect" if opts.model == "fixed" else "random_effects",
        "tau2_method": stats["tau2_method"],
        "ci_method": stats["ci_method"],
        "level": stats["level"],
        "p_value": round(stats["p_value"], 4) if stats["p_value"] is not None else None,
        "tau2": round(stats["tau2"], 4),
        "tau": round(stats["tau"], 4),
        "q": round(stats["q"], 4),
        "q_p": round(stats["q_p"], 4) if stats["q_p"] is not None else None,
        "df": stats["df"],
        "h2": stats["h2"],
    }
    if stats["prediction_low"] is not None:
        block["prediction_low"] = round(unscale(stats["prediction_low"]), 4)
        block["prediction_high"] = round(unscale(stats["prediction_high"]), 4)
    return block, stats


def _leave_one_out(trials: list[dict], opts: MetaOptions, log_scale: bool) -> list:
    if len(trials) < 3:
        return []
    rows = []
    for i in range(len(trials)):
        subset = trials[:i] + trials[i + 1:]
        block, _ = _pooled_block(subset, opts, log_scale)
        rows.append({
            "omitted": trials[i]["paper_id"],
            "omitted_author": trials[i]["first_author"],
            "effect": block["effect"], "ci_low": block["ci_low"],
            "ci_high": block["ci_high"], "i2": block["i2"],
        })
    return rows


UNKNOWN_SUBGROUP = "(unknown)"


def _subgroups(trials: list[dict], opts: MetaOptions, log_scale: bool) -> dict | None:
    """Per-level pooling plus the Q-between test of subgroup differences.

    Trials missing the moderator are pooled and reported as a separate
    "(unknown)" group but are NOT a level of the test: "not recorded" is not a
    category of the moderator, and letting it enter Q-between manufactures (or
    hides) a subgroup difference out of missing data.
    """
    buckets: dict[str, list[dict]] = {}
    for t in trials:
        key = t.get("_subgroup")
        buckets.setdefault(UNKNOWN_SUBGROUP if key is None else str(key), []).append(t)
    if len(buckets) < 2:
        return None
    groups = []
    tested = []  # (pooled effect, its SE) for each KNOWN level
    for name, ts in sorted(buckets.items()):
        block, stats = _pooled_block(ts, opts, log_scale)
        known = name != UNKNOWN_SUBGROUP
        groups.append({"subgroup": name, "n_trials": len(ts), "in_between_test": known, **block})
        if known and stats["pooled_se"] > 0:
            tested.append((stats["pooled"], stats["pooled_se"]))
    unknown = buckets.get(UNKNOWN_SUBGROUP, [])
    out = {"by": opts.subgroup_by, "groups": groups,
           "n_unknown": len(unknown),
           "q_between": None, "df": None, "p_value": None}
    if len(tested) < 2:
        out["note"] = "Fewer than two recorded moderator levels; no test of subgroup differences."
        return out
    # Q-between: heterogeneity among the known levels' pooled effects, each
    # weighted by its inverse variance.
    ws = [1.0 / (se * se) for (_m, se) in tested]
    ms = [m for (m, _se) in tested]
    sw = sum(ws)
    gm = sum(w * m for w, m in zip(ws, ms)) / sw
    q_between = sum(w * (m - gm) ** 2 for w, m in zip(ws, ms))
    df_between = len(ms) - 1
    p_between = chi2_sf(q_between, df_between)
    out.update(q_between=round(q_between, 4), df=df_between, p_value=round(p_between, 4))
    return out


def meta_regression(points: list[tuple[float, float, float]], *,
                    tau2_method: str = "DL", level: float = 0.95,
                    knapp_hartung: bool = True) -> dict | None:
    """Random-effects meta-regression of effect on ONE continuous moderator.

    ``points`` are ``(y, se, x)`` triples on the analysis scale (log scale for
    ratio measures, raw otherwise). Fits ``y = b0 + b1*x`` by weighted least
    squares with weights ``1/(se^2 + tau2)``, where tau2 is the RESIDUAL
    heterogeneity estimated from the regression residuals rather than from the
    intercept-only model.

    Knapp-Hartung scales the coefficient SEs by sqrt(Q_resid/df) and uses a
    t-reference distribution. With the small k typical of adverse-effect
    reviews this is materially better calibrated than the normal approximation,
    which is anticonservative -- so it is on by default.

    Returns None when there are too few points or no spread in the moderator
    (a regression through a single x is undefined). Callers should additionally
    require a decent k before *reporting* the slope: at k<8 the CI is usually
    too wide to be informative, and a non-significant slope means "underpowered",
    not "no dose-response".
    """
    pts = [(y, se, x) for (y, se, x) in points
           if y is not None and se is not None and x is not None and se > 0]
    k = len(pts)
    df = k - 2                      # two fitted parameters
    if df < 1:
        return None
    xs = [x for (_y, _se, x) in pts]
    if max(xs) - min(xs) <= 0:
        return None                 # no moderator spread

    ys = [y for (y, _se, _x) in pts]
    ses = [se for (_y, se, _x) in pts]

    def wls(tau2: float):
        w = [1.0 / (se * se + tau2) for se in ses]
        sw = sum(w)
        mx = sum(wi * x for wi, x in zip(w, xs)) / sw
        my = sum(wi * y for wi, y in zip(w, ys)) / sw
        sxx = sum(wi * (x - mx) ** 2 for wi, x in zip(w, xs))
        if sxx <= 0:
            return None
        sxy = sum(wi * (x - mx) * (y - my) for wi, x, y in zip(w, xs, ys))
        b1 = sxy / sxx
        b0 = my - b1 * mx
        resid = [y - (b0 + b1 * x) for x, y in zip(xs, ys)]
        q_resid = sum(wi * r * r for wi, r in zip(w, resid))
        return b0, b1, sxx, w, sw, mx, q_resid

    # Residual tau^2 by the method of moments (DerSimonian-Laird generalized to
    # a regression): solve Q_resid(tau2=0) == df for tau2 via its trace term.
    fit0 = wls(0.0)
    if fit0 is None:
        return None
    _b0, _b1, sxx0, w0, sw0, mx0, q0 = fit0
    tau2 = 0.0
    if tau2_method != "fixed" and q0 > df:
        # trace term for the hat-matrix correction under weights w0
        s1 = sum(w0)
        s2 = sum(wi * wi for wi in w0)
        sxx_w = sum(wi * (x - mx0) ** 2 for wi, x in zip(w0, xs))
        sx2w = sum(wi * wi * (x - mx0) ** 2 for wi, x in zip(w0, xs))
        trace = s1 - (s2 / s1) - (sx2w / sxx_w if sxx_w > 0 else 0.0)
        if trace > 0:
            tau2 = max(0.0, (q0 - df) / trace)

    fit = wls(tau2)
    if fit is None:
        return None
    b0, b1, sxx, w, sw, mx, q_resid = fit

    se_b1 = math.sqrt(1.0 / sxx)
    se_b0 = math.sqrt(1.0 / sw + (mx * mx) / sxx)
    method = "wls_random_effects"
    if knapp_hartung and df >= 1 and q_resid > 0:
        scale = math.sqrt(q_resid / df)
        se_b1 *= scale
        se_b0 *= scale
        method = "knapp_hartung"

    crit = t_ppf(1.0 - (1.0 - level) / 2.0, df)
    tstat = b1 / se_b1 if se_b1 > 0 else None
    p = 2.0 * (1.0 - t_cdf(abs(tstat), df)) if tstat is not None else None

    # R^2 analogue: proportion of between-study variance explained, relative to
    # the intercept-only tau^2.
    tau2_total = tau2_dl(ys, ses)
    r2 = None
    if tau2_total > 0:
        r2 = max(0.0, min(1.0, 1.0 - tau2 / tau2_total))

    return {
        "moderator_n": k,
        "slope": round(b1, 6),
        "slope_se": round(se_b1, 6),
        "slope_ci_low": round(b1 - crit * se_b1, 6),
        "slope_ci_high": round(b1 + crit * se_b1, 6),
        "intercept": round(b0, 6),
        "intercept_se": round(se_b0, 6),
        "p_value": round(p, 4) if p is not None else None,
        "t_stat": round(tstat, 4) if tstat is not None else None,
        "df": df,
        "tau2_residual": round(tau2, 6),
        "tau2_unexplained_ratio": round(tau2 / tau2_total, 4) if tau2_total > 0 else None,
        "r2": round(r2, 4) if r2 is not None else None,
        "q_residual": round(q_resid, 4),
        "method": method,
        "level": level,
        "x_min": round(min(xs), 4),
        "x_max": round(max(xs), 4),
    }


# Below this many trials a regression slope is reported but flagged: the CI is
# usually too wide to distinguish "no effect" from "underpowered". The usual
# convention is ~10 studies per moderator.
META_REGRESSION_MIN_K = 8


def _meta_regression_block(trials: list[dict], opts: MetaOptions,
                           log_scale: bool) -> dict | None:
    """Regress trial effects on opts.regress_on, plus the raw scatter points.

    The scatter is always returned when there are >=3 points, even if the
    regression itself is suppressed -- it is the honest artifact and stays
    interpretable at any k.
    """
    pts, scatter = [], []
    for t in trials:
        x = t.get("_moderator")
        if x is None:
            continue
        try:
            xf = float(x)
        except (TypeError, ValueError):
            continue
        pts.append((t["_y"], t["_se"], xf))
        scatter.append({
            "paper_id": t.get("paper_id"),
            "first_author": t.get("first_author"),
            "year": t.get("year"),
            "x": round(xf, 4),
            "effect": round(math.exp(t["_y"]) if log_scale else t["_y"], 4),
            "se": round(t["_se"], 4),
            # Inverse-variance weight drives bubble area on the scatter.
            "weight": round(1.0 / (t["_se"] ** 2), 4) if t["_se"] > 0 else None,
            "n_total": t.get("n_total"),
            "subgroup": t.get("_subgroup"),
        })
    if len(scatter) < 3:
        return None

    out = {"by": opts.regress_on, "n_points": len(scatter),
           "scatter": sorted(scatter, key=lambda p: p["x"])}
    reg = meta_regression(pts, tau2_method=opts.tau2_method, level=opts.level)
    if reg is not None:
        out["regression"] = reg
        out["underpowered"] = len(pts) < META_REGRESSION_MIN_K
        out["min_k_for_interpretation"] = META_REGRESSION_MIN_K
        if log_scale:
            # On a log scale the slope is a multiplicative effect per unit x.
            out["slope_ratio_per_unit"] = round(math.exp(reg["slope"]), 4)
    return out


def _publication_bias(trials: list[dict], opts: MetaOptions, log_scale: bool,
                      pooled_scale_effect: float) -> dict | None:
    if len(trials) < 3:
        return None
    pts = [(t["_y"], t["_se"]) for t in trials]
    egger = eggers_test(pts)
    tf = trim_and_fill(pts, model=opts.model, tau2_method=opts.tau2_method)
    funnel = funnel_coordinates(pts, pooled_scale_effect)

    def unscale(v):
        return None if v is None else (math.exp(v) if log_scale else v)

    tf_out = {
        "n_imputed": tf["n_imputed"], "side": tf["side"],
        "adjusted_effect": round(unscale(tf["adjusted"]), 4) if tf["adjusted"] is not None else None,
        "imputed": [{"effect": round(unscale(y), 4), "se": round(se, 4)}
                    for (y, se) in tf["imputed"]],
    }
    # Funnel effects to display scale.
    funnel_out = {
        "pooled_effect": round(unscale(funnel["pooled_effect"]), 4),
        "log_scale": log_scale,
        "points": [{"effect": round(unscale(p["effect"]), 4), "se": round(p["se"], 4)}
                   for p in funnel["points"]],
        "max_se": round(funnel["max_se"], 4),
    }
    return {"egger": egger, "trim_fill": tf_out, "funnel": funnel_out}


def _attach_certainty(g: dict, trials: list[dict], block: dict, log_scale: bool) -> None:
    """Risk-of-bias mix + GRADE certainty for one pooled group (in place).

    Per-study RoB comes from each record via ``grading.parse_rob``; the group's
    design class (a pool key, so homogeneous) sets the GRADE starting level.
    Egger's test only counts at k >= ``grading.EGGER_MIN_K``.
    """
    from ber_core.grading import EGGER_MIN_K, grade_certainty
    rob = {"low": 0, "some": 0, "high": 0, "unknown": 0}
    for t in trials:
        rob[t.get("_rob") or "unknown"] += 1
    ns = [t.get("n_total") or t.get("_n_study") for t in trials]
    total_n = sum(ns) if ns and all(isinstance(n, (int, float)) and n > 0 for n in ns) else None
    egger = (eggers_test([(t["_y"], t["_se"]) for t in trials])
             if len(trials) >= EGGER_MIN_K else None)
    g["rob_summary"] = rob
    g["total_n"] = total_n
    g["certainty"] = grade_certainty(
        design_cls=g.get("design_class"), n_trials=len(trials), total_n=total_n,
        i2=block["i2"], ci_low=block["ci_low"], ci_high=block["ci_high"],
        null_value=1.0 if log_scale else 0.0, rob=rob, egger=egger)


def _finalize_group(g: dict, trials: list[dict], opts: MetaOptions, log_scale: bool):
    """Attach pooled stats + optional bias/LOO/subgroup blocks to a group."""
    if len(trials) >= opts.min_trials:
        block, stats = _pooled_block(trials, opts, log_scale)
        tw = stats["total_weight"]
        for t, w in zip(trials, stats["weights"]):
            g["trials"].append(_trial_out(t, weight=w / tw))
        g["pooled"] = block
        if opts.publication_bias:
            pb = _publication_bias(trials, opts, log_scale, stats["pooled"])
            if pb is not None:
                g["publication_bias"] = pb
        if opts.leave_one_out:
            loo = _leave_one_out(trials, opts, log_scale)
            if loo:
                g["leave_one_out"] = loo
        if opts.subgroup_by:
            sg = _subgroups(trials, opts, log_scale)
            if sg is not None:
                g["subgroups"] = sg
        if opts.regress_on:
            mr = _meta_regression_block(trials, opts, log_scale)
            if mr is not None:
                g["meta_regression"] = mr
        _attach_certainty(g, trials, block, log_scale)
    else:
        for t in trials:
            g["trials"].append(_trial_out(t, weight=None))


def build_meta_analysis(records: list[dict], get_group, opts: MetaOptions | None = None,
                        group_field: str = "spray_type", policy: dict | None = None) -> dict:
    """Pool the shared validated selections, preserving native effect measures.

    get_group is retained for API compatibility. Explicit arm identities and
    outcome compatibility, rather than a broad taxonomy, determine the pools.
    """
    from collections import defaultdict
    from ber_core.scientific import prepare_synthesis, group_key, RATIOS, RULES_VERSION
    opts = (opts or MetaOptions()).normalized()
    prepared = prepare_synthesis(records, policy)
    metadata = {r.get("paper_id"): r for r in records}
    buckets = defaultdict(list)
    for selected in prepared["selected"]:
        buckets[group_key(selected)].append(selected)
    groups = []
    for _, selections in sorted(buckets.items()):
        first = selections[0]
        log_scale = first["effect_measure"] in RATIOS
        trials = []
        for s in selections:
            r = metadata.get(s["paper_id"], {})
            trials.append({"paper_id": s["paper_id"],
                "first_author": effects.first_author_surname(r.get("authors", "")),
                "year": r.get("year"), "effect": s["effect"],
                "ci_low": s["ci_low"], "ci_high": s["ci_high"],
                "p_value": s["p_value"], "n_total": s["n_total"],
                "outcome_name": s["outcome_name"], "_y": s["_y"],
                "_se": s["standard_error"], "_log": log_scale,
                "_selection": s,
                "_rob": effects.parse_rob(r),
                "_n_study": _study_n(r),
                "_subgroup": _dotted_get(r, opts.subgroup_by) if opts.subgroup_by else None,
                "_moderator": _dotted_get(r, opts.regress_on) if opts.regress_on else None})
        g = {group_field: first["intervention"], "ingredient": first["intervention"],
             "outcome_domain": first["outcome_domain"], "outcome_concept": first["outcome_concept"],
             "domainLabel": f"{first['outcome_concept']} — {first['comparator']} — {first['time_window']}",
             "comparator": first["comparator"], "unit": first["unit"],
             "basis": first["basis"], "time_window": first["time_window"],
             "population": first["population"], "design_class": first["design_class"],
             "effect_measure": first["effect_measure"], "n_trials": len(selections),
             "scale": "log" if log_scale else "linear", "trials": [],
             "reference_higher_is_better": first["higher_is_better"],
             "rules_version": RULES_VERSION}
        _finalize_group(g, trials, opts, log_scale)
        if g.get("pooled") and len(trials) < 10:
            pts = [(t["_y"], t["_se"]) for t in trials]
            sensitivity = {}
            for ci in ("wald", "hksj", "modified_hksj"):
                stats = pool(pts, model=opts.model, tau2_method=opts.tau2_method, ci_method=ci, level=opts.level)
                transform = math.exp if log_scale else float
                sensitivity[ci] = {"effect": transform(stats["pooled"]),
                    "ci_low": transform(stats["ci_low"]), "ci_high": transform(stats["ci_high"]),
                    "p_value": stats["p_value"]}
            statuses = {v["ci_low"] > (1 if log_scale else 0) or v["ci_high"] < (1 if log_scale else 0) for v in sensitivity.values()}
            g["ci_sensitivity"] = sensitivity
            g["method_dependent"] = len(statuses) > 1
            g["small_study_note"] = "Few independent studies: uncertainty and heterogeneity estimates are unstable; interval methods can disagree."
        groups.append(g)
    return {"options": opts.to_dict(), "methods": opts.to_dict(), "min_trials": opts.min_trials,
            "rules_version": RULES_VERSION, "validation_status": "validated",
            "groups": groups, "withheld": prepared["withheld"], "validation": prepared["validation"],
            "possible_companion_reports": prepared["possible_companion_reports"]}
