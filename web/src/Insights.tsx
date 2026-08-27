// The ten metrics, in order, including the ones that cannot be measured.
//
// A screen that quietly drops metric 6 and metric 10 lets a reader believe
// eight is all there ever were. They are shown, labelled "cannot be measured",
// with the reason -- which is more useful than a blank and much more useful
// than a zero.

import {VStack, HStack} from '@astryxdesign/core/Layout';
import {Grid} from '@astryxdesign/core/Grid';
import {Card} from '@astryxdesign/core/Card';
import {Text, Heading} from '@astryxdesign/core/Text';
import {Divider} from '@astryxdesign/core/Divider';
import {Table, proportional, pixel} from '@astryxdesign/core/Table';
import type {TableColumn} from '@astryxdesign/core/Table';
import {CoverageChart, TrendChart} from './charts';
import {MeasureTable} from './MeasureTable';
import {SectionHeading, StatCard, StatusBadge} from './shell';
import type {Cycle, Person, Snapshot} from './data';
import {format, isAttributed, visible} from './data';

function headline(snap: Snapshot, shown: Person[]) {
  const sum = (groupId: string, key: string) => {
    const group = snap.activity.groups.find(g => g.id === groupId);
    const measure = group?.measures.find(m => m.key === key);
    if (!measure) {
      return null;
    }
    if (!isAttributed(measure.series)) {
      return measure.series.reduce<number>((a, v) => a + (v ?? 0), 0);
    }
    return shown.reduce<number>(
      (total, person) =>
        total + (measure.series as Record<string, Array<number | null>>)[person.name]
          .reduce<number>((a, v) => a + (v ?? 0), 0),
      0,
    );
  };
  return {
    bugs: sum('finding', 'raised_bug'),
    runs: sum('running', 'runs'),
    merged: sum('delivering', 'merged'),
    cost: sum('ai', 'cost'),
  };
}

export function Insights({
  snap,
  picked,
}: {
  snap: Snapshot;
  picked: string | null;
}) {
  const shown = visible(snap.people, picked);
  const totals = headline(snap, shown);
  const coverage = snap.coverage;

  const cycleColumns: TableColumn<Cycle & Record<string, unknown>>[] = [
    {key: 'key', header: 'Test cycle', width: pixel(130)},
    {key: 'area', header: 'Area', width: proportional(1),
     renderCell: row => <Text type="body">{row.area ?? '—'}</Text>},
    {key: 'cases', header: 'Tests', width: pixel(80), align: 'end',
     renderCell: row => <Text type="body">{row.cases.toLocaleString()}</Text>},
    {key: 'pct', header: 'Automated', width: pixel(100), align: 'end',
     renderCell: row => <Text type="body">{format(row.pct, 'percent')}</Text>},
    {key: 'ours', header: 'Run by', width: proportional(1),
     renderCell: row => {
       const mine = shown.filter(p => row.ours[p.name]);
       return (
         <Text type="supporting" color="secondary">
           {mine.length
             ? mine.map(p => `${p.name.split(' ')[0]} ${row.ours[p.name]}`).join(' · ')
             : 'neither'}
         </Text>
       );
     }},
    {key: 'window', header: 'Window', width: pixel(170),
     renderCell: row => (
       <Text type="supporting" color="secondary">
         {row.from} → {row.to}
       </Text>
     )},
  ];

  return (
    <VStack gap={8}>
      {/* --- the four numbers worth reading on their own ------------------ */}
      <Grid columns={{minWidth: 220, repeat: 'fit'}} gap={4}>
        <StatCard
          label="Testing automated"
          value={format(coverage.pct, 'percent')}
          meta={`${coverage.automated.toLocaleString()} of ${coverage.cases.toLocaleString()} tests`}
          note="Across the cycles being delivered"
        />
        <StatCard
          label="Bugs raised"
          value={totals.bugs?.toLocaleString() ?? '—'}
          meta="Reported to the developers"
          note={picked ? picked.split(' ')[0] : 'Both people'}
        />
        <StatCard
          label="Tests run"
          value={totals.runs?.toLocaleString() ?? '—'}
          meta="By hand and by automation"
        />
        <StatCard
          label="AI cost, estimated"
          value={totals.cost != null ? `$${totals.cost.toFixed(2)}` : '—'}
          meta="Against list prices"
          note="Not the bill — see metric 9"
        />
      </Grid>

      {/* --- coverage, the one metric with a real headline ---------------- */}
      <VStack gap={4}>
        <SectionHeading
          title="How much of the testing is automated"
          eyebrow="Metric 2 · Automation Coverage"
        />
        <Text type="body" color="secondary">
          {coverage.note}
        </Text>
        <Card>
          <VStack gap={5}>
            <CoverageChart cycles={coverage.cycles} height={40 + coverage.cycles.length * 30} />
            <Divider />
            <Table<Cycle & Record<string, unknown>>
              data={coverage.cycles as Array<Cycle & Record<string, unknown>>}
              columns={cycleColumns}
              idKey="key"
              density="compact"
              dividers="rows"
              hasHover
            />
          </VStack>
        </Card>
      </VStack>

      <Divider />

      {/* --- the ten, in order ------------------------------------------- */}
      <VStack gap={6}>
        <SectionHeading title="The ten metrics" eyebrow="All of them, measured or not" />
        {snap.metrics.map(metric => {
          const chartable = metric.measures.find(
            m => isAttributed(m.series) || Array.isArray(m.series),
          );
          return (
            <Card key={metric.n}>
              <VStack gap={4}>
                <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
                  <HStack gap={3} vAlign="center">
                    <Heading level={4}>
                      {metric.n} · {metric.name}
                    </Heading>
                    <StatusBadge status={metric.status} />
                  </HStack>
                  <Text type="supporting" color="secondary">
                    {metric.want === 'up' ? 'higher is better' : 'lower is better'}
                  </Text>
                </HStack>

                {metric.note ? (
                  <Text type="body" color="secondary">
                    {metric.note}
                  </Text>
                ) : null}

                {metric.headline ? (
                  <VStack gap={0}>
                    <Heading level={2}>{format(metric.headline.value, 'percent')}</Heading>
                    <Text type="supporting" color="secondary">
                      {metric.headline.of}
                    </Text>
                  </VStack>
                ) : null}

                {metric.measures.length ? (
                  <>
                    <MeasureTable
                      measures={metric.measures}
                      weeks={snap.weeks}
                      people={snap.people}
                      shown={shown}
                    />
                    {chartable ? (
                      <VStack gap={2}>
                        <Text type="supporting" color="secondary">
                          {chartable.label}, week by week
                        </Text>
                        <TrendChart
                          measure={chartable}
                          weeks={snap.weeks}
                          people={shown}
                          height={180}
                        />
                      </VStack>
                    ) : null}
                  </>
                ) : null}
              </VStack>
            </Card>
          );
        })}
      </VStack>

      <Text type="supporting" color="secondary">
        Counted from {snap.sources.join(', ')}. A dash means nothing was measured — it does not
        mean zero. Volume trends span whole weeks only, so a short week never misreads as a fall.
      </Text>
    </VStack>
  );
}
