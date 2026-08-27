// Entry point. Two screens, one bundle, and a router that is just the path --
// there are exactly two destinations and neither takes a parameter, so a
// routing library would be more moving parts than the problem has.

import {StrictMode, useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';

import '@astryxdesign/core/reset.css';
import '@astryxdesign/core/astryx.css';
import {Theme} from '@astryxdesign/core/theme';

// Self-hosted, because this is served on a LAN box that cannot be assumed to
// reach a font CDN. Archivo sets the headings, Public Sans the prose, and
// JetBrains Mono every figure and key -- IML-CY-207 is an identifier and 3,946
// is a count, and both want tabular figures rather than prose.
// The variable builds, which ship woff2 and nothing else. That matters: the
// static JetBrains subsets also emit legacy .woff, which the server does not
// serve, so a browser that reached for one would get a 404 instead of a font.
// Vietnamese is not an optional subset here -- it is the team's own alphabet,
// and dropping it renders one name in a fallback face beside every other name
// in the right one.
import '@fontsource-variable/archivo/wght.css';
import '@fontsource-variable/public-sans/wght.css';
import '@fontsource-variable/jetbrains-mono/wght.css';

import {qaTheme} from '../theme';
import './app.css';

import {VStack} from '@astryxdesign/core/Layout';
import {Text, Heading} from '@astryxdesign/core/Text';
import {Spinner} from '@astryxdesign/core/Spinner';
import {Banner} from '@astryxdesign/core/Banner';

import {Activities, ActivitiesHero} from './Activities';
import {Insights, InsightsHero} from './Insights';
import {Shell} from './shell';
import type {Screen} from './shell';
import type {Snapshot} from './data';
import {fetchSnapshot} from './data';

function screenOf(path: string): Screen {
  return path.startsWith('/activities') ? 'activities' : 'insights';
}

function App() {
  const [screen, setScreen] = useState<Screen>(() => screenOf(window.location.pathname));
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    const onPop = () => setScreen(screenOf(window.location.pathname));
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  useEffect(() => {
    fetchSnapshot()
      .then(setSnap)
      .catch((error: Error) => setFailed(error.message));
  }, []);

  // The nav items are real links, so a middle-click or a bookmark still works.
  // A plain left-click is intercepted to swap the screen without a round trip.
  useEffect(() => {
    const onClick = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey) {
        return;
      }
      const anchor = (event.target as HTMLElement | null)?.closest?.('a');
      const href = anchor?.getAttribute('href');
      if (href !== '/insights' && href !== '/activities') {
        return;
      }
      event.preventDefault();
      window.history.pushState({}, '', href);
      setScreen(screenOf(href));
    };
    document.addEventListener('click', onClick);
    return () => document.removeEventListener('click', onClick);
  }, []);

  if (failed) {
    return (
      <VStack gap={4} padding={6}>
        <Heading level={2}>Nothing to show yet</Heading>
        <Banner
          status={failed === 'signed-out' ? 'warning' : 'error'}
          title={failed === 'signed-out' ? 'Sign in first' : 'The figures have not been generated'}
          description={
            failed === 'signed-out'
              ? 'Open the daybook, sign in with the passcode, then come back to this page.'
              : failed
          }
        />
        <Text type="supporting" color="secondary">
          Nothing here is a zero. Until the figures load there is simply nothing to read.
        </Text>
      </VStack>
    );
  }

  if (!snap) {
    return (
      <VStack gap={3} padding={6} vAlign="center">
        <Spinner size="md" label="Reading the figures" />
      </VStack>
    );
  }

  return (
    <Shell
      screen={screen}
      people={snap.people}
      picked={picked}
      onPick={setPicked}
      hero={screen === 'insights' ? <InsightsHero snap={snap} /> : <ActivitiesHero />}>
      {screen === 'insights' ? (
        <Insights snap={snap} picked={picked} />
      ) : (
        <Activities snap={snap} picked={picked} />
      )}
    </Shell>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* `system` rather than a pinned light: every token in the theme declares
        both modes, and someone reading this at 9pm on a laptop set to dark
        should get the dark one. */}
    <Theme theme={qaTheme} mode="system">
      <App />
    </Theme>
  </StrictMode>,
);
