// Sign in, from whichever screen you happened to open.
//
// This is the same passcode and the same routes the daybook uses, so a session
// started here is the session there. Before this existed, landing on /insights
// signed out told you to go to the daybook and come back -- which is a page
// giving directions instead of doing the thing.

import {useState} from 'react';
import {VStack, HStack} from '@astryxdesign/core/Layout';
import {Card} from '@astryxdesign/core/Card';
import {Text, Heading} from '@astryxdesign/core/Text';
import {TextInput} from '@astryxdesign/core/TextInput';
import {Button} from '@astryxdesign/core/Button';
import {Refused, signIn} from './session';

export function Gate({onOpen}: {onOpen: () => void}) {
  const [passcode, setPasscode] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!passcode || busy) {
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      await signIn(passcode);
      setPasscode('');
      onOpen();
    } catch (error) {
      setNotice(
        error instanceof Refused ? error.message : 'Could not reach the endpoint.',
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate">
      <Card>
        <VStack gap={5}>
          <VStack gap={1}>
            <Heading level={3}>Sign in</Heading>
            <Text type="supporting" color="secondary">
              The same passcode as the daybook. One sign-in covers all three screens.
            </Text>
          </VStack>

          {/* The refusal rides on the field rather than floating under the
              card: the design system colours the border, sets aria-invalid and
              announces the message, which hand-rolled error text does not. */}
          <TextInput
            label="Passcode"
            type="password"
            value={passcode}
            onChange={setPasscode}
            onEnter={submit}
            isDisabled={busy}
            statusVariant="detached"
            status={notice ? {type: 'error', message: notice} : undefined}
          />

          <HStack hAlign="end">
            <Button
              label={busy ? 'Signing in…' : 'Sign in'}
              variant="primary"
              onClick={submit}
              isDisabled={busy || !passcode}
            />
          </HStack>
        </VStack>
      </Card>
    </div>
  );
}
