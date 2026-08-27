// The ledger: ten cells, one per metric, filled by how much we actually know.
//
// This is the first thing on the page, and it is deliberately not a big
// percentage. The honest headline for this dashboard is not any single figure
// -- it is that two of its ten measures are solid, five are partial, and three
// cannot be measured at all. Leading with 93.1% and an arrow would be the
// template answer and would imply a confidence the data does not have.
//
// Each cell is a link to its metric, so the strip is also the page's index.

import {VStack, HStack} from '@astryxdesign/core/Layout';
import {Text, Heading} from '@astryxdesign/core/Text';
import type {Metric} from './data';


const SAID = {
  live: 'measured',
  partial: 'partly measured',
  impossible: 'not measurable',
} as const;

const WORDS = ['None', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven',
  'Eight', 'Nine', 'Ten'];

function said(n: number, capital = false): string {
  const word = WORDS[n] ?? String(n);
  return capital ? word : word.toLowerCase();
}

/** "A", "A and B", "A, B and C" -- so the sentence reads however many there are. */
function listed(items: string[]): string {
  if (items.length < 2) {
    return items[0] ?? '';
  }
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

const MARK = {
  live: 'mark mark--measured',
  partial: 'mark mark--partial',
  impossible: 'mark mark--unmeasured',
} as const;

function Cell({metric, index}: {metric: Metric; index: number}) {
  return (
    <a
      className="ledger-cell"
      href={`#metric-${metric.n}`}
      aria-label={`${metric.n}. ${metric.name} — ${SAID[metric.status]}`}
      // The load sequence runs left to right, once, and is over before it is
      // noticed. Turned off entirely under prefers-reduced-motion.
      style={{animationDelay: `${index * 35}ms`}}>
      <VStack gap={2}>
        <span className={MARK[metric.status]} />
        <VStack gap={0}>
          <Text type="code" color="secondary">
            {metric.n}
          </Text>
          <span className="ledger-cell__name">
            <Text type="supporting" color="secondary">
              {metric.name}
            </Text>
          </span>
        </VStack>
      </VStack>
    </a>
  );
}

export function Ledger({metrics, window}: {metrics: Metric[]; window: string}) {
  const count = (s: Metric['status']) => metrics.filter(m => m.status === s).length;
  const live = count('live');
  const partial = count('partial');
  const gone = count('impossible');
  const solid = metrics.filter(m => m.status === 'live').map(m => m.name);

  return (
    <VStack gap={5}>
      <VStack gap={2}>
        <Text type="code" color="secondary">
          {window}
        </Text>
        <Heading level={1}>
          {said(live, true)} measured. {said(partial, true)} partly.{' '}
          {said(gone, true)} we can&rsquo;t.
        </Heading>
        <Text type="large" color="secondary">
          {solid.length
            ? `${listed(solid)} ${solid.length === 1 ? 'is the one measure' : `are the ${said(live)} measures`} you can quote without a caveat. `
            : ''}
          Everything else on this page marks its own gaps instead of filling
          them with zeros.
        </Text>
      </VStack>

      <HStack gap={2} wrap="wrap">
        {metrics.map((metric, i) => (
          <Cell key={metric.n} metric={metric} index={i} />
        ))}
      </HStack>

      <HStack gap={5} wrap="wrap">
        {(['live', 'partial', 'impossible'] as const).map(status => (
          <HStack key={status} gap={2} vAlign="center">
            <span
              className={MARK[status]}
              style={{width: 'var(--spacing-4, 16px)', height: 'var(--spacing-3, 12px)'}}
            />
            <Text type="supporting" color="secondary">
              {SAID[status]}
            </Text>
          </HStack>
        ))}
      </HStack>
    </VStack>
  );
}
