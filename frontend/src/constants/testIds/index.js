// constants/testIds/ — a registry of data-testid values intended for the
// end-to-end testing agent (qabot) to locate and interact with UI elements.
//
// NOTE: it is currently VESTIGIAL. Nothing imports it, and every data-testid in the
// JSX is hardcoded inline. Treat these values as documentation of the existing
// markup, not as the source of truth -- do not refactor pages to import from here
// unless that is explicitly requested.
//
// Structure: each feature lives in its own file (auth.js, ...) and is re-exported
// from here, so consumers can do a single import like
// `import { LOGIN } from '@/constants/testIds'` (or relative).
//
// Adding a new feature:
//   1. Create constants/testIds/<feature>.js
//   2. Export named objects (e.g. `export const PROFILE = { ... }`)
//   3. Re-export here: `export * from './<feature>';`

export * from './auth';
