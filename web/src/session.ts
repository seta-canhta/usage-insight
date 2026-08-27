// One session, shared by all three screens.
//
// The cookie was always shared -- it is Path=/ and opens both /dashboard/* and
// /insights/data -- but only the daybook knew how to sign in or out, so these
// two screens could see the state and not change it. That is what made the
// auth feel like it lived on one side: signing in meant leaving for the
// daybook and navigating back by hand, signing out was impossible from here,
// and an expired session dead-ended.
//
// Same routes as the daybook, same passcode, same words. Whichever screen you
// are on, signing in signs you in everywhere and signing out signs you out
// everywhere.

export type SessionState = 'unknown' | 'in' | 'out';

async function call(path: string, body?: unknown): Promise<Response> {
  return fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin',
    headers: body === undefined ? undefined : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** Whether the cookie we already hold is still good. */
export async function check(): Promise<SessionState> {
  try {
    const response = await call('/dashboard/session');
    if (!response.ok) {
      return 'out';
    }
    const payload = await response.json();
    return payload.signed_in ? 'in' : 'out';
  } catch {
    return 'out';
  }
}

export class Refused extends Error {
  /** Seconds until another attempt is allowed, when the address is locked out. */
  retryAfter: number | null;
  constructor(message: string, retryAfter: number | null = null) {
    super(message);
    this.retryAfter = retryAfter;
  }
}

export async function signIn(passcode: string): Promise<void> {
  const response = await call('/dashboard/login', {passcode});
  if (response.ok) {
    return;
  }
  const detail = await response.json().catch(() => ({}) as Record<string, unknown>);
  if (response.status === 429) {
    // Eight wrong tries locks the address for a minute. Saying how long is the
    // difference between a wait and a thing that looks broken.
    const wait = Number(detail.retry_after) || 60;
    throw new Refused(
      `Too many tries. Wait ${wait} second${wait === 1 ? '' : 's'} and try again.`,
      wait,
    );
  }
  // Never says whether the passcode was close, long or short -- the server
  // refuses to, and repeating a guess back would undo that.
  throw new Refused('That passcode was not right.');
}

export async function signOut(): Promise<void> {
  await call('/dashboard/logout', {});
}
