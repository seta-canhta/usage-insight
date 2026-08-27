// Entry point. Two screens, one bundle, and a router that is just the path --
// there are exactly two destinations and neither takes a parameter, so a
// routing library would be more moving parts than the problem has.

import {StrictMode, useCallback, useEffect, useState} from 'react';
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
import {Gate} from './Gate';
import {Shell} from './shell';
import type {Screen} from './shell';
import type {Snapshot} from './data';
import {fetchSnapshot} from './data';
import {check, signOut} from './session';
import type {SessionState} from './session';

function screenOf(path: string): Screen {
  return path.startsWith('/activities') ? 'activities' : 'insights';
}

function App() {
  const [screen, setScreen] = useState<Screen>(() => screenOf(window.location.pathname));
  const [session, setSession] = useState<SessionState>('unknown');
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    const onPop = () => setScreen(screenOf(window.location.pathname));
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  // Ask whether the cookie we already hold is good before fetching anything.
  // Otherwise a signed-out visitor's first sight of the page is a failed
  // request, and "not signed in" and "the figures are broken" look the same.
  useEffect(() => {
    check().then(setSession);
  }, []);

  const load = useCallback(() => {
    setFailed(null);
    fetchSnapshot()
      .then(payload => {
        setSnap(payload);
        setSession('in');
      })
      .catch((error: Error) => {
        // A session can lapse while the page is open -- twelve hours, or a
        // restart, which mints a new signing key. Drop back to the gate rather
        // than showing an error about something the reader can just fix.
        if (error.message === 'signed-out') {
          setSession('out');
          setSnap(null);
          return;
        }
        setFailed(error.message);
      });
  }, []);

  useEffect(() => {
    if (session === 'in') {
      load();
    }
  }, [session, load]);

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

  const leave = useCallback(async () => {
    await signOut();
    setSnap(null);
    setSession('out');
  }, []);

  if (session === 'unknown') {
    return (
      <VStack gap={3} padding={6} vAlign="center">
        <Spinner size="md" label="Checking your sign-in" />
      </VStack>
    );
  }

  if (session === 'out') {
    return <Gate onOpen={() => setSession('in')} />;
  }

  if (failed) {
    return (
      <VStack gap={4} padding={6}>
        <Heading level={2}>Nothing to show yet</Heading>
        <Banner
          status="error"
          title="The figures have not been generated"
          description={failed}
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
      onSignOut={leave}
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
