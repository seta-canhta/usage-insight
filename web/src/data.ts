// The snapshot, its types, and the two pieces of arithmetic that must not be
// done twice: which weeks a volume may be compared across, and what counts as
// a trend. Both have already produced a wrong number in this project, so they
// live here and nothing recomputes them locally.

export type Series = Record<string, Array<number | null>>;

export type Measure = {
  key?: string;
  label: string;
  unit: 'count' | 'percent' | 'usd' | 'hours';
  want?: 'up' | 'down';
  note?: string;
  // Attributed measures carry one array per person. Project-level measures --
  // anything the source records no author for -- carry a single flat array,
  // and the member filter must leave them alone.
  series: Series | Array<number | null>;
};

export type Group = {
  id: string;
  name: string;
  why: string;
  attributed: boolean;
  note?: string;
  measures: Measure[];
};

export type Cycle = {
  key: string;
  area: string | null;
  cases: number;
  automated: number;
  pct: number | null;
  runs: number;
  failed: number;
  defects: number;
  from: string;
  to: string;
  ours: Record<string, number>;
};

export type Metric = {
  n: number;
  name: string;
  want: 'up' | 'down';
  status: 'live' | 'partial' | 'impossible';
  note?: string;
  headline?: {value: number | null; unit: string; of: string | null};
  by_cycle?: Cycle[];
  measures: Measure[];
};

export type Week = {
  week: string;
  short: string;
  label: string;
  from: string;
  to: string;
  days: number;
  partial: boolean;
  note: string | null;
  full: boolean;
};

export type Person = {name: string; role: string | null; pronouns: string};

export type Snapshot = {
  schema: number;
  generated_at: string;
  weeks: Week[];
  people: Person[];
  metrics: Metric[];
  coverage: {
    cases: number;
    automated: number;
    pct: number | null;
    cycles: Cycle[];
    note: string;
  };
  activity: {
    groups: Group[];
    cycles_by_person: Record<string, Array<{key: string; area: string | null; runs: number; pct: number | null}>>;
  };
  sources: string[];
};

export async function fetchSnapshot(): Promise<Snapshot> {
  const response = await fetch('/insights/data', {credentials: 'same-origin'});
  if (response.status === 401) {
    throw new Error('signed-out');
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error || 'the snapshot could not be read');
  }
  return response.json();
}

// --- the two rules ---------------------------------------------------------

export function isAttributed(series: Measure['series']): series is Series {
  return !Array.isArray(series);
}

/** The people a measure should draw, honouring the filter and the source.
 *
 * A project-level measure ignores the filter entirely: the source records no
 * author, so narrowing it to one person would be inventing one.
 */
export function visible(people: Person[], picked: string | null): Person[] {
  if (!picked) {
    return people;
  }
  return people.filter(p => p.name === picked);
}

export type Trend = {
  // `no direction` is not `not measured`. A defect count is measured and real;
  // it just has no better or worse -- and a failure found is the job working,
  // so calling a rise in it "worse" would be the screen lying. The two states
  // are separate words because collapsing them told a reader that data we
  // hold was data we never collected.
  word: 'better' | 'worse' | 'no change' | 'up and down' | 'no direction' | 'not measured';
  change: number | null;
  good: boolean | null;
};

/** First to last, and only across weeks that actually finished.
 *
 * Volume may not be compared against a part week -- doing that once turned a
 * +74% into a -17%, which is very nearly the opposite claim. Rates survive a
 * short week because they are a ratio of two things measured in the same
 * window; counts do not.
 */
export function trend(
  values: Array<number | null>,
  weeks: Week[],
  want: 'up' | 'down' | undefined,
  isRate: boolean,
): Trend {
  const idx = isRate
    ? values.map((_, i) => i)
    : weeks.map((w, i) => (w.full ? i : -1)).filter(i => i >= 0);
  const points = idx.map(i => values[i]).filter((v): v is number => v != null);
  if (points.length < 2) {
    return {word: 'not measured', change: null, good: null};
  }

  const first = points[0];
  const last = points[points.length - 1];
  const change = first === 0 ? null : Math.round(((last - first) / first) * 100);

  if (want == null) {
    return {word: 'no direction', change, good: null};
  }

  // A sequence that falls then rises is not a decline. Reporting its endpoints
  // as a trend is how 189, 61, 75, 97 became "down 49%".
  const steps = points.slice(1).map((v, i) => Math.sign(v - points[i]));
  const moved = steps.filter(s => s !== 0);
  const mixed = moved.length > 1 && new Set(moved).size > 1;
  if (moved.length === 0) {
    return {word: 'no change', change: 0, good: null};
  }
  if (mixed) {
    const net = last === first ? 0 : Math.sign(last - first);
    return {
      word: 'up and down',
      change,
      good: net === 0 ? null : (net > 0) === (want === 'up'),
    };
  }
  const up = moved[0] > 0;
  return {word: up === (want === 'up') ? 'better' : 'worse', change, good: up === (want === 'up')};
}

export function format(value: number | null | undefined, unit: Measure['unit']): string {
  // An unmeasured quantity and a measured zero must never render the same.
  if (value == null) {
    return '—';
  }
  switch (unit) {
    case 'percent':
      return `${value}%`;
    case 'usd':
      return `$${value.toFixed(2)}`;
    case 'hours':
      return `${value}h`;
    default:
      return value.toLocaleString();
  }
}
