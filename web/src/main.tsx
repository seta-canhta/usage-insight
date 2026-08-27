// Entry point. Two screens, one bundle, and a router that is just the path --
// there are exactly two destinations and neither takes a parameter, so a
// routing library would be more moving parts than the problem has.

import {StrictMode, useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';

import '@astryxdesign/core/reset.css';
import '@astryxdesign/core/astryx.css';
import {Theme} from '@astryxdesign/core/theme';
import {neutralTheme} from '@astryxdesign/theme-neutral/built';
import '@astryxdesign/theme-neutral/theme.css';

import {VStack} from '@astryxdesign/core/Layout';
import {Text, Heading} from '@astryxdesign/core/Text';
import {Spinner} from '@astryxdesign/core/Spinner';
import {Banner} from '@astryxdesign/core/Banner';

import {Activities} from './Activities';
import {Insights} from './Insights';
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

  const title = screen === 'insights' ? 'AI effectiveness' : 'What they worked on';
  const lede =
    screen === 'insights'
      ? 'The ten metrics this system exists to measure, and what each one can honestly say.'
      : 'The work itself, grouped by the job being done rather than by the system that recorded it.';

  if (failed) {
    return (
      <VStack gap={4} padding={6}>
        <Heading level={2}>The screen could not load</Heading>
        <Banner
          status={failed === 'signed-out' ? 'warning' : 'error'}
          title={failed === 'signed-out' ? 'Not signed in' : 'No data'}
          description={
            failed === 'signed-out'
              ? 'Open the daybook and sign in with the passcode, then come back.'
              : failed
          }
        />
        <Text type="supporting" color="secondary">
          This is a missing source, not a score of zero. Nothing on this screen should be read as
          a measurement until it loads.
        </Text>
      </VStack>
    );
  }

  if (!snap) {
    return (
      <VStack gap={3} padding={6} vAlign="center">
        <Spinner size="md" label="Loading the figures" />
      </VStack>
    );
  }

  return (
    <Shell
      screen={screen}
      people={snap.people}
      picked={picked}
      onPick={setPicked}
      title={title}
      lede={lede}>
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
    <Theme theme={neutralTheme}>
      <App />
    </Theme>
  </StrictMode>,
);
