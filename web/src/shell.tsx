// The frame both screens sit in: nav on the side, one filter row above the
// content, and the pieces every section is built from.
//
// The member filter is a SegmentedControl and not a TabList on purpose. It
// selects a mode, not a destination -- the URL does not change and neither
// does which screen you are on. TabList would say otherwise.

import {VStack, HStack, Layout, LayoutContent} from '@astryxdesign/core/Layout';
import {AppShell} from '@astryxdesign/core/AppShell';
import {SideNav, SideNavItem} from '@astryxdesign/core/SideNav';
import {Text, Heading} from '@astryxdesign/core/Text';
import {Card} from '@astryxdesign/core/Card';
import {Badge} from '@astryxdesign/core/Badge';
import {Divider} from '@astryxdesign/core/Divider';
import {Icon} from '@astryxdesign/core/Icon';
import {
  SegmentedControl,
  SegmentedControlItem,
} from '@astryxdesign/core/SegmentedControl';
import {Button} from '@astryxdesign/core/Button';
import {
  ArrowDownIcon,
  ArrowRightStartOnRectangleIcon,
  ArrowUpIcon,
  ArrowsUpDownIcon,
  ChartBarSquareIcon,
  ClipboardDocumentCheckIcon,
  CalendarDaysIcon,
  MinusIcon,
} from '@heroicons/react/24/outline';
import type {Person, Trend} from './data';
import {shortOf} from './tokens';

export type Screen = 'insights' | 'activities';

export function Shell({
  screen,
  people,
  picked,
  onPick,
  onSignOut,
  hero,
  children,
}: {
  screen: Screen;
  people: Person[];
  picked: string | null;
  onPick: (name: string | null) => void;
  onSignOut: () => void;
  /** Each screen opens with its own thesis. There is no shared page title:
   *  one would only repeat what the hero already says, and the nav already
   *  says which screen you are on. */
  hero: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <AppShell
      sideNav={
        <SideNav
          footerIcons={
            // Signing out has to be reachable from here too. It lived only on
            // the daybook, which made the session look like that page's
            // property when it is one session across all three screens.
            <Button
              label="Sign out"
              variant="ghost"
              size="sm"
              onClick={onSignOut}
              icon={<Icon icon={ArrowRightStartOnRectangleIcon} size="sm" />}
            />
          }>
          <SideNavItem
            label="Insights"
            href="/insights"
            isSelected={screen === 'insights'}
            icon={<Icon icon={ChartBarSquareIcon} size="sm" />}
          />
          <SideNavItem
            label="Activities"
            href="/activities"
            isSelected={screen === 'activities'}
            icon={<Icon icon={ClipboardDocumentCheckIcon} size="sm" />}
          />
          <SideNavItem
            label="Daybook"
            href="/dashboard"
            icon={<Icon icon={CalendarDaysIcon} size="sm" />}
          />
        </SideNav>
      }>
      <Layout
        height="fill"
        content={
          <LayoutContent padding={6}>
            <VStack gap={6}>
              {hero}
              <Divider />
              {/* The filter sits under the hero, not over it. You decide whose
                  figures to read after you know what the page can tell you,
                  and on Insights the hero does not change with the choice. */}
              <HStack gap={3} vAlign="center" wrap="wrap">
                <SegmentedControl
                  label="Whose figures to show"
                  value={picked ?? 'both'}
                  onChange={value => onPick(value === 'both' ? null : value)}>
                  <SegmentedControlItem value="both" label="Everyone" />
                  {people.map(person => (
                    <SegmentedControlItem
                      key={person.name}
                      value={person.name}
                      label={person.short}
                    />
                  ))}
                </SegmentedControl>
                <Text type="supporting" color="secondary">
                  {picked
                    ? people.find(p => p.name === picked)?.role ?? ''
                    : people
                        .map(p => [shortOf(people, p.name), p.role].filter(Boolean).join(' '))
                        .join(' · ')}
                </Text>
              </HStack>
              {children}
            </VStack>
          </LayoutContent>
        }
      />
    </AppShell>
  );
}

/** A trend, said in words first. The arrow is the decoration, not the claim. */
export function TrendTag({trend}: {trend: Trend}) {
  if (trend.word === 'not measured') {
    return (
      <Text type="supporting" color="secondary">
        not measured
      </Text>
    );
  }
  if (trend.word === 'no direction') {
    // Measured, but neither direction is the good one. State the movement and
    // pass no judgement on it.
    return (
      <Text type="supporting" color="secondary">
        {trend.change == null
          ? 'no better or worse'
          : `net ${trend.change > 0 ? '+' : ''}${trend.change}%`}
      </Text>
    );
  }
  const colour = trend.good == null ? 'secondary' : trend.good ? 'success' : 'error';
  const icon =
    trend.word === 'up and down'
      ? ArrowsUpDownIcon
      : trend.word === 'no change'
        ? MinusIcon
        : (trend.change ?? 0) >= 0
          ? ArrowUpIcon
          : ArrowDownIcon;
  return (
    <HStack gap={1} vAlign="center">
      <Icon icon={icon} size="xsm" color={colour === 'secondary' ? undefined : colour} />
      <Text type="supporting" color={colour === 'secondary' ? 'secondary' : undefined}>
        {trend.word}
        {trend.change == null
          ? ''
          : trend.word === 'up and down'
            ? ` · net ${trend.change > 0 ? '+' : ''}${trend.change}%`
            : ` · ${trend.change > 0 ? '+' : ''}${trend.change}%`}
      </Text>
    </HStack>
  );
}

/** A standalone widget carrying one number worth reading on its own. */
export function StatCard({
  label,
  value,
  meta,
  note,
}: {
  label: string;
  value: string;
  meta?: string;
  note?: string;
}) {
  return (
    <Card>
      <VStack gap={2}>
        <Heading level={4}>{label}</Heading>
        <Heading level={2}>{value}</Heading>
        {meta ? (
          <Text type="body" color="secondary">
            {meta}
          </Text>
        ) : null}
        {note ? (
          <Text type="supporting" color="secondary">
            {note}
          </Text>
        ) : null}
      </VStack>
    </Card>
  );
}

/** What a metric's state is, said as a word rather than left to a colour. */
export function StatusBadge({status}: {status: 'live' | 'partial' | 'impossible'}) {
  const said = {
    live: {label: 'measured', variant: 'success' as const},
    partial: {label: 'partly measured', variant: 'warning' as const},
    impossible: {label: 'not measurable', variant: 'neutral' as const},
  }[status];
  return <Badge label={said.label} variant={said.variant} />;
}

export function SectionHeading({
  title,
  eyebrow,
  children,
}: {
  title: string;
  eyebrow?: string;
  children?: React.ReactNode;
}) {
  return (
    <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
      <VStack gap={0}>
        {eyebrow ? (
          <Text type="supporting" color="secondary">
            {eyebrow}
          </Text>
        ) : null}
        <Heading level={3}>{title}</Heading>
      </VStack>
      {children}
    </HStack>
  );
}
