// What a QA engineer's week actually consists of, grouped by the job being
// done rather than by which system happened to record it.
//
// The order is the order the work happens in: chase a problem and file it,
// design the case, run it, write the script that runs it next time, get that
// script reviewed and merged, and ask AI for help throughout. Eighteen
// counters in a flat list is a list nobody reads.

import {VStack} from '@astryxdesign/core/Layout';
import {Card} from '@astryxdesign/core/Card';
import {Grid} from '@astryxdesign/core/Grid';
import {Text, Heading} from '@astryxdesign/core/Text';
import {Badge} from '@astryxdesign/core/Badge';
import {Divider} from '@astryxdesign/core/Divider';
import {Table, proportional, pixel} from '@astryxdesign/core/Table';
import type {TableColumn} from '@astryxdesign/core/Table';
import {TrendChart} from './charts';
import {MeasureTable} from './MeasureTable';
import {SectionHeading} from './shell';
import {shortOf} from './tokens';
import type {Person, Snapshot} from './data';
import {format, isAttributed, visible} from './data';

type CycleRow = Record<string, unknown> & {
  id: string;
  who: string;
  key: string;
  area: string | null;
  runs: number;
  pct: number | null;
};

/** Six kinds of work, named the way the people doing them would name them. */
export function ActivitiesHero() {
  return (
    <VStack gap={2}>
      <Text type="code" color="secondary">
        Six kinds of work
      </Text>
      <Heading level={1}>Where the month went.</Heading>
      <Text type="large" color="secondary">
        Grouped the way the work happens: chase a problem and file it, write the case,
        run it, automate it, get it merged. Nothing here is one of the ten metrics —
        it is what the ten are made of.
      </Text>
    </VStack>
  );
}

export function Activities({snap, picked}: {snap: Snapshot; picked: string | null}) {
  const shown = visible(snap.people, picked);

  const cycleRows: CycleRow[] = shown.flatMap(person =>
    (snap.activity.cycles_by_person[person.name] ?? []).map(cycle => ({
      id: `${person.name}:${cycle.key}`,
      who: person.name,
      key: cycle.key,
      area: cycle.area,
      runs: cycle.runs,
      pct: cycle.pct,
    })),
  );

  const cycleColumns: TableColumn<CycleRow>[] = [
    {key: 'who', header: 'Who', width: pixel(120),
     renderCell: row => <Text type="body">{shortOf(snap.people, row.who)}</Text>},
    {key: 'key', header: 'Test cycle', width: pixel(130)},
    {key: 'area', header: 'Area', width: proportional(1),
     renderCell: row => <Text type="body">{row.area ?? '—'}</Text>},
    {key: 'runs', header: 'Tests they ran', width: pixel(120), align: 'end',
     renderCell: row => <Text type="body">{row.runs.toLocaleString()}</Text>},
    {key: 'pct', header: 'Cycle automated', width: pixel(130), align: 'end',
     renderCell: row => <Text type="body">{format(row.pct, 'percent')}</Text>},
  ];

  return (
    <VStack gap={8}>
      <Grid columns={{minWidth: 340, repeat: 'fit'}} gap={4}>
        {snap.activity.groups.map(group => {
          const total = group.measures[0];
          const sum = !total
            ? null
            : isAttributed(total.series)
              ? shown.reduce<number>(
                  (a, p) =>
                    a + (total.series as Record<string, Array<number | null>>)[p.name]
                      .reduce<number>((x, v) => x + (v ?? 0), 0),
                  0,
                )
              : total.series.reduce<number>((x, v) => x + (v ?? 0), 0);
          return (
            <Card key={group.id}>
              <VStack gap={1}>
                <Text type="supporting" color="secondary">
                  {group.name}
                </Text>
                <Heading level={3}>{sum?.toLocaleString() ?? '—'}</Heading>
                <Text type="supporting" color="secondary">
                  {total?.label ?? ''}
                  {group.attributed ? '' : ' · no names on these'}
                </Text>
              </VStack>
            </Card>
          );
        })}
      </Grid>

      {snap.activity.groups.map(group => (
        <VStack key={group.id} gap={4}>
          <SectionHeading title={group.name} eyebrow={group.why}>
            {group.attributed ? null : (
              // The member filter is real, so where it does not apply the
              // screen has to say so rather than silently showing the same
              // numbers for whichever person is selected.
              <Badge label="no names on these" variant="warning" />
            )}
          </SectionHeading>

          {group.note ? (
            <Text type="body" color="secondary">
              {group.note}
            </Text>
          ) : null}

          <Card>
            <VStack gap={5}>
              <MeasureTable
                measures={group.measures}
                weeks={snap.weeks}
                people={snap.people}
                shown={shown}
              />
              {group.measures[0] ? (
                <>
                  <Divider />
                  <VStack gap={2}>
                    <Text type="supporting" color="secondary">
                      {group.measures[0].label}, week by week
                    </Text>
                    <TrendChart
                      measure={group.measures[0]}
                      weeks={snap.weeks}
                      people={group.attributed ? shown : []}
                      all={snap.people}
                      height={190}
                    />
                  </VStack>
                </>
              ) : null}
            </VStack>
          </Card>
        </VStack>
      ))}

      <VStack gap={4}>
        <SectionHeading title="Which cycles the runs landed in" eyebrow="Test execution by cycle" />
        <Card>
          {cycleRows.length ? (
            <Table<CycleRow>
              data={cycleRows}
              columns={cycleColumns}
              idKey="id"
              density="compact"
              dividers="rows"
              hasHover
            />
          ) : (
            <Text type="body" color="secondary">
              No test cycles recorded for who you have selected.
            </Text>
          )}
        </Card>
      </VStack>

      <Text type="supporting" color="secondary">
        Counted from {snap.sources.join(', ')}. Every figure with a name against it counts only that
        person. A dash means nothing was measured, not zero.
      </Text>
    </VStack>
  );
}

export type {Person};
