// One measure, one row per person, one column per week, and a trend at the end.
//
// The table is the honest form for this: five weekly numbers per person is
// dense data, and a reader who wants the exact figure should not have to hover
// a chart to get it. The charts beside it are for shape, not for lookup.

import {Table, proportional, pixel} from '@astryxdesign/core/Table';
import type {TableColumn} from '@astryxdesign/core/Table';
import {VStack, HStack} from '@astryxdesign/core/Layout';
import {Text} from '@astryxdesign/core/Text';
import {Icon} from '@astryxdesign/core/Icon';
import {StopIcon} from '@heroicons/react/24/solid';
import {TrendTag} from './shell';
import {hueFor} from './charts';
import type {Measure, Person, Week} from './data';
import {format, isAttributed, trend} from './data';

type Row = Record<string, unknown> & {
  id: string;
  measure: string;
  who: string | null;
  note?: string;
};

export function MeasureTable({
  measures,
  weeks,
  people,
  shown,
}: {
  measures: Measure[];
  weeks: Week[];
  people: Person[];
  shown: Person[];
}) {
  const rows: Row[] = [];
  measures.forEach((measure, mi) => {
    const isRate = measure.unit === 'percent';
    const source = measure.series;
    if (isAttributed(source)) {
      shown.forEach(person => {
        const values = source[person.name] ?? [];
        const row: Row = {
          id: `${mi}:${person.name}`,
          measure: measure.label,
          who: person.name,
          note: measure.note,
          trend: trend(values, weeks, measure.want, isRate),
        };
        weeks.forEach((week, i) => {
          row[week.short] = format(values[i], measure.unit);
        });
        rows.push(row);
      });
    } else {
      // No author in the source, so one row and no name against it.
      const values = source;
      const row: Row = {
        id: `${mi}:all`,
        measure: measure.label,
        who: null,
        note: measure.note,
        trend: trend(values, weeks, measure.want, isRate),
      };
      weeks.forEach((week, i) => {
        row[week.short] = format(values[i], measure.unit);
      });
      rows.push(row);
    }
  });

  const columns: TableColumn<Row>[] = [
    {
      key: 'measure',
      header: 'What we counted',
      width: proportional(2),
      renderCell: row => (
        <VStack gap={0}>
          <Text type="body">{row.measure}</Text>
          {row.note ? (
            <Text type="supporting" color="secondary">
              {row.note}
            </Text>
          ) : null}
        </VStack>
      ),
    },
    {
      key: 'who',
      header: 'Who',
      width: pixel(130),
      renderCell: row =>
        row.who ? (
          <HStack gap={2} vAlign="center">
            <Icon icon={StopIcon} size="xsm" style={{color: hueFor(people, row.who)}} />
            <Text type="body">{row.who.split(' ')[0]}</Text>
          </HStack>
        ) : (
          <Text type="supporting" color="secondary">
            whole project
          </Text>
        ),
    },
    // A week is flagged partial for two different reasons -- it has not
    // finished, or the pull window starts mid-week -- and "(7d)" against a
    // week called partial reads as a contradiction. Say which it is.
    ...weeks.map(week => ({
      key: week.short,
      header: !week.partial
        ? week.short
        : week.days < 7
          ? `${week.short} · ${week.days}d`
          : `${week.short} · part`,
      width: pixel(92),
      align: 'end' as const,
    })),
    {
      key: 'trend',
      header: 'Trend',
      width: pixel(170),
      renderCell: row => <TrendTag trend={row.trend as ReturnType<typeof trend>} />,
    },
  ];

  return (
    <Table<Row>
      data={rows}
      columns={columns}
      idKey="id"
      density="compact"
      dividers="rows"
      hasHover
    />
  );
}
