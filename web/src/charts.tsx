// Chart forms, chosen by what the data is doing rather than by habit.
//
//   change over time, 1-2 people   -> line
//   discrete weekly counts         -> grouped column
//   magnitude across named things  -> sorted horizontal bar, one hue
//
// Two rules hold everywhere here. There is never a second y-axis: two measures
// of different scale get two charts. And a null is a hole in the line, not a
// zero -- `connectNulls` stays off so an unmeasured week reads as unmeasured.

import {VStack, HStack} from '@astryxdesign/core/Layout';
import {Text} from '@astryxdesign/core/Text';
import {Icon} from '@astryxdesign/core/Icon';
import {Card} from '@astryxdesign/core/Card';
import {StopIcon} from '@heroicons/react/24/solid';
import {
  Bar,
  BarChart,
  LabelList,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type {Measure, Person, Week} from './data';
import {hueOf} from './tokens';
import {format, isAttributed} from './data';

// Colour comes from the snapshot, which assigns each member a design-system
// categorical hue by their position in the member list. Nothing here picks a
// colour, so a person keeps theirs when the filter changes and the palette is
// never rotated to make a chart look better.
const AXIS = {
  fontSize: 'var(--font-size-sm, 12px)',
  fill: 'var(--color-text-secondary, #4E606F)',
};
const GRID = 'var(--color-border, rgba(5, 54, 89, 0.1))';

type Row = Record<string, number | null | string>;

/** Weekly rows for recharts, one row per week with a key per series. */
function rowsOf(measure: Measure, weeks: Week[], names: string[]): Row[] {
  const source = measure.series;
  return weeks.map((week, i) => {
    const row: Row = {week: week.short, label: week.label, partial: week.partial ? 'yes' : ''};
    if (isAttributed(source)) {
      names.forEach(name => {
        row[name] = source[name]?.[i] ?? null;
      });
    } else {
      row['Everyone'] = source[i] ?? null;
    }
    return row;
  });
}

function ChartTooltip({
  active,
  payload,
  label,
  unit,
  weeks,
}: {
  active?: boolean;
  payload?: Array<{name: string; value: number | null; color: string}>;
  label?: string;
  unit: Measure['unit'];
  weeks: Week[];
}) {
  if (!active || !payload?.length) {
    return null;
  }
  const week = weeks.find(w => w.short === label);
  return (
    <Card padding={3}>
      <VStack gap={1}>
        <Text type="supporting" color="secondary">
          {week ? `${week.label}${week.partial ? ` · ${week.days} days` : ''}` : label}
        </Text>
        {payload.map(entry => (
          <HStack key={entry.name} gap={2} vAlign="center">
            <Icon icon={StopIcon} size="xsm" style={{color: entry.color}} />
            <Text type="supporting">
              {entry.name}: {format(entry.value, unit)}
            </Text>
          </HStack>
        ))}
      </VStack>
    </Card>
  );
}

function Legend({entries}: {entries: Array<{label: string; colour: string}>}) {
  // Present whenever there are two or more series, so identity is never
  // carried by colour alone. One series needs none -- the title names it.
  if (entries.length < 2) {
    return null;
  }
  return (
    <HStack gap={4} wrap="wrap">
      {entries.map(entry => (
        <HStack key={entry.label} gap={2} vAlign="center">
          <Icon icon={StopIcon} size="xsm" style={{color: entry.colour}} />
          <Text type="supporting" color="secondary">
            {entry.label}
          </Text>
        </HStack>
      ))}
    </HStack>
  );
}

export function TrendChart({
  measure,
  weeks,
  people,
  all,
  height = 200,
}: {
  measure: Measure;
  weeks: Week[];
  /** The members to draw -- the filter's answer. */
  people: Person[];
  /** Every member, so a hue stays with its person when the filter narrows. */
  all: Person[];
  height?: number;
}) {
  const attributed = isAttributed(measure.series);
  const names = attributed ? people.map(p => p.name) : ['Everyone'];
  const rows = rowsOf(measure, weeks, names);
  const entries = names.map(name => ({
    name,
    label: attributed ? (people.find(p => p.name === name)?.short ?? name) : name,
    colour: attributed ? hueOf(all, name) : 'var(--color-accent)',
  }));

  // Rates are a ratio of two things measured in the same window, so a short
  // week is comparable and a line is honest. Counts are not, so they get
  // columns -- discrete blocks that invite comparison but do not imply the
  // continuity between weeks that a line does.
  const asLine = measure.unit === 'percent';

  return (
    <VStack gap={3}>
      <ResponsiveContainer width="100%" height={height}>
        {asLine ? (
          <LineChart data={rows} margin={{top: 5, right: 8, left: 0, bottom: 0}}>
            <CartesianGrid horizontal vertical={false} stroke={GRID} />
            <XAxis dataKey="week" tick={AXIS} axisLine={false} tickLine={false} />
            <YAxis tick={AXIS} axisLine={false} tickLine={false} width={38} />
            <Tooltip
              content={<ChartTooltip unit={measure.unit} weeks={weeks} />}
              cursor={{stroke: GRID}}
            />
            {entries.map(entry => (
              <Line
                key={entry.name}
                type="monotone"
                dataKey={entry.name}
                stroke={entry.colour}
                strokeWidth={2}
                dot={{r: 3, strokeWidth: 0, fill: entry.colour}}
                activeDot={{r: 5}}
                connectNulls={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        ) : (
          <BarChart data={rows} margin={{top: 5, right: 8, left: 0, bottom: 0}} barGap={2}>
            <CartesianGrid horizontal vertical={false} stroke={GRID} />
            <XAxis dataKey="week" tick={AXIS} axisLine={false} tickLine={false} />
            <YAxis tick={AXIS} axisLine={false} tickLine={false} width={38} />
            <Tooltip
              content={<ChartTooltip unit={measure.unit} weeks={weeks} />}
              cursor={{fill: 'var(--color-background-muted, rgba(5,54,89,0.05))'}}
            />
            {entries.map(entry => (
              <Bar
                key={entry.name}
                dataKey={entry.name}
                fill={entry.colour}
                radius={[4, 4, 0, 0]}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        )}
      </ResponsiveContainer>
      <Legend entries={entries} />
    </VStack>
  );
}

/** Magnitude across named things: sorted, one hue, no legend needed. */
export function CoverageChart({
  cycles,
  height = 220,
}: {
  cycles: Array<{key: string; cases: number; pct: number | null; area: string | null}>;
  height?: number;
}) {
  const rows = cycles
    .filter(c => c.pct != null)
    .map(c => ({
      name: `${c.key} (${c.cases.toLocaleString()})`,
      pct: c.pct as number,
      area: c.area ?? '—',
    }))
    .sort((a, b) => b.pct - a.pct);

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={rows} layout="vertical" margin={{top: 0, right: 40, left: 0, bottom: 0}}>
        <CartesianGrid horizontal={false} vertical stroke={GRID} />
        <XAxis type="number" domain={[0, 100]} tick={AXIS} axisLine={false} tickLine={false} />
        <YAxis
          type="category"
          dataKey="name"
          tick={AXIS}
          axisLine={false}
          tickLine={false}
          width={150}
        />
        <Tooltip
          cursor={{fill: 'var(--color-background-muted, rgba(5,54,89,0.05))'}}
          content={({active, payload}) => {
            if (!active || !payload?.length) {
              return null;
            }
            const row = payload[0].payload as {name: string; pct: number; area: string};
            return (
              <Card padding={3}>
                <VStack gap={1}>
                  <Text type="supporting">{row.name}</Text>
                  <Text type="supporting" color="secondary">
                    {row.area} · {row.pct}% automated
                  </Text>
                </VStack>
              </Card>
            );
          }}
        />
        {/* minPointSize gives a measured zero a visible stub. Without it the
            three cycles that are run by hand on purpose draw nothing at all,
            and a reader cannot tell "0%" from "this row has no data" -- which
            is the one distinction this whole page is built to make. */}
        <Bar
          dataKey="pct"
          radius={[0, 4, 4, 0]}
          minPointSize={3}
          isAnimationActive={false}>
          {/* A 0% bar has no length, so without a label it reads as missing
              data. These cycles are run by hand on purpose -- a measured zero,
              and the one number on this chart that most needs saying out loud. */}
          <LabelList
            dataKey="pct"
            position="right"
            formatter={(v: unknown) => (v == null ? '' : `${v}%`)}
            style={{fontSize: 'var(--font-size-sm, 12px)', fill: 'var(--color-text-secondary, #4E606F)'}}
          />
          {rows.map(row => (
            // One hue for magnitude. A cycle automated on purpose at 0% is
            // muted rather than alarming -- it is a scope decision, not a gap.
            <Cell
              key={row.name}
              fill={row.pct === 0 ? 'var(--color-data-neutral, #8494A3)' : 'var(--color-accent)'}
            />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
