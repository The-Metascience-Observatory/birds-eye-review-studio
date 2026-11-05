"""Conservative numerical aids; diagnostics are not INSPECT-SR judgments."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
import re
import unicodedata
import html


def numeric_tokens(text):
    """Comparable reported numbers, preserving signs and decimal magnitudes."""
    text = re.sub(r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s+[^<>]*)?/?>", " ", text)
    text = html.unescape(text).replace("\\n", "\n").replace("\\r", "\n").replace("\\t", " ")
    text = unicodedata.normalize("NFKC", text).translate(str.maketrans({"−": "-", "–": "-", "—": "-"}))
    # Medical publishers use middle dots as decimal separators. Require a
    # characteristic fractional decimal in the passage before interpreting them;
    # a standalone multiplication expression such as 2·3 is not normalized.
    if re.search(r"(?<!\d)0\s*[·⋅]\s*\d{2,}", text):
        text = re.sub(r"(?<=\d)\s*[·⋅]\s*(?=\d)", ".", text)
    # Decimal-comma tables also use nonzero fractional values. Require a
    # characteristic zero fraction; retain ambiguous three-digit groups, so
    # an English-style n=1,234 cannot become 1.234. Comma-separated lists with
    # spaces are not decimal tokens.
    if re.search(r"(?<![\d,.])0,\d+", text):
        text = re.sub(r"(?<![\d,.])(\d+),(\d{1,2})(?!\d|,\d)", r"\1.\2", text)
    # A leading zero distinguishes a fractional decimal comma from thousands
    # grouping. Ambiguous nonzero forms such as 1,234 retain their usual grouping.
    text = re.sub(r"(?<![\d,.])0,(\d+)(?!\d|,\d)", r"0.\1", text)
    # Distinguish a spaced confidence-interval separator from a negative bound:
    # '0.12 - 0.47' is a range; '-2 - -1' retains both negative signs.
    text = re.sub(r"(?<=\d)%?\s*-\s*(?=[+-]?(?:\d|\.\d))", " to ", text)
    text = re.sub(r"(?<!\w)-\s+(?=\d)", "-", text)
    units = dict(zip(("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split(), range(20)))
    tens = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
    text = re.sub(r"\b(" + "|".join(tens) + r")(?:\s*-\s*|\s+)(" + "|".join(list(units)[1:10]) + r")\b",
                  lambda m: str(tens[m[1].lower()] + units[m[2].lower()]), text, flags=re.I)
    words = {**units, **tens}
    text = re.sub(r"\b(" + "|".join(words) + r")\b", lambda m: str(words[m[1].lower()]), text, flags=re.I)
    # A hyphen in FSS-7 or 10-15 is not a negative sign. A decimal such as .05
    # is numerically identical to 0.05; 5 is not. DOI/registry identifiers do not
    # establish a clinical number simply because they contain digits.
    pattern = r"(?<![A-Za-z0-9])[-+]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    return {str(Decimal(v.replace(",", "")).normalize()) for v in re.findall(pattern, text)}


def rounded_interval(value: str):
    """Closed interval deliberately accepts both rounding tie conventions."""
    x = Decimal(str(value))
    if not x.is_finite():
        raise ValueError("Finite reported decimal required")
    half = Decimal(10) ** x.as_tuple().exponent / 2
    return x - half, x + half


def integer_moments(mean: str, n: int, *, sd: str | None = None,
                    integer_measure_confirmed=False):
    """GRIM and GRIMMER necessary moment conditions, with reported precision.

    Requires the actual outcome denominator and confirmed integer observations.
    Does not certify existence of a dataset or bounded-scale feasibility.
    """
    if not integer_measure_confirmed:
        return {"status": "not_assessable", "reason": "Integer measurement not established"}
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0 or (sd is not None and n < 2):
        raise ValueError("Positive integer outcome denominator required")
    lo, hi = rounded_interval(mean)
    first = int((lo * n).to_integral_value(rounding=ROUND_CEILING))
    last = int((hi * n).to_integral_value(rounding=ROUND_FLOOR))
    if last - first > 100000:
        return {"status": "not_assessable", "reason": "Reported precision gives too many possible sums"}
    sums = range(first, last + 1)
    possible = first <= last
    if sd is not None:
        sl, sh = rounded_interval(sd)
        if Decimal(sd) < 0:
            raise ValueError("SD cannot be negative")
        sl = max(sl, Decimal(0))
        possible = False
        for total in sums:
            center = Decimal(total) ** 2 / n
            low = int(((n - 1) * sl ** 2 + center).to_integral_value(rounding=ROUND_CEILING))
            high = int(((n - 1) * sh ** 2 + center).to_integral_value(rounding=ROUND_FLOOR))
            # Sum of squares and sum of integer observations have the same parity.
            low += (total - low) % 2
            if low <= high:
                possible = True
                break
    return {"status": "compatible_necessary_conditions" if possible else "inconsistent",
            "mean": mean, "n": n, "sd": sd,
            "limitation": "Confirm denominator, scale, missingness and rounding before a judgment; compatibility is not proof of integrity."}


def pooled_t_p_range(mean1, sd1, n1, mean2, sd2, n2):
    """Range allowed by rounding for a two-sided unadjusted equal-variance t test."""
    from scipy.stats import t
    if min(n1, n2) < 2 or not all(isinstance(n, int) and not isinstance(n, bool) for n in (n1, n2)):
        raise ValueError("Group sizes must be integers >= 2")
    m1, m2 = rounded_interval(mean1), rounded_interval(mean2)
    s1, s2 = rounded_interval(sd1), rounded_interval(sd2)
    if min(Decimal(sd1), Decimal(sd2)) <= 0:
        raise ValueError("Positive SDs required")
    df = n1 + n2 - 2
    dmin = max(Decimal(0), m1[0] - m2[1], m2[0] - m1[1])
    dmax = max(abs(m1[0] - m2[1]), abs(m1[1] - m2[0]))
    def se(a, b):
        return math.sqrt(((n1 - 1) * float(a) ** 2 + (n2 - 1) * float(b) ** 2) / df * (1/n1 + 1/n2))
    se_min = se(max(s1[0], 0), max(s2[0], 0))
    se_max = se(s1[1], s2[1])
    return {"p_min": float(2 * t.sf(float(dmax) / se_min, df)) if se_min else 0.0,
            "p_max": float(2 * t.sf(float(dmin) / se_max, df)),
            "assumptions": "Two-sided, independent groups, pooled variance, unadjusted; does not apply to Welch, paired, covariate-adjusted or nonparametric tests."}


def participant_diagnostics(extraction):
    sizes = extraction.get("sample_sizes") or {}
    total = sizes.get("n_randomized_total")
    arms = sizes.get("per_arm") or []
    counts = [a.get("n_randomized") for a in arms]
    is_count = lambda x: isinstance(x, int) and not isinstance(x, bool) and x >= 0
    out = []
    if is_count(total) and counts and all(is_count(n) for n in counts) and sum(counts) != total:
        out.append({"check": "4.6", "finding": "Extracted randomized arm counts do not sum to extracted total",
                    "total": total, "arm_counts": counts,
                    "requires": "Verify extraction against the original CONSORT diagram; crossover periods are not independent arms."})
    return out
