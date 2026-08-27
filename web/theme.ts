// The theme for the QA screens.
//
// The page's job is to say what we know about two people's month and, just as
// plainly, what we do not. Three of the ten metrics cannot be measured at all
// and five only partly, and a dashboard that renders those the same as the two
// it is sure of is lying by uniformity. So the identity is built around one
// distinction -- measured, partly, not at all -- and everything here serves it.
//
// Accent is a deep instrument teal. It is deliberately not either of the two
// categorical hues: those belong to the people on the team, colour follows
// the person, and an accent sharing a hue with one of them would quietly
// imply the page was taking a side.
//
// Type is three faces doing three jobs. Archivo is signage -- flat terminals,
// real width, built to be read across a room -- and carries the headings. Public
// Sans was drawn for plain-language government reporting, which is exactly what
// this page is: an account someone has to be able to check. JetBrains Mono sets
// every figure and every key, because IML-CY-207 is an identifier and 3,946 is a
// count, and both want tabular figures rather than prose.

import {defineTheme} from '@astryxdesign/core/theme';

export const qaTheme = defineTheme({
  name: 'qa-record',

  // Cool neutrals, not warm: this is a record on graph paper, not a letter.
  color: {accent: '#0B4F55', neutralStyle: 'cool', contrast: 'standard'},

  typography: {
    scale: {base: 15, ratio: 1.25},
    body: {
      family: 'Public Sans Variable',
      fallbacks: '"Public Sans", -apple-system, system-ui, sans-serif',
    },
    heading: {
      family: 'Archivo Variable',
      fallbacks: 'Archivo, "Helvetica Neue", Arial, sans-serif',
      weight: 'bold',
      weights: {1: 'bold', 2: 'bold', 3: 'semibold', 4: 'semibold'},
    },
    code: {
      family: 'JetBrains Mono Variable',
      fallbacks: '"JetBrains Mono", "SF Mono", ui-monospace, monospace',
    },
  },

  // Nearly square. A record card has corners; a soft one reads as a widget,
  // and 0 reads as the broadsheet everyone else is printing this year.
  radius: {base: 2, multiplier: 1},

  // One short, unshowy tempo. The only motion on the page is the ledger
  // settling in on load, and it should be over before it is noticed.
  motion: {fast: 120, medium: 240, slow: 480, ratio: 0.75},

  tokens: {
    // Pale cool paper, so a white card sits on it as a card rather than
    // dissolving into the page.
    '--color-background-body': ['#EEF1F4', '#131619'],
    '--color-background-surface': ['#FFFFFF', '#1B1F23'],
    '--color-background-card': ['#FFFFFF', '#1B1F23'],

    // The series palette. Astryx ships these values but does not emit them
    // into CSS unless a theme asks for them, and a var nothing defines
    // resolves to nothing -- which is how the person swatches came out black.
    //
    // Order matters: the snapshot hands out hues by a member's position in
    // the list, so the first two slots are the pair a two-person team will
    // actually see. Purple and orange, measured together, separate by dE 36.7
    // for protanopia and 44.0 for normal vision -- the widest of the pairs
    // available here -- and both stay clear of the teal accent, so a person's
    // line is never mistaken for the page's own colour. Blue would have been
    // the obvious first slot and sits at dE 17.4 from that teal.
    '--color-data-categorical-purple': ['#6B1EFD', '#7952FF'],
    '--color-data-categorical-orange': ['#EB6E00', '#FB8C00'],
    '--color-data-categorical-blue': ['#0171E3', '#2694FE'],
    '--color-data-categorical-green': ['#0B991F', '#26A756'],
    '--color-data-categorical-pink': ['#F351C0', '#F351C0'],
    '--color-data-categorical-cyan': ['#0171A4', '#0171A4'],
    '--color-data-categorical-red': ['#F5394F', '#F5394F'],
    '--color-data-categorical-teal': ['#08A3A3', '#08A3A3'],
    '--color-data-categorical-brown': ['#965E03', '#965E03'],
    '--color-data-categorical-indigo': ['#6F8AFF', '#6F8AFF'],
    '--color-data-neutral': ['#8494A3', '#8C939B'],
  },
});

export default qaTheme;
