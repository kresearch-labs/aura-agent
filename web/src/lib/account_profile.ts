export type AccountProfile = { name: string; display: string };
export type AccountSession = {
  created: number; last_used: number | null; expires: number;
  ended: number | null; revoked_at: number | null; current: boolean;
};

// auth.session_person expires non-current browser sessions after seven
// days unused; pre-last_used rows age by their creation timestamp.
export function sessionIsActive(s: AccountSession,
                                now = Date.now() / 1000): boolean {
  const fresh = s.last_used || s.created;
  return !s.ended && s.revoked_at === null &&
    (s.current || (Number(s.expires) > now && fresh >= now - 7 * 86400));
}

// The account field may contain a legacy name accepted by the backend only
// when unchanged. Display text follows the server's existing str(value)[:80]
// rule, not a client-side trim. Never include untouched privileged fields.
export function profilePatch(original: AccountProfile,
                             draft: AccountProfile): Partial<AccountProfile> {
  const patch: Partial<AccountProfile> = {};
  const nextName = draft.name.trim();       // same rule as people/me, for edits
  if (nextName !== original.name) patch.name = nextName;
  if (draft.display !== original.display) patch.display = draft.display;
  return patch;
}
