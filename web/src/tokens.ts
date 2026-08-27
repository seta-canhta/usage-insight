// Two things every screen shares: which colour belongs to whom, and what a
// gap looks like.

import type {CSSProperties} from 'react';
import type {Person} from './data';

/** A person's hue, resolved from the name the snapshot gave them.
 *
 * The snapshot assigns hues by position in the member list, so a person keeps
 * theirs when the filter changes and colour follows the person rather than
 * their rank. Anyone past the end of the palette gets the neutral: the
 * snapshot refuses to generate an eleventh colour, and so does this.
 */
export function hueOf(people: Person[], name: string): string {
  const person = people.find(p => p.name === name);
  return person?.hue
    ? `var(--color-data-categorical-${person.hue})`
    : 'var(--color-data-neutral, #8494A3)';
}

export function shortOf(people: Person[], name: string): string {
  return people.find(p => p.name === name)?.short ?? name;
}

/** What a gap looks like.
 *
 * This is the page's one piece of decoration and it is load-bearing. Three of
 * the ten metrics cannot be measured and five only partly, and a dashboard
 * that draws those the same as the two it is sure of is lying by uniformity.
 * So absence gets a texture -- the diagonal rule struck through a field on a
 * paper record that could not be completed -- and it means exactly one thing
 * everywhere it appears. A reader can see the holes in what this page knows
 * from across the room, which is the opposite of what a dashboard usually
 * spends its confidence on.
 *
 * The pitch is in px because a hatch is not on the spacing scale; the colours
 * are tokens.
 */
export const RULED: CSSProperties = {
  backgroundImage:
    'repeating-linear-gradient(45deg, var(--color-border-emphasized) 0 1px, transparent 1px 7px)',
};

/** Half-ruled: measured in part. The rule thins out rather than stopping. */
export const HALF_RULED: CSSProperties = {
  backgroundImage:
    'repeating-linear-gradient(45deg, var(--color-border) 0 1px, transparent 1px 7px)',
};
